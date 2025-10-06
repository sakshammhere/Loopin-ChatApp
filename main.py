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
    # Registers a new user
    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if not username or not email or not password:
        flash("Please fill all fields", "error")
        return redirect(url_for("auth_page"))

    # unique username check too
    exists_u = supabase.table("users").select("id").eq("username", username).limit(1).execute()
    if exists_u.data:
        flash("username already taken", "error")
        return redirect(url_for("auth_page"))

    existing = supabase.table("users").select("id").eq("email", email).execute()
    if existing.data:
        flash("Email already registered", "error")
        return redirect(url_for("auth_page"))

    hashed = hash_password(password)
    user_data = {
        "username": username,
        "email": email,
        "password_hash": hashed,
        "created_at": datetime.utcnow().isoformat()
    }
    supabase.table("users").insert(user_data).execute()
    flash("Account created successfully! Please log in.", "info")
    return redirect(url_for("auth_page"))

@app.route("/login", methods=["POST"])
def login():
    # Authenticate user credentials
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

        # only store id + username (keep email private)
        session["user"] = {"id": user["id"], "username": user["username"]}
        flash("Welcome back!", "success")
        return redirect(url_for("home"))
    except Exception as e:
        print("Login error:", e)
        flash("Server error during login", "error")
        return redirect(url_for("auth_page"))

@app.route("/logout")
def logout():
    # Clears session.
    session.clear()
    flash("Logged out successfully.", "info")
    return redirect(url_for("auth_page"))

# 6. Friends system (endpoints + pages)

@app.route("/api/users/search")
def api_users_search():
    # search users by username (username prefix matching), hides emails
    if not current_user():
        return jsonify({"error":"auth required"}), 401
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"users":[]})
    res = supabase.table("users").select("id,username").ilike("username", f"{q}%").limit(10).execute()
    # exclude self(user itself)
    me = current_user()
    users = [u for u in (res.data or []) if u["id"] != me["id"]]
    return jsonify({"users": users})

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

    return render_template(
        "friends.html",
        user=user,
        friends=friends.data or [],
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

@app.route("/api/messages", methods=["GET"])
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
        return jsonify({"status":"queued","lane":"requests","request":req})

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

@app.route("/api/requests/respond", methods=["POST"])
def respond_message_request():
    # accept/reject a message request; accept moves future chat to messages (friendship updates)
    if not current_user():
        return jsonify({"error":"auth required"}), 401
    me = current_user()
    data = request.get_json(force=True, silent=True) or {}
    req_id = data.get("request_id")
    action = data.get("action")  # "accept" | "reject"
    if not req_id or action not in ("accept","reject"):
        return jsonify({"error":"bad input"}), 400

    # load request and validate ownership
    r = supabase.table("message_requests").select("*").eq("id", req_id).limit(1).execute()
    if not r.data:
        return jsonify({"error":"not found"}), 404
    req = r.data[0]
    if req["receiver_id"] != me["id"]:
        return jsonify({"error":"not yours"}), 403

    if action == "reject":
        supabase.table("message_requests").delete().eq("id", req_id).execute()
        return jsonify({"status":"rejected"})

    # accept: move this one request into messages and delete it
    msg = {
        "sender_id": req["sender_id"],
        "receiver_id": req["receiver_id"],
        "content": req["content"],
        "created_at": req["created_at"]  # keep original timestamp
    }
    supabase.table("messages").insert(msg).execute()
    supabase.table("message_requests").delete().eq("id", req_id).execute()
    # broadcast to SSE for receiver’s open threads
    _sse_broadcast({"type":"message", "row": msg})
    return jsonify({"status":"accepted","moved":msg})

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

#  Add friend via AJAX (used by search dropdown “Add”)

@app.route("/api/add_friend", methods=["POST"])
def api_add_friend():
    if not current_user():
        return jsonify({"error": "Unauthorized"}), 401
    sender_id = current_user()["id"]
    payload = request.get_json(silent=True) or {}
    receiver_id = (payload.get("receiver_id") or "").strip()

    if not receiver_id:
        return jsonify({"error": "Missing receiver_id"}), 400

    if sender_id == receiver_id:
        return jsonify({"error": "Cannot add yourself"}), 400

    # deny duplicates/pending
    existing = (
        supabase.table("friends")
        .select("id,status")
        .or_(f"(sender_id.eq.{sender_id},receiver_id.eq.{receiver_id}),(sender_id.eq.{receiver_id},receiver_id.eq.{sender_id})")
        .limit(1).execute()
    )
    if existing.data:
        return jsonify({"error": f"already {existing.data[0]['status']}"}), 400

    try:
        supabase.table("friends").insert({
            "sender_id": sender_id,
            "receiver_id": receiver_id,
            "status": "pending",
            "created_at": datetime.utcnow().isoformat()
        }).execute()

        # Update friend count for both (optimistic)
        for uid in (sender_id, receiver_id):
            count_friends(uid)

        return jsonify({"message": "Friend request sent!"})
    except Exception as e:
        print("Add friend error:", e)
        return jsonify({"error": str(e)}), 500

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

@app.route("/api/update_username", methods=["POST"])
def api_update_username():
    if not current_user():
        return jsonify({"error":"auth required"}), 401
    me = current_user()
    data = request.get_json(silent=True) or {}
    new_username = (data.get("username") or "").strip()
    if not new_username:
        return jsonify({"error":"missing"}), 400

    # uniqueness check
    exists = supabase.table("users").select("id").eq("username", new_username).limit(1).execute()
    if exists.data and exists.data[0]["id"] != me["id"]:
        return jsonify({"error":"taken"}), 400

    supabase.table("users").update({"username": new_username}).eq("id", me["id"]).execute()
    # sync session
    session["user"]["username"] = new_username
    return jsonify({"ok": True})

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

    # Update in Supabase
    supabase.table("users").update({"username": new_username}).eq("id", me["id"]).execute()

    # Also update in current Flask session immediately (DOESNT WORK)
    session["user"]["username"] = new_username

    return jsonify({"message": "Username updated successfully", "username": new_username})


# 12. App entrypoint

if __name__ == "__main__":
    print("Connected to Supabase successfully")
    start_realtime_thread()
    app.run(debug=True, port=5000)
