import os
import hashlib
import threading
import queue
import json
import time
from datetime import datetime
from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, Response, stream_with_context
)
from supabase import create_client, Client
from passlib.hash import bcrypt

# 1. Load env variables

load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")  # needed for realtime (listener uses anon)
SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Missing Supabase credentials in .env file!")

# 2. Flask app and Supabase client setup

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = SECRET_KEY
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
supabase_readonly: Client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

@app.context_processor
def inject_globals():
    from datetime import datetime
    return dict(datetime=datetime)

# 3. Helper funcs

def current_user():
    # Return the currently logged-in user dict from Flask session
    return session.get("user")

def hash_password(password: str) -> str:
    # Hashing password using SHA-256 + bcrypt.
    sha = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return bcrypt.hash(sha)

def verify_password(password: str, hashed: str) -> bool:
    # Verifying a password against its stored bcrypt hash
    try:
        sha = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return bcrypt.verify(sha, hashed)
    except Exception:
        return False

def are_friends(user_id: str, peer_id: str) -> bool:
    # check if two users are friends (accepted)
    try:
        res = (
            supabase.table("friends")
            .select("id,status")
            .or_(f"(sender_id.eq.{user_id},receiver_id.eq.{peer_id}),(sender_id.eq.{peer_id},receiver_id.eq.{user_id})")
            .eq("status", "accepted").limit(1).execute()
        )
        return bool(res.data)
    except Exception:
        return False

def pair_key(a: str, b: str) -> str:
    return f"{min(a,b)}::{max(a,b)}"

def get_or_create_thread(a: str, b: str):
    pk = pair_key(a, b)
    r = supabase.table("threads").select("*").eq("pair_key", pk).limit(1).execute()
    if r.data:
        return r.data[0]
    created = supabase.table("threads").insert({"user_a": a, "user_b": b}).execute()
    return created.data[0]


# 4. Realtime connection setup : uses anon key + correct endpoint
#    + server-sent events broadcaster so /events stops 404 errors

_sse_subscribers_lock = threading.Lock()
_sse_subscribers: list[queue.Queue] = []

def _sse_subscribe() -> queue.Queue:
    q = queue.Queue(maxsize=100)
    with _sse_subscribers_lock:
        _sse_subscribers.append(q)
    return q

def _sse_unsubscribe(q: queue.Queue):
    with _sse_subscribers_lock:
        try:
            _sse_subscribers.remove(q)
        except ValueError:
            pass

def _sse_broadcast(payload: dict):
    dead = []
    with _sse_subscribers_lock:
        for q in _sse_subscribers:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            try:
                _sse_subscribers.remove(q)
            except ValueError:
                pass

def start_realtime_thread():
    # Start realtime listener for message table inserts
    try:
        ws_host = SUPABASE_URL.split("//")[1]
        ws_url = f"wss://{ws_host}/realtime/v1/websocket"
        from websocket import create_connection

        def listen():
            try:
                # connect with anon key
                ws = create_connection(f"{ws_url}?apikey={SUPABASE_ANON_KEY}&vsn=1.0.0")
                print("realtime connected")
                # join inserts on public:messages
                join_msg = {"topic": "realtime:public:messages", "event": "phx_join", "payload": {}, "ref": 1}
                ws.send(json.dumps(join_msg))
                print("listening for message inserts...")

                # heartbeat to keep phoenix channel alive
                def heartbeat():
                    ref = 2
                    while True:
                        try:
                            ws.send(json.dumps({"topic": "phoenix", "event": "heartbeat", "payload": {}, "ref": ref}))
                            ref += 1
                            time.sleep(25)
                        except Exception:
                            break

                threading.Thread(target=heartbeat, daemon=True).start()

                while True:
                    raw = ws.recv()
                    if not raw:
                        break
                    # basic filter: broadcast any payloads that include INSERT
                    if '"INSERT"' in raw or '"INSERT"' in raw.upper():
                        try:
                            data = json.loads(raw)
                            # supabase realtime payload has "payload" with "record" under key "new"
                            new_row = None
                            if isinstance(data, dict):
                                payload = data.get("payload") or {}
                                new_row = payload.get("record") or payload.get("new")
                            if new_row:
                                _sse_broadcast({"type": "message", "row": new_row})
                        except Exception:
                            pass
            except Exception as e:
                print(f"realtime crashed: {e}")

        threading.Thread(target=listen, daemon=True).start()
    except Exception as e:
        print(f"Realtime connection failed: {e}")

