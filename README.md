# MR X Bot Hosting Panel — v2 (SaaS Edition)

A complete, sellable bot-hosting website. Visitors see a professional
landing page, register for a free account, and host Telegram bots from
their own panel. Premium is unlocked with **license keys** that you (the
owner) generate and sell.

> One honest line: "unlimited bots" means unlimited bots in the panel.
> How many can run at the same time depends on your host's RAM, and free
> hosts sleep when idle / limit free hours — so on a free host plan, only
> a few bots per user will truly stay up 24/7.

---

## Plans

| Plan | Bots | How to get it |
|---|---|---|
| **Free** | 2 bots | Just register |
| **Premium — 2 Days** | Unlimited | Redeem a 2-day key |
| **Premium — 7 Days** | Unlimited | Redeem a 7-day key |
| **Premium — 30 Days** | Unlimited | Redeem a 30-day key |

Premium durations are set by the key. When a Premium user redeems
another key, the new days are **added on top** of their current expiry.

## License keys

- Format: `MRX-XXXXXX-XXXXXX-XXXXXX`
- Each key = Premium for a set number of days (presets: **2 / 7 / 30
  days**, or any custom number of days).
- A key can be redeemed **once**. States: `unused` → `used` (by whom,
  when) or `revoked` (can never be redeemed).
- **User flow:** Register → Login → **🎟️ Redeem Key** → paste key →
  Premium activates instantly, expiry date shows in their dashboard.

## Admin panel (the owner — that's you)

**How to log in:** open the site → **Login** → username **`admin`**,
password = your **`PANEL_PASSWORD`** (the same environment variable from
v1 — it did not change).

Admin can:

- **📊 Stats** — total users, total bots, running bots, keys unused/used,
  premium users.
- **👥 Users** — see every user (plan, Premium expiry, bot count, joined
  date) and: **Ban / Unban** (a banned user cannot log in), **Delete**
  (removes the user and all their bots), **Grant Premium** (add days
  directly, no key needed).
- **🎟️ Keys** — generate keys (choose 2 / 7 / 30 days or a custom
  duration, quantity 1–20). New keys are shown once in a copyable list —
  send one key to each buyer. The table shows every key's status, and
  unused keys can be **Revoked**.
- **🤖 All Bots** — every bot on the server with its owner, status,
  Start / Stop / Restart / Delete, logs. Bots you add here belong to the
  admin and have no plan limit.

### How selling works

