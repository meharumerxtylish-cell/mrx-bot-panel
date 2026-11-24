#!/usr/bin/env python3
"""
MR X Sample Echo Bot — use this to test the hosting panel.

- Reads its token from the BOT_TOKEN environment variable
  (the panel sets it automatically from the token you paste in Add Bot).
- Zero extra dependencies: only Python's standard library (urllib).
- Long-polling echo bot: send it any message, it sends the same text back.
  Send /start for a hello message.
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
API = "https://api.telegram.org/bot%s/" % TOKEN


def call(method, params=None):
    data = None
    if params:
        data = json.dumps(params).encode()
    req = urllib.request.Request(API + method, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.loads(resp.read().decode())


def main():
    if not TOKEN:
        print("ERROR: BOT_TOKEN is not set. Add the bot again in the panel "
              "and paste its token from @BotFather.", flush=True)
        sys.exit(1)
    me = call("getMe")["result"]
    print("Sample bot started: @%s — send me a message on Telegram!"
          % me["username"], flush=True)
    offset = 0
    while True:
        try:
            updates = call("getUpdates", {"offset": offset, "timeout": 30})
        except urllib.error.URLError as e:
            print("Network error: %s — retrying in 5s" % e, flush=True)
            time.sleep(5)
            continue
        for upd in updates.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            text = msg.get("text")
            if chat_id is None:
                continue
            if text == "/start":
                reply = "Hello! I am the MR X sample bot, hosted on the MR X panel. Send me anything and I will echo it back."
            elif text:
                reply = "You said: " + text
            else:
                reply = "Send me a text message and I will echo it back."
            try:
                call("sendMessage", {"chat_id": chat_id, "text": reply})
            except urllib.error.URLError as e:
                print("Send error: %s" % e, flush=True)


if __name__ == "__main__":
    main()