@app.route("/")
def landing_page():
    # Landing page + lights background
    return render_template("index.html", datetime=datetime)

@app.route("/about")
def about_page():
    # About / Learn More page
    return render_template("about.html")

@app.route("/login")
def login_page_redirect():
    # Redirects to auth page (login/signup combined)
    return redirect(url_for("auth_page"))

# 5. Authentication routes

@app.route("/home")
def home():
    # Main chat hub — show all messages and dummy peer(chat not implemented yet)
    if not current_user():
        return redirect(url_for("auth_page"))
    user = current_user()

    # Fetch user messages (both directions), includes timestamps
    try:
        resp = (
            supabase.table("messages")
            .select("*")
            .or_(f"sender_id.eq.{user['id']},receiver_id.eq.{user['id']}")
            .order("created_at")
            .execute()
        )
        messages = resp.data if hasattr(resp, "data") else []
    except Exception as e:
        print("Failed to fetch messages:", e)
        messages = []

    # temporary placeholder until friend chat implemented
    dummy_peer = {"id": "none", "username": "Friend", "email": "friend@example.com"}

    return render_template("chat.html", user=user, messages=messages, peer=dummy_peer)

@app.route("/chat")
def chat_hub():
    return redirect(url_for("home"))

@app.route("/auth", methods=["GET"])
def auth_page():
    # Login/signup page (combined)
    return render_template("loginsignup.html")

@app.route("/signup", methods=["POST"])
def signup():
    try:
        # Registers a new user
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not username or not email or not password:
            flash("Please fill all fields", "error")
            return redirect(url_for("auth_page"))

        print(f"Checking for existing email: {email}")  # Debug log
        # Check if email exists (case insensitive)
        existing_email = supabase.table("users").select("id, email").ilike("email", email).execute()
        print(f"Existing email check result: {existing_email.data}")  # Debug log
        
        if existing_email.data:
            flash(f"Email already registered. Please use a different email.", "error")
            return redirect(url_for("auth_page"))

        print(f"Checking for existing username: {username}")  # Debug log
        # Check if username exists
        existing_username = supabase.table("users").select("id").eq("username", username).execute()
        print(f"Existing username check result: {existing_username.data}")  # Debug log
        
        if existing_username.data:
            flash("Username already taken. Please choose a different username.", "error")
            return redirect(url_for("auth_page"))

        # Create new user
        hashed = hash_password(password)
        user_data = {
            "username": username,
            "email": email,
            "password_hash": hashed,
            "created_at": datetime.utcnow().isoformat()
        }
        
        print(f"Attempting to create user with email: {email}")  # Debug log
        result = supabase.table("users").insert(user_data).execute()
        print(f"User creation result: {result.data}")  # Debug log
        
        flash("Account created successfully! Please log in.", "info")
        return redirect(url_for("auth_page"))
        
    except Exception as e:
        print(f"Signup error - Type: {type(e)}, Error: {str(e)}")  # Debug log
        if "users_email_key" in str(e):
            # This means there's a duplicate email in the database
            print(f"Duplicate email detected: {email}")  # Debug log
            flash("This email is already registered. Please try logging in or use a different email.", "error")
        else:
            flash("An error occurred during signup. Please try again.", "error")
        return redirect(url_for("auth_page"))

@app.route("/login", methods=["POST"])
def login():
    # Always clear any existing session first
    session.clear()

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    try:
        resp = supabase.table("users").select("*").eq("email", email).limit(1).execute()
        if not resp.data:
            flash("No account found with this email", "error")
            return redirect(url_for("auth_page"))

        user = resp.data[0]
        if not verify_password(password, user["password_hash"]):
            flash("Invalid credentials", "error")
            return redirect(url_for("auth_page"))

        # Store only id + username in session
        session["user"] = {"id": user["id"], "username": user["username"]}
        session.modified = True

        flash(f"Welcome back, {user['username']}!", "success")
        return redirect(url_for("home"))

    except Exception as e:
        print("Login error:", e)
        flash("Server error during login", "error")
        return redirect(url_for("auth_page"))


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out successfully.", "info")
    response = redirect(url_for("auth_page"))
    response.set_cookie("session", "", expires=0)  # ensure cookie removal
    return response


