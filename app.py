#!/usr/bin/env python3
"""
MR X Bot Hosting Panel — v2 (SaaS edition)
------------------------------------------
A single-file Flask panel to host Telegram bots, now with:

- A public landing page with Free / Premium plans.
- User accounts (register / login). Each user sees only their own bots.
- Plan limits: Free = 2 bots, Premium = unlimited bots.
- License keys (MRX-XXXXXX-XXXXXX-XXXXXX) that activate Premium for a
  number of days. Users redeem keys in the panel.
- An admin panel (stats, users, keys, all bots).

Admin login: username "admin" + the PANEL_PASSWORD environment variable.
If PANEL_PASSWORD is not set, a random password is generated at first
start and printed in the server log (see the console / host logs).

Each bot is an uploaded .py file run as its own subprocess. Bot tokens
are stored per-bot in data/bots/<id>/config.env on disk and are passed
to the bot as the BOT_TOKEN environment variable. The full token is
never shown in the UI again (only the last 4 chars).

Run:  python app.py        (PORT env var sets the port, default 5000)
"""

import os
import sys
import json
import time
import shutil
import secrets
import string
import threading
import subprocess
from datetime import datetime
from functools import wraps

from flask import (Flask, request, session, redirect, url_for,
                   render_template_string, abort)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

# --------------------------------------------------------------------------
# Paths & storage
# --------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
BOTS_DIR = os.path.join(DATA_DIR, "bots")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
DB_FILE = os.path.join(DATA_DIR, "bots.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
KEYS_FILE = os.path.join(DATA_DIR, "keys.json")

os.makedirs(BOTS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# --------------------------------------------------------------------------
# App setup
# --------------------------------------------------------------------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB upload limit


def _load_or_make_secret():
    """Persist a Flask secret key so logins survive panel restarts."""
    key_file = os.path.join(DATA_DIR, "secret.key")
    if os.path.exists(key_file):
        with open(key_file) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(key_file, "w") as f:
        f.write(key)
    return key


app.secret_key = os.environ.get("SECRET_KEY") or _load_or_make_secret()

# --------------------------------------------------------------------------
# Panel / admin password
# --------------------------------------------------------------------------
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "").strip()
PASSWORD_GENERATED = False
if not PANEL_PASSWORD:
    alphabet = string.ascii_letters + string.digits
    PANEL_PASSWORD = "".join(secrets.choice(alphabet) for _ in range(14))
    PASSWORD_GENERATED = True

# --------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------
FREE_BOT_LIMIT = 2
PREMIUM_BOT_LIMIT = 999  # shown to users as "Unlimited"

# --------------------------------------------------------------------------
# Generic JSON store helpers
# --------------------------------------------------------------------------
_store_lock = threading.Lock()


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (ValueError, OSError):
        return default


def _save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Bot registry (plain JSON file, no database)
# --------------------------------------------------------------------------
_db_lock = threading.Lock()
_processes = {}          # bot_id -> {"proc": Popen, "started": float, "restarts": int}
_proc_lock = threading.Lock()


def load_db():
    return _load_json(DB_FILE, {"bots": []})


def save_db(db):
    _save_json(DB_FILE, db)


def get_bot(bot_id):
    db = load_db()
    for bot in db["bots"]:
        if bot["id"] == bot_id:
            return bot
    return None


def update_bot(bot_id, **fields):
    with _db_lock:
        db = load_db()
        for bot in db["bots"]:
            if bot["id"] == bot_id:
                bot.update(fields)
        save_db(db)


def bot_dir(bot_id):
    return os.path.join(BOTS_DIR, bot_id)


def bot_log_path(bot_id):
    return os.path.join(LOGS_DIR, bot_id + ".log")


def read_bot_token(bot_id):
    """Read the token from the bot's own config.env (never stored in bots.json)."""
    cfg = os.path.join(bot_dir(bot_id), "config.env")
    if not os.path.exists(cfg):
        return ""
    with open(cfg) as f:
        for line in f:
            line = line.strip()
            if line.startswith("BOT_TOKEN="):
                return line[len("BOT_TOKEN="):].strip()
    return ""


def delete_bot_everywhere(bot_id):
    """Stop a bot and remove its record, files and log."""
    stop_bot(bot_id)
    with _proc_lock:
        _processes.pop(bot_id, None)
    shutil.rmtree(bot_dir(bot_id), ignore_errors=True)
    try:
        os.remove(bot_log_path(bot_id))
    except OSError:
        pass
    with _db_lock:
        db = load_db()
        db["bots"] = [b for b in db["bots"] if b["id"] != bot_id]
        save_db(db)


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------
def load_users():
    return _load_json(USERS_FILE, {"users": []})


def save_users(data):
    _save_json(USERS_FILE, data)


def get_user(user_id):
    if not user_id:
        return None
    for user in load_users()["users"]:
        if user["id"] == user_id:
            return user
    return None


def find_user_by_username(username):
    username = (username or "").strip().lower()
    for user in load_users()["users"]:
        if user["username"] == username:
            return user
    return None


def user_is_premium(user):
    return bool(user) and int(user.get("premium_until") or 0) > int(time.time())


def user_plan_name(user):
    return "Premium" if user_is_premium(user) else "Free"


def user_bot_limit(user):
    return PREMIUM_BOT_LIMIT if user_is_premium(user) else FREE_BOT_LIMIT


def user_bots(user_id):
    return [b for b in load_db()["bots"] if b.get("owner") == user_id]


def fmt_date(ts):
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts)).strftime("%d %b %Y")


def fmt_datetime(ts):
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts)).strftime("%d %b %Y, %H:%M")


# --------------------------------------------------------------------------
# License keys
# --------------------------------------------------------------------------
_KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I confusion


def load_keys():
    return _load_json(KEYS_FILE, {"keys": []})


def save_keys(data):
    _save_json(KEYS_FILE, data)


def make_key_code():
    def part():
        return "".join(secrets.choice(_KEY_ALPHABET) for _ in range(6))
    return "MRX-%s-%s-%s" % (part(), part(), part())


def generate_keys(duration_days, quantity):
    """Create fresh unused Premium keys; returns the list of key records."""
    with _store_lock:
        data = load_keys()
        existing = {k["code"] for k in data["keys"]}
        made = []
        for _ in range(quantity):
            code = make_key_code()
            while code in existing:
                code = make_key_code()
            existing.add(code)
            rec = {
                "code": code,
                "plan": "premium",
                "duration_days": int(duration_days),
                "status": "unused",
                "created": int(time.time()),
                "used_by": None,
                "used_by_name": None,
                "used_at": None,
            }
            data["keys"].append(rec)
            made.append(rec)
        save_keys(data)
        return made


# --------------------------------------------------------------------------
# Bot runner
# --------------------------------------------------------------------------
def _log_line(bot_id, text):
    with open(bot_log_path(bot_id), "a") as f:
        f.write(text + "\n")


def start_bot(bot_id):
    bot = get_bot(bot_id)
    if not bot:
        return False
    with _proc_lock:
        entry = _processes.get(bot_id)
        if entry and entry["proc"].poll() is None:
            return True  # already running
        script = os.path.join(bot_dir(bot_id), bot["file"])
        if not os.path.exists(script):
            _log_line(bot_id, "[panel] ERROR: bot file missing: " + bot["file"])
            return False
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        token = read_bot_token(bot_id)
        if token:
            env["BOT_TOKEN"] = token
        logf = open(bot_log_path(bot_id), "a")
        logf.write("\n===== [panel] starting bot '%s' =====\n" % bot["name"])
        logf.flush()
        try:
            proc = subprocess.Popen(
                [sys.executable, "-u", script],
                cwd=bot_dir(bot_id),
                env=env,
                stdout=logf,
                stderr=subprocess.STDOUT,
            )
        except OSError as e:
            logf.write("[panel] ERROR: could not start process: %s\n" % e)
            logf.close()
            return False
        _processes[bot_id] = {"proc": proc, "started": time.time(),
                              "restarts": entry["restarts"] if entry else 0}
    update_bot(bot_id, desired_running=True)
    return True


