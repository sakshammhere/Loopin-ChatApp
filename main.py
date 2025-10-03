import os, re
from flask import Flask, render_template, request, redirect, url_for, session
from dotenv import load_dotenv
from supabase import create_client, Client
from urllib.parse import urlencode

# load env variables
load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

app = Flask(__name__, template_folder="templates")
app.secret_key = SECRET_KEY

# routes

@app.route("/")
def index():
    if "user" in session:
        return f"hello, {session['user']['email']}! <a href='/logout'>logout</a>"
    return redirect(url_for("auth_page"))

@app.route("/auth", methods=["GET"])
def auth_page():
    return render_template("loginsignup.html")

@app.route("/auth/signup", methods=["POST"])
def signup():
    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    # Validations
    # checking empty fields
    if not username or not email or not password or not confirm_password:
        msg = urlencode({"error": "please fill all fields"})
        return redirect(url_for("auth_page") + f"?{msg}")

    # validate email using regex (must have @ and .something)
    email_regex = r"^[\w\.-]+@[\w\.-]+\.\w+$"
    if not re.match(email_regex, email):
        msg = urlencode({"error": "invalid email format"})
        return redirect(url_for("auth_page") + f"?{msg}")

    # validate password length and strength
    # must be at least 8 chars, 1 caps, 1 digit, 1 special char
    pw_regex = r"^(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]{8,}$"
    if not re.match(pw_regex, password):
        msg = urlencode({"error": "password must be 8+ chars, include uppercase, number, and special char"})
        return redirect(url_for("auth_page") + f"?{msg}")

    # confirm password check
    if password != confirm_password:
        msg = urlencode({"error": "passwords do not match"})
        return redirect(url_for("auth_page") + f"?{msg}")

    # -------- supabase signup --------
    try:
        resp = supabase.auth.admin.create_user(
            {
                "email": email,
                "password": password,
                "email_confirm": True,
            }
        )
        user = resp["user"]
        user_id = user["id"]

        supabase.table("users").insert({"id": user_id, "username": username}).execute()

    except Exception as e:
        msg = urlencode({"error": f"signup error: {e}"})
        return redirect(url_for("auth_page") + f"?{msg}")

    msg = urlencode({"info": "account created successfully, you can log in now", "mode": "login"})
    return redirect(url_for("auth_page") + f"?{msg}")

@app.route("/auth/login", methods=["POST"])
def login():
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    try:
        resp = supabase.auth.sign_in_with_password({"email": email, "password": password})
        data = getattr(resp, "data", None) or resp.get("data") if isinstance(resp, dict) else resp

        user = data.get("user") if isinstance(data, dict) else None
        if not user:
            msg = urlencode({"error": "login failed, check credentials", "mode": "login"})
            return redirect(url_for("auth_page") + f"?{msg}")

        session["user"] = {"id": user["id"], "email": user["email"]}
        return redirect(url_for("index"))

    except Exception as e:
        msg = urlencode({"error": f"login error: {e}", "mode": "login"})
        return redirect(url_for("auth_page") + f"?{msg}")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth_page"))

if __name__ == "__main__":
    app.run(debug=True, port=5000)