# 6. Friends system (endpoints + pages)

@app.route("/api/users/search")
def api_users_search_v2():
    if not current_user():
        return jsonify({"error": "auth required"}), 401

    q = (request.args.get("q") or "").strip()  # Removed .lower() to preserve original case
    if not q:
        return jsonify({"users": []})

    try:
        res = supabase.table("users").select("id,username,email")\
        .or_(f"username.ilike.%{q}%,email.ilike.%{q}%")\
        .limit(10).execute()

        me = current_user()
        users = [u for u in (res.data or []) if u["id"] != me["id"]]
        return jsonify({"users": users})
    except Exception as e:
        print("Search error:", e)
        return jsonify({"users": []})


@app.route("/friends", methods=["GET"])
def friends_page():
    # Display friend list and pending requests.
    if not current_user():
        return redirect(url_for("auth_page"))
    user = current_user()

    # accepted friendships involving me
    friends = (
        supabase.table("friends")
        .select("id,status,sender_id,receiver_id,users:sender_id(username),peer:receiver_id(username)")
        .or_(f"sender_id.eq.{user['id']},receiver_id.eq.{user['id']}")
        .eq("status","accepted").execute()
    )

    # incoming friend requests (you are receiver, pending)
    incoming = (
        supabase.table("friends")
        .select("id,sender_id,receiver_id,users:sender_id(username)")
        .eq("receiver_id", user["id"]).eq("status","pending").execute()
    )

    # outgoing friend requests (you are sender, pending)
    outgoing = (
        supabase.table("friends")
        .select("id,sender_id,receiver_id,peer:receiver_id(username)")
        .eq("sender_id", user["id"]).eq("status","pending").execute()
    )

    friends_list = friends.data or []
    print("Debug - Friends data:", friends_list)  # Debug print
    
    return render_template(
        "friends.html",
        user=user,
        friends=friends_list,
        incoming=incoming.data or [],
        outgoing=outgoing.data or []
    )

@app.route("/friends/add", methods=["POST"])
def add_friend():
    # Send a friend request by username.
    if not current_user():
        return redirect(url_for("auth_page"))
    me = current_user()
    target_username = (request.form.get("username") or "").strip()

    if not target_username:
        flash("enter a username", "error"); return redirect(url_for("friends_page"))

    lookup = supabase.table("users").select("id,username").eq("username", target_username).limit(1).execute()
    if not lookup.data:
        flash("no such user", "error"); return redirect(url_for("friends_page"))

    target_id = lookup.data[0]["id"]
    if target_id == me["id"]:
        flash("you can’t add yourself", "error"); return redirect(url_for("friends_page"))

    # prevent duplicate pending/accepted
    existing = (
        supabase.table("friends")
        .select("id,status")
        .or_(f"(sender_id.eq.{me['id']},receiver_id.eq.{target_id}),(sender_id.eq.{target_id},receiver_id.eq.{me['id']})")
        .limit(1).execute()
    )
    if existing.data:
        flash(f"already {existing.data[0]['status']}", "info"); return redirect(url_for("friends_page"))

    supabase.table("friends").insert({
        "sender_id": me["id"],
        "receiver_id": target_id,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat()
    }).execute()

    # update friend count for both
    for uid in (me["id"], target_id):
        count_friends(uid)

    flash("friend request sent!", "success")
    return redirect(url_for("friends_page"))

@app.route("/friends/respond", methods=["POST"])
def respond_request():
    # Accept/reject a friend request.
    if not current_user():
        return redirect(url_for("auth_page"))
    me = current_user()
    req_id = request.form.get("request_id")
    action = request.form.get("action")
    status = "accepted" if action == "accept" else "rejected"

    # make sure this request belongs to me as receiver
    check = supabase.table("friends").select("id,receiver_id,sender_id").eq("id", req_id).limit(1).execute()
    if not check.data or check.data[0]["receiver_id"] != me["id"]:
        flash("invalid request", "error"); return redirect(url_for("friends_page"))

    supabase.table("friends").update({"status": status}).eq("id", req_id).execute()

    # update counters if accepted
    if status == "accepted":
        sender = check.data[0]["sender_id"]
        for uid in (me["id"], sender):
            count_friends(uid)

    flash(f"request {status}", "info")
    return redirect(url_for("friends_page"))