def stop_bot(bot_id, mark_stopped=True):
    with _proc_lock:
        entry = _processes.get(bot_id)
        if entry and entry["proc"].poll() is None:
            entry["proc"].terminate()
            try:
                entry["proc"].wait(timeout=5)
            except subprocess.TimeoutExpired:
                entry["proc"].kill()
                entry["proc"].wait(timeout=5)
            _log_line(bot_id, "[panel] bot stopped by panel")
    if mark_stopped:
        update_bot(bot_id, desired_running=False)


def bot_status(bot):
    """Truthful status from the real process state."""
    with _proc_lock:
        entry = _processes.get(bot["id"])
    if entry and entry["proc"].poll() is None:
        return "running"
    if bot.get("desired_running") and entry is not None:
        # It was started but the process has exited on its own.
        if entry["proc"].returncode not in (0, None):
            return "crashed"
        return "stopped"
    return "stopped"


def bot_uptime(bot):
    with _proc_lock:
        entry = _processes.get(bot["id"])
    if not entry or entry["proc"].poll() is not None:
        return "-"
    secs = int(time.time() - entry["started"])
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%dh %dm" % (h, m)
    if m:
        return "%dm %ds" % (m, s)
    return "%ds" % s


def _monitor_loop():
    """Crash watcher: restarts bots that have auto-restart ON."""
    # On panel boot: only auto-restart bots come back on their own.
    db = load_db()
    for bot in db["bots"]:
        if bot.get("desired_running") and bot.get("auto_restart"):
            start_bot(bot["id"])
    while True:
        time.sleep(3)
        db = load_db()
        for bot in db["bots"]:
            if not (bot.get("auto_restart") and bot.get("desired_running")):
                continue
            with _proc_lock:
                entry = _processes.get(bot["id"])
            if entry is None or entry["proc"].poll() is not None:
                if entry is not None and entry["proc"].returncode in (0, None):
                    continue  # clean exit: leave it stopped
                _log_line(bot["id"], "[panel] auto-restart: bot died, restarting...")
                if start_bot(bot["id"]):
                    with _proc_lock:
                        if bot["id"] in _processes:
                            _processes[bot["id"]]["restarts"] = entry["restarts"] + 1 if entry else 1


# --------------------------------------------------------------------------
# Auth helpers
# --------------------------------------------------------------------------
def current_user():
    """The logged-in regular user record, or None (admin is separate)."""
    uid = session.get("user_id")
    if not uid:
        return None
    user = get_user(uid)
    if user is None or user.get("banned"):
        return None
    return user


def is_admin():
    return bool(session.get("is_admin"))


def flash(msg, kind="ok"):
    session["flash"] = {"msg": msg, "kind": kind}


def pop_flash():
    return session.pop("flash", None)


def login_required(view):
    """Any valid session (user or admin). Banned/deleted users are kicked."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if is_admin():
            return view(*args, **kwargs)
        uid = session.get("user_id")
        if not uid:
            return redirect(url_for("login"))
        user = get_user(uid)
        if user is None or user.get("banned"):
            session.clear()
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


def user_required(view):
    """Regular user pages. Admins are sent to the admin panel."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if is_admin():
            return redirect(url_for("admin_home"))
        user = current_user()
        if user is None:
            if session.get("user_id"):
                session.clear()
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_admin():
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


def can_access_bot(bot):
    """Admins can touch every bot; users only their own."""
    if bot is None:
        return False
    if is_admin():
        return True
    user = current_user()
    return bool(user) and bot.get("owner") == user["id"]


def _after_action():
    return redirect(url_for("admin_bots") if is_admin() else url_for("dashboard"))


