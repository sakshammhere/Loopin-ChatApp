import os
import re
import hashlib
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, flash
from supabase import create_client, Client
from passlib.hash import bcrypt  # ✅ using bcrypt with manual SHA256 pre-hash


# ---------------- Load environment variables ----------------
load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Please set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in your .env file")

# Create Supabase client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# Flask setup
app = Flask(__name__, template_folder="templates")
app.secret_key = SECRET_KEY


# ---------------- Helpers ----------------
def valid_email(email: str) -> bool:
    """Basic email validation regex"""
    return re.match(r"^[\w\.-]+@[\w\.-]+\.\w+$", email) is not None


def valid_password(password: str) -> bool:
    """Password rules: min 8 chars, 1 uppercase, 1 digit, 1 special char"""
    return re.match(r"^(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&]).{8,}$", password) is not None


def hash_password(password: str) -> str:
    """Hash password safely using SHA256 + bcrypt to avoid 72-byte limit."""
    sha = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return bcrypt.hash(sha)


def verify_password(password: str, hashed: str) -> bool:
    """Verify password hashed with SHA256 + bcrypt"""
    try:
        sha = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return bcrypt.verify(sha, hashed)
    except Exception:
        return False


# ---------------- Routes ----------------
@app.route("/")
def index():
    """Home page — greets logged-in user or redirects to auth."""
    if "user" in session:
        return render_template("index.html", user=session["user"])
    return redirect(url_for("auth_page"))


@app.route("/auth", methods=["GET"])
def auth_page():
    """Login / Signup page"""
    return render_template("loginsignup.html")


@app.route("/auth/signup", methods=["POST"])
def signup():
    """Handle signup: validate → hash → insert into public.users"""
    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    # ---- validations ----
    if not username or not email or not password or not confirm_password:
        return redirect(url_for("auth_page", error="please fill all fields"))
    if not valid_email(email):
        return redirect(url_for("auth_page", error="invalid email format"))
    if not valid_password(password):
        return redirect(url_for("auth_page", error="password must be 8+ chars, include uppercase, number, and special char"))
    if password != confirm_password:
        return redirect(url_for("auth_page", error="passwords do not match"))

    # ---- check if email exists ----
    try:
        existing = supabase.table("users").select("id").eq("email", email).limit(1).execute()
        existing_data = existing.get("data") if isinstance(existing, dict) else getattr(existing, "data", None)
        if existing_data and len(existing_data) > 0:
            return redirect(url_for("auth_page", error="email already registered"))
    except Exception:
        pass

    # ---- insert user ----
    try:
        hashed = hash_password(password)
        new_user = {
            "username": username,
            "email": email,
            "password_hash": hashed,
            "created_at": datetime.utcnow().isoformat()
        }
        resp = supabase.table("users").insert(new_user).execute()

        data = resp.get("data") if isinstance(resp, dict) else getattr(resp, "data", None)
        if not data:
            return redirect(url_for("auth_page", error="signup failed (db error)"))

        return redirect(url_for("auth_page", info="account created successfully, please login", mode="login"))

    except Exception as e:
        return redirect(url_for("auth_page", error=f"signup error: {str(e)}"))


@app.route("/auth/login", methods=["POST"])
def login():
    """Login: fetch user by email, verify hash, set session"""
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if not email or not password:
        return redirect(url_for("auth_page", error="please provide email and password", mode="login"))

    try:
        resp = supabase.table("users").select("*").eq("email", email).limit(1).execute()
        row = resp.get("data") if isinstance(resp, dict) else getattr(resp, "data", None)
        if not row:
            return redirect(url_for("auth_page", error="no account with this email", mode="login"))

        user = row[0]
        stored_hash = user.get("password_hash")
        if not stored_hash or not verify_password(password, stored_hash):
            return redirect(url_for("auth_page", error="invalid credentials", mode="login"))

        session["user"] = {
            "id": user.get("id"),
            "email": user.get("email"),
            "username": user.get("username")
        }
        return redirect(url_for("index"))

    except Exception as e:
        return redirect(url_for("auth_page", error=f"login error: {str(e)}", mode="login"))


@app.route("/profile", methods=["GET", "POST"])
def profile():
    """Profile page — view/update username"""
    if "user" not in session:
        return redirect(url_for("auth_page"))

    user_id = session["user"]["id"]

    if request.method == "POST":
        new_username = request.form.get("username", "").strip()
        if not new_username:
            flash("Username cannot be empty", "error")
            return redirect(url_for("profile"))

        try:
            supabase.table("users").update({"username": new_username}).eq("id", user_id).execute()
            session["user"]["username"] = new_username
            flash("Username updated successfully", "success")
        except Exception as e:
            flash(f"Error updating username: {e}", "error")
        return redirect(url_for("profile"))

    # Fetch latest username
    resp = supabase.table("users").select("username").eq("id", user_id).limit(1).execute()
    row = resp.get("data") if isinstance(resp, dict) else getattr(resp, "data", None)
    username = row[0].get("username") if row else None

    session["user"]["username"] = username

    # ✅ Pass user to template
    return render_template("profile.html", user=session["user"])


@app.route("/logout")
def logout():
    """Clear session"""
    session.clear()
    return redirect(url_for("auth_page"))


# ---------------- Run Flask ----------------
if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