# 7. Messaging + message requests (like instagram)

def _resolve_peer(peer_id: str | None, peer_username: str | None):
    if peer_id:
        r = supabase.table("users").select("id,username").eq("id", peer_id).limit(1).execute()
        if r.data:
            return r.data[0]
    if peer_username:
        r = supabase.table("users").select("id,username").eq("username", peer_username).limit(1).execute()
        if r.data:
            return r.data[0]
    return None

@app.route("/api/messages_convo", methods=["GET"])
def api_messages():
    # fetch full convo with peer (by id or username), includes timestamps
    if not current_user():
        return jsonify({"error":"auth required"}), 401
    me = current_user()
    peer_id = (request.args.get("peer_id") or "").strip() or None
    peer_username = (request.args.get("peer_username") or "").strip() or None

    peer = _resolve_peer(peer_id, peer_username)
    if not peer:
        return jsonify({"messages":[],"requests":[]})

    friends_ok = are_friends(me["id"], peer["id"])

    # normal messages shown (only if accepted friends OR request accepted)
    msgs = []
    if friends_ok:
        r = (
            supabase.table("messages").select("*")
            .or_(f"(sender_id.eq.{me['id']},receiver_id.eq.{peer['id']}),(sender_id.eq.{peer['id']},receiver_id.eq.{me['id']})")
            .order("created_at").execute()
        )
        msgs = r.data or []

    # message requests (pending)
    reqs = (
        supabase.table("message_requests").select("*")
        .or_(f"(sender_id.eq.{me['id']},receiver_id.eq.{peer['id']}),(sender_id.eq.{peer['id']},receiver_id.eq.{me['id']})")
        .order("created_at").execute()
    )

    return jsonify({
        "peer": {"id": peer["id"], "username": peer["username"]},
        "friends": friends_ok,
        "messages": msgs,
        "requests": reqs.data or []
    })
'''  COMMENTED DUPLICATE
@app.route("/api/send_message", methods=["POST"])
def api_send_message():
    # Send message or message request depending on friendship status
    if not current_user():
        return jsonify({"error":"auth required"}), 401
    me = current_user()
    data = request.get_json(force=True, silent=True) or {}
    peer_id = (data.get("peer_id") or "").strip() or None
    peer_username = (data.get("peer_username") or "").strip() or None
    content = (data.get("content") or "").strip()
    if not (peer_id or peer_username) or not content:
        return jsonify({"error":"missing fields"}), 400

    peer = _resolve_peer(peer_id, peer_username)
    if not peer:
        return jsonify({"error":"user not found"}), 404

    now_iso = datetime.utcnow().isoformat()

    if are_friends(me["id"], peer["id"]):
        # drop into messages
        msg = {
            "sender_id": me["id"],
            "receiver_id": peer["id"],
            "content": content,
            "created_at": now_iso
        }
        supabase.table("messages").insert(msg).execute()
        # also push to SSE immediately
        _sse_broadcast({"type":"message", "row": msg})
        return jsonify({"status":"sent","lane":"inbox","message":msg})
    else:
        # create message request
        req = {
            "sender_id": me["id"],
            "receiver_id": peer["id"],
            "content": content,
            "created_at": now_iso
        }
        supabase.table("message_requests").insert(req).execute()
        return jsonify({"status":"queued","lane":"requests","request":req})  '''

@app.route("/requests", methods=["GET"])
def requests_page():
    # inbox page for message requests (incoming/outgoing)
    if not current_user():
        return redirect(url_for("auth_page"))
    me = current_user()

    incoming = (
        supabase.table("message_requests").select("*")
        .eq("receiver_id", me["id"]).order("created_at").execute()
    )
    outgoing = (
        supabase.table("message_requests").select("*")
        .eq("sender_id", me["id"]).order("created_at").execute()
    )
    return render_template("requests.html", user=me, incoming=incoming.data or [], outgoing=outgoing.data or [])


# 8. Profile page (counts + private email)