# --------------------------------------------------------------------------
# HTML (dark MR X style: black + gold)
# --------------------------------------------------------------------------
CSS = """
<style>
  * { box-sizing: border-box; }
  html { font-size:17px; }
  body { margin:0; font-family:'Segoe UI', Arial, sans-serif; background:#080808; color:#f2f2f2;
         background-image:radial-gradient(1000px 500px at 50% -100px, rgba(212,175,55,.10), transparent);
         min-height:100vh; display:flex; flex-direction:column; }
  main { flex:1; width:100%; }
  header { background:rgba(15,15,15,.95); border-bottom:2px solid #d4af37; padding:18px 26px;
           display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px;
           box-shadow:0 4px 24px rgba(0,0,0,.5); position:sticky; top:0; z-index:10; }
  header .logo { color:#d4af37; font-size:26px; font-weight:800; letter-spacing:2px;
                 text-shadow:0 2px 12px rgba(212,175,55,.35); }
  header .logo a { color:#d4af37; background:none; padding:0; box-shadow:none; font-size:26px; }
  header nav { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
  header a { color:#111; background:linear-gradient(180deg,#e8c95a,#c9a227); text-decoration:none;
             font-size:15px; font-weight:700; padding:9px 18px; border-radius:8px; }
  header a.ghost { background:#2c2c2c; color:#eee; border:1px solid #3d3d3d; }
  footer { border-top:1px solid #262626; padding:22px; text-align:center; color:#8a8a8a;
           font-size:14px; margin-top:20px; }
  footer b { color:#d4af37; }
  .wrap { max-width:1060px; margin:34px auto; padding:0 20px; width:100%; }
  .card { background:linear-gradient(180deg,#171717,#111); border:1px solid #2e2e2e; border-radius:14px;
          padding:26px; margin-bottom:26px; box-shadow:0 10px 30px rgba(0,0,0,.45); }
  h2 { color:#d4af37; margin:0 0 18px; font-size:22px; letter-spacing:.5px; }
  h3 { color:#eee; margin:0 0 10px; font-size:18.5px; }
  .tablewrap { overflow-x:auto; }
  table { width:100%; border-collapse:collapse; font-size:16px; }
  th, td { padding:13px 10px; border-bottom:1px solid #262626; text-align:left; vertical-align:middle; }
  th { color:#d4af37; font-weight:700; font-size:15px; text-transform:uppercase; letter-spacing:.5px; }
  .badge { padding:4px 13px; border-radius:20px; font-size:13.5px; font-weight:700; white-space:nowrap; }
  .running { background:#14532d; color:#4ade80; }
  .stopped { background:#333; color:#bbb; }
  .crashed { background:#5b1a1a; color:#f87171; }
  .plan-premium { background:linear-gradient(180deg,#e8c95a,#c9a227); color:#111; }
  .plan-free { background:#333; color:#bbb; }
  .key-unused { background:#14532d; color:#4ade80; }
  .key-used { background:#333; color:#bbb; }
  .key-revoked { background:#5b1a1a; color:#f87171; }
  .banned { background:#5b1a1a; color:#f87171; }
  button, .btn { background:linear-gradient(180deg,#e8c95a,#c9a227); color:#111; border:none;
                 padding:10px 18px; border-radius:9px; font-weight:700; cursor:pointer; font-size:15px;
                 text-decoration:none; display:inline-block; box-shadow:0 3px 10px rgba(0,0,0,.4);
                 transition:transform .08s ease, filter .15s ease; }
  button:hover, .btn:hover { filter:brightness(1.08); transform:translateY(-1px); }
  button.grey { background:#2c2c2c; color:#eee; border:1px solid #3d3d3d; }
  button.red { background:#7f1d1d; color:#fecaca; }
  button:disabled { opacity:.5; cursor:not-allowed; transform:none; }
  form.inline { display:inline; }
  input[type=text], input[type=password], input[type=email], input[type=number], select {
                 background:#0b0b0b; border:1.5px solid #3a3a3a; color:#fff;
                 padding:14px 15px; border-radius:9px; width:100%; margin:7px 0 16px; font-size:16.5px; }
  input:focus, select:focus { outline:none; border-color:#d4af37; box-shadow:0 0 0 3px rgba(212,175,55,.18); }
  input[type=file] { margin:7px 0 16px; color:#ddd; font-size:15.5px; width:auto; }
  input[type=checkbox] { width:19px; height:19px; vertical-align:-3px; accent-color:#d4af37; }
  label { font-size:15.5px; color:#cfcfcf; font-weight:600; }
  .err { background:#5b1a1a; color:#fecaca; padding:13px; border-radius:9px; margin-bottom:16px; font-size:15.5px; }
  .okmsg { background:#14532d; color:#bbf7d0; padding:13px; border-radius:9px; margin-bottom:16px; font-size:15.5px; }
  pre.log { background:#000; color:#4ade80; padding:18px; border-radius:10px; font-size:14px;
            white-space:pre-wrap; word-wrap:break-word; max-height:70vh; overflow:auto; }
  .muted { color:#9a9a9a; font-size:13.5px; }
  .center { max-width:440px; margin:8vh auto; }
  .tagline { text-align:center; color:#9a9a9a; font-size:15px; margin-top:14px; letter-spacing:.4px; }
  /* Landing page */
  .hero { text-align:center; padding:84px 20px 60px; max-width:900px; margin:0 auto; }
  .hero .kicker { color:#d4af37; font-weight:700; letter-spacing:3px; font-size:14px; text-transform:uppercase; }
  .hero h1 { font-size:52px; line-height:1.12; margin:18px 0 8px; font-weight:800; }
  .hero h1 .gold { color:#d4af37; text-shadow:0 4px 30px rgba(212,175,55,.35); }
  .hero p.sub { font-size:20px; color:#c9c9c9; max-width:640px; margin:14px auto 32px; line-height:1.55; }
  .hero .cta { display:flex; gap:14px; justify-content:center; flex-wrap:wrap; }
  .hero .cta .btn { padding:15px 32px; font-size:17.5px; border-radius:11px; }
  .hero .cta .btn.ghost { background:#1c1c1c; color:#eee; border:1.5px solid #d4af37; box-shadow:none; }
  .section { max-width:1060px; margin:0 auto; padding:26px 20px; width:100%; }
  .section h2 { text-align:center; font-size:30px; margin-bottom:26px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:18px; }
  .feature { background:linear-gradient(180deg,#171717,#101010); border:1px solid #2b2b2b; border-radius:14px;
             padding:24px; box-shadow:0 8px 24px rgba(0,0,0,.4); }
  .feature .icon { font-size:30px; margin-bottom:10px; }
  .feature p { color:#bdbdbd; font-size:15.5px; line-height:1.55; margin:0; }
  .pricing { display:grid; grid-template-columns:repeat(auto-fit,minmax(225px,1fr)); gap:22px;
             max-width:1060px; margin:14px auto 0; }
  .price-card { background:linear-gradient(180deg,#161616,#0f0f0f); border:1.5px solid #2e2e2e;
                border-radius:16px; padding:32px 28px; text-align:center; position:relative;
                box-shadow:0 12px 32px rgba(0,0,0,.5); }
  .price-card.premium { border-color:#d4af37; box-shadow:0 12px 40px rgba(212,175,55,.22); }
  .price-card .planname { font-size:19px; font-weight:800; letter-spacing:2px; text-transform:uppercase; }
  .price-card.premium .planname { color:#d4af37; }
  .price-card .price { font-size:40px; font-weight:800; margin:12px 0 4px; }
  .price-card ul { list-style:none; padding:0; margin:20px 0 26px; text-align:left; }
  .price-card li { padding:8px 0; border-bottom:1px solid #222; color:#d8d8d8; font-size:16px; }
  .price-card li:before { content:"✓ "; color:#d4af37; font-weight:800; }
  .price-card .ribbon { position:absolute; top:-14px; left:50%; transform:translateX(-50%);
                background:linear-gradient(180deg,#e8c95a,#c9a227); color:#111; font-weight:800;
                font-size:13px; padding:5px 16px; border-radius:20px; letter-spacing:1px; white-space:nowrap; }
  /* Admin stat cards */
  .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:16px; margin-bottom:26px; }
  .stat { background:linear-gradient(180deg,#171717,#101010); border:1px solid #2b2b2b; border-radius:14px;
          padding:22px; text-align:center; }
  .stat .num { font-size:38px; font-weight:800; color:#d4af37; line-height:1.1; }
  .stat .lbl { color:#9a9a9a; font-size:14px; text-transform:uppercase; letter-spacing:1px; margin-top:4px; }
  .keybox { background:#000; border:1px solid #d4af37; border-radius:10px; padding:16px 18px;
            font-family:Consolas, monospace; font-size:16.5px; line-height:1.9; color:#4ade80;
            white-space:pre-wrap; word-wrap:break-word; }
  code.key { font-family:Consolas, monospace; background:#000; padding:2px 8px; border-radius:6px; color:#4ade80; }
  @media (max-width:640px) { .hero h1 { font-size:36px; } .hero { padding:56px 16px 40px; } }
</style>
"""

FOOTER = """<footer><b>MR X BOT HOSTING</b> — One panel. Unlimited bots.<br>
<span style="font-size:12.5px">Run your Telegram bots 24/7 from one simple panel.</span></footer>"""


LANDING_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MR X Bot Hosting — One Panel. Unlimited Bots.</title>""" + CSS + """</head><body>
<header><div class="logo">MR X BOT HOSTING</div>
<nav><a class="ghost" href="{{ url_for('login') }}">Login</a>
<a href="{{ url_for('register') }}">Register Free</a></nav></header>
<main>
<div class="hero">
  <div class="kicker">Telegram Bot Hosting</div>
  <h1>MR X Bot Hosting<br><span class="gold">One panel. Unlimited bots.</span></h1>
  <p class="sub">Upload your bot file, paste the token, press Start. Your Telegram
  bots run 24/7 from one clean, powerful panel — no server skills needed.</p>
  <div class="cta">
    <a class="btn" href="{{ url_for('register') }}">Start Free — 2 Bots</a>
    <a class="btn ghost" href="{{ url_for('login') }}">Login to Panel</a>
  </div>
  <p class="muted" style="margin-top:18px">Free forever plan · No card needed · Premium unlocks unlimited bots</p>
</div>

<div class="section">
  <h2>Why MR X Bot Hosting?</h2>
  <div class="grid">
    <div class="feature"><div class="icon">🚀</div><h3>One-Click Start</h3>
      <p>Add a bot in under a minute: name, .py file, token. Start, stop and restart with one tap.</p></div>
    <div class="feature"><div class="icon">📜</div><h3>Live Logs</h3>
      <p>Watch exactly what your bot is doing. Errors show in the log with the real error line.</p></div>
    <div class="feature"><div class="icon">🔁</div><h3>Auto-Restart</h3>
      <p>Bot crashed at 3 AM? Turn on auto-restart and the panel brings it back on its own.</p></div>
    <div class="feature"><div class="icon">🔐</div><h3>Tokens Stay Private</h3>
      <p>Your bot token is stored on the server only and never shown again — just the last 4 digits.</p></div>
    <div class="feature"><div class="icon">📱</div><h3>Works on Your Phone</h3>
      <p>The whole panel is big, clean and mobile-friendly. Manage bots from anywhere.</p></div>
    <div class="feature"><div class="icon">♾️</div><h3>Scales With You</h3>
      <p>Start free with 2 bots. Redeem a Premium key and host unlimited bots in the same panel.</p></div>
  </div>
</div>

