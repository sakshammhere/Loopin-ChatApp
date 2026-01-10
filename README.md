# Loopin-ChatApp 💬

**Loopin** is a modern real-time chat application built as a personal project to explore how people can *loop in* with friends and family through clean design, real-time communication, and social features. The name **Loopin** reflects the idea of staying connected — always in the loop.

This project focuses on **core system design**, **real-time messaging**, and **social graph features** rather than deployment or scaling.

---

## ✨ What is Loopin?

> *Loop in with your friends and family — instantly, securely, and beautifully.*

Loopin is a full-stack chatting platform where users can:

* Create accounts and authenticate securely
* Search users and send friend requests
* Accept or reject connections
* Chat in real time once connected
* Manage profiles and usernames
* Experience a smooth, animated chat UI

It is inspired by modern messaging apps (Instagram, Messenger) but built **from scratch** using Flask and Supabase.

---

## 🧠 Core Features

### 🔐 Authentication

* Login & signup system
* Secure password hashing (SHA-256 + bcrypt)
* Session-based authentication (Flask sessions)

### 👥 Friends System

* Search users by username/email
* Send & receive friend requests
* Accept / reject requests
* Friend-only chat access

### 💬 Real-Time Chat

* One-to-one chat threads
* Real-time updates using:

  * Supabase Realtime (WebSockets)
  * Server-Sent Events (SSE)
* Message persistence with timestamps
* Emoji picker support 😊

### 🧵 Chat Threads

* Automatic thread creation when friends connect
* Messages grouped per thread
* Sidebar shows active chats

### 📬 Message Requests

* Message requests for non-friends
* Incoming and outgoing request tracking

### 👤 Profile

* View username, friend count, loops (future feature)
* Change username live without page reload

### 🎨 UI / UX

* Dark modern theme
* Animated idle chat screen
* Gradient branding & glowing effects
* Modal-based navigation (friends, requests, profile)

---

## 🧰 Tech Stack

### Backend

* **Python**
* **Flask**
* **Supabase (PostgreSQL + Realtime)**
* Server-Sent Events (SSE)
* WebSockets (Supabase Realtime)

### Frontend

* HTML5
* CSS3 (custom, animated UI)
* Vanilla JavaScript (no framework)

### Security

* passlib (bcrypt)
* SHA-256 preprocessing
* Environment-based secrets

---

## 🔑 Environment Variables

Create a `.env` file in the root directory:

```
FLASK_SECRET_KEY=your_secret_key
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your_service_role_key
SUPABASE_ANON_KEY=your_anon_key
```

> ⚠️ These keys are required for authentication, database access, and realtime messaging.

---

## Running Locally

> Deployment is **intentionally excluded**. This project is meant to be run locally.

### 1. Clone the repository

```bash
git clone https://github.com/sakshammhere/Loopin-ChatApp.git
cd Loopin-ChatApp
```

### 2. Create virtual environment

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Add `.env` file

Add your Supabase credentials as shown above.

### 5. Run the app

```bash
python main.py
```

## 🖼️ Screenshots 

---

<img width="1536" height="1024" alt="loopin_screenshot" src="https://github.com/user-attachments/assets/066db245-931c-4990-940e-6478d61e8c29" />




<img width="1536" height="1024" alt="loopin_screenshot2" src="https://github.com/user-attachments/assets/a323d0ac-afa0-445d-bc1e-92337e5c9ed4" />


---

## 🛠️ API Overview (Selected)

| Endpoint               | Method | Description           |
| ---------------------- | ------ | --------------------- |
| `/signup`              | POST   | Create account        |
| `/login`               | POST   | Login user            |
| `/api/users/search`    | GET    | Search users          |
| `/api/friends/add`     | POST   | Send friend request   |
| `/api/friends/respond` | POST   | Accept/Reject request |
| `/api/messages`        | GET    | Fetch messages        |
| `/api/send_message`    | POST   | Send message          |
| `/events`              | GET    | Real-time SSE stream  |

---



## Author

**Saksham Gupta**
Personal project built to explore full-stack development, real-time systems, and UI design.

**Loop in. Stay connected. Welcome to Loopin.** 🚀