@app.route("/profile")
def profile_page():
    # Show profile page for current user. change username doesnt update live on page, but updates db
    if not current_user():
        return redirect(url_for("auth_page"))
    me = current_user()

    # friends count (accepted only)
    f = (
        supabase.table("friends")
        .select("id", count="exact")
        .or_(f"sender_id.eq.{me['id']},receiver_id.eq.{me['id']}")
        .eq("status","accepted").execute()
    )
    friends_count = f.count or 0

    # loops count (placeholder: 0 until we add posts table)
    loops_count = 0

    # fetch latest username (email stays private)
    u = supabase.table("users").select("username").eq("id", me["id"]).limit(1).execute()
    username = u.data[0]["username"] if u.data else me["username"]

    user_pub = {"id": me["id"], "username": username}
    return render_template("profile.html", user=user_pub, friends_count=friends_count, loops_count=loops_count)

# Search users (live search) – endpoint for search bar dropdown

@app.route("/api/search_users_live")
def search_users_live():
    if not current_user():
        return jsonify({"results": []})

    q = request.args.get("q", "").strip().lower()
    if not q:
        return jsonify({"results": []})

    try:
        resp = (
            supabase.table("users")
            .select("id, username")
            .ilike("username", f"%{q}%")
            .limit(5)
            .execute()
        )
        # exclude self
        results = [u for u in resp.data if u["id"] != current_user()["id"]]
        return jsonify({"results": results})
    except Exception as e:
        print("Search error:", e)
        return jsonify({"results": []})

@app.route("/api/friends/add", methods=["POST"])
def api_friends_add():
    me = current_user()
    if not me:
        return jsonify({"error": "auth required"}), 401

    data = request.get_json(force=True, silent=True) or {}
    target_id = str((data.get("target_id") or "")).strip()  # Ensure string type
    if not target_id:
        return jsonify({"error": "missing target_id"}), 400
    if target_id == str(me["id"]):
        return jsonify({"error": "cannot add yourself"}), 400

    try:
        # Verify target user exists
        target_user = supabase.table("users").select("id,username").eq("id", target_id).single().execute()
        if not target_user.data:
            return jsonify({"error": "user not found"}), 404

        pk = pair_key(str(me["id"]), target_id)  # Ensure string IDs
        print(f"Creating friend request with pair_key: {pk}")  # Debug log

        # Check for existing relationship
        existing = supabase.table("friends").select("id,status").eq("pair_key", pk).limit(1).execute()
        if existing.data:
            status = existing.data[0]["status"]
            if status == "pending":
                return jsonify({"message": "Request already pending"})
            if status == "accepted":
                return jsonify({"message": "Already friends"})
            # If rejected, create new pending request
            now = datetime.utcnow().isoformat()
            supabase.table("friends").update({
                "sender_id": me["id"],
                "receiver_id": target_id,
                "status": "pending",
                "created_at": now,
                "updated_at": now
            }).eq("id", existing.data[0]["id"]).execute()
            print(f"Updated friend request: {me['id']} -> {target_id}")  # Debug log
            return jsonify({"message": "Friend request re-sent"})

        # Insert new pending request
        now = datetime.utcnow().isoformat()
        result = supabase.table("friends").insert({
            "sender_id": me["id"],
            "receiver_id": target_id,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
            "pair_key": pk
        }).execute()
        print(f"Created new friend request: {me['id']} -> {target_id}")  # Debug log
        return jsonify({"message":"Friend request sent!", "request_id": result.data[0]["id"] if result.data else None})
    except Exception as e:
        print("api_friends_add error:", e, flush=True)  # Better error logging
        return jsonify({"error": str(e)}), 500

