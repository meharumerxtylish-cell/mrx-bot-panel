#!/usr/bin/env python3
"""
MR X Bot Hosting Panel
----------------------
A single-file Flask panel to host unlimited Telegram bots from one place.

- Login protected by the PANEL_PASSWORD environment variable.
  If PANEL_PASSWORD is not set, a random password is generated at first
  start and printed in the server log (see the console / host logs).
- Each bot is an uploaded .py file run as its own subprocess.
- Bot tokens are stored per-bot in data/bots/<id>/config.env on disk and
  are passed to the bot as the BOT_TOKEN environment variable.
  The full token is never shown in the UI again (only the last 4 chars).

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
from functools import wraps

from flask import (Flask, request, session, redirect, url_for,
                   render_template_string, abort)
from werkzeug.utils import secure_filename

# --------------------------------------------------------------------------
# Paths & storage
# --------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
BOTS_DIR = os.path.join(DATA_DIR, "bots")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
DB_FILE = os.path.join(DATA_DIR, "bots.json")

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
# Panel password
# --------------------------------------------------------------------------
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "").strip()
PASSWORD_GENERATED = False
if not PANEL_PASSWORD:
    alphabet = string.ascii_letters + string.digits
    PANEL_PASSWORD = "".join(secrets.choice(alphabet) for _ in range(14))
    PASSWORD_GENERATED = True

# --------------------------------------------------------------------------
# Bot registry (plain JSON file, no database)
# --------------------------------------------------------------------------
_db_lock = threading.Lock()
_processes = {}          # bot_id -> {"proc": Popen, "started": float, "restarts": int}
_proc_lock = threading.Lock()


def load_db():
    if not os.path.exists(DB_FILE):
        return {"bots": []}
    try:
        with open(DB_FILE) as f:
            return json.load(f)
    except (ValueError, OSError):
        return {"bots": []}


def save_db(db):
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(db, f, indent=2)
    os.replace(tmp, DB_FILE)


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
# Auth
# --------------------------------------------------------------------------
def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


# --------------------------------------------------------------------------
# HTML (dark MR X style: black + gold)
# --------------------------------------------------------------------------
CSS = """
<style>
  * { box-sizing: border-box; }
  body { margin:0; font-family: 'Segoe UI', Arial, sans-serif; background:#0a0a0a; color:#eee; }
  header { background:#111; border-bottom:2px solid #d4af37; padding:14px 22px;
           display:flex; justify-content:space-between; align-items:center; }
  header .logo { color:#d4af37; font-size:20px; font-weight:700; letter-spacing:1px; }
  header a { color:#d4af37; text-decoration:none; font-size:14px; }
  .wrap { max-width:980px; margin:26px auto; padding:0 16px; }
  .card { background:#151515; border:1px solid #2b2b2b; border-radius:10px; padding:18px; margin-bottom:20px; }
  h2 { color:#d4af37; margin:0 0 14px; font-size:17px; }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  th, td { padding:9px 8px; border-bottom:1px solid #262626; text-align:left; vertical-align:middle; }
  th { color:#d4af37; font-weight:600; }
  .badge { padding:2px 9px; border-radius:20px; font-size:12px; font-weight:700; }
  .running { background:#14532d; color:#4ade80; }
  .stopped { background:#333; color:#bbb; }
  .crashed { background:#5b1a1a; color:#f87171; }
  button, .btn { background:#d4af37; color:#111; border:none; padding:6px 12px; border-radius:6px;
                 font-weight:700; cursor:pointer; font-size:13px; text-decoration:none; display:inline-block; }
  button:hover, .btn:hover { background:#e8c95a; }
  button.grey { background:#333; color:#eee; }
  button.red { background:#7f1d1d; color:#fecaca; }
  form.inline { display:inline; }
  input[type=text], input[type=password] { background:#0d0d0d; border:1px solid #333; color:#eee;
                 padding:9px; border-radius:6px; width:100%; margin:5px 0 12px; }
  input[type=file] { margin:5px 0 12px; color:#ccc; }
  label { font-size:13px; color:#bbb; }
  .err { background:#5b1a1a; color:#fecaca; padding:10px; border-radius:6px; margin-bottom:14px; }
  pre.log { background:#000; color:#4ade80; padding:14px; border-radius:8px; font-size:12px;
            white-space:pre-wrap; word-wrap:break-word; max-height:70vh; overflow:auto; }
  .muted { color:#888; font-size:12px; }
  .center { max-width:380px; margin:9vh auto; }
</style>
"""

LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MR X Bot Hosting — Login</title>""" + CSS + """</head><body>
<div class="wrap center"><div class="card">
<h2 style="text-align:center">MR X BOT HOSTING</h2>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<form method="post">
  <label>Panel password</label>
  <input type="password" name="password" required autofocus>
  <button type="submit" style="width:100%; padding:11px">Login</button>
</form>
<p class="muted" style="text-align:center">One panel. Unlimited bots.</p>
</div></div></body></html>"""

DASH_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MR X Bot Hosting — Dashboard</title>""" + CSS + """</head><body>
<header><div class="logo">MR X BOT HOSTING</div><a href="{{ url_for('logout') }}">Logout</a></header>
<div class="wrap">

<div class="card">
<h2>My Bots ({{ bots|length }})</h2>
{% if bots %}
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
{% else %}
<p class="muted">No bots yet. Add your first bot below.</p>
{% endif %}
</div>

<div class="card">
<h2>Add a Bot</h2>
<form id="addform" method="post" action="{{ url_for('add_bot') }}" enctype="multipart/form-data">
  <label>Bot name</label>
  <input type="text" name="name" placeholder="e.g. My Echo Bot" required>
  <label>Bot file (.py)</label><br>
  <input type="file" name="botfile" accept=".py" required><br>
  <label>Bot token (from @BotFather — stored on this server only, never shown again)</label>
  <input type="text" name="token" placeholder="123456:ABC... (optional if the token is inside your .py file)">
  <label><input type="checkbox" name="auto_restart"> Auto-restart if the bot crashes</label>
  <br><br>
  <button type="submit" style="padding:10px 22px">Add Bot</button>
</form>
<p class="muted">Tip: your bot can read its token from the environment variable <b>BOT_TOKEN</b>.
Keep each bot's token separate — one token per bot, pasted only here.</p>
</div>

</div>
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
<header><div class="logo">LOGS — {{ name|e }}</div><a href="{{ url_for('dashboard') }}">← Dashboard</a></header>
<div class="wrap"><pre class="log">{{ log_text|e }}</pre>
<p class="muted">Auto-refreshes every 5 seconds. Showing the last 300 lines.</p></div>
</body></html>"""


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if secrets.compare_digest(request.form.get("password", ""), PANEL_PASSWORD):
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        error = "Wrong password. Try again."
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    db = load_db()
    bots = []
    for bot in db["bots"]:
        b = dict(bot)
        b["status"] = bot_status(bot)
        b["uptime"] = bot_uptime(bot)
        bots.append(b)
    return render_template_string(DASH_HTML, bots=bots)


@app.route("/add", methods=["POST"])
@login_required
def add_bot():
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
            "name": name,
            "file": filename,
            "token_masked": masked,
            "auto_restart": auto_restart,
            "desired_running": False,
            "created": int(time.time()),
        })
        save_db(db)
    _log_line(bot_id, "[panel] bot '%s' added (%s)" % (name, filename))
    return redirect(url_for("dashboard"))


@app.route("/bot/<bot_id>/start", methods=["POST"])
@login_required
def start(bot_id):
    if not get_bot(bot_id):
        abort(404)
    start_bot(bot_id)
    return redirect(url_for("dashboard"))


@app.route("/bot/<bot_id>/stop", methods=["POST"])
@login_required
def stop(bot_id):
    if not get_bot(bot_id):
        abort(404)
    stop_bot(bot_id)
    return redirect(url_for("dashboard"))


@app.route("/bot/<bot_id>/restart", methods=["POST"])
@login_required
def restart(bot_id):
    if not get_bot(bot_id):
        abort(404)
    stop_bot(bot_id, mark_stopped=False)
    start_bot(bot_id)
    return redirect(url_for("dashboard"))


@app.route("/bot/<bot_id>/delete", methods=["POST"])
@login_required
def delete(bot_id):
    if not get_bot(bot_id):
        abort(404)
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
    return redirect(url_for("dashboard"))


@app.route("/bot/<bot_id>/autorestart", methods=["POST"])
@login_required
def toggle_autorestart(bot_id):
    bot = get_bot(bot_id)
    if not bot:
        abort(404)
    update_bot(bot_id, auto_restart=not bot.get("auto_restart"))
    return redirect(url_for("dashboard"))


@app.route("/bot/<bot_id>/logs")
@login_required
def logs(bot_id):
    bot = get_bot(bot_id)
    if not bot:
        abort(404)
    path = bot_log_path(bot_id)
    text = ""
    if os.path.exists(path):
        with open(path, errors="replace") as f:
            text = "".join(f.readlines()[-300:])
    if not text:
        text = "(no logs yet — start the bot first)"
    return render_template_string(LOG_HTML, name=bot["name"], log_text=text)


@app.route("/health")
def health():
    return "ok"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print("=" * 56, flush=True)
    print("  MR X BOT HOSTING PANEL", flush=True)
    print("  Listening on port %d" % port, flush=True)
    if PASSWORD_GENERATED:
        print("  PANEL_PASSWORD was not set, so a password was", flush=True)
        print("  generated for this run. WRITE IT DOWN:", flush=True)
        print("  >>>  %s  <<<" % PANEL_PASSWORD, flush=True)
        print("  Better: set the PANEL_PASSWORD env var yourself.", flush=True)
    print("=" * 56, flush=True)
    threading.Thread(target=_monitor_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
