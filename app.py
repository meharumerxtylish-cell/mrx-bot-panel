#!/usr/bin/env python3
"""
MR X Bot Hosting Panel — v3 (Jarvis edition)
------------------------------------------
A single-file Flask panel to host Telegram bots, now with:

- A public landing page with Free / Premium plans.
- User accounts (register / login). Each user sees only their own bots.
- Plan limits: Free = 2 bots, Premium = unlimited bots.
- License keys (MRX-XXXXXX-XXXXXX-XXXXXX) that activate Premium for a
  number of days. Users redeem keys in the panel.
- An admin panel (stats, users, keys, all bots).
- v3: Jarvis HUD theme (Orbitron/Rajdhani, glassmorphism, gold + cyan
  glow), drag-and-drop uploads, ZIP project support with safe
  extraction, automatic entry-point detection and requirements.txt
  installation, per-bot env vars, an owner Settings page (prices,
  WhatsApp number, free limit, announcement, maintenance mode,
  registration on/off) and auto-restore of running bots on boot.

Admin login: username "admin" + the PANEL_PASSWORD environment variable.
If PANEL_PASSWORD is not set, a random password is generated at first
start and printed in the server log (see the console / host logs).

Each bot is an uploaded .py file (or a .zip project) run as its own subprocess. Bot tokens
are stored per-bot in data/bots/<id>/config.env on disk and are passed
to the bot as the BOT_TOKEN environment variable. The full token is
never shown in the UI again (only the last 4 chars).

Run:  python app.py        (PORT env var sets the port, default 5000)
"""

import io
import os
import re
import sys
import json
import time
import shutil
import secrets
import string
import threading
import subprocess
import zipfile
from datetime import datetime
from functools import wraps

from flask import (Flask, request, session, redirect, url_for,
                   render_template_string, abort, jsonify, send_file)
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
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB upload limit (.py or .zip projects)


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
# Owner settings (data/settings.json) — editable in the admin panel
# --------------------------------------------------------------------------
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
DEFAULT_SETTINGS = {
    "whatsapp": "923044507018",
    "price_2": 50,
    "price_7": 150,
    "price_30": 400,
    "free_limit": 2,
    "announcement": "",
    "maintenance": False,
    "registration_open": True,
}


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    stored = _load_json(SETTINGS_FILE, {})
    if isinstance(stored, dict):
        s.update(stored)
    return s


def save_settings(s):
    _save_json(SETTINGS_FILE, s)


def free_bot_limit():
    try:
        return int(load_settings().get("free_limit", FREE_BOT_LIMIT))
    except (TypeError, ValueError):
        return FREE_BOT_LIMIT


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
    return PREMIUM_BOT_LIMIT if user_is_premium(user) else free_bot_limit()


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
# ZIP project uploads (safe extraction, entry detection, pip install)
# --------------------------------------------------------------------------
ENTRY_CANDIDATES = ("main.py", "bot.py", "app.py", "run.py")
_SKIP_DIRS = {"__pycache__", "__MACOSX", "venv", ".venv", "env", ".git",
              ".idea", "node_modules"}
_MAX_ZIP_UNPACKED = 200 * 1024 * 1024  # 200 MB safety cap on extracted size


def _zip_entry_is_junk(name):
    parts = [p for p in name.replace("\\", "/").split("/")
             if p not in ("", ".")]
    if not parts:
        return True
    if parts[0] == "__MACOSX":
        return True
    base = parts[-1]
    return base.startswith(".") or base == ".DS_Store"


def _find_entry_file(root):
    """Pick the runnable .py inside an extracted project folder.

    Order: main.py / bot.py / app.py / run.py at the root, then a single
    .py at the root, then (projects nested in one subfolder) an entry
    candidate or a lone .py anywhere below the root. Returns the path
    relative to root, or None when nothing runnable can be determined.
    """
    for cand in ENTRY_CANDIDATES:
        if os.path.isfile(os.path.join(root, cand)):
            return cand
    try:
        root_pys = sorted(f for f in os.listdir(root)
                          if f.lower().endswith(".py")
                          and os.path.isfile(os.path.join(root, f)))
    except OSError:
        root_pys = []
    if len(root_pys) == 1:
        return root_pys[0]
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.lower().endswith(".py"):
                found.append(os.path.relpath(os.path.join(dirpath, fn), root))
    if not found:
        return None
    if len(found) == 1:
        return found[0]

    def rank(rel):
        base = os.path.basename(rel).lower()
        pri = (ENTRY_CANDIDATES.index(base) if base in ENTRY_CANDIDATES
               else len(ENTRY_CANDIDATES))
        return (pri, rel.count(os.sep), rel)

    found.sort(key=rank)
    best = found[0]
    if os.path.basename(best).lower() in ENTRY_CANDIDATES:
        return best
    return None


def extract_project_zip(bot_id, zip_path):
    """Extract an uploaded project ZIP into data/bots/<id>/project/.

    Returns the entry file path relative to the bot dir (for example
    "project/main.py"), or None when no entry file can be determined.
    Raises ValueError for invalid archives, oversize unpacks or unsafe
    (path-traversal) entries; the caller cleans up in that case.
    """
    dest_root = os.path.join(bot_dir(bot_id), "project")
    os.makedirs(dest_root, exist_ok=True)
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError):
        raise ValueError("That file is not a valid ZIP archive.")
    dest_real = os.path.realpath(dest_root)
    total = 0
    with zf:
        for info in zf.infolist():
            if info.is_dir() or _zip_entry_is_junk(info.filename):
                continue
            total += info.file_size
            if total > _MAX_ZIP_UNPACKED:
                raise ValueError("That ZIP unpacks to more than 200 MB — too big.")
            target = os.path.realpath(os.path.join(dest_root, info.filename))
            if target != dest_real and not target.startswith(dest_real + os.sep):
                raise ValueError("Unsafe path inside ZIP: %s" % info.filename)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    entry = _find_entry_file(dest_root)
    if entry is None:
        return None
    return os.path.join("project", entry)


def install_requirements(bot_id, entry_rel):
    """pip-install a ZIP project's requirements.txt once, at add time.

    Looks next to the entry file first, then at the project root. The
    result (success or failure, with the last output lines) is written to
    the bot's log. A failed install never blocks the add.
    """
    bdir = bot_dir(bot_id)
    entry_dir = os.path.dirname(os.path.join(bdir, entry_rel))
    candidates = [os.path.join(entry_dir, "requirements.txt"),
                  os.path.join(bdir, "project", "requirements.txt"),
                  os.path.join(bdir, "requirements.txt")]
    req = None
    for cand in candidates:
        if os.path.isfile(cand):
            req = cand
            break
    if req is None:
        return
    _log_line(bot_id, "[panel] installing requirements (%s) ..."
              % os.path.relpath(req, bdir))
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", req],
            capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        _log_line(bot_id, "[panel] pip install timed out after 180s — the bot "
                          "was still added; some packages may be missing.")
        return
    except OSError as e:
        _log_line(bot_id, "[panel] pip install could not run: %s" % e)
        return
    _log_line(bot_id, "[panel] pip install finished (exit code %d). Last lines:"
              % proc.returncode)
    tail = (proc.stdout + "\n" + proc.stderr).strip().splitlines()[-8:]
    for line in tail:
        _log_line(bot_id, "[pip] " + line)
    if proc.returncode != 0:
        _log_line(bot_id, "[panel] WARNING: some requirements failed to "
                          "install — the bot was still added. See the lines "
                          "above; fix requirements.txt and re-add if needed.")


def _store_bot_upload(bot_id, upload):
    """Save an uploaded .py or .zip into the bot's directory.

    Returns (entry_rel, is_zip, error). entry_rel is the runnable file
    relative to the bot dir; error is a user-facing message or None.
    """
    filename = secure_filename(upload.filename or "")
    lower = filename.lower()
    if lower.endswith(".py"):
        upload.save(os.path.join(bot_dir(bot_id), filename))
        return filename, False, None
    if lower.endswith(".zip"):
        zip_path = os.path.join(bot_dir(bot_id), "project.zip")
        upload.save(zip_path)
        try:
            entry_rel = extract_project_zip(bot_id, zip_path)
        except ValueError as e:
            return None, True, str(e)
        try:
            os.remove(zip_path)
        except OSError:
            pass
        if entry_rel is None:
            return None, True, ("No Python entry file found in that ZIP. "
                                "Add a main.py (or bot.py / app.py / run.py) "
                                "to the project and try again.")
        return entry_rel, True, None
    return None, False, "Only .py or .zip files are allowed."