<div class="section">
  <h2>Simple Pricing</h2>
  <div class="pricing">
    <div class="price-card">
      <div class="planname">Free</div>
      <div class="price">Rs. 0</div>
      <div class="muted">forever</div>
      <ul>
        <li>Up to 2 bots</li>
        <li>Start / stop / restart</li>
        <li>Live logs</li>
        <li>Auto-restart option</li>
      </ul>
      <a class="btn grey" style="background:#2c2c2c;color:#eee;border:1px solid #3d3d3d"
         href="{{ url_for('register') }}">Register Free</a>
    </div>
    <div class="price-card premium">
      <div class="planname">Premium</div>
      <div class="price">Rs. 50</div>
      <div class="muted">for 2 days · with a license key</div>
      <ul>
        <li><b>Unlimited</b> bots for 2 days</li>
        <li>Start / stop / restart</li>
        <li>Live logs</li>
        <li>Auto-restart option</li>
      </ul>
      <a class="btn" target="_blank" rel="noopener" href="https://wa.me/923044507018?text=Assalam-o-Alaikum!%20I%20want%20to%20buy%20a%20Premium%20key%20(2%20Days%20-%20Rs.%2050)%20for%20MR%20X%20Bot%20Hosting.">Get a Key</a>
    </div>
    <div class="price-card premium">
      <div class="planname">Premium</div>
      <div class="price">Rs. 150</div>
      <div class="muted">for 7 days · with a license key</div>
      <ul>
        <li><b>Unlimited</b> bots for 7 days</li>
        <li>Start / stop / restart</li>
        <li>Live logs</li>
        <li>Auto-restart option</li>
      </ul>
      <a class="btn" target="_blank" rel="noopener" href="https://wa.me/923044507018?text=Assalam-o-Alaikum!%20I%20want%20to%20buy%20a%20Premium%20key%20(7%20Days%20-%20Rs.%20150)%20for%20MR%20X%20Bot%20Hosting.">Get a Key</a>
    </div>
    <div class="price-card premium">
      <div class="ribbon">MOST POPULAR</div>
      <div class="planname">Premium</div>
      <div class="price">Rs. 400</div>
      <div class="muted">for 30 days · with a license key</div>
      <ul>
        <li><b>Unlimited</b> bots for 30 days</li>
        <li>Start / stop / restart</li>
        <li>Live logs</li>
        <li>Auto-restart option</li>
        <li>Best value for serious sellers</li>
      </ul>
      <a class="btn" target="_blank" rel="noopener" href="https://wa.me/923044507018?text=Assalam-o-Alaikum!%20I%20want%20to%20buy%20a%20Premium%20key%20(30%20Days%20-%20Rs.%20400)%20for%20MR%20X%20Bot%20Hosting.">Get a Key</a>
    </div>
  </div>
  <p class="muted" style="text-align:center;margin-top:22px">
  Premium keys are sold by the owner directly. Already bought a key? Register, log in,
  and open <b>Redeem Key</b> in your panel — Premium activates instantly.</p>
</div>
</main>
""" + FOOTER + """</body></html>"""

LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MR X Bot Hosting — Login</title>""" + CSS + """</head><body>
<main><div class="wrap center"><div class="card">
<h2 style="text-align:center">MR X BOT HOSTING</h2>
{% if flash %}<div class="{{ 'okmsg' if flash.kind == 'ok' else 'err' }}">{{ flash.msg }}</div>{% endif %}
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<form method="post">
  <label>Username</label>
  <input type="text" name="username" required autofocus autocomplete="username">
  <label>Password</label>
  <input type="password" name="password" required autocomplete="current-password">
  <button type="submit" style="width:100%; padding:13px">Login</button>
</form>
<p class="tagline">No account yet? <a href="{{ url_for('register') }}" style="color:#d4af37">Register free</a></p>
<p class="muted" style="text-align:center">Admin? Log in with username <b>admin</b> and the panel password.</p>
</div></div></main>
""" + FOOTER + """</body></html>"""

REGISTER_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MR X Bot Hosting — Register</title>""" + CSS + """</head><body>
<main><div class="wrap center"><div class="card">
<h2 style="text-align:center">Create Your Account</h2>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<form method="post">
  <label>Your name</label>
  <input type="text" name="name" value="{{ name|e }}" required>
  <label>Username (for login)</label>
  <input type="text" name="username" value="{{ username|e }}" required autocomplete="username">
  <label>Email (optional)</label>
  <input type="email" name="email" value="{{ email|e }}" autocomplete="email">
  <label>Password (min 6 characters)</label>
  <input type="password" name="password" required autocomplete="new-password">
  <button type="submit" style="width:100%; padding:13px">Register — Free Plan</button>
</form>
<p class="tagline">Already registered? <a href="{{ url_for('login') }}" style="color:#d4af37">Login</a></p>
<p class="muted" style="text-align:center">Free plan includes 2 bots. Premium = unlimited, with a license key.</p>
</div></div></main>
""" + FOOTER + """</body></html>"""

REDEEM_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Redeem Key — MR X Bot Hosting</title>""" + CSS + """</head><body>
<header><div class="logo">MR X BOT HOSTING</div>
<nav><a class="ghost" href="{{ url_for('dashboard') }}">← Dashboard</a>
<a href="{{ url_for('logout') }}">Logout</a></nav></header>
<main><div class="wrap"><div class="card" style="max-width:560px;margin:0 auto">
<h2>🎟️ Redeem Premium Key</h2>
{% if flash %}<div class="{{ 'okmsg' if flash.kind == 'ok' else 'err' }}">{{ flash.msg }}</div>{% endif %}
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<p class="muted" style="font-size:15px">Your current plan:
<span class="badge {{ 'plan-premium' if plan == 'Premium' else 'plan-free' }}">{{ plan }}</span>
{% if plan == 'Premium' %} — active until <b>{{ expiry }}</b>{% endif %}</p>
<form method="post">
  <label>License key</label>
  <input type="text" name="key" placeholder="MRX-XXXXXX-XXXXXX-XXXXXX" required
         style="font-family:Consolas,monospace; text-transform:uppercase">
  <button type="submit" style="width:100%; padding:13px">Activate Premium</button>
</form>
<p class="muted">Format: MRX-XXXXXX-XXXXXX-XXXXXX. If you are already Premium,
a new key extends your Premium from your current expiry date.</p>
</div></div></main>
""" + FOOTER + """</body></html>"""

USER_HEADER = """<header><div class="logo">MR X BOT HOSTING</div>
<nav>
<a class="ghost" href="{{ url_for('redeem') }}">🎟️ Redeem Key</a>
<a href="{{ url_for('logout') }}">Logout</a>
</nav></header>"""

DASH_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MR X Bot Hosting — Dashboard</title>""" + CSS + """</head><body>
""" + USER_HEADER + """
<main><div class="wrap">

<div class="card" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px">
  <div>
    <h2 style="margin:0 0 6px">👋 {{ user.name|e }}</h2>
    <span class="muted">@{{ user.username|e }} · Bots used: <b style="color:#eee">{{ bots|length }} / {{ limit_label }}</b>
    {% if plan == 'Premium' %} · Premium until <b style="color:#eee">{{ expiry }}</b>{% endif %}</span>
  </div>
  <span class="badge {{ 'plan-premium' if plan == 'Premium' else 'plan-free' }}" style="font-size:16px;padding:8px 20px">
    {{ plan|upper }} PLAN</span>
</div>

{% if flash %}<div class="{{ 'okmsg' if flash.kind == 'ok' else 'err' }}">{{ flash.msg }}</div>{% endif %}

