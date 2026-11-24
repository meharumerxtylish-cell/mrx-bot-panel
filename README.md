# MR X Bot Hosting Panel

One web panel where you can add and run **unlimited Telegram bots**.
Add a bot (name + `.py` file + token), press Start — done. Start, stop,
restart, delete, watch logs, and auto-restart, all from one page.

> One honest line: "unlimited" means unlimited bots in the panel. How many
> can run at the same time depends on your host's RAM, and free hosts sleep
> when idle / limit free hours — so on a free plan, keep only a few bots
> running at once.

---

## What you need first

1. A **panel password** that you choose yourself (you will set it as an
   environment variable called `PANEL_PASSWORD` — see the deploy steps).
2. For every bot: a **bot token** from Telegram's **@BotFather**
   (`/newbot`). One token per bot. Paste each token **only** into that
   bot's own Add Bot form in the panel — never into another bot, and never
   into the panel-password field.

If you forget to set `PANEL_PASSWORD`, the panel generates one and prints
it in the host's **Logs** at first start — but setting your own is better.

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
5. Open the URL, log in with your `PANEL_PASSWORD`. Deployed ✅

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
5. Click **Create Web Service**, wait for the build, open the URL, log in.

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
6. Open your `YOUR_USERNAME.pythonanywhere.com` URL and log in.

   Note: PythonAnywhere's free plan is made for websites, not always-on
   background bots — bots may stop when the web app reloads. Railway or
   Render work better for 24/7 bots.

---

## Add your first bot (2 minutes)

1. In Telegram, open **@BotFather** → `/newbot` → follow the steps → copy
   the token he gives you.
2. Open your panel URL → log in.
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
- **Panel password ≠ bot token.** The password opens the panel; each
  token runs one bot. Never mix them up. If you ever paste a token in the
  wrong place (a chat, another bot, a public page), treat it as exposed:
  revoke it in @BotFather with `/revoke` and make a fresh one.

### Statuses

- **running** — the bot process is alive.
- **stopped** — stopped by you, or it exited cleanly.
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
Open http://localhost:5000 and log in.

## Files in this project

| File | What it is |
|---|---|
| `app.py` | The whole panel (Flask, single file) |
| `sample_bot.py` | Tiny echo bot to test with — no extra installs |
| `requirements.txt` | Only Flask |
| `Procfile` / `render.yaml` / `railway.json` / `runtime.txt` | Deploy settings for the hosts |
| `data/` | Created at runtime: bot files, tokens, logs (not in git) |