# api friends respond (live, from friends page)
@app.route("/api/friends/respond", methods=["POST"])
def api_friends_respond():
    me = current_user()
    if not me:
        return jsonify({"error":"auth required"}), 401

    data = request.get_json(force=True, silent=True) or {}
    req_id = data.get("request_id")
    action = data.get("action")
    if action not in ("accept", "reject"):
        return jsonify({"error":"bad action"}), 400

    try:
        # Get full request details including sender info
        row = (
            supabase.table("friends")
            .select("*, users:sender_id(username)")
            .eq("id", req_id)
            .single()
            .execute()
        )
        if not row.data:
            return jsonify({"error":"request not found"}), 404
            
        fr = row.data
        if str(fr["receiver_id"]) != str(me["id"]):
            return jsonify({"error":"not your request"}), 403

        status = "accepted" if action == "accept" else "rejected"
        now = datetime.utcnow().isoformat()
        
        # Update request status
        supabase.table("friends").update({
            "status": status,
            "updated_at": now
        }).eq("id", req_id).execute()

        if status == "accepted":
            try:
                # Create chat thread for the new friends
                thread = get_or_create_thread(str(fr["sender_id"]), str(fr["receiver_id"]))
                print(f"Created chat thread {thread['id']} for friends {fr['sender_id']} and {fr['receiver_id']}")
                
                # Update friend counts
                for uid in (fr["sender_id"], fr["receiver_id"]):
                    count_friends(uid)
                
                return jsonify({
                    "message": f"Request {status}",
                    "thread_id": thread["id"],
                    "friend": {
                        "id": fr["sender_id"],
                        "username": fr["users"]["username"]
                    }
                })
            except Exception as e:
                print(f"Error in post-acceptance processing: {e}", flush=True)
                # Still return success even if thread creation fails
                return jsonify({"message": f"Request {status}, but chat setup failed"})
        
        return jsonify({"message": f"Request {status}"})
        
    except Exception as e:
        print(f"Error handling friend request: {e}", flush=True)
        return jsonify({"error": "Failed to process request"}), 500

@app.route("/api/threads", methods=["GET"])
def api_threads():
    me = current_user()
    if not me:
        return jsonify({"error":"auth required"}), 401

    try:
        print(f"Fetching chat threads for user {me['id']}")  # Debug log
        
        # Get all threads where user is participant and there's an accepted friendship
        tr = (
            supabase.table("threads")
            .select("*")
            .or_(f"user_a.eq.{me['id']},user_b.eq.{me['id']}")
            .order("created_at", desc=True)
            .execute()
        )
        
        threads = []
        for t in (tr.data or []):
            try:
                peer_id = t["user_b"] if t["user_a"] == me["id"] else t["user_a"]
                
                # Only include thread if friendship is accepted
                fr = supabase.table("friends").select("status").eq("pair_key", pair_key(str(me["id"]), str(peer_id))).single().execute()
                if not fr.data or fr.data["status"] != "accepted":
                    print(f"Skipping thread {t['id']} - not accepted friends")  # Debug log
                    continue
                
                # Get peer details
                u = supabase.table("users").select("id,username").eq("id", peer_id).single().execute()
                peer = u.data if u.data else {"id": peer_id, "username": "unknown"}
                
                # Get last message
                lm = (
                    supabase.table("messages")
                    .select("content,created_at,sender_id")
                    .eq("thread_id", t["id"])
                    .order("created_at", desc=True)
                    .limit(1)
                    .execute()
                )
                last = lm.data[0] if lm.data else None
                
                threads.append({
                    "thread": t,
                    "peer": peer,
                    "last": last
                })
                print(f"Added thread with peer {peer['username']}")  # Debug log
                
            except Exception as e:
                print(f"Error processing thread {t['id']}: {e}", flush=True)
                continue
        
        print(f"Returning {len(threads)} active chat threads")  # Debug log
        return jsonify({"threads": threads})
        
    except Exception as e:
        print(f"Error fetching threads: {e}", flush=True)
        return jsonify({"error": "Failed to load chats"}), 500

@app.route("/api/messages", methods=["GET"])
def api_messages_v2():
    me = current_user()
    if not me:
        return jsonify({"error":"auth required"}), 401

    thread_id = request.args.get("thread_id")
    friend_id = request.args.get("friend_id")  # fallback if thread not provided
    before = request.args.get("before")  # ISO timestamp for pagination
    limit = int(request.args.get("limit", 50))
    limit = max(1, min(limit, 200))

    if not (thread_id or friend_id):
        return jsonify({"messages":[]})

    if not thread_id and friend_id:
        # Allow creating/using a thread even if not friends yet
        t = get_or_create_thread(str(me["id"]), str(friend_id))
        thread_id = t["id"]

    q = supabase.table("messages").select("*").eq("thread_id", thread_id)
    if before:
        q = q.lt("created_at", before)
    msgs = q.order("created_at").limit(limit).execute()
    return jsonify({"messages": msgs.data or []})