<div class="card">
<h2>💎 Get Premium — Buy a Key</h2>
<p class="muted" style="margin-top:0">Premium = <b style="color:#eee">Unlimited bots</b>. Pick a plan and tap
<b style="color:#eee">Get a Key</b> — you will be taken straight to our WhatsApp to buy your key.</p>
<div style="display:flex;gap:14px;flex-wrap:wrap">
  <div style="flex:1;min-width:180px;background:#101010;border:1.5px solid #d4af37;border-radius:12px;padding:18px;text-align:center">
    <div style="font-weight:800;letter-spacing:1.5px;color:#d4af37">PREMIUM · 2 DAYS</div>
    <div style="font-size:32px;font-weight:800;margin:8px 0">Rs. 50</div>
    <a class="btn" target="_blank" rel="noopener"
       href="https://wa.me/923044507018?text={{ ('Assalam-o-Alaikum! I want to buy a Premium key (2 Days - Rs. 50) for MR X Bot Hosting. My username is: ' ~ user.username)|urlencode }}">Get a Key</a>
  </div>
  <div style="flex:1;min-width:180px;background:#101010;border:1.5px solid #d4af37;border-radius:12px;padding:18px;text-align:center">
    <div style="font-weight:800;letter-spacing:1.5px;color:#d4af37">PREMIUM · 7 DAYS</div>
    <div style="font-size:32px;font-weight:800;margin:8px 0">Rs. 150</div>
    <a class="btn" target="_blank" rel="noopener"
       href="https://wa.me/923044507018?text={{ ('Assalam-o-Alaikum! I want to buy a Premium key (7 Days - Rs. 150) for MR X Bot Hosting. My username is: ' ~ user.username)|urlencode }}">Get a Key</a>
  </div>
  <div style="flex:1;min-width:180px;background:#101010;border:1.5px solid #d4af37;border-radius:12px;padding:18px;text-align:center">
    <div style="font-weight:800;letter-spacing:1.5px;color:#d4af37">PREMIUM · 30 DAYS</div>
    <div style="font-size:32px;font-weight:800;margin:8px 0">Rs. 400</div>
    <a class="btn" target="_blank" rel="noopener"
       href="https://wa.me/923044507018?text={{ ('Assalam-o-Alaikum! I want to buy a Premium key (30 Days - Rs. 400) for MR X Bot Hosting. My username is: ' ~ user.username)|urlencode }}">Get a Key</a>
  </div>
</div>
<p class="muted" style="margin-bottom:0">Already bought a key? Open <b>Redeem Key</b> in the menu and enter it — Premium activates instantly.</p>
</div>

<div class="card">
<h2>My Bots ({{ bots|length }})</h2>
{% if bots %}
<div class="tablewrap">
<table>
<tr><th>Name</th><th>Status</th><th>Uptime</th><th>Token</th><th>Auto-restart</th><th>Actions</th></tr>
{% for b in bots %}
<tr>
  <td><b>{{ b.name|e }}</b><br><span class="muted">{{ b.file|e }}</span></td>
  <td><span class="badge {{ b.status }}">{{ b.status }}</span></td>
  <td>{{ b.uptime }}</td>
  <td class="muted">{{ b.token_masked }}</td>
  <td>
    <form class="inline" method="post" action="{{ url_for('toggle_autorestart', bot_id=b.id) }}">
      <button class="grey" type="submit">{{ 'ON' if b.auto_restart else 'OFF' }}</button>
    </form>
  </td>
  <td>
    {% if b.status == 'running' %}
    <form class="inline" method="post" action="{{ url_for('stop', bot_id=b.id) }}"><button class="grey">Stop</button></form>
    <form class="inline" method="post" action="{{ url_for('restart', bot_id=b.id) }}"><button>Restart</button></form>
    {% else %}
    <form class="inline" method="post" action="{{ url_for('start', bot_id=b.id) }}"><button>Start</button></form>
    {% endif %}
    <a class="btn grey" href="{{ url_for('logs', bot_id=b.id) }}">Logs</a>
    <form class="inline" method="post" action="{{ url_for('delete', bot_id=b.id) }}"
          onsubmit="return confirm('Delete this bot and its files?')">
      <button class="red">Delete</button></form>
  </td>
</tr>
{% endfor %}
</table>
</div>
{% else %}
<p class="muted">No bots yet. Add your first bot below.</p>
{% endif %}
</div>

<div class="card">
<h2>Add a Bot</h2>
{% if at_limit %}
<div class="err">{{ limit_msg }}</div>
{% endif %}
<form id="addform" method="post" action="{{ url_for('add_bot') }}" enctype="multipart/form-data">
  <label>Bot name</label>
  <input type="text" name="name" placeholder="e.g. My Echo Bot" required>
  <label>Bot file (.py)</label><br>
  <input type="file" name="botfile" accept=".py" required><br>
  <label>Bot token (from @BotFather — stored on this server only, never shown again)</label>
  <input type="text" name="token" placeholder="123456:ABC... (optional if the token is inside your .py file)">
  <label><input type="checkbox" name="auto_restart"> Auto-restart if the bot crashes</label>
  <br><br>
  <button type="submit" style="padding:13px 30px;font-size:16.5px" {% if at_limit %}disabled{% endif %}>Add Bot</button>
</form>
<p class="muted">Tip: your bot can read its token from the environment variable <b>BOT_TOKEN</b>.
Keep each bot's token separate — one token per bot, pasted only here.</p>
</div>

</div></main>
""" + FOOTER + """
<script>
// Refresh the bot list every 15s, but NEVER while the Add Bot form is in use
// (typing, a chosen file, or focus inside the form all pause the refresh).
setInterval(function(){
  var f = document.getElementById('addform');
  if (!f) { location.reload(); return; }
  var name = f.querySelector('input[name="name"]');
  var token = f.querySelector('input[name="token"]');
  var file = f.querySelector('input[name="botfile"]');
  var busy = (name && name.value) || (token && token.value) ||
             (file && file.files && file.files.length) ||
             f.contains(document.activeElement);
  if (!busy) location.reload();
}, 15000);
</script>
</body></html>"""

LOG_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="5">
<title>Logs — {{ name|e }}</title>""" + CSS + """</head><body>
<header><div class="logo">LOGS — {{ name|e }}</div>
<nav><a href="{{ back_url }}">{{ back_label }}</a></nav></header>
<main><div class="wrap"><pre class="log">{{ log_text|e }}</pre>
<p class="muted">Auto-refreshes every 5 seconds. Showing the last 300 lines.</p></div></main>
""" + FOOTER + """</body></html>"""


ADMIN_NAV = """<header><div class="logo">MR X — ADMIN</div>
<nav>
<a class="ghost" href="{{ url_for('admin_home') }}">📊 Stats</a>
<a class="ghost" href="{{ url_for('admin_users') }}">👥 Users</a>
<a class="ghost" href="{{ url_for('admin_keys') }}">🎟️ Keys</a>
<a class="ghost" href="{{ url_for('admin_bots') }}">🤖 All Bots</a>
<a href="{{ url_for('logout') }}">Logout</a>
</nav></header>"""

ADMIN_HOME_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin — MR X Bot Hosting</title>""" + CSS + """</head><body>
""" + ADMIN_NAV + """
<main><div class="wrap">
<h2 style="font-size:26px">Admin Panel</h2>
{% if flash %}<div class="{{ 'okmsg' if flash.kind == 'ok' else 'err' }}">{{ flash.msg }}</div>{% endif %}
<div class="stats">
  <div class="stat"><div class="num">{{ stats.users }}</div><div class="lbl">Total Users</div></div>
  <div class="stat"><div class="num">{{ stats.bots }}</div><div class="lbl">Total Bots</div></div>
  <div class="stat"><div class="num">{{ stats.running }}</div><div class="lbl">Running Bots</div></div>
  <div class="stat"><div class="num">{{ stats.keys_unused }}</div><div class="lbl">Keys Unused</div></div>
  <div class="stat"><div class="num">{{ stats.keys_used }}</div><div class="lbl">Keys Used</div></div>
  <div class="stat"><div class="num">{{ stats.premium_users }}</div><div class="lbl">Premium Users</div></div>
