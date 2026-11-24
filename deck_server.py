"""Home Deck helper.

Opens apps on this PC when a PC pad on the Echo is tapped. The deck pages
themselves live on the Echo (sync_echo.py copies them there), so the Echo
works with this PC off. It only opens what is listed in pads.json, and only
for requests that carry the secret key in .deck-key.

Run:  python deck_server.py
(At login it starts hidden via a Startup shortcut; its link is then in deck.log.)
"""
import ctypes
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).parent
PORT = 8765

PADS = json.loads((HERE / "pads.json").read_text(encoding="utf-8"))
# your own settings (weather place, calendar links, adb); settings.example.json is the template
SETTINGS = json.loads(((HERE / "settings.json") if (HERE / "settings.json").exists() else (HERE / "settings.example.json")).read_text(encoding="utf-8"))
ALL = PADS["left"] + PADS["right"]
PC_PADS = [p for p in ALL if "pc" in p]
ACTIONS = {p["id"]: p["pc"] for p in PC_PADS}
assert len(ACTIONS) == len(PC_PADS), "every PC pad needs its own unique id"
assert all(p["url"].startswith("https://") or (HERE / p["url"]).is_file() for p in ALL if "pc" not in p), "Echo pads need an https:// link or a page in this folder"

# Windows virtual-key codes for the media buttons under the clock
MEDIA_KEYS = {"prev": 0xB1, "play": 0xB3, "next": 0xB0, "voldown": 0xAE, "volup": 0xAF, "mute": 0xAD}


def press(vk):
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, 2, 0)  # 2 = key up


KEY_FILE = HERE / ".deck-key"
if not KEY_FILE.exists() or len(KEY_FILE.read_text().strip()) < 16:
    # a missing, empty or too-short key would let anyone in, so make a fresh one (then run sync_echo.py again)
    KEY_FILE.write_text(secrets.token_urlsafe(18))
KEY = KEY_FILE.read_text().strip()
assert len(KEY) >= 16, "the deck key in .deck-key is too short"


# Claude usage for the deck: every 5 minutes, ask Anthropic for this account's plan usage using the
# sign-in Claude Code saved on this PC, and keep the numbers in usage.json (served at /usage).
# The sign-in is only read here and only sent to api.anthropic.com; usage.json holds just the numbers.
# shortcut: this is the unofficial endpoint Claude Code's own usage screen uses, so it may change.
# If it can't update (PC asleep, signed out), the deck shows the last numbers and "Last checked ... ago".
CLAUDE_LOGIN = Path.home() / ".claude" / ".credentials.json"


CLAUDE_CLI = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")


def claude_login():
    login = json.loads(CLAUDE_LOGIN.read_text(encoding="utf-8"))["claudeAiOauth"]
    if login["expiresAt"] / 1000 < time.time() + 600:
        # the sign-in lasts about 8 hours; "claude auth status" renews it (no model call, uses no Claude usage)
        subprocess.run([CLAUDE_CLI, "auth", "status"], capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        login = json.loads(CLAUDE_LOGIN.read_text(encoding="utf-8"))["claudeAiOauth"]
    return login["accessToken"]


def refresh_usage():
    token = claude_login()
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
        "Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20", "User-Agent": "home-deck"})
    with urllib.request.urlopen(req, timeout=20) as r:
        u = json.load(r)
    pick = lambda w: w and {"percent": w.get("utilization") or 0, "resets_at": w.get("resets_at")}
    out = {"updated": datetime.now(timezone.utc).isoformat(), "five_hour": pick(u.get("five_hour")), "weekly": pick(u.get("seven_day"))}
    (HERE / "usage.json").write_text(json.dumps(out), encoding="utf-8")
    return out


def usage_loop():
    while True:
        try:
            refresh_usage()
        except Exception as e:  # logs the kind of problem only, never the sign-in
            print(f"Claude usage not updated: {type(e).__name__} {getattr(e, 'code', '')}")
        time.sleep(300)


class Deck(BaseHTTPRequestHandler):
    def _authorized(self, query):
        given = query.get("k", [""])[0]
        return bool(given) and secrets.compare_digest(given, KEY)  # a missing or empty key never matches

    # keep the key out of deck.log
    def log_request(self, code="-", size="-"):
        self.log_message('"%s" %s %s', self.requestline, code, size)

    def log_message(self, fmt, *args):
        super().log_message(fmt, *(re.sub(r"k=[^ &]*", "k=***", str(x)) for x in args))

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if not self._authorized(parse_qs(url.query)):
            return self._send(403, b"Wrong key")
        if url.path == "/usage":  # Claude usage for the deck's middle panel
            f = HERE / "usage.json"
            return self._send(200, f.read_bytes() if f.exists() else b"{}", "application/json")
        self._send(404, b"Not found")

    def do_POST(self):
        url = urlparse(self.path)
        if not self._authorized(parse_qs(url.query)):
            return self._send(403, b"Wrong key")
        if url.path.startswith("/key/"):
            vk = MEDIA_KEYS.get(url.path.removeprefix("/key/"))
            if vk is None:
                return self._send(404, b"Unknown key")
            press(vk)
            return self._send(204)
        target = ACTIONS.get(url.path.removeprefix("/run/"))
        if target is None:
            return self._send(404, b"Unknown pad")
        os.startfile(target)  # Windows: opens the app, URI or link like double-clicking it
        self._send(204)


def lan_ip():
    # connecting a UDP socket sends nothing; it just picks the Wi-Fi address
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]


if __name__ == "__main__":
    if sys.stderr is None:  # started hidden with pythonw at login: log to a file instead
        sys.stdout = sys.stderr = open(HERE / "deck.log", "a", buffering=1, encoding="utf-8")
    print(f"Home Deck helper is running at http://{lan_ip()}:{PORT} (the Echo's PC pads call it).")
    print("Keep this window open. Press Ctrl+C to stop.")
    if SETTINGS.get("claude_usage", True):
        threading.Thread(target=usage_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Deck).serve_forever()