# --------------------------------------------------------------------------
# Per-bot environment (config.env: BOT_TOKEN + custom variables)
# --------------------------------------------------------------------------
def read_bot_env(bot_id):
    """All KEY=value pairs from the bot's config.env (incl. BOT_TOKEN)."""
    env = {}
    cfg = os.path.join(bot_dir(bot_id), "config.env")
    if os.path.exists(cfg):
        with open(cfg, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def write_bot_env(bot_id, env):
    lines = []
    if env.get("BOT_TOKEN"):
        lines.append("BOT_TOKEN=%s" % env["BOT_TOKEN"])
    for k in sorted(env):
        if k != "BOT_TOKEN":
            lines.append("%s=%s" % (k, env[k]))
    with open(os.path.join(bot_dir(bot_id), "config.env"), "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))


def bot_memory(bot):
    """Resident memory of a running bot (Linux /proc), else '—'."""
    with _proc_lock:
        entry = _processes.get(bot["id"])
    if not entry or entry["proc"].poll() is not None:
        return "—"
    try:
        with open("/proc/%d/status" % entry["proc"].pid) as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    if kb >= 1024:
                        return "%.1f MB" % (kb / 1024.0)
                    return "%d KB" % kb
    except (OSError, ValueError, IndexError):
        pass
    return "—"


def _dir_size(path):
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fn in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    return total


def fmt_size(num):
    if num >= 1024 * 1024:
        return "%.1f MB" % (num / (1024.0 * 1024))
    if num >= 1024:
        return "%.1f KB" % (num / 1024.0)
    return "%d B" % num


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
        env.update(read_bot_env(bot_id))
        token = read_bot_token(bot_id)
        if token:
            env["BOT_TOKEN"] = token
        logf = open(bot_log_path(bot_id), "a")
        logf.write("\n===== [panel] starting bot '%s' =====\n" % bot["name"])
        logf.flush()
        try:
            proc = subprocess.Popen(
                [sys.executable, "-u", script],
                cwd=os.path.dirname(script) or bot_dir(bot_id),
                env=env,
                stdout=logf,
                stderr=subprocess.STDOUT,
            )
        except OSError as e:
            logf.write("[panel] ERROR: could not start process: %s\n" % e)
            logf.close()
            return False
        _processes[bot_id] = {"proc": proc, "started": time.time(),
                              "restarts": entry["restarts"] if entry else 0,
                              "crash_seen": False}
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
    # On panel boot: every bot that was running when the panel stopped
    # comes back on its own (auto-restore), auto-restart or not.
    db = load_db()
    for bot in db["bots"]:
        if bot.get("desired_running"):
            start_bot(bot["id"])
    while True:
        time.sleep(3)
        db = load_db()
        for bot in db["bots"]:
            with _proc_lock:
                entry = _processes.get(bot["id"])
            if entry is not None and entry["proc"].poll() is not None \
                    and entry["proc"].returncode not in (0, None) \
                    and not entry.get("crash_seen"):
                entry["crash_seen"] = True
                update_bot(bot["id"], last_crash=int(time.time()))
            if not (bot.get("auto_restart") and bot.get("desired_running")):
                continue
            if entry is None or entry["proc"].poll() is not None:
                if entry is not None and entry["proc"].returncode in (0, None):
                    continue  # clean exit: leave it stopped
                _log_line(bot["id"], "[panel] auto-restart: bot died, restarting...")
                if start_bot(bot["id"]):
                    update_bot(bot["id"],
                               restart_count=(bot.get("restart_count") or 0) + 1)
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



@app.context_processor
def _inject_settings():
    """Make the owner settings available to every template."""
    return {"settings": load_settings()}


@app.before_request
def _maintenance_gate():
    """Maintenance mode: only the admin (and the login page) get through."""
    if request.endpoint == "health":
        return None
    try:
        if not load_settings().get("maintenance"):
            return None
    except Exception:
        return None
    if is_admin():
        return None
    if request.path in ("/login", "/logout"):
        return None
    return render_template_string(MAINTENANCE_HTML), 503


# --------------------------------------------------------------------------
# HTML (v3 Jarvis HUD theme: deep blue-black, gold + cyan glow)
# --------------------------------------------------------------------------
CSS = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;800;900&family=Rajdhani:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  * { box-sizing: border-box; }
  html { font-size:17px; }
  body { margin:0; font-family:'Rajdhani','Segoe UI', Arial, sans-serif; color:#e8f1f8;
         background-color:#05080f;
         background-image:
           radial-gradient(1000px 520px at 50% -120px, rgba(53,211,247,.13), transparent),
           radial-gradient(760px 420px at 88% 108%, rgba(212,175,55,.08), transparent),
           linear-gradient(rgba(53,211,247,.05) 1px, transparent 1px),
           linear-gradient(90deg, rgba(53,211,247,.05) 1px, transparent 1px);
         background-size:auto, auto, 44px 44px, 44px 44px;
         min-height:100vh; display:flex; flex-direction:column; }
  ::selection { background:rgba(212,175,55,.85); color:#0a0a0a; }
  main { flex:1; width:100%; }
  a { color:#e8c95a; }
  header { background:rgba(6,10,18,.92); backdrop-filter:blur(8px);
           border-bottom:1px solid rgba(212,175,55,.55); padding:14px 26px;
           display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px;
           box-shadow:0 0 22px rgba(212,175,55,.16), 0 6px 28px rgba(0,0,0,.6);
           position:sticky; top:0; z-index:10; }
  header .logo { font-family:'Orbitron',sans-serif; color:#d4af37; font-size:24px; font-weight:800;
                 letter-spacing:3px; text-shadow:0 0 16px rgba(212,175,55,.55); }
  header .logo a { color:#d4af37; background:none; padding:0; box-shadow:none; font-size:24px; }
  header nav { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
  header a { font-family:'Orbitron',sans-serif; color:#111; background:linear-gradient(180deg,#e8c95a,#c9a227);
             text-decoration:none; font-size:13px; font-weight:700; letter-spacing:.5px;
             padding:9px 16px; border-radius:8px; box-shadow:0 0 12px rgba(212,175,55,.3); }
  header a.ghost { background:rgba(53,211,247,.07); color:#bfeaff; border:1px solid rgba(53,211,247,.45);
                   box-shadow:none; }
  footer { border-top:1px solid rgba(53,211,247,.18); padding:22px; text-align:center; color:#7d92a8;
           font-size:14px; margin-top:20px; }
  footer b { color:#d4af37; font-family:'Orbitron',sans-serif; letter-spacing:1px; }
  .wrap { max-width:1120px; margin:34px auto; padding:0 20px; width:100%; }
  .card { background:linear-gradient(160deg, rgba(20,30,48,.72), rgba(8,12,22,.88));
          border:1px solid rgba(53,211,247,.28); border-radius:14px;
          padding:26px; margin-bottom:26px;
          box-shadow:0 0 0 1px rgba(212,175,55,.05), 0 12px 34px rgba(0,0,0,.55), 0 0 24px rgba(53,211,247,.06);
          backdrop-filter:blur(6px); }
  h2 { font-family:'Orbitron',sans-serif; color:#d4af37; margin:0 0 18px; font-size:21px;
       font-weight:700; letter-spacing:1px; text-shadow:0 0 14px rgba(212,175,55,.35); }
  h3 { color:#eaf6ff; margin:0 0 10px; font-size:18.5px; font-weight:700; }
  .tablewrap { overflow-x:auto; }
  table { width:100%; border-collapse:collapse; font-size:16px; }
  th, td { padding:13px 10px; border-bottom:1px solid rgba(255,255,255,.07); text-align:left; vertical-align:middle; }
  th { font-family:'Orbitron',sans-serif; color:#35d3f7; font-weight:700; font-size:12.5px;
       text-transform:uppercase; letter-spacing:1.5px; border-bottom:1px solid rgba(53,211,247,.3); }
  .badge { font-family:'Orbitron',sans-serif; padding:4px 12px; border-radius:20px; font-size:11.5px;
           font-weight:700; letter-spacing:.5px; white-space:nowrap; border:1px solid transparent; }
  .running { background:rgba(20,83,45,.6); color:#4ade80; border-color:rgba(74,222,128,.45); box-shadow:0 0 10px rgba(74,222,128,.22); }
  .stopped { background:rgba(60,70,84,.5); color:#b9cbdd; border-color:rgba(185,203,221,.3); }
  .crashed { background:rgba(91,26,26,.6); color:#f87171; border-color:rgba(248,113,113,.45); box-shadow:0 0 10px rgba(248,113,113,.2); }
  .plan-premium { background:linear-gradient(180deg,#e8c95a,#c9a227); color:#111; box-shadow:0 0 14px rgba(212,175,55,.45); }
  .plan-free { background:rgba(60,70,84,.5); color:#b9cbdd; border-color:rgba(185,203,221,.3); }
  .key-unused { background:rgba(20,83,45,.6); color:#4ade80; border-color:rgba(74,222,128,.45); }
  .key-used { background:rgba(60,70,84,.5); color:#b9cbdd; border-color:rgba(185,203,221,.3); }
  .key-revoked { background:rgba(91,26,26,.6); color:#f87171; border-color:rgba(248,113,113,.45); }
  .banned { background:rgba(91,26,26,.6); color:#f87171; border-color:rgba(248,113,113,.45); }
  button, .btn { font-family:'Orbitron',sans-serif; background:linear-gradient(180deg,#e8c95a,#c9a227);
                 color:#111; border:none; padding:10px 18px; border-radius:9px; font-weight:700;
                 cursor:pointer; font-size:13px; letter-spacing:.5px; text-decoration:none;
                 display:inline-block; box-shadow:0 0 14px rgba(212,175,55,.32);
                 transition:transform .08s ease, filter .15s ease, box-shadow .15s ease; }
  button:hover, .btn:hover { filter:brightness(1.09); transform:translateY(-1px); box-shadow:0 0 22px rgba(212,175,55,.5); }
  button.grey, .btn.grey { background:rgba(53,211,247,.07); color:#cfe9ff; border:1px solid rgba(53,211,247,.45); box-shadow:none; }
  button.grey:hover, .btn.grey:hover { box-shadow:0 0 14px rgba(53,211,247,.3); }
  button.red { background:rgba(127,29,29,.92); color:#fecaca; border:1px solid rgba(248,113,113,.5); box-shadow:none; }
  button:disabled { opacity:.5; cursor:not-allowed; transform:none; }
  form.inline { display:inline; }
  input[type=text], input[type=password], input[type=email], input[type=number], select, textarea {
                 background:rgba(2,6,12,.85); border:1px solid rgba(53,211,247,.35); color:#eaf6ff;
                 padding:13px 14px; border-radius:9px; width:100%; margin:7px 0 16px;
                 font-family:'Rajdhani',sans-serif; font-size:16.5px; font-weight:500; }
  textarea { min-height:110px; resize:vertical; }
  input:focus, select:focus, textarea:focus { outline:none; border-color:#35d3f7;
                 box-shadow:0 0 0 3px rgba(53,211,247,.16), 0 0 14px rgba(53,211,247,.25); }
  input[type=file] { margin:7px 0 16px; color:#cfe9ff; font-size:15.5px; width:auto; }
  input[type=checkbox] { width:19px; height:19px; vertical-align:-3px; accent-color:#d4af37; }
  label { font-size:15.5px; color:#b9cbdd; font-weight:600; }
  .err { background:rgba(91,26,26,.55); border:1px solid rgba(248,113,113,.5); color:#fecaca;
         padding:13px; border-radius:9px; margin-bottom:16px; font-size:15.5px; }
  .okmsg { background:rgba(20,83,45,.5); border:1px solid rgba(74,222,128,.45); color:#bbf7d0;
           padding:13px; border-radius:9px; margin-bottom:16px; font-size:15.5px; }
  .announce { background:rgba(212,175,55,.12); border:1px solid rgba(212,175,55,.55); color:#ffe98a;
              padding:14px 18px; border-radius:12px; margin-bottom:26px; font-size:16.5px; font-weight:600;
              box-shadow:0 0 18px rgba(212,175,55,.15); }
  pre.log { background:#020409; color:#4ade80; border:1px solid rgba(53,211,247,.3); padding:18px;
            border-radius:10px; font-size:14px; white-space:pre-wrap; word-wrap:break-word;
            max-height:70vh; overflow:auto; box-shadow:inset 0 0 30px rgba(53,211,247,.05); }
  .muted { color:#8fa3b8; font-size:13.5px; }
  .center { max-width:440px; margin:8vh auto; }
  .tagline { text-align:center; color:#8fa3b8; font-size:15px; margin-top:14px; letter-spacing:.4px; }
  /* Landing page */
  .hero { text-align:center; padding:64px 20px 54px; max-width:900px; margin:0 auto; }
  .hud-rings { position:relative; width:180px; height:180px; margin:0 auto 16px; }
  .hud-rings span { position:absolute; inset:0; border-radius:50%; border:2px dashed rgba(53,211,247,.55);
                    box-shadow:0 0 24px rgba(53,211,247,.22), inset 0 0 24px rgba(53,211,247,.12);
                    animation:hudspin 26s linear infinite; }
  .hud-rings span:nth-child(2) { inset:25px; border:2px solid rgba(212,175,55,.55); border-style:solid;
                    box-shadow:0 0 20px rgba(212,175,55,.22), inset 0 0 20px rgba(212,175,55,.1);
                    animation:none; }
  .hud-rings span:nth-child(3) { inset:50px; border:2px dashed rgba(53,211,247,.7);
                    animation:hudspin 16s linear infinite reverse; }
  .hud-rings .core { position:absolute; inset:72px; border-radius:50%;
                     background:radial-gradient(circle at 35% 35%, #ffe98a, #d4af37 60%, #8a6d1c);
                     box-shadow:0 0 34px rgba(212,175,55,.65); }
  @keyframes hudspin { to { transform:rotate(360deg); } }
  .hero .kicker { font-family:'Orbitron',sans-serif; color:#35d3f7; font-weight:700; letter-spacing:4px;
                  font-size:13.5px; text-transform:uppercase; text-shadow:0 0 12px rgba(53,211,247,.5); }
  .hero h1 { font-family:'Orbitron',sans-serif; font-size:50px; line-height:1.14; margin:18px 0 8px; font-weight:900; }
  .hero h1 .gold { color:#d4af37; text-shadow:0 0 30px rgba(212,175,55,.5); }
  .hero p.sub { font-size:20px; color:#b9cbdd; max-width:640px; margin:14px auto 32px; line-height:1.55; }
  .hero .cta { display:flex; gap:14px; justify-content:center; flex-wrap:wrap; }
  .hero .cta .btn { padding:15px 32px; font-size:15px; border-radius:11px; }
  .hero .cta .btn.ghost { background:rgba(53,211,247,.06); color:#eaf6ff; border:1.5px solid rgba(212,175,55,.7); box-shadow:none; }
  .section { max-width:1120px; margin:0 auto; padding:26px 20px; width:100%; }
  .section h2 { text-align:center; font-size:28px; margin-bottom:26px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:18px; }
  .feature { background:linear-gradient(160deg, rgba(20,30,48,.66), rgba(8,12,22,.85));
             border:1px solid rgba(53,211,247,.24); border-radius:14px; padding:24px;
             box-shadow:0 8px 24px rgba(0,0,0,.4), 0 0 16px rgba(53,211,247,.05); }
  .feature .icon { font-size:30px; margin-bottom:10px; }
  .feature p { color:#a9bfd2; font-size:15.5px; line-height:1.55; margin:0; }
  .pricing { display:grid; grid-template-columns:repeat(auto-fit,minmax(225px,1fr)); gap:22px;
             max-width:1120px; margin:14px auto 0; }
  .price-card { background:linear-gradient(160deg, rgba(20,30,48,.66), rgba(8,12,22,.9));
                border:1.5px solid rgba(53,211,247,.28); border-radius:16px; padding:32px 28px;
                text-align:center; position:relative;
                box-shadow:0 12px 32px rgba(0,0,0,.5), 0 0 18px rgba(53,211,247,.06); }
  .price-card.premium { border-color:rgba(212,175,55,.75); box-shadow:0 12px 40px rgba(0,0,0,.5), 0 0 34px rgba(212,175,55,.24); }
  .price-card .planname { font-family:'Orbitron',sans-serif; font-size:17px; font-weight:800; letter-spacing:2px; text-transform:uppercase; }
  .price-card.premium .planname { color:#d4af37; }
  .price-card .price { font-family:'Orbitron',sans-serif; font-size:38px; font-weight:800; margin:12px 0 4px; }
  .price-card.premium .price { color:#ffd95e; text-shadow:0 0 18px rgba(212,175,55,.4); }
  .price-card ul { list-style:none; padding:0; margin:20px 0 26px; text-align:left; }
  .price-card li { padding:8px 0; border-bottom:1px solid rgba(255,255,255,.07); color:#d8e4ee; font-size:16px; }
  .price-card li:before { content:"✓ "; color:#d4af37; font-weight:800; }
  .price-card .ribbon { position:absolute; top:-14px; left:50%; transform:translateX(-50%);
                background:linear-gradient(180deg,#e8c95a,#c9a227); color:#111; font-weight:800;
                font-family:'Orbitron',sans-serif; font-size:12px; padding:5px 16px; border-radius:20px;
                letter-spacing:1px; white-space:nowrap; box-shadow:0 0 16px rgba(212,175,55,.45); }
  .faq details { background:linear-gradient(160deg, rgba(20,30,48,.6), rgba(8,12,22,.85));
                 border:1px solid rgba(53,211,247,.25); border-radius:12px;
                 padding:16px 20px; margin-bottom:12px; }
  .faq summary { font-family:'Orbitron',sans-serif; font-size:15px; color:#eaf6ff; cursor:pointer; letter-spacing:.4px; }
  .faq p { color:#a9bfd2; font-size:15.5px; line-height:1.55; margin:10px 0 0; }
  /* Stat cards (admin + dashboard strips) */
  .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:16px; margin-bottom:26px; }
  .stat { background:linear-gradient(160deg, rgba(20,30,48,.66), rgba(8,12,22,.88));
          border:1px solid rgba(53,211,247,.26); border-radius:14px; padding:22px; text-align:center;
          box-shadow:0 0 16px rgba(53,211,247,.05); }
  .stat .num { font-family:'Orbitron',sans-serif; font-size:34px; font-weight:800; color:#d4af37;
               line-height:1.1; text-shadow:0 0 16px rgba(212,175,55,.35); }
  .stat .lbl { color:#8fa3b8; font-size:13.5px; text-transform:uppercase; letter-spacing:1px; margin-top:4px; }
  .pbar { height:12px; background:rgba(255,255,255,.07); border:1px solid rgba(212,175,55,.4);
          border-radius:8px; overflow:hidden; margin-top:10px; }
  .pbar > div { height:100%; background:linear-gradient(90deg,#c9a227,#e8c95a);
                box-shadow:0 0 14px rgba(212,175,55,.6); }
  .keybox { background:#020409; border:1px solid rgba(212,175,55,.6); border-radius:10px; padding:16px 18px;
            font-family:Consolas, monospace; font-size:16.5px; line-height:1.9; color:#4ade80;
            white-space:pre-wrap; word-wrap:break-word; }
  code.key { font-family:Consolas, monospace; background:#020409; padding:2px 8px; border-radius:6px; color:#4ade80; }
  /* Dashboard premium plan boxes */
  .planbox { flex:1; min-width:180px; background:rgba(10,16,28,.66); border:1.5px solid rgba(212,175,55,.6);
             border-radius:12px; padding:18px; text-align:center; box-shadow:0 0 16px rgba(212,175,55,.12); }
  .planbox .pname { font-family:'Orbitron',sans-serif; font-weight:800; letter-spacing:1.5px; font-size:13px; color:#d4af37; }
  .planbox .pprice { font-family:'Orbitron',sans-serif; font-size:30px; font-weight:800; margin:8px 0; color:#fff; }
  /* Drag & drop upload zone */
  .dropzone { border:2px dashed rgba(53,211,247,.45); border-radius:14px; padding:30px 18px;
              text-align:center; cursor:pointer; background:rgba(53,211,247,.04);
              transition:border-color .15s ease, background .15s ease, box-shadow .15s ease;
              margin:7px 0 16px; }
  .dropzone:hover { border-color:rgba(212,175,55,.75); background:rgba(212,175,55,.05); }
  .dropzone.dragover { border-color:#35d3f7; background:rgba(53,211,247,.12);
              box-shadow:0 0 24px rgba(53,211,247,.35), inset 0 0 24px rgba(53,211,247,.08); }
  .dropzone .dz-icon { font-size:38px; }
  .dropzone .dz-title { font-family:'Orbitron',sans-serif; color:#eaf6ff; font-size:16.5px; letter-spacing:1px; margin-top:6px; }
  .dropzone .dz-sub { color:#8fa3b8; font-size:14.5px; margin-top:6px; }
  .dropzone .dz-file { margin-top:12px; color:#35d3f7; font-weight:700; font-size:16px; word-break:break-all; }
  .dropzone input[type=file] { display:none; }
  /* Dashboard two-column layout */
  .dash-grid { display:grid; grid-template-columns:1fr; gap:26px; align-items:start; }
  .dash-grid .card { margin-bottom:0; }
  @media (min-width:980px) { .dash-grid { grid-template-columns:1.45fr 1fr; } }
  @media (max-width:640px) { .hero h1 { font-size:34px; } .hero { padding:44px 16px 36px; }
                             header { padding:12px 14px; } header .logo { font-size:19px; } }
</style>
"""

FOOTER = """<footer><b>MR X BOT HOSTING</b> — One panel. Unlimited bots.<br>
<span style="font-size:12.5px">Run your Telegram bots 24/7 from one simple panel.</span></footer>"""



DROPZONE_HTML = """
<div class="dropzone" id="dz">
  <div class="dz-icon">📥</div>
  <div class="dz-title">Drag &amp; drop your .py or .zip file here</div>
  <div class="dz-sub">or click to browse — a single .py bot, or a full project .zip
  (extracted automatically, requirements.txt installed for you)</div>
  <div class="dz-file" id="dzfile"></div>
  <input type="file" name="botfile" accept=".py,.zip" required>
</div>
"""

DROPZONE_JS = """
<script>
(function(){
  var dz = document.getElementById('dz');
  if (!dz) return;
  var input = dz.querySelector('input[type=file]');
  var label = document.getElementById('dzfile');
  function show(){
    if (input.files && input.files.length) {
      var f = input.files[0];
      label.textContent = '📄 ' + f.name + ' (' + Math.max(1, Math.round(f.size/1024)) + ' KB)';
    }
  }
  dz.addEventListener('click', function(e){ if (e.target !== input) input.click(); });
  input.addEventListener('change', show);
  ['dragenter','dragover'].forEach(function(ev){
    dz.addEventListener(ev, function(e){ e.preventDefault(); dz.classList.add('dragover'); });
  });
  ['dragleave','drop'].forEach(function(ev){
    dz.addEventListener(ev, function(e){ e.preventDefault(); dz.classList.remove('dragover'); });
  });
  dz.addEventListener('drop', function(e){
    var files = e.dataTransfer && e.dataTransfer.files;
    if (files && files.length) { try { input.files = files; } catch (err) {} show(); }
  });
})();
</script>
"""


LANDING_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MR X Bot Hosting — One Panel. Unlimited Bots.</title>""" + CSS + """</head><body>
<header><div class="logo">MR X BOT HOSTING</div>
<nav><a class="ghost" target="_blank" rel="noopener" href="https://wa.me/{{ settings.whatsapp }}">💬 Support</a>
<a class="ghost" href="{{ url_for('login') }}">Login</a>
<a href="{{ url_for('register') }}">Register Free</a></nav></header>
<main>
<div class="hero">
  <div class="hud-rings"><span></span><span></span><span></span><div class="core"></div></div>
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

<div class="section" style="padding-top:0">
  <div class="stats">
    <div class="stat"><div class="num">{{ total_users }}</div><div class="lbl">Registered Users</div></div>
    <div class="stat"><div class="num">{{ total_bots }}</div><div class="lbl">Bots Hosted</div></div>
    <div class="stat"><div class="num">24/7</div><div class="lbl">Hosting Uptime</div></div>
  </div>
</div>

<div class="section">
  <h2>Why MR X Bot Hosting?</h2>
  <div class="grid">
    <div class="feature"><div class="icon">🚀</div><h3>One-Click Start</h3>
      <p>Add a bot in under a minute: name, a .py file or a full .zip project, token. Start, stop and restart with one tap.</p></div>
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
      <div class="price">Rs. {{ settings.price_2 }}</div>
      <div class="muted">for 2 days · with a license key</div>
      <ul>
        <li><b>Unlimited</b> bots for 2 days</li>
        <li>Start / stop / restart</li>
        <li>Live logs</li>
        <li>Auto-restart option</li>
      </ul>
      <a class="btn" target="_blank" rel="noopener" href="https://wa.me/{{ settings.whatsapp }}?text={{ ('Assalam-o-Alaikum! I want to buy a Premium key (2 Days - Rs. ' ~ settings.price_2 ~ ') for MR X Bot Hosting.')|urlencode }}">Get a Key</a>
    </div>
    <div class="price-card premium">
      <div class="planname">Premium</div>
      <div class="price">Rs. {{ settings.price_7 }}</div>
      <div class="muted">for 7 days · with a license key</div>
      <ul>
        <li><b>Unlimited</b> bots for 7 days</li>
        <li>Start / stop / restart</li>
        <li>Live logs</li>
        <li>Auto-restart option</li>
      </ul>
      <a class="btn" target="_blank" rel="noopener" href="https://wa.me/{{ settings.whatsapp }}?text={{ ('Assalam-o-Alaikum! I want to buy a Premium key (7 Days - Rs. ' ~ settings.price_7 ~ ') for MR X Bot Hosting.')|urlencode }}">Get a Key</a>
    </div>
    <div class="price-card premium">
      <div class="ribbon">MOST POPULAR</div>
      <div class="planname">Premium</div>
      <div class="price">Rs. {{ settings.price_30 }}</div>
      <div class="muted">for 30 days · with a license key</div>
      <ul>
        <li><b>Unlimited</b> bots for 30 days</li>
        <li>Start / stop / restart</li>
        <li>Live logs</li>
        <li>Auto-restart option</li>
        <li>Best value for serious sellers</li>
      </ul>
      <a class="btn" target="_blank" rel="noopener" href="https://wa.me/{{ settings.whatsapp }}?text={{ ('Assalam-o-Alaikum! I want to buy a Premium key (30 Days - Rs. ' ~ settings.price_30 ~ ') for MR X Bot Hosting.')|urlencode }}">Get a Key</a>
    </div>
  </div>
  <p class="muted" style="text-align:center;margin-top:22px">
  Premium keys are sold by the owner directly. Already bought a key? Register, log in,
  and open <b>Redeem Key</b> in your panel — Premium activates instantly.</p>
</div>

<div class="section">
  <h2>Frequently Asked Questions</h2>
  <div class="faq">
    <details><summary>How does MR X Bot Hosting work?</summary>
      <p>Register for a free account, then add your bot: upload a .py file (or a full .zip project), paste the token from @BotFather, and press Start. Your bot runs 24/7 from our panel — no server skills needed.</p></details>
    <details><summary>What files can I upload?</summary>
      <p>A single Python .py file, or a complete project as a .zip. ZIPs are extracted automatically, the entry file (main.py, bot.py, app.py or run.py) is detected for you, and requirements.txt is installed automatically.</p></details>
    <details><summary>How do I get Premium?</summary>
      <p>Tap "Get a Key" on a Premium plan to message us on WhatsApp and buy a license key. Then open Redeem Key in your panel and enter it — Premium (unlimited bots) activates instantly for 2, 7 or 30 days.</p></details>
    <details><summary>How do I pay?</summary>
      <p>Payment is taken by the owner directly on WhatsApp — JazzCash and EasyPaisa are accepted. Your key is sent right after payment.</p></details>
    <details><summary>Is my bot token safe?</summary>
      <p>Yes. Your token is stored only on the server, is passed to your bot as an environment variable, and is never shown again in the panel — only its last 4 characters. Use one token per bot.</p></details>
  </div>
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
{% if closed %}<div class="err">🚫 Registrations are currently closed. Please check back later, or contact us on WhatsApp for help.</div>{% else %}
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
{% endif %}
<p class="tagline">Already registered? <a href="{{ url_for('login') }}" style="color:#d4af37">Login</a></p>
<p class="muted" style="text-align:center">Free plan includes 2 bots. Premium = unlimited, with a license key.</p>
</div></div></main>
""" + FOOTER + """</body></html>"""

REDEEM_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Redeem Key — MR X Bot Hosting</title>""" + CSS + """</head><body>
<header><div class="logo">MR X BOT HOSTING</div>
<nav><a class="ghost" href="{{ url_for('dashboard') }}">← Dashboard</a>
<a class="ghost" href="{{ url_for('my_keys') }}">🎟️ My Keys</a>
<a class="ghost" href="{{ url_for('profile') }}">👤 Profile</a>
<a class="ghost" target="_blank" rel="noopener" href="https://wa.me/{{ settings.whatsapp }}">💬 Support</a>
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
<a class="ghost" href="{{ url_for('dashboard') }}">🤖 My Bots</a>
<a class="ghost" href="{{ url_for('my_keys') }}">🎟️ My Keys</a>
<a class="ghost" href="{{ url_for('profile') }}">👤 Profile</a>
<a class="ghost" href="{{ url_for('redeem') }}">💎 Redeem Key</a>
<a class="ghost" target="_blank" rel="noopener" href="https://wa.me/{{ settings.whatsapp }}">💬 Support</a>
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

{% if settings.announcement %}<div class="announce">📣 {{ settings.announcement|e }}</div>{% endif %}

<div class="stats">
  <div class="stat"><div class="num">{{ stats.total }}</div><div class="lbl">My Bots</div></div>
  <div class="stat"><div class="num">{{ stats.running }}</div><div class="lbl">Running</div></div>
  <div class="stat"><div class="num">{{ plan }}</div><div class="lbl">Current Plan</div></div>
  <div class="stat"><div class="num">{{ days_left if plan == 'Premium' else '—' }}</div><div class="lbl">Premium Days Left</div></div>
</div>
{% if plan == 'Premium' %}
<div class="card" style="padding:18px 26px">
  <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px">
    <b>💎 Premium active</b>
    <span class="muted">{{ days_left }} day(s) left · until {{ expiry }}</span>
  </div>
  <div class="pbar"><div style="width:{{ bar_pct }}%"></div></div>
</div>
{% endif %}

<div class="card">
<h2>💎 Get Premium — Buy a Key</h2>
<p class="muted" style="margin-top:0">Premium = <b style="color:#eee">Unlimited bots</b>. Pick a plan and tap
<b style="color:#eee">Get a Key</b> — you will be taken straight to our WhatsApp to buy your key.</p>
<div style="display:flex;gap:14px;flex-wrap:wrap">
  <div class="planbox">
    <div class="pname">PREMIUM · 2 DAYS</div>
    <div class="pprice">Rs. {{ settings.price_2 }}</div>
    <a class="btn" target="_blank" rel="noopener"
       href="https://wa.me/{{ settings.whatsapp }}?text={{ ('Assalam-o-Alaikum! I want to buy a Premium key (2 Days - Rs. ' ~ settings.price_2 ~ ') for MR X Bot Hosting. My username is: ' ~ user.username)|urlencode }}">Get a Key</a>
  </div>
  <div class="planbox">
    <div class="pname">PREMIUM · 7 DAYS</div>
    <div class="pprice">Rs. {{ settings.price_7 }}</div>
    <a class="btn" target="_blank" rel="noopener"
       href="https://wa.me/{{ settings.whatsapp }}?text={{ ('Assalam-o-Alaikum! I want to buy a Premium key (7 Days - Rs. ' ~ settings.price_7 ~ ') for MR X Bot Hosting. My username is: ' ~ user.username)|urlencode }}">Get a Key</a>
  </div>
  <div class="planbox">
    <div class="pname">PREMIUM · 30 DAYS</div>
    <div class="pprice">Rs. {{ settings.price_30 }}</div>
    <a class="btn" target="_blank" rel="noopener"
       href="https://wa.me/{{ settings.whatsapp }}?text={{ ('Assalam-o-Alaikum! I want to buy a Premium key (30 Days - Rs. ' ~ settings.price_30 ~ ') for MR X Bot Hosting. My username is: ' ~ user.username)|urlencode }}">Get a Key</a>
  </div>
</div>
<p class="muted" style="margin-bottom:0">Already bought a key? Open <b>Redeem Key</b> in the menu and enter it — Premium activates instantly.</p>
</div>

<div class="dash-grid">
<div class="card">
<h2>My Bots ({{ bots|length }})</h2>
{% if bots %}
<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px">
  <input type="text" id="botsearch" placeholder="🔎 Search my bots by name…" style="flex:1;min-width:180px;margin:0">
  <form class="inline" method="post" action="{{ url_for('start_all') }}"><button type="submit">▶ Start all</button></form>
  <form class="inline" method="post" action="{{ url_for('stop_all') }}"><button class="grey" type="submit">⏹ Stop all</button></form>
</div>
<div class="tablewrap">
<table>
<tr><th>Name</th><th>Status</th><th>Uptime</th><th>Memory</th><th>Restarts</th><th>Token</th><th>Auto-restart</th><th>Actions</th></tr>
{% for b in bots %}
<tr class="botrow" data-name="{{ b.name|lower|e }}">
  <td><b>{{ b.name|e }}</b><br><span class="muted">{{ b.file|e }}</span>{% if b.notes %}<br><span class="muted">📝 {{ b.notes|e }}</span>{% endif %}</td>
  <td><span class="badge {{ b.status }}">{{ b.status }}</span></td>
  <td>{{ b.uptime }}</td>
  <td>{{ b.memory }}</td>
  <td>{{ b.restart_count }}{% if b.last_crash %}<br><span class="muted">last crash: {{ b.last_crash }}</span>{% endif %}</td>
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
    <a class="btn grey" href="{{ url_for('edit_bot', bot_id=b.id) }}">Edit</a>
    <a class="btn grey" href="{{ url_for('download_bot', bot_id=b.id) }}">⬇ Get</a>
    <form class="inline" method="post" action="{{ url_for('duplicate_bot', bot_id=b.id) }}">
      <button class="grey" type="submit">⧉ Copy</button></form>
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
  <label>Bot file (.py) or a full project (.zip)</label>
""" + DROPZONE_HTML + """
  <label>Bot token (from @BotFather — stored on this server only, never shown again)</label>
  <input type="text" name="token" placeholder="123456:ABC... (optional if the token is inside your .py file)">
  <label><input type="checkbox" name="auto_restart"> Auto-restart if the bot crashes</label>
  <br><br>
  <button type="submit" style="padding:13px 30px;font-size:16.5px" {% if at_limit %}disabled{% endif %}>Add Bot</button>
</form>
<p class="muted">Bot file (.py) or a full project (.zip) — ZIPs are extracted automatically and requirements.txt is installed for you.<br>
Tip: your bot can read its token from the environment variable <b>BOT_TOKEN</b>.
Keep each bot's token separate — one token per bot, pasted only here.</p>
</div>
</div>

</div></main>
""" + FOOTER + """
<script>
// Bot search: filter the rows by name without reloading.
(function(){
  var bs = document.getElementById('botsearch');
  if (!bs) return;
  bs.addEventListener('input', function(){
    var q = bs.value.toLowerCase();
    document.querySelectorAll('.botrow').forEach(function(r){
      r.style.display = (r.getAttribute('data-name') || '').indexOf(q) >= 0 ? '' : 'none';
    });
  });
})();
// Refresh the bot list every 15s, but NEVER while the Add Bot form is in use
// (typing, a chosen file, or focus inside the form all pause the refresh).
setInterval(function(){
  var f = document.getElementById('addform');
  if (!f) { location.reload(); return; }
  var name = f.querySelector('input[name="name"]');
  var token = f.querySelector('input[name="token"]');
  var file = f.querySelector('input[name="botfile"]');
  var bs = document.getElementById('botsearch');
  var busy = (name && name.value) || (token && token.value) ||
             (file && file.files && file.files.length) ||
             (bs && (bs.value || document.activeElement === bs)) ||
             f.contains(document.activeElement);
  if (!busy) location.reload();
}, 15000);
</script>
""" + DROPZONE_JS + """</body></html>"""

LOG_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Logs — {{ name|e }}</title>""" + CSS + """</head><body>
<header><div class="logo">LOGS — {{ name|e }}</div>
<nav><a href="{{ back_url }}">{{ back_label }}</a></nav></header>
<main><div class="wrap">
<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px">
  <input type="text" id="logsearch" placeholder="🔎 Search in logs…" style="flex:1;min-width:180px;margin:0">
  <a class="btn grey" href="{{ download_url }}">⬇ Download log</a>
  <form class="inline" method="post" action="{{ clear_url }}" onsubmit="return confirm('Clear this log file?')"><button class="red" type="submit">🧹 Clear log</button></form>
</div>
<pre class="log" id="logbox">{{ log_text|e }}</pre>
<p class="muted">Auto-refreshes every 5 seconds (pauses while you search). Showing the last 300 lines.</p></div></main>
""" + FOOTER + """
<script>
(function(){
  var box = document.getElementById('logbox');
  var search = document.getElementById('logsearch');
  var original = box.textContent;
  var NL = String.fromCharCode(10);
  function apply(){
    var q = search.value.trim().toLowerCase();
    if (!q) { box.textContent = original; return; }
    var lines = original.split(NL).filter(function(l){ return l.toLowerCase().indexOf(q) >= 0; });
    box.textContent = lines.length ? lines.join(NL) : '(no lines match your search)';
  }
  search.addEventListener('input', apply);
  setInterval(function(){
    if (!search.value && document.activeElement !== search) location.reload();
  }, 5000);
})();
</script>
</body></html>"""


ADMIN_NAV = """<header><div class="logo">MR X — ADMIN</div>
<nav>
<a class="ghost" href="{{ url_for('admin_home') }}">📊 Stats</a>
<a class="ghost" href="{{ url_for('admin_users') }}">👥 Users</a>
<a class="ghost" href="{{ url_for('admin_keys') }}">🎟️ Keys</a>
<a class="ghost" href="{{ url_for('admin_bots') }}">🤖 All Bots</a>
<a class="ghost" href="{{ url_for('admin_settings') }}">⚙️ Settings</a>
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
  <div class="stat"><div class="num">{{ stats.disk }}</div><div class="lbl">Data Folder Size</div></div>
</div>
<div class="card">
<h2>Quick Actions</h2>
<a class="btn" href="{{ url_for('admin_keys') }}">🎟️ Generate License Keys</a>
<a class="btn grey" href="{{ url_for('admin_users') }}">👥 Manage Users</a>
<a class="btn grey" href="{{ url_for('admin_bots') }}">🤖 Manage All Bots</a>
<a class="btn grey" href="{{ url_for('admin_settings') }}">⚙️ Settings</a>
<form class="inline" method="post" action="{{ url_for('admin_stop_all') }}"><button class="red" type="submit">⏹ Stop ALL Bots</button></form>
<form class="inline" method="post" action="{{ url_for('admin_restart_all') }}"><button type="submit">🔁 Restart Running Bots</button></form>
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
<form method="get" style="display:flex;gap:10px;margin-bottom:16px">
  <input type="text" name="q" value="{{ q|e }}" placeholder="🔎 Search users by name, username or email…" style="flex:1;margin:0">
  <button type="submit">Search</button>
</form>
{% if flash %}<div class="{{ 'okmsg' if flash.kind == 'ok' else 'err' }}">{{ flash.msg }}</div>{% endif %}
<div class="card">
{% if users %}
<div class="tablewrap">
<table>
<tr><th>User</th><th>Plan</th><th>Premium Until</th><th>Bots</th><th>Joined</th><th>Status</th><th>Actions</th></tr>
{% for u in users %}
<tr>
  <td><a href="{{ url_for('admin_user_detail', user_id=u.id) }}"><b>{{ u.name|e }}</b></a><br><span class="muted">@{{ u.username|e }}{% if u.email %} · {{ u.email|e }}{% endif %}</span></td>
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
    {% if u.plan == 'Premium' %}
    <form class="inline" method="post" action="{{ url_for('admin_user_revoke_premium', user_id=u.id) }}"
          onsubmit="return confirm('Revoke Premium from this user right now?')">
      <button class="red" type="submit">Revoke Premium</button></form>
    {% endif %}
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
<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px">
  <a class="btn grey" href="{{ url_for('admin_keys_export') }}">⬇ Export Unused Keys (.txt)</a>
  <form class="inline" method="post" action="{{ url_for('admin_keys_delete_unused') }}" onsubmit="return confirm('Delete ALL unused keys? Used and revoked keys stay.')">
    <button class="red" type="submit">🗑 Delete All Unused Keys</button></form>
</div>
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
<form method="get" style="display:flex;gap:10px;margin-bottom:16px">
  <input type="text" name="q" value="{{ q|e }}" placeholder="🔎 Search bots by name or owner…" style="flex:1;margin:0">
  <button type="submit">Search</button>
</form>
{% if flash %}<div class="{{ 'okmsg' if flash.kind == 'ok' else 'err' }}">{{ flash.msg }}</div>{% endif %}
<div class="card">
{% if bots %}
<div class="tablewrap">
<table>
<tr><th>Bot</th><th>Owner</th><th>Status</th><th>Uptime</th><th>Memory</th><th>Restarts</th><th>Token</th><th>Auto-restart</th><th>Actions</th></tr>
{% for b in bots %}
<tr class="botrow" data-name="{{ b.name|lower|e }}">
  <td><b>{{ b.name|e }}</b><br><span class="muted">{{ b.file|e }}</span>{% if b.notes %}<br><span class="muted">📝 {{ b.notes|e }}</span>{% endif %}</td>
  <td>{{ b.owner_name|e }}</td>
  <td><span class="badge {{ b.status }}">{{ b.status }}</span></td>
  <td>{{ b.uptime }}</td>
  <td>{{ b.memory }}</td>
  <td>{{ b.restart_count }}{% if b.last_crash %}<br><span class="muted">last crash: {{ b.last_crash }}</span>{% endif %}</td>
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
    <a class="btn grey" href="{{ url_for('edit_bot', bot_id=b.id) }}">Edit</a>
    <a class="btn grey" href="{{ url_for('download_bot', bot_id=b.id) }}">⬇ Get</a>
    <form class="inline" method="post" action="{{ url_for('duplicate_bot', bot_id=b.id) }}">
      <button class="grey" type="submit">⧉ Copy</button></form>
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
  <label>Bot file (.py) or a full project (.zip)</label>
""" + DROPZONE_HTML + """
  <label>Bot token (from @BotFather — stored on this server only, never shown again)</label>
  <input type="text" name="token" placeholder="123456:ABC... (optional if the token is inside your .py file)">
  <label><input type="checkbox" name="auto_restart"> Auto-restart if the bot crashes</label>
  <br><br>
  <button type="submit" style="padding:13px 30px;font-size:16.5px">Add Bot</button>
</form>
<p class="muted">Bot file (.py) or a full project (.zip) — ZIPs are extracted automatically and requirements.txt is installed for you.
Bots added here belong to the admin (they are not tied to any user account and have no plan limit).</p>
</div>
</div></main>
""" + FOOTER + DROPZONE_JS + """</body></html>"""



BOT_EDIT_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Edit Bot — MR X Bot Hosting</title>""" + CSS + """</head><body>
""" + USER_HEADER + """
<main><div class="wrap">
<p><a class="btn grey" href="{{ back_url }}">← Back</a></p>
{% if flash %}<div class="{{ 'okmsg' if flash.kind == 'ok' else 'err' }}">{{ flash.msg }}</div>{% endif %}

<div class="card">
<h2>✏️ Edit Bot — {{ bot.name|e }}</h2>
<p class="muted" style="font-size:15px">Status: <span class="badge {{ bot.status }}">{{ bot.status }}</span>
 · File: <b style="color:#eee">{{ bot.file|e }}</b>
 · Token: <b style="color:#eee">{{ bot.token_masked }}</b></p>
<form method="post" action="{{ url_for('bot_details', bot_id=bot.id) }}">
  <label>Bot name</label>
  <input type="text" name="name" value="{{ bot.name|e }}" required>
  <label>Notes / description (only you see this)</label>
  <textarea name="notes" style="min-height:70px" placeholder="e.g. Main shop bot, do not stop">{{ bot.notes|e }}</textarea>
  <button type="submit">Save Details</button>
</form>
</div>

<div class="card">
<h2>📦 Replace Bot File / Project</h2>
<p class="muted" style="font-size:15px">Upload a new .py file or a full .zip project. The bot is
stopped first; ZIPs are extracted automatically and requirements.txt is installed for you.</p>
<form method="post" action="{{ url_for('bot_replace', bot_id=bot.id) }}" enctype="multipart/form-data">
""" + DROPZONE_HTML + """
  <button type="submit">Replace File</button>
</form>
</div>

<div class="card">
<h2>🔑 Update Bot Token</h2>
<p class="muted" style="font-size:15px">Current token: <b style="color:#eee">{{ bot.token_masked }}</b>.
Paste a new token from @BotFather to replace it (leave empty and save to remove the token).
Restart the bot afterwards to use the new token.</p>
<form method="post" action="{{ url_for('bot_token', bot_id=bot.id) }}">
  <label>New bot token</label>
  <input type="text" name="token" placeholder="123456:ABC...">
  <button type="submit">Save Token</button>
</form>
</div>

<div class="card">
<h2>🧪 Environment Variables</h2>
<p class="muted" style="font-size:15px">One variable per line, as <b>KEY=value</b>. These are passed
to your bot when it starts (read them with os.environ). BOT_TOKEN is managed by the token form above.
Restart the bot to apply changes.</p>
<form method="post" action="{{ url_for('bot_env', bot_id=bot.id) }}">
  <textarea name="env" style="font-family:Consolas,monospace;min-height:130px"
    placeholder="MY_FLAG=hello&#10;MODE=production">{{ extras|e }}</textarea>
  <button type="submit">Save Variables</button>
</form>
</div>

</div></main>
""" + FOOTER + DROPZONE_JS + """</body></html>"""

PROFILE_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>My Profile — MR X Bot Hosting</title>""" + CSS + """</head><body>
""" + USER_HEADER + """
<main><div class="wrap" style="max-width:760px">
{% if flash %}<div class="{{ 'okmsg' if flash.kind == 'ok' else 'err' }}">{{ flash.msg }}</div>{% endif %}

<div class="card">
<h2>👤 My Profile</h2>
<p class="muted" style="font-size:15px">Username: <b style="color:#eee">@{{ user.username|e }}</b>
 · Plan: <span class="badge {{ 'plan-premium' if plan == 'Premium' else 'plan-free' }}">{{ plan }}</span></p>
<form method="post" action="{{ url_for('profile_details') }}">
  <label>Display name</label>
  <input type="text" name="name" value="{{ user.name|e }}" required>
  <label>Email</label>
  <input type="email" name="email" value="{{ user.email|e }}">
  <button type="submit">Save Profile</button>
</form>
</div>

<div class="card">
<h2>🔒 Change Password</h2>
<form method="post" action="{{ url_for('profile_password') }}">
  <label>Current password</label>
  <input type="password" name="current_password" required autocomplete="current-password">
  <label>New password (min 6 characters)</label>
  <input type="password" name="new_password" required autocomplete="new-password">
  <button type="submit">Change Password</button>
</form>
</div>

<div class="card" style="border-color:rgba(248,113,113,.5)">
<h2 style="color:#f87171">⚠️ Delete My Account</h2>
<p class="muted" style="font-size:15px">This permanently deletes your account and ALL your bots.
It cannot be undone. Enter your password to confirm.</p>
<form method="post" action="{{ url_for('profile_delete') }}"
      onsubmit="return confirm('Delete your account and all your bots forever?')">
  <label>Your password</label>
  <input type="password" name="password" required autocomplete="current-password">
  <button class="red" type="submit">Delete My Account</button>
</form>
</div>

</div></main>
""" + FOOTER + """</body></html>"""

MYKEYS_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>My Keys — MR X Bot Hosting</title>""" + CSS + """</head><body>
""" + USER_HEADER + """
<main><div class="wrap">
{% if flash %}<div class="{{ 'okmsg' if flash.kind == 'ok' else 'err' }}">{{ flash.msg }}</div>{% endif %}
<div class="card">
<h2>🎟️ My Redeemed Keys</h2>
{% if keys %}
<div class="tablewrap">
<table>
<tr><th>Key</th><th>Duration</th><th>Redeemed On</th><th>Premium Until (then)</th></tr>
{% for k in keys %}
<tr>
  <td><code class="key">MRX-••••••-••••••-{{ k.masked }}</code></td>
  <td>{{ k.duration }} days</td>
  <td>{{ k.used_at }}</td>
  <td>{{ k.expiry }}</td>
</tr>
{% endfor %}
</table>
</div>
<p class="muted">Full key codes are never shown again — only the last 4 characters.</p>
{% else %}
<p class="muted">You have not redeemed any keys yet. Buy a key on WhatsApp and redeem it from
<b>Redeem Key</b> in the menu.</p>
{% endif %}
</div>
</div></main>
""" + FOOTER + """</body></html>"""

ADMIN_SETTINGS_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin Settings — MR X Bot Hosting</title>""" + CSS + """</head><body>
""" + ADMIN_NAV + """
<main><div class="wrap" style="max-width:820px">
<h2 style="font-size:26px">⚙️ Settings</h2>
{% if flash %}<div class="{{ 'okmsg' if flash.kind == 'ok' else 'err' }}">{{ flash.msg }}</div>{% endif %}
<div class="card">
<form method="post" action="{{ url_for('admin_settings') }}">
  <label>WhatsApp number for key sales &amp; support (with country code, digits only)</label>
  <input type="text" name="whatsapp" value="{{ settings.whatsapp|e }}">
  <label>Premium price — 2 days (Rs.)</label>
  <input type="number" name="price_2" value="{{ settings.price_2 }}" min="0">
  <label>Premium price — 7 days (Rs.)</label>
  <input type="number" name="price_7" value="{{ settings.price_7 }}" min="0">
  <label>Premium price — 30 days (Rs.)</label>
  <input type="number" name="price_30" value="{{ settings.price_30 }}" min="0">
  <label>Free plan bot limit (bots per free user)</label>
  <input type="number" name="free_limit" value="{{ settings.free_limit }}" min="0" max="1000">
  <label>Announcement banner (shown on every user dashboard — leave empty to hide)</label>
  <textarea name="announcement" style="min-height:70px">{{ settings.announcement|e }}</textarea>
  <label><input type="checkbox" name="maintenance" {% if settings.maintenance %}checked{% endif %}>
    Maintenance mode (visitors &amp; users see an "Under Maintenance" page; admin still works)</label>
  <br>
  <label><input type="checkbox" name="registration_open" {% if settings.registration_open %}checked{% endif %}>
    Registrations open (untick to close new sign-ups)</label>
  <br><br>
  <button type="submit" style="padding:13px 30px;font-size:15px">Save Settings</button>
</form>
<p class="muted">Prices, the WhatsApp number and the free limit update the landing page,
the dashboard Get Premium card and all support buttons immediately.</p>
</div>
</div></main>
""" + FOOTER + """</body></html>"""

ADMIN_USER_DETAIL_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>User — MR X Bot Hosting Admin</title>""" + CSS + """</head><body>
""" + ADMIN_NAV + """
<main><div class="wrap">
<p><a class="btn grey" href="{{ url_for('admin_users') }}">← All Users</a></p>
{% if flash %}<div class="{{ 'okmsg' if flash.kind == 'ok' else 'err' }}">{{ flash.msg }}</div>{% endif %}
<div class="card">
<h2>👤 {{ u.name|e }} <span class="muted" style="font-size:15px">@{{ u.username|e }}</span></h2>
<table>
<tr><th style="width:210px">Email</th><td>{{ u.email|e or '—' }}</td></tr>
<tr><th>Joined</th><td>{{ u.joined }}</td></tr>
<tr><th>Plan</th><td><span class="badge {{ 'plan-premium' if u.plan == 'Premium' else 'plan-free' }}">{{ u.plan }}</span></td></tr>
<tr><th>Premium Until</th><td>{{ u.expiry }}</td></tr>
<tr><th>Status</th><td>{% if u.banned %}<span class="badge banned">banned</span>{% else %}<span class="badge running">active</span>{% endif %}</td></tr>
</table>
</div>

<div class="card">
<h2>🤖 Their Bots ({{ bots|length }})</h2>
{% if bots %}
<div class="tablewrap"><table>
<tr><th>Name</th><th>File</th><th>Status</th><th>Uptime</th></tr>
{% for b in bots %}
<tr><td><b>{{ b.name|e }}</b></td><td class="muted">{{ b.file|e }}</td>
<td><span class="badge {{ b.status }}">{{ b.status }}</span></td><td>{{ b.uptime }}</td></tr>
{% endfor %}
</table></div>
{% else %}<p class="muted">This user has no bots.</p>{% endif %}
</div>

<div class="card">
<h2>🎟️ Keys They Redeemed ({{ keys|length }})</h2>
{% if keys %}
<div class="tablewrap"><table>
<tr><th>Key</th><th>Duration</th><th>Redeemed On</th></tr>
{% for k in keys %}
<tr><td><code class="key">MRX-••••••-••••••-{{ k.masked }}</code></td>
<td>{{ k.duration }} days</td><td>{{ k.used_at }}</td></tr>
{% endfor %}
</table></div>
{% else %}<p class="muted">No keys redeemed by this user.</p>{% endif %}
</div>
</div></main>
""" + FOOTER + """</body></html>"""

MAINTENANCE_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Under Maintenance — MR X Bot Hosting</title>""" + CSS + """</head><body>
<main><div class="wrap center" style="text-align:center">
<div class="hud-rings"><span></span><span></span><span></span><div class="core"></div></div>
<div class="card">
<h2 style="font-size:26px">🛠️ UNDER MAINTENANCE</h2>
<p style="font-size:18px;color:#b9cbdd;line-height:1.6">MR X Bot Hosting is being upgraded right now.
Please check back in a little while — your bots and accounts are safe.</p>
<p class="muted">Admin? <a href="{{ url_for('login') }}">Log in here</a></p>
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
    return render_template_string(
        LANDING_HTML, total_users=len(load_users()["users"]),
        total_bots=len(load_db()["bots"]))


@app.route("/register", methods=["GET", "POST"])
def register():
    if is_admin():
        return redirect(url_for("admin_home"))
    if current_user() is not None:
        return redirect(url_for("dashboard"))
    if not load_settings().get("registration_open", True):
        return render_template_string(REGISTER_HTML, error=None, closed=True,
                                      name="", username="", email="")
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
    return render_template_string(REGISTER_HTML, error=error, closed=False,
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
        b["memory"] = bot_memory(bot)
        b["restart_count"] = bot.get("restart_count") or 0
        b["last_crash"] = (fmt_datetime(bot.get("last_crash"))
                           if bot.get("last_crash") else "")
        b["notes"] = bot.get("notes") or ""
        bots.append(b)
    limit = user_bot_limit(user)
    at_limit = len(bots) >= limit
    limit_msg = ""
    if at_limit and not user_is_premium(user):
        limit_msg = ("Free plan limit reached (%d bots) — redeem a Premium key "
                     "for unlimited bots." % free_bot_limit())
    elif at_limit:
        limit_msg = "Bot limit reached (%d bots)." % limit
    now = int(time.time())
    premium = user_is_premium(user)
    days_left = 0
    bar_pct = 0
    if premium:
        days_left = max(0, (int(user.get("premium_until") or 0)
                            - now + 86399) // 86400)
        bar_pct = max(4, min(100, days_left * 100 // 30))
    return {
        "user": user,
        "bots": bots,
        "plan": user_plan_name(user),
        "expiry": fmt_date(user.get("premium_until")),
        "limit_label": "Unlimited" if premium else str(free_bot_limit()),
        "at_limit": at_limit,
        "limit_msg": limit_msg,
        "stats": {"total": len(bots),
                  "running": sum(1 for b in bots if b["status"] == "running")},
        "days_left": days_left,
        "bar_pct": bar_pct,
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
                rec["resulting_expiry"] = new_expiry
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
        abort(400, "Bot name and a .py or .zip file are required.")
    filename = secure_filename(upload.filename)
    if not (filename.lower().endswith(".py")
            or filename.lower().endswith(".zip")):
        abort(400, "Only .py or .zip files are allowed.")
    bot_id = "%s-%s" % (secrets.token_hex(3), int(time.time()))
    os.makedirs(bot_dir(bot_id), exist_ok=True)
    entry_rel, is_zip, error = _store_bot_upload(bot_id, upload)
    if error:
        shutil.rmtree(bot_dir(bot_id), ignore_errors=True)
        flash(error, "err")
        return _after_action()
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
            "file": entry_rel,
            "kind": "zip" if is_zip else "py",
            "notes": "",
            "restart_count": 0,
            "last_crash": None,
            "token_masked": masked,
            "auto_restart": auto_restart,
            "desired_running": False,
            "created": int(time.time()),
        })
        save_db(db)
    _log_line(bot_id, "[panel] bot '%s' added (%s)" % (name, entry_rel))
    if is_zip:
        _log_line(bot_id, "[panel] ZIP project extracted; entry file: %s" % entry_rel)
        install_requirements(bot_id, entry_rel)
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
    if start_bot(bot_id):
        update_bot(bot_id, restart_count=(bot.get("restart_count") or 0) + 1)
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
    return render_template_string(
        LOG_HTML, name=bot["name"], log_text=text,
        back_url=back_url, back_label=back_label,
        download_url=url_for("logs_download", bot_id=bot_id),
        clear_url=url_for("logs_clear", bot_id=bot_id))



# --------------------------------------------------------------------------
# Routes — profile, my keys, bot editing, extras (v3)
# --------------------------------------------------------------------------
@app.route("/profile")
@user_required
def profile():
    user = current_user()
    return render_template_string(PROFILE_HTML, user=user,
                                  plan=user_plan_name(user),
                                  flash=pop_flash())


@app.route("/profile/details", methods=["POST"])
@user_required
def profile_details():
    user = current_user()
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip()
    if not name:
        flash("Name cannot be empty.", "err")
        return redirect(url_for("profile"))
    with _store_lock:
        data = load_users()
        for u in data["users"]:
            if u["id"] == user["id"]:
                u["name"] = name
                u["email"] = email
        save_users(data)
    flash("Profile updated.")
    return redirect(url_for("profile"))


@app.route("/profile/password", methods=["POST"])
@user_required
def profile_password():
    user = current_user()
    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    if not check_password_hash(user["password_hash"], current):
        flash("Current password is wrong.", "err")
        return redirect(url_for("profile"))
    if len(new) < 6:
        flash("New password must be at least 6 characters.", "err")
        return redirect(url_for("profile"))
    with _store_lock:
        data = load_users()
        for u in data["users"]:
            if u["id"] == user["id"]:
                u["password_hash"] = generate_password_hash(new)
        save_users(data)
    flash("Password changed. Use the new password next time you log in.")
    return redirect(url_for("profile"))


@app.route("/profile/delete", methods=["POST"])
@user_required
def profile_delete():
    user = current_user()
    pw = request.form.get("password") or ""
    if not check_password_hash(user["password_hash"], pw):
        flash("Password is wrong — account not deleted.", "err")
        return redirect(url_for("profile"))
    for bot in list(user_bots(user["id"])):
        delete_bot_everywhere(bot["id"])
    with _store_lock:
        data = load_users()
        data["users"] = [u for u in data["users"] if u["id"] != user["id"]]
        save_users(data)
    session.clear()
    return redirect(url_for("home"))


@app.route("/my-keys")
@user_required
def my_keys():
    user = current_user()
    rows = []
    for k in load_keys()["keys"]:
        if k.get("used_by") == user["id"]:
            rows.append({
                "masked": k["code"][-4:],
                "duration": k["duration_days"],
                "used_at": fmt_datetime(k.get("used_at")),
                "expiry": (fmt_date(k.get("resulting_expiry"))
                           if k.get("resulting_expiry") else "—"),
            })
    rows.reverse()
    return render_template_string(MYKEYS_HTML, keys=rows, flash=pop_flash())


def _bot_or_404(bot_id):
    bot = get_bot(bot_id)
    if not can_access_bot(bot):
        abort(404)
    return bot


@app.route("/bot/<bot_id>/edit")
@login_required
def edit_bot(bot_id):
    bot = _bot_or_404(bot_id)
    env = read_bot_env(bot_id)
    extras = "\n".join("%s=%s" % (k, env[k]) for k in sorted(env)
                       if k != "BOT_TOKEN")
    b = dict(bot)
    b["status"] = bot_status(bot)
    b["notes"] = bot.get("notes") or ""
    if is_admin():
        back_url = url_for("admin_bots")
    else:
        back_url = url_for("dashboard")
    return render_template_string(BOT_EDIT_HTML, bot=b, extras=extras,
                                  back_url=back_url, flash=pop_flash())


@app.route("/bot/<bot_id>/details", methods=["POST"])
@login_required
def bot_details(bot_id):
    _bot_or_404(bot_id)
    name = (request.form.get("name") or "").strip()
    notes = (request.form.get("notes") or "").strip()[:500]
    if not name:
        flash("Bot name cannot be empty.", "err")
        return redirect(url_for("edit_bot", bot_id=bot_id))
    update_bot(bot_id, name=name, notes=notes)
    flash("Bot details saved.")
    return redirect(url_for("edit_bot", bot_id=bot_id))


@app.route("/bot/<bot_id>/replace", methods=["POST"])
@login_required
def bot_replace(bot_id):
    bot = _bot_or_404(bot_id)
    upload = request.files.get("botfile")
    if upload is None or not upload.filename:
        flash("Choose a .py or .zip file first.", "err")
        return redirect(url_for("edit_bot", bot_id=bot_id))
    filename = secure_filename(upload.filename)
    if not (filename.lower().endswith(".py")
            or filename.lower().endswith(".zip")):
        flash("Only .py or .zip files are allowed.", "err")
        return redirect(url_for("edit_bot", bot_id=bot_id))
    stop_bot(bot_id)
    bdir = bot_dir(bot_id)
    # Clear the previous project's files before storing the new upload.
    shutil.rmtree(os.path.join(bdir, "project"), ignore_errors=True)
    old_rel = bot["file"]
    if os.path.dirname(old_rel) == "":
        old_path = os.path.join(bdir, old_rel)
        if os.path.exists(old_path):
            try:
                os.remove(old_path)
            except OSError:
                pass
    entry_rel, is_zip, error = _store_bot_upload(bot_id, upload)
    if error:
        flash(error, "err")
        return redirect(url_for("edit_bot", bot_id=bot_id))
    update_bot(bot_id, file=entry_rel, kind="zip" if is_zip else "py",
               desired_running=False)
    _log_line(bot_id, "[panel] bot file replaced; entry file: %s" % entry_rel)
    if is_zip:
        install_requirements(bot_id, entry_rel)
    flash("Bot file replaced. Press Start to run the new version.")
    return redirect(url_for("edit_bot", bot_id=bot_id))


@app.route("/bot/<bot_id>/token", methods=["POST"])
@login_required
def bot_token(bot_id):
    _bot_or_404(bot_id)
    token = (request.form.get("token") or "").strip()
    env = read_bot_env(bot_id)
    if token:
        env["BOT_TOKEN"] = token
    else:
        env.pop("BOT_TOKEN", None)
    write_bot_env(bot_id, env)
    masked = ("••••" + token[-4:]) if len(token) >= 4 else ("••••" if token else "—")
    update_bot(bot_id, token_masked=masked)
    flash("Bot token updated. Restart the bot to use the new token.")
    return redirect(url_for("edit_bot", bot_id=bot_id))


@app.route("/bot/<bot_id>/env", methods=["POST"])
@login_required
def bot_env(bot_id):
    _bot_or_404(bot_id)
    raw = request.form.get("env") or ""
    extras = {}
    bad = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            bad.append(line)
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k == "BOT_TOKEN":
            continue  # managed by the token form, never overwritten here
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
            bad.append(line)
            continue
        extras[k] = v
    if bad:
        flash("These lines were skipped (use KEY=value, one per line): %s"
              % ", ".join(bad[:3]), "err")
        return redirect(url_for("edit_bot", bot_id=bot_id))
    env = read_bot_env(bot_id)
    new_env = {}
    if env.get("BOT_TOKEN"):
        new_env["BOT_TOKEN"] = env["BOT_TOKEN"]
    new_env.update(extras)
    write_bot_env(bot_id, new_env)
    flash("Environment variables saved. Restart the bot to apply them.")
    return redirect(url_for("edit_bot", bot_id=bot_id))


@app.route("/bot/<bot_id>/duplicate", methods=["POST"])
@login_required
def duplicate_bot(bot_id):
    bot = _bot_or_404(bot_id)
    if not is_admin():
        user = current_user()
        if len(user_bots(user["id"])) >= user_bot_limit(user):
            if user_is_premium(user):
                flash("Bot limit reached (%d bots)." % user_bot_limit(user), "err")
            else:
                flash("Free plan limit reached — redeem a Premium key for "
                      "unlimited bots.", "err")
            return redirect(url_for("dashboard"))
    new_id = "%s-%s" % (secrets.token_hex(3), int(time.time()))
    shutil.copytree(bot_dir(bot_id), bot_dir(new_id))
    rec = dict(bot)
    rec.update({
        "id": new_id,
        "name": (bot["name"] + " copy")[:80],
        "desired_running": False,
        "restart_count": 0,
        "last_crash": None,
        "created": int(time.time()),
    })
    with _db_lock:
        db = load_db()
        db["bots"].append(rec)
        save_db(db)
    _log_line(new_id, "[panel] bot duplicated from '%s'" % bot["name"])
    flash("Bot duplicated as '%s' (stopped)." % rec["name"])
    return _after_action()


@app.route("/bot/<bot_id>/download")
@login_required
def download_bot(bot_id):
    bot = _bot_or_404(bot_id)
    bdir = bot_dir(bot_id)
    is_zip_bot = (bot.get("kind") == "zip"
                  or os.path.isdir(os.path.join(bdir, "project")))
    if is_zip_bot:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for dirpath, dirnames, filenames in os.walk(bdir):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for fn in filenames:
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, bdir)
                    if rel == "config.env":
                        continue  # never ship the bot's token
                    z.write(full, rel)
        buf.seek(0)
        return send_file(buf, as_attachment=True,
                         download_name=secure_filename(bot["name"]) + ".zip",
                         mimetype="application/zip")
    script = os.path.join(bdir, bot["file"])
    if not os.path.exists(script):
        abort(404)
    return send_file(script, as_attachment=True,
                     download_name=os.path.basename(bot["file"]))


@app.route("/bot/<bot_id>/logs/download")
@login_required
def logs_download(bot_id):
    bot = _bot_or_404(bot_id)
    path = bot_log_path(bot_id)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True,
                     download_name=secure_filename(bot["name"]) + "-log.txt")


@app.route("/bot/<bot_id>/logs/clear", methods=["POST"])
@login_required
def logs_clear(bot_id):
    _bot_or_404(bot_id)
    with open(bot_log_path(bot_id), "w") as f:
        f.write("[panel] log cleared\n")
    return redirect(url_for("logs", bot_id=bot_id))


@app.route("/bots/start-all", methods=["POST"])
@user_required
def start_all():
    user = current_user()
    n = 0
    for bot in user_bots(user["id"]):
        if start_bot(bot["id"]):
            n += 1
    flash("Started %d bot(s)." % n)
    return redirect(url_for("dashboard"))


@app.route("/bots/stop-all", methods=["POST"])
@user_required
def stop_all():
    user = current_user()
    for bot in user_bots(user["id"]):
        stop_bot(bot["id"])
    flash("All your bots were stopped.")
    return redirect(url_for("dashboard"))


@app.route("/api/my-bots")
@login_required
def api_my_bots():
    if is_admin():
        bots = load_db()["bots"]
    else:
        bots = user_bots(current_user()["id"])
    out = []
    for bot in bots:
        out.append({
            "name": bot["name"],
            "status": bot_status(bot),
            "uptime": bot_uptime(bot),
            "memory": bot_memory(bot),
        })
    return jsonify({"bots": out})


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
        "disk": fmt_size(_dir_size(DATA_DIR)),
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
    q = (request.args.get("q") or "").strip().lower()
    if q:
        users = [u for u in users
                 if q in (u.get("name") or "").lower()
                 or q in (u.get("username") or "").lower()
                 or q in (u.get("email") or "").lower()]
    return render_template_string(ADMIN_USERS_HTML, users=users, q=q,
                                  flash=pop_flash())


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
        b["memory"] = bot_memory(bot)
        b["restart_count"] = bot.get("restart_count") or 0
        b["last_crash"] = (fmt_datetime(bot.get("last_crash"))
                           if bot.get("last_crash") else "")
        b["notes"] = bot.get("notes") or ""
        owner = users_by_id.get(bot.get("owner") or "")
        b["owner_name"] = ("@" + owner["username"]) if owner else "Admin (ownerless)"
        bots.append(b)
    q = (request.args.get("q") or "").strip().lower()
    if q:
        bots = [b for b in bots
                if q in (b.get("name") or "").lower()
                or q in (b.get("owner_name") or "").lower()]
    return render_template_string(ADMIN_BOTS_HTML, bots=bots, q=q,
                                  flash=pop_flash())



# --------------------------------------------------------------------------
# Routes — admin extras (v3): settings, user detail, key tools, global ops
# --------------------------------------------------------------------------
@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    s = load_settings()
    if request.method == "POST":
        wa = "".join(c for c in (request.form.get("whatsapp") or "")
                     if c.isdigit())

        def _int(name, default, lo, hi):
            try:
                v = int(request.form.get(name) or default)
            except (TypeError, ValueError):
                v = default
            return max(lo, min(hi, v))

        s["whatsapp"] = wa or DEFAULT_SETTINGS["whatsapp"]
        s["price_2"] = _int("price_2", s["price_2"], 0, 1000000)
        s["price_7"] = _int("price_7", s["price_7"], 0, 1000000)
        s["price_30"] = _int("price_30", s["price_30"], 0, 1000000)
        s["free_limit"] = _int("free_limit", s["free_limit"], 0, 1000)
        s["announcement"] = (request.form.get("announcement") or "").strip()[:300]
        s["maintenance"] = bool(request.form.get("maintenance"))
        s["registration_open"] = bool(request.form.get("registration_open"))
        save_settings(s)
        flash("Settings saved.")
        return redirect(url_for("admin_settings"))
    return render_template_string(ADMIN_SETTINGS_HTML, settings=s,
                                  flash=pop_flash())


@app.route("/admin/users/<user_id>")
@admin_required
def admin_user_detail(user_id):
    user = get_user(user_id)
    if user is None:
        abort(404)
    u = dict(user)
    u["plan"] = user_plan_name(user)
    u["expiry"] = (fmt_date(user.get("premium_until"))
                   if user_is_premium(user) else "—")
    u["joined"] = fmt_datetime(user.get("created"))
    bots = []
    for bot in user_bots(user_id):
        b = dict(bot)
        b["status"] = bot_status(bot)
        b["uptime"] = bot_uptime(bot)
        bots.append(b)
    keys = []
    for k in load_keys()["keys"]:
        if k.get("used_by") == user_id:
            keys.append({
                "masked": k["code"][-4:],
                "duration": k["duration_days"],
                "used_at": fmt_datetime(k.get("used_at")),
            })
    return render_template_string(ADMIN_USER_DETAIL_HTML, u=u, bots=bots,
                                  keys=keys, flash=pop_flash())


@app.route("/admin/users/<user_id>/revoke-premium", methods=["POST"])
@admin_required
def admin_user_revoke_premium(user_id):
    with _store_lock:
        data = load_users()
        for u in data["users"]:
            if u["id"] == user_id:
                u["premium_until"] = int(time.time())
                flash("Premium revoked from @%s." % u["username"])
        save_users(data)
    return redirect(url_for("admin_users"))


@app.route("/admin/keys/export")
@admin_required
def admin_keys_export():
    codes = [k["code"] for k in load_keys()["keys"] if k["status"] == "unused"]
    body = "\n".join(codes) + ("\n" if codes else "")
    return send_file(io.BytesIO(body.encode()), as_attachment=True,
                     download_name="mrx-unused-keys.txt",
                     mimetype="text/plain")


@app.route("/admin/keys/delete-unused", methods=["POST"])
@admin_required
def admin_keys_delete_unused():
    with _store_lock:
        data = load_keys()
        before = len(data["keys"])
        data["keys"] = [k for k in data["keys"] if k["status"] != "unused"]
        removed = before - len(data["keys"])
        save_keys(data)
    flash("Deleted %d unused key(s)." % removed)
    return redirect(url_for("admin_keys"))


@app.route("/admin/stop-all", methods=["POST"])
@admin_required
def admin_stop_all():
    for bot in load_db()["bots"]:
        stop_bot(bot["id"])
    flash("All bots on the server were stopped.")
    return redirect(url_for("admin_home"))


@app.route("/admin/restart-all", methods=["POST"])
@admin_required
def admin_restart_all():
    n = 0
    for bot in load_db()["bots"]:
        if bot_status(bot) == "running":
            stop_bot(bot["id"], mark_stopped=False)
            if start_bot(bot["id"]):
                n += 1
    flash("Restarted %d running bot(s)." % n)
    return redirect(url_for("admin_home"))


@app.route("/health")
def health():
    return "ok"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print("=" * 56, flush=True)
    print("  MR X BOT HOSTING PANEL v3", flush=True)
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