</div>
<div class="card">
<h2>Quick Actions</h2>
<a class="btn" href="{{ url_for('admin_keys') }}">🎟️ Generate License Keys</a>
<a class="btn grey" href="{{ url_for('admin_users') }}">👥 Manage Users</a>
<a class="btn grey" href="{{ url_for('admin_bots') }}">🤖 Manage All Bots</a>
<p class="muted" style="margin-top:16px">Sell a key → the buyer registers on the landing page →
redeems the key in their panel → Premium activates instantly for the key's duration.</p>
</div>
</div></main>
""" + FOOTER + """</body></html>"""

ADMIN_USERS_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin Users — MR X Bot Hosting</title>""" + CSS + """</head><body>
""" + ADMIN_NAV + """
<main><div class="wrap">
<h2 style="font-size:26px">👥 Users ({{ users|length }})</h2>
{% if flash %}<div class="{{ 'okmsg' if flash.kind == 'ok' else 'err' }}">{{ flash.msg }}</div>{% endif %}
<div class="card">
{% if users %}
<div class="tablewrap">
<table>
<tr><th>User</th><th>Plan</th><th>Premium Until</th><th>Bots</th><th>Joined</th><th>Status</th><th>Actions</th></tr>
{% for u in users %}
<tr>
  <td><b>{{ u.name|e }}</b><br><span class="muted">@{{ u.username|e }}{% if u.email %} · {{ u.email|e }}{% endif %}</span></td>
  <td><span class="badge {{ 'plan-premium' if u.plan == 'Premium' else 'plan-free' }}">{{ u.plan }}</span></td>
  <td>{{ u.expiry }}</td>
  <td>{{ u.bot_count }}</td>
  <td>{{ u.joined }}</td>
  <td>{% if u.banned %}<span class="badge banned">banned</span>{% else %}<span class="badge running">active</span>{% endif %}</td>
  <td>
    <form class="inline" method="post" action="{{ url_for('admin_user_grant', user_id=u.id) }}">
      <input type="number" name="days" value="30" min="1" max="3650"
             style="width:80px;display:inline-block;padding:8px;margin:0">
      <button type="submit">+ Days Premium</button>
    </form>
    {% if u.banned %}
    <form class="inline" method="post" action="{{ url_for('admin_user_unban', user_id=u.id) }}">
      <button class="grey" type="submit">Unban</button></form>
    {% else %}
    <form class="inline" method="post" action="{{ url_for('admin_user_ban', user_id=u.id) }}">
      <button class="grey" type="submit">Ban</button></form>
    {% endif %}
    <form class="inline" method="post" action="{{ url_for('admin_user_delete', user_id=u.id) }}"
          onsubmit="return confirm('Delete this user AND all their bots? This cannot be undone.')">
      <button class="red" type="submit">Delete</button></form>
  </td>
</tr>
{% endfor %}
</table>
</div>
{% else %}
<p class="muted">No registered users yet.</p>
{% endif %}
</div>
</div></main>
""" + FOOTER + """</body></html>"""

ADMIN_KEYS_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin Keys — MR X Bot Hosting</title>""" + CSS + """</head><body>
""" + ADMIN_NAV + """
<main><div class="wrap">
<h2 style="font-size:26px">🎟️ License Keys</h2>
{% if flash %}<div class="{{ 'okmsg' if flash.kind == 'ok' else 'err' }}">{{ flash.msg }}</div>{% endif %}

{% if new_keys %}
<div class="card">
<h2>✅ New Keys — copy them now</h2>
<p class="muted" style="font-size:15px">These keys were just generated. Send each key to one buyer.
They stay listed in the table below as <b>unused</b> until redeemed.</p>
<div class="keybox">{% for k in new_keys %}{{ k }}
{% endfor %}</div>
</div>
{% endif %}

<div class="card">
<h2>Generate Keys</h2>
<form method="post" action="{{ url_for('admin_keys_generate') }}">
  <label>Premium plan duration</label>
  <select name="duration_days">
    <option value="2">Premium — 2 Days</option>
    <option value="7">Premium — 7 Days</option>
    <option value="30" selected>Premium — 30 Days</option>
  </select>
  <label>Custom duration in days (optional — overrides the choice above)</label>
  <input type="number" name="custom_days" min="1" max="3650" placeholder="Leave empty to use the plan above">
  <label>How many keys? (1–20)</label>
  <input type="number" name="quantity" value="1" min="1" max="20" required>
  <button type="submit" style="padding:13px 30px;font-size:16.5px">Generate Keys</button>
</form>
</div>

<div class="card">
<h2>All Keys ({{ keys|length }})</h2>
{% if keys %}
<div class="tablewrap">
<table>
<tr><th>Key</th><th>Duration</th><th>Status</th><th>Used By</th><th>Created</th><th>Action</th></tr>
{% for k in keys %}
<tr>
  <td><code class="key">{{ k.code }}</code></td>
  <td>{{ k.duration_days }} days</td>
  <td><span class="badge key-{{ k.status }}">{{ k.status }}</span></td>
  <td>{% if k.used_by_name %}@{{ k.used_by_name|e }}<br><span class="muted">{{ k.used_at }}</span>{% else %}—{% endif %}</td>
  <td>{{ k.created }}</td>
  <td>
    {% if k.status == 'unused' %}
    <form class="inline" method="post" action="{{ url_for('admin_key_revoke', code=k.code) }}"
          onsubmit="return confirm('Revoke this key? It can no longer be redeemed.')">
      <button class="red" type="submit">Revoke</button></form>
    {% else %}—{% endif %}
  </td>
</tr>
{% endfor %}
</table>
</div>
{% else %}
<p class="muted">No keys yet. Generate your first key above.</p>
{% endif %}
</div>
</div></main>
""" + FOOTER + """</body></html>"""

ADMIN_BOTS_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin Bots — MR X Bot Hosting</title>""" + CSS + """</head><body>
""" + ADMIN_NAV + """
<main><div class="wrap">
<h2 style="font-size:26px">🤖 All Bots ({{ bots|length }})</h2>
{% if flash %}<div class="{{ 'okmsg' if flash.kind == 'ok' else 'err' }}">{{ flash.msg }}</div>{% endif %}
<div class="card">
{% if bots %}
<div class="tablewrap">
<table>
<tr><th>Bot</th><th>Owner</th><th>Status</th><th>Uptime</th><th>Token</th><th>Auto-restart</th><th>Actions</th></tr>
{% for b in bots %}
<tr>
  <td><b>{{ b.name|e }}</b><br><span class="muted">{{ b.file|e }}</span></td>
  <td>{{ b.owner_name|e }}</td>
  <td><span class="badge {{ b.status }}">{{ b.status }}</span></td>
  <td>{{ b.uptime }}</td>
  <td class="muted">{{ b.token_masked }}</td>
  <td>
    <form class="inline" method="post" action="{{ url_for('toggle_autorestart', bot_id=b.id) }}">
      <button class="grey" type="submit">{{ 'ON' if b.auto_restart else 'OFF' }}</button>
    </form>
  </td>
  <td>
    {% if b.status == 'running' %}
    <form class="inline" method="post" action="{{ url_for('stop', bot_id=b.id) }}"><button class="grey">Stop</button></form>
    <form class="inline" method="post" action="{{ url_for('restart', bot_id=b.id) }}"><button>Restart</button></form>
    {% else %}
    <form class="inline" method="post" action="{{ url_for('start', bot_id=b.id) }}"><button>Start</button></form>
    {% endif %}
    <a class="btn grey" href="{{ url_for('logs', bot_id=b.id) }}">Logs</a>
    <form class="inline" method="post" action="{{ url_for('delete', bot_id=b.id) }}"
          onsubmit="return confirm('Delete this bot and its files?')">
      <button class="red">Delete</button></form>
  </td>
</tr>
{% endfor %}
</table>
</div>
{% else %}
<p class="muted">No bots on the server yet.</p>
{% endif %}
</div>

<div class="card">
<h2>Add a Bot (Admin's own)</h2>
<form id="addform" method="post" action="{{ url_for('add_bot') }}" enctype="multipart/form-data">
  <label>Bot name</label>
  <input type="text" name="name" placeholder="e.g. My Echo Bot" required>
  <label>Bot file (.py)</label><br>
  <input type="file" name="botfile" accept=".py" required><br>
  <label>Bot token (from @BotFather — stored on this server only, never shown again)</label>
  <input type="text" name="token" placeholder="123456:ABC... (optional if the token is inside your .py file)">
  <label><input type="checkbox" name="auto_restart"> Auto-restart if the bot crashes</label>
  <br><br>
  <button type="submit" style="padding:13px 30px;font-size:16.5px">Add Bot</button>
</form>
<p class="muted">Bots added here belong to the admin (they are not tied to any user account and have no plan limit).</p>
</div>
</div></main>
""" + FOOTER + """</body></html>"""