@app.route("/api/send_message", methods=["POST"])
def api_send_message_v3():
    me = current_user()
    if not me:
        return jsonify({"error":"auth required"}), 401

    data = request.get_json(force=True, silent=True) or {}
    friend_id = (data.get("friend_id") or "").strip()
    thread_id = (data.get("thread_id") or "").strip()
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error":"missing content"}), 400

    # resolve thread
    if not thread_id:
        if not friend_id:
            return jsonify({"error":"missing friend_id/thread_id"}), 400
        t = get_or_create_thread(str(me["id"]), str(friend_id))
        thread_id = t["id"]

    msg = {
        "thread_id": thread_id,
        "sender_id": me["id"],
        "receiver_id": friend_id if friend_id else None,
        "content": content,
        "created_at": datetime.utcnow().isoformat()
    }
    supabase.table("messages").insert(msg).execute()

    return jsonify({"success": True, "message": msg})



# friends & requests API (For Chat UI Popups)
# v1 version removed - now using v2 version with more complete friend info

@app.route("/api/requests", methods=["GET"])
def api_friend_requests_v3():
    me = current_user()
    if not me:
        return jsonify({"error": "auth required"}), 401

    try:
        # Get ALL pending requests where I am the receiver
        print(f"Fetching requests for user {me['id']}")  # Debug log
        data = (
            supabase.table("friends")
            .select("*, users:sender_id(username)")  # Get sender's username
            .eq("receiver_id", me["id"])
            .eq("status", "pending")
            .order("created_at", desc=True)
            .execute()
        )
        
        requests = []
        for row in (data.data or []):
            try:
                requests.append({
                    "id": row["id"],
                    "username": row["users"]["username"],
                    "sender_id": row["sender_id"],
                    "created_at": row["created_at"],
                    "pair_key": row.get("pair_key", "")
                })
            except Exception as e:
                print(f"Error processing request row: {e}", flush=True)
                continue
        
        print(f"Found {len(requests)} pending requests for user {me['id']}")  # Debug log
        print(f"Request data: {requests}")  # Debug log for request content
        return jsonify({"requests": requests})
    except Exception as e:
        print("Error fetching requests:", e, flush=True)  # Better error logging
        return jsonify({"requests": []})



@app.route("/api/requests/respond", methods=["POST"])
def api_friend_respond_v3():
    if not current_user():
        return jsonify({"error": "auth required"}), 401
    me = current_user()
    data = request.get_json(force=True)
    req_id = data.get("request_id")
    action = data.get("action")

    status = "accepted" if action == "accept" else "rejected"
    row = (
        supabase.table("friends").select("*").eq("id", req_id).single().execute()
    )
    if not row.data:
        return jsonify({"error": "not found"}), 404
    if row.data["receiver_id"] != me["id"]:
        return jsonify({"error": "not your request"}), 403

    supabase.table("friends").update({"status": status}).eq("id", req_id).execute()
    return jsonify({"message": f"Request {status}"})


# Add friend via AJAX (used by search dropdown “Add”)
# Older version removed - now using /api/friends/add with pair_key support

# Friend count helper

def count_friends(user_id):
    # Recalculate and update friend count for a user.
    try:
        resp = (
            supabase.table("friends")
            .select("*")
            .or_(f"(sender_id.eq.{user_id},receiver_id.eq.{user_id})")
            .eq("status", "accepted").execute()
        )
        count = len(resp.data or [])
        supabase.table("users").update({"friend_count": count}).eq("id", user_id).execute()
    except Exception as e:
        print("Count friend update failed:", e)

# 9. Server-Sent Events endpoint (SSE) for chat updates

@app.route("/events")
def sse_events():
    # stream new-message events to the browser
    if not current_user():
        return Response("unauthorized", status=401)

    q = _sse_subscribe()

    @stream_with_context
    def gen():
        try:
            # initial ping so EventSource opens cleanly
            yield f"event: ping\ndata: ok\n\n"
            while True:
                try:
                    ev = q.get(timeout=25)
                    yield f"data: {json.dumps(ev)}\n\n"
                except queue.Empty:
                    # keep-alive ping
                    yield f"event: ping\ndata: ok\n\n"
        finally:
            _sse_unsubscribe(q)

    return Response(gen(), mimetype="text/event-stream")