1. A buyer registers on your site (Free plan).
2. You generate a key with the duration they paid for (2 / 7 / 30 days).
3. You send them the key (WhatsApp/Telegram — your choice).
4. They paste it in **Redeem Key** → Premium is live instantly.
5. When it expires, they drop back to Free (their bots stay, but they
   can't add new ones beyond the Free limit until they renew).

---

## What you need first

1. A **panel/admin password** that you choose yourself — set it as the
   environment variable `PANEL_PASSWORD` (see deploy steps). It is now
   the **admin** password (login username: `admin`).
2. For every bot: a **bot token** from Telegram's **@BotFather**
   (`/newbot`). One token per bot. Users paste each token **only** into
   that bot's own Add Bot form — never into another bot, never into the
   password field, never in a chat.

If you forget to set `PANEL_PASSWORD`, the panel generates one and
prints it in the host's **Logs** at first start — but setting your own
is better.

---

## Option A — Deploy on Railway (easiest)

1. Put this project in a GitHub repo (upload all files from this folder),
   or use Railway's "Deploy from GitHub".
2. Go to **railway.app** → sign in → **New Project** → **Deploy from
   GitHub repo** → pick this repo.
3. Open your service → **Variables** tab → add:
   - `PANEL_PASSWORD` = your own password (you choose it)
   Railway sets `PORT` itself — nothing else to change.
4. Open **Settings → Networking → Generate Domain** to get your URL.
5. Open the URL — you will see the public landing page. Log in as
   `admin` with your `PANEL_PASSWORD` to open the admin panel. Deployed ✅

   *(Updating from v1? Just replace `app.py` in the repo — Railway
   redeploys automatically. Your `PANEL_PASSWORD` and existing bots keep
   working; old bots show up in the admin's All Bots page.)*

## Option B — Deploy on Render

1. Put this project in a GitHub repo.
2. Go to **render.com** → sign in → **New → Web Service** → connect the
   repo. (This folder has a `render.yaml`, so Render can also do
   **New → Blueprint** and fill everything in for you.)
3. Settings Render should show:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python app.py`
4. **Environment** tab → add:
   - `PANEL_PASSWORD` = your own password
5. Click **Create Web Service**, wait for the build, open the URL.

## Option C — Run on PythonAnywhere

1. Sign up at **pythonanywhere.com**, open a **Bash console**:
   ```
   pip install --user flask
   ```
2. Upload this project's files (Files tab) into a folder, e.g.
   `mrx_bot_hosting`.
3. Go to the **Web** tab → **Add a new web app** → **Manual configuration**
   → Python 3.12.
4. Open the WSGI file PythonAnywhere gives you and replace its contents
   with:
   ```python
   import sys
   sys.path.insert(0, '/home/YOUR_USERNAME/mrx_bot_hosting')
   from app import app as application
   ```
   (use your real PythonAnywhere username and folder path)
5. In the Web tab → **Environment variables**, add
   `PANEL_PASSWORD` = your own password. Reload the web app.
6. Open your `YOUR_USERNAME.pythonanywhere.com` URL.

   Note: PythonAnywhere's free plan is made for websites, not always-on
   background bots — bots may stop when the web app reloads. Railway or
   Render work better for 24/7 bots.

---

## Add your first bot (2 minutes)

1. In Telegram, open **@BotFather** → `/newbot` → follow the steps → copy
   the token he gives you.
2. Open the site → **Register** (or log in as admin and use All Bots).
3. In **Add a Bot**:
   - **Bot name:** anything, e.g. `Test Echo Bot`
   - **Bot file:** choose `sample_bot.py` (included in this project) to
     test, or your own bot's `.py` file.
   - **Bot token:** paste the token from BotFather.
4. Press **Add Bot**, then press **Start** next to it.
5. Status turns **running**. Open the bot in Telegram, send `/start` —
   it replies. In the panel, **Logs** shows what the bot is doing.

### How tokens work in this panel

- Your bot code can read its token from the environment variable
  **`BOT_TOKEN`** (the sample bot does this). The panel sets it
  automatically when the bot starts.
- If your bot already has its token written inside the `.py` file, leave
  the token field empty — that works too.
- After saving, the panel only ever shows the **last 4 characters** of a
  token. The full token lives only in that bot's own config file on the
  server (`data/bots/<id>/config.env`).
- **Panel password ≠ bot token.** The password opens the admin panel;
  each token runs one bot. Never mix them up. If you ever paste a token
  in the wrong place (a chat, another bot, a public page), treat it as
  exposed: revoke it in @BotFather with `/revoke` and make a fresh one.

### Statuses

- **running** — the bot process is alive.
- **stopped** — stopped by the owner, or it exited cleanly.
- **crashed** — it started but died with an error. Open **Logs** to see
  the exact error line — fix only what the log shows.
- **Auto-restart** (per bot, off by default) — if ON, the panel restarts
  the bot when it crashes. Bots do **not** auto-start after a panel/host
  restart unless auto-restart is ON for them.

---

## Run it on your own computer first (optional test)

```
pip install -r requirements.txt
set PANEL_PASSWORD=myownpassword     (Windows)
export PANEL_PASSWORD=myownpassword  (Mac/Linux)
python app.py
```
Open http://localhost:5000 — landing page. Register a test user, and in
another browser log in as `admin` / `myownpassword`.

## Files in this project

| File | What it is |
|---|---|
| `app.py` | The whole website + panel (Flask, single file) |
| `sample_bot.py` | Tiny echo bot to test with — no extra installs |
| `requirements.txt` | Only Flask (Werkzeug comes with it) |
| `Procfile` / `render.yaml` / `railway.json` / `runtime.txt` | Deploy settings for the hosts |
| `data/` | Created at runtime: `users.json`, `keys.json`, `bots.json`, bot files, tokens, logs (not in git) |