# --------------------------------------------------------------------------
# Routes — public
# --------------------------------------------------------------------------
@app.route("/")
def home():
    if is_admin():
        return redirect(url_for("admin_home"))
    if current_user() is not None:
        return redirect(url_for("dashboard"))
    return render_template_string(LANDING_HTML)


@app.route("/register", methods=["GET", "POST"])
def register():
    if is_admin():
        return redirect(url_for("admin_home"))
    if current_user() is not None:
        return redirect(url_for("dashboard"))
    error = None
    name = username = email = ""
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        username = (request.form.get("username") or "").strip().lower()
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        if not name or not username:
            error = "Name and username are required."
        elif len(username) < 3 or len(username) > 30 or \
                not all(c.isalnum() or c in "_." for c in username):
            error = "Username must be 3–30 characters: letters, numbers, _ or . only."
        elif username == "admin":
            error = "That username is reserved. Pick another one."
        elif len(password) < 6:
            error = "Password must be at least 6 characters."
        elif find_user_by_username(username) is not None:
            error = "That username is already taken. Try another."
        else:
            with _store_lock:
                data = load_users()
                user = {
                    "id": secrets.token_hex(6),
                    "name": name,
                    "username": username,
                    "email": email,
                    "password_hash": generate_password_hash(password),
                    "premium_until": 0,
                    "banned": False,
                    "created": int(time.time()),
                }
                data["users"].append(user)
                save_users(data)
            session.clear()
            session["user_id"] = user["id"]
            flash("Welcome to MR X Bot Hosting! Your Free plan is active — add up to 2 bots.")
            return redirect(url_for("dashboard"))
    return render_template_string(REGISTER_HTML, error=error,
                                  name=name, username=username, email=email)


@app.route("/login", methods=["GET", "POST"])
def login():
    if is_admin():
        return redirect(url_for("admin_home"))
    if current_user() is not None:
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        if username == "admin":
            if secrets.compare_digest(password, PANEL_PASSWORD):
                session.clear()
                session["is_admin"] = True
                return redirect(url_for("admin_home"))
            error = "Wrong admin password. Try again."
        else:
            user = find_user_by_username(username)
            if user is None or not check_password_hash(user["password_hash"], password):
                error = "Wrong username or password. Try again."
            elif user.get("banned"):
                error = "This account has been banned. Please contact the owner."
            else:
                session.clear()
                session["user_id"] = user["id"]
                return redirect(url_for("dashboard"))
    return render_template_string(LOGIN_HTML, error=error, flash=pop_flash())


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


# --------------------------------------------------------------------------
# Routes — user dashboard & redeem
# --------------------------------------------------------------------------
def _dash_context(user):
    bots = []
    for bot in user_bots(user["id"]):
        b = dict(bot)
        b["status"] = bot_status(bot)
        b["uptime"] = bot_uptime(bot)
        bots.append(b)
    limit = user_bot_limit(user)
    at_limit = len(bots) >= limit
    limit_msg = ""
    if at_limit and not user_is_premium(user):
        limit_msg = ("Free plan limit reached (%d bots) — redeem a Premium key "
                     "for unlimited bots." % FREE_BOT_LIMIT)
    elif at_limit:
        limit_msg = "Bot limit reached (%d bots)." % limit
    return {
        "user": user,
        "bots": bots,
        "plan": user_plan_name(user),
        "expiry": fmt_date(user.get("premium_until")),
        "limit_label": "Unlimited" if user_is_premium(user) else str(FREE_BOT_LIMIT),
        "at_limit": at_limit,
        "limit_msg": limit_msg,
        "flash": pop_flash(),
    }


@app.route("/dashboard")
@user_required
def dashboard():
    return render_template_string(DASH_HTML, **_dash_context(current_user()))


@app.route("/redeem", methods=["GET", "POST"])
@user_required
def redeem():
    user = current_user()
    error = None
    if request.method == "POST":
        code = (request.form.get("key") or "").strip().upper()
        with _store_lock:
            data = load_keys()
            rec = None
            for k in data["keys"]:
                if k["code"] == code:
                    rec = k
                    break
            if rec is None:
                error = "Invalid key. Check the key and try again."
            elif rec["status"] == "used":
                error = "This key has already been used."
            elif rec["status"] == "revoked":
                error = "This key has been revoked. Please contact the seller."
            else:
                now = int(time.time())
                base = max(now, int(user.get("premium_until") or 0))
                new_expiry = base + int(rec["duration_days"]) * 86400
                users = load_users()
                for u in users["users"]:
                    if u["id"] == user["id"]:
                        u["premium_until"] = new_expiry
                save_users(users)
                rec["status"] = "used"
                rec["used_by"] = user["id"]
                rec["used_by_name"] = user["username"]
                rec["used_at"] = now
                save_keys(data)
                flash("🎉 Premium activated! Your Premium is now active until %s."
                      % fmt_date(new_expiry))
                return redirect(url_for("dashboard"))
    user = current_user()
    return render_template_string(
        REDEEM_HTML, error=error, flash=pop_flash(),
        plan=user_plan_name(user), expiry=fmt_date(user.get("premium_until")))


# --------------------------------------------------------------------------
# Routes — bots (user: own bots only · admin: all bots)
# --------------------------------------------------------------------------
@app.route("/add", methods=["POST"])
@login_required
def add_bot():
    if is_admin():
        owner = None
        limit = None
    else:
        user = current_user()
        owner = user["id"]
        limit = user_bot_limit(user)
        if len(user_bots(owner)) >= limit:
            if user_is_premium(user):
                flash("Bot limit reached (%d bots)." % limit, "err")
            else:
                flash("Free plan limit reached — redeem a Premium key for "
                      "unlimited bots.", "err")
            return redirect(url_for("dashboard"))
    name = (request.form.get("name") or "").strip()
    token = (request.form.get("token") or "").strip()
    auto_restart = bool(request.form.get("auto_restart"))
    upload = request.files.get("botfile")
    if not name or upload is None or not upload.filename:
        abort(400, "Bot name and a .py file are required.")
    filename = secure_filename(upload.filename)
    if not filename.lower().endswith(".py"):
        abort(400, "Only .py files are allowed.")
    bot_id = "%s-%s" % (secrets.token_hex(3), int(time.time()))
    os.makedirs(bot_dir(bot_id), exist_ok=True)
    upload.save(os.path.join(bot_dir(bot_id), filename))
    if token:
        with open(os.path.join(bot_dir(bot_id), "config.env"), "w") as f:
            f.write("BOT_TOKEN=%s\n" % token)
    masked = ("••••" + token[-4:]) if len(token) >= 4 else ("••••" if token else "—")
    with _db_lock:
        db = load_db()
        db["bots"].append({
            "id": bot_id,
            "owner": owner,
            "name": name,
            "file": filename,
            "token_masked": masked,
            "auto_restart": auto_restart,
            "desired_running": False,
            "created": int(time.time()),
        })
        save_db(db)
    _log_line(bot_id, "[panel] bot '%s' added (%s)" % (name, filename))
    flash("Bot '%s' added. Press Start to run it." % name)
    return _after_action()


@app.route("/bot/<bot_id>/start", methods=["POST"])
@login_required
def start(bot_id):
    bot = get_bot(bot_id)
    if not can_access_bot(bot):
        abort(404)
    start_bot(bot_id)
    return _after_action()


@app.route("/bot/<bot_id>/stop", methods=["POST"])
@login_required
def stop(bot_id):
    bot = get_bot(bot_id)
    if not can_access_bot(bot):
        abort(404)
    stop_bot(bot_id)
    return _after_action()