# 10. update username via AJAX
# v1 version removed - now using v2 version with better error handling

# 11. Classic endpoints your templates may call (back-compat)

@app.route("/api/messages_by_peerid", methods=["GET"])
def api_messages_by_peerid():
    # we just forward to /api/messages handler by copying args
    peer_id = request.args.get("peer_id")
    if peer_id:
        with app.test_request_context(f"/api/messages?peer_id={peer_id}"):
            return api_messages()
    return jsonify({"messages": [], "requests": []})

# Update username (live update)

@app.route("/api/update_username", methods=["POST"])
def api_update_username_v2(): 
    if not current_user():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(force=True, silent=True) or {}
    new_username = (data.get("username") or "").strip()
    if not new_username:
        return jsonify({"error": "Username required"}), 400

    me = current_user()

    # Check if taken
    exists = supabase.table("users").select("id").eq("username", new_username).neq("id", me["id"]).execute()
    if exists.data:
        return jsonify({"error": "Username already taken"}), 400

    supabase.table("users").update({"username": new_username}).eq("id", me["id"]).execute()

    session["user"]["username"] = new_username

    return jsonify({"message": "Username updated successfully", "username": new_username})

@app.route("/api/friend/status")
def api_friend_status():
    if not current_user():
        return jsonify({"error": "auth required"}), 401
    me = current_user()
    try:
        res = supabase.table("friend_requests").select("from_user, to_user, status").execute()
        data = res.data or []
        return jsonify({"requests": data})
    except Exception as e:
        print("Status error:", e)
        return jsonify({"requests": []})


@app.route("/api/messages_preview/<target_id>")
def api_messages_preview(target_id):
    me = current_user()
    messages = [
        {"sender_id": me["id"], "receiver_id": target_id, "content": "Hey there!", "is_me": True},
        {"sender_id": target_id, "receiver_id": me["id"], "content": "Hello 👋", "is_me": False},
    ]
    return jsonify({"messages": messages})

@app.route("/api/friends/list", methods=["GET"])
def api_get_friends_list_v2():
    if not current_user():
        return jsonify({"error": "auth required"}), 401

    user = current_user()
    user_id = user["id"]

    try:
        data = (
            supabase.table("friends")
            .select("*")
            .or_(f"sender_id.eq.{user_id},receiver_id.eq.{user_id}")
            .eq("status", "accepted")
            .execute()
        )
        friends = []
        for row in data.data:
            friend_id = row["receiver_id"] if row["sender_id"] == user_id else row["sender_id"]
            friend = (
                supabase.table("users")
                .select("id,username")
                .eq("id", friend_id)
                .single()
                .execute()
            )
            if friend.data:
                friends.append(friend.data)
        return jsonify({"friends": friends})
    except Exception as e:
        print("Error fetching friends:", e)
        return jsonify({"friends": []})


@app.route("/api/messages/<friend_id>")
def api_get_messages(friend_id):
    me = current_user()
    if not me:
        return jsonify({"error":"auth required"}), 401

    r = (
        supabase.table("messages")
        .select("*")
        .or_(f"(sender_id.eq.{me['id']},receiver_id.eq.{friend_id}),(sender_id.eq.{friend_id},receiver_id.eq.{me['id']})")
        .order("created_at")
        .execute()
    )
    return jsonify({"messages": r.data or []})

# v2 version removed - now using v3 version with thread support

@app.route("/api/profile/stats", methods=["GET"])
def api_profile_stats():
    if not current_user():
        return jsonify({"error": "auth required"}), 401
    me = current_user()

    # friends count (accepted only)
    f = (
        supabase.table("friends")
        .select("id", count="exact")
        .or_(f"sender_id.eq.{me['id']},receiver_id.eq.{me['id']}")
        .eq("status", "accepted").execute()
    )
    friends_count = f.count or 0

    # loops placeholder
    loops_count = 0

    # latest username
    u = supabase.table("users").select("username").eq("id", me["id"]).limit(1).execute()
    username = u.data[0]["username"] if u.data else me["username"]

    return jsonify({
        "username": username,
        "friends_count": friends_count,
        "loops_count": loops_count
    })


# 12. App entrypoint

if __name__ == "__main__":
    print("Connected to Supabase successfully")
    start_realtime_thread()
    app.run(debug=True, port=5000)
