import os
import hashlib
import threading
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from supabase import create_client, Client
from passlib.hash import bcrypt
from realtime import Socket

# 1. Load env variables

load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")  # ✅ Added anon key for realtime
SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Missing Supabase credentials in .env file!")

# 2. Flask app and Supabase client setup

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = SECRET_KEY
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

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


# 4. Realtime connection setup : Fixed version (uses anon key)
def start_realtime_thread():
    """Start realtime listener for 'messages' table using anon key (Supabase v2 Realtime)."""
    try:
        # Realtime v2 requires anon key and correct websocket endpoint
        websocket_url = f"wss://{SUPABASE_URL.split('//')[1]}/realtime/v1/websocket"

        from websocket import create_connection
        import json, threading

        def listen_realtime():
            try:
                ws = create_connection(f"{websocket_url}?apikey={os.getenv('SUPABASE_ANON_KEY')}&vsn=1.0.0")
                print("✅ Connected to Supabase Realtime")

                # Subscribe to the public:messages channel
                payload = {
                    "topic": "realtime:public:messages",
                    "event": "phx_join",
                    "payload": {},
                    "ref": 1
                }
                ws.send(json.dumps(payload))
                print("📡 Listening for message inserts...")

                while True:
                    data = ws.recv()
                    if '"INSERT"' in data:
                        print(f"📩 New message received: {data}")
            except Exception as e:
                print(f"⚠️ Realtime listener crashed: {e}")

        threading.Thread(target=listen_realtime, daemon=True).start()
    except Exception as e:
        print(f"⚠️ Failed to start realtime thread: {e}")




# 5. Authentication routes
@app.route("/")
def home():
    # Main chat hub — show all messages and dummy peer(chat not implemented yet)
    if not current_user():
        return redirect(url_for("auth_page"))
    user = current_user()

    # Fetch user messages
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

@app.route("/auth/signup", methods=["POST"])
def signup():
    # Registers a new user
    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if not username or not email or not password:
        flash("Please fill all fields", "error")
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

@app.route("/auth/login", methods=["POST"])
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

        session["user"] = {"id": user["id"], "email": user["email"], "username": user["username"]}
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


# 6. Friends system (pending implementation)
@app.route("/friends", methods=["GET"])
def friends_page():
    # Display friend list and pending requests.

    if not current_user():
        return redirect(url_for("auth_page"))
    user = current_user()

    friends = supabase.rpc("get_friends_list", {"user_id": user["id"]}).execute()
    pending = supabase.rpc("get_pending_requests", {"user_id": user["id"]}).execute()

    return render_template(
        "friends.html",
        user=user,
        friends=friends.data or [],
        requests=pending.data or []
    )

@app.route("/friends/add", methods=["POST"])
def add_friend():
    # Send a friend request.

    if not current_user():
        return redirect(url_for("auth_page"))
    sender_id = current_user()["id"]
    target_email = request.form.get("email")

    target = supabase.table("users").select("id").eq("email", target_email).execute()
    if not target.data:
        flash("No such user", "error")
        return redirect(url_for("friends_page"))

    receiver_id = target.data[0]["id"]
    supabase.table("friends").insert(
        {"sender_id": sender_id, "receiver_id": receiver_id, "status": "pending"}
    ).execute()
    flash("Friend request sent!", "success")
    return redirect(url_for("friends_page"))

@app.route("/friends/respond", methods=["POST"])
def respond_request():
    # Accept/reject a friend request.

    if not current_user():
        return redirect(url_for("auth_page"))
    request_id = request.form.get("request_id")
    action = request.form.get("action")
    status = "accepted" if action == "accept" else "rejected"
    supabase.table("friends").update({"status": status}).eq("id", request_id).execute()
    flash(f"Request {status}.", "info")
    return redirect(url_for("friends_page"))

# 7. Messaging
@app.route("/send_message", methods=["POST"])
def send_message():
    # Send message to another user.

    if not current_user():
        return redirect(url_for("auth_page"))
    sender = current_user()
    receiver_email = request.form.get("receiver")
    content = request.form.get("message", "").strip()

    if not receiver_email or not content:
        return jsonify({"error": "Missing fields"}), 400

    receiver = supabase.table("users").select("id").eq("email", receiver_email).execute()
    if not receiver.data:
        return jsonify({"error": "Receiver not found"}), 404

    msg = {
        "sender_id": sender["id"],
        "receiver_id": receiver.data[0]["id"],
        "content": content,
        "created_at": datetime.utcnow().isoformat()
    }
    supabase.table("messages").insert(msg).execute()
    return jsonify({"message": "sent"})

# 8. Profile page
@app.route("/profile")
def profile_page():
    # Show profile page for current user. (change username doesnt work for now)
    if not current_user():
        return redirect(url_for("auth_page"))
    user = current_user()
    return render_template("profile.html", user=user)

# 9. API & SSE routes for JS frontend

@app.route("/events")
def events():
    # Simulate SSE (Server-Sent Events)
    def stream():
        import time
        while True:
            yield f"data: ping {datetime.utcnow().isoformat()}\n\n"
            time.sleep(5)
    return app.response_class(stream(), mimetype="text/event-stream")

@app.route("/api/messages")
def api_messages():
    # Return all messages for given peer_id
    if not current_user():
        return jsonify({"error": "Unauthorized"}), 403

    peer_id = request.args.get("peer_id")
    user = current_user()
    resp = (
        supabase.table("messages")
        .select("*")
        .or_(f"sender_id.eq.{user['id']},receiver_id.eq.{user['id']}")
        .order("created_at")
        .execute()
    )
    return jsonify(resp.data or [])

@app.route("/api/send_message", methods=["POST"])
def api_send_message():
    # JS-based message sending API
    if not current_user():
        return jsonify({"error": "Unauthorized"}), 403

    sender = current_user()
    data = request.get_json()
    receiver_email = data.get("receiver")
    content = data.get("message", "").strip()

    receiver = supabase.table("users").select("id").eq("email", receiver_email).execute()
    if not receiver.data:
        return jsonify({"error": "Receiver not found"}), 404

    msg = {
        "sender_id": sender["id"],
        "receiver_id": receiver.data[0]["id"],
        "content": content,
        "created_at": datetime.utcnow().isoformat()
    }
    supabase.table("messages").insert(msg).execute()
    return jsonify({"message": "sent"})

@app.route("/api/update_username", methods=["POST"])
def api_update_username():
    # Update user's display name
    if not current_user():
        return jsonify({"error": "Unauthorized"}), 403

    user = current_user()
    data = request.get_json()
    new_username = data.get("username", "").strip()

    if not new_username:
        return jsonify({"error": "Missing username"}), 400

    supabase.table("users").update({"username": new_username}).eq("id", user["id"]).execute()
    session["user"]["username"] = new_username
    return jsonify({"message": "Username updated"})

if __name__ == "__main__":
    print("Connected to Supabase successfully")
    start_realtime_thread()
    app.run(debug=True, port=5000)