@app.route("/bot/<bot_id>/restart", methods=["POST"])
@login_required
def restart(bot_id):
    bot = get_bot(bot_id)
    if not can_access_bot(bot):
        abort(404)
    stop_bot(bot_id, mark_stopped=False)
    start_bot(bot_id)
    return _after_action()


@app.route("/bot/<bot_id>/delete", methods=["POST"])
@login_required
def delete(bot_id):
    bot = get_bot(bot_id)
    if not can_access_bot(bot):
        abort(404)
    delete_bot_everywhere(bot_id)
    flash("Bot deleted.")
    return _after_action()


@app.route("/bot/<bot_id>/autorestart", methods=["POST"])
@login_required
def toggle_autorestart(bot_id):
    bot = get_bot(bot_id)
    if not can_access_bot(bot):
        abort(404)
    update_bot(bot_id, auto_restart=not bot.get("auto_restart"))
    return _after_action()


@app.route("/bot/<bot_id>/logs")
@login_required
def logs(bot_id):
    bot = get_bot(bot_id)
    if not can_access_bot(bot):
        abort(404)
    path = bot_log_path(bot_id)
    text = ""
    if os.path.exists(path):
        with open(path, errors="replace") as f:
            text = "".join(f.readlines()[-300:])
    if not text:
        text = "(no logs yet — start the bot first)"
    if is_admin():
        back_url, back_label = url_for("admin_bots"), "← All Bots"
    else:
        back_url, back_label = url_for("dashboard"), "← Dashboard"
    return render_template_string(LOG_HTML, name=bot["name"], log_text=text,
                                  back_url=back_url, back_label=back_label)


# --------------------------------------------------------------------------
# Routes — admin
# --------------------------------------------------------------------------
@app.route("/admin")
@admin_required
def admin_home():
    users = load_users()["users"]
    bots = load_db()["bots"]
    keys = load_keys()["keys"]
    stats = {
        "users": len(users),
        "bots": len(bots),
        "running": sum(1 for b in bots if bot_status(b) == "running"),
        "keys_unused": sum(1 for k in keys if k["status"] == "unused"),
        "keys_used": sum(1 for k in keys if k["status"] == "used"),
        "premium_users": sum(1 for u in users if user_is_premium(u)),
    }
    return render_template_string(ADMIN_HOME_HTML, stats=stats, flash=pop_flash())


@app.route("/admin/users")
@admin_required
def admin_users():
    users = []
    counts = {}
    for b in load_db()["bots"]:
        counts[b.get("owner")] = counts.get(b.get("owner"), 0) + 1
    for u in load_users()["users"]:
        row = dict(u)
        row["plan"] = user_plan_name(u)
        row["expiry"] = fmt_date(u.get("premium_until")) if user_is_premium(u) else "—"
        row["bot_count"] = counts.get(u["id"], 0)
        row["joined"] = fmt_date(u.get("created"))
        users.append(row)
    users.sort(key=lambda x: x.get("created") or 0, reverse=True)
    return render_template_string(ADMIN_USERS_HTML, users=users, flash=pop_flash())


@app.route("/admin/users/<user_id>/ban", methods=["POST"])
@admin_required
def admin_user_ban(user_id):
    with _store_lock:
        data = load_users()
        for u in data["users"]:
            if u["id"] == user_id:
                u["banned"] = True
                flash("User @%s has been banned." % u["username"])
        save_users(data)
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<user_id>/unban", methods=["POST"])
@admin_required
def admin_user_unban(user_id):
    with _store_lock:
        data = load_users()
        for u in data["users"]:
            if u["id"] == user_id:
                u["banned"] = False
                flash("User @%s has been unbanned." % u["username"])
        save_users(data)
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<user_id>/grant", methods=["POST"])
@admin_required
def admin_user_grant(user_id):
    try:
        days = int(request.form.get("days") or 0)
    except ValueError:
        days = 0
    if days < 1 or days > 3650:
        flash("Enter a valid number of days (1–3650).", "err")
        return redirect(url_for("admin_users"))
    with _store_lock:
        data = load_users()
        for u in data["users"]:
            if u["id"] == user_id:
                base = max(int(time.time()), int(u.get("premium_until") or 0))
                u["premium_until"] = base + days * 86400
                flash("Granted %d days of Premium to @%s (until %s)."
                      % (days, u["username"], fmt_date(u["premium_until"])))
        save_users(data)
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<user_id>/delete", methods=["POST"])
@admin_required
def admin_user_delete(user_id):
    user = get_user(user_id)
    if user is None:
        abort(404)
    for bot in list(user_bots(user_id)):
        delete_bot_everywhere(bot["id"])
    with _store_lock:
        data = load_users()
        data["users"] = [u for u in data["users"] if u["id"] != user_id]
        save_users(data)
    flash("User @%s and all their bots were deleted." % user["username"])
    return redirect(url_for("admin_users"))


@app.route("/admin/keys")
@admin_required
def admin_keys():
    raw = sorted(load_keys()["keys"],
                 key=lambda x: x.get("created") or 0, reverse=True)
    keys = []
    for k in raw:
        row = dict(k)
        row["created"] = fmt_datetime(k.get("created"))
        row["used_at"] = fmt_datetime(k.get("used_at")) if k.get("used_at") else ""
        keys.append(row)
    new_keys = session.pop("new_keys", None)
    return render_template_string(ADMIN_KEYS_HTML, keys=keys,
                                  new_keys=new_keys, flash=pop_flash())


@app.route("/admin/keys/generate", methods=["POST"])
@admin_required
def admin_keys_generate():
    custom = (request.form.get("custom_days") or "").strip()
    days = None
    if custom:
        try:
            days = int(custom)
        except ValueError:
            days = None
    if days is None:
        try:
            days = int(request.form.get("duration_days") or 30)
        except ValueError:
            days = 30
    try:
        qty = int(request.form.get("quantity") or 1)
    except ValueError:
        qty = 1
    if days < 1 or days > 3650:
        flash("Duration must be between 1 and 3650 days.", "err")
        return redirect(url_for("admin_keys"))
    if qty < 1 or qty > 20:
        flash("Quantity must be between 1 and 20.", "err")
        return redirect(url_for("admin_keys"))
    made = generate_keys(days, qty)
    session["new_keys"] = [k["code"] for k in made]
    flash("%d key(s) generated for %d days of Premium." % (qty, days))
    return redirect(url_for("admin_keys"))


@app.route("/admin/keys/<code>/revoke", methods=["POST"])
@admin_required
def admin_key_revoke(code):
    with _store_lock:
        data = load_keys()
        for k in data["keys"]:
            if k["code"] == code and k["status"] == "unused":
                k["status"] = "revoked"
                flash("Key %s revoked." % code)
        save_keys(data)
    return redirect(url_for("admin_keys"))


@app.route("/admin/bots")
@admin_required
def admin_bots():
    users_by_id = {u["id"]: u for u in load_users()["users"]}
    bots = []
    for bot in load_db()["bots"]:
        b = dict(bot)
        b["status"] = bot_status(bot)
        b["uptime"] = bot_uptime(bot)
        owner = users_by_id.get(bot.get("owner") or "")
        b["owner_name"] = ("@" + owner["username"]) if owner else "Admin (ownerless)"
        bots.append(b)
    return render_template_string(ADMIN_BOTS_HTML, bots=bots, flash=pop_flash())


@app.route("/health")
def health():
    return "ok"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print("=" * 56, flush=True)
    print("  MR X BOT HOSTING PANEL v2", flush=True)
    print("  Listening on port %d" % port, flush=True)
    print("  Admin login: username 'admin' + PANEL_PASSWORD", flush=True)
    if PASSWORD_GENERATED:
        print("  PANEL_PASSWORD was not set, so a password was", flush=True)
        print("  generated for this run. WRITE IT DOWN:", flush=True)
        print("  >>>  %s  <<<" % PANEL_PASSWORD, flush=True)
        print("  Better: set the PANEL_PASSWORD env var yourself.", flush=True)
    print("=" * 56, flush=True)
    threading.Thread(target=_monitor_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
