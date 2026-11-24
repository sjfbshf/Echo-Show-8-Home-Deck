"""Connect the Home Deck helper to your Spotify account (optional, once).

With this, the deck's now-playing card gets Spotify's exact position, the real cover, and lets you drag
through songs (dragging needs Spotify Premium). See "Spotify (optional)" in the README for the steps
before this one: make a free app on developer.spotify.com and put its Client ID in settings.json.

Run:  python spotify_login.py
Your browser opens Spotify's own sign-in page. After you agree, the helper keeps a renewal token in
%USERPROFILE%\\.homedeck\\.spotify-token (on this PC only, never uploaded). To disconnect, delete that file,
or remove the app at https://www.spotify.com/account/apps/.
"""
import base64
import os
import hashlib
import json
import secrets
import sys
import threading
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from deck_server import DATA, SETTINGS  # the same settings and private folder the helper uses
CLIENT_ID = (SETTINGS.get("spotify_client_id") or "").strip()
REDIRECT = "http://127.0.0.1:8766/callback"  # must match the Redirect URI in your Spotify app exactly
SCOPES = "user-read-playback-state user-modify-playback-state"
TOKEN_FILE = DATA / ".spotify-token"

if not CLIENT_ID:
    sys.exit('Put your Spotify app\'s Client ID in settings.json as "spotify_client_id" first (see the README).')

# PKCE: a one-time secret made here, so no client secret has to be stored anywhere
verifier = secrets.token_urlsafe(64)
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
state = secrets.token_urlsafe(16)
result = {}


class Callback(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path != "/callback":
            self.send_response(404); self.end_headers(); return
        ok = q.get("state", [""])[0] == state and "code" in q
        result["code"] = q["code"][0] if ok else None
        result["error"] = None if ok else q.get("error", ["the sign-in didn't match"])[0]
        body = ("Connected. You can close this tab." if ok else f"Not connected: {result['error']}").encode()
        self.send_response(200); self.send_header("Content-Type", "text/plain; charset=utf-8"); self.end_headers(); self.wfile.write(body)
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, *args):  # keep the one-time code out of the console
        pass


server = HTTPServer(("127.0.0.1", 8766), Callback)
auth = "https://accounts.spotify.com/authorize?" + urlencode({
    "client_id": CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT, "scope": SCOPES,
    "code_challenge_method": "S256", "code_challenge": challenge, "state": state})
print("Opening Spotify's sign-in page in your browser. If it doesn't open, paste this link into it:\n" + auth)
webbrowser.open(auth)
server.serve_forever()

if not result.get("code"):
    sys.exit(f"Not connected: {result.get('error')}")
req = urllib.request.Request("https://accounts.spotify.com/api/token", data=urlencode({
    "grant_type": "authorization_code", "code": result["code"], "redirect_uri": REDIRECT,
    "client_id": CLIENT_ID, "code_verifier": verifier}).encode(),
    headers={"Content-Type": "application/x-www-form-urlencoded"})
with urllib.request.urlopen(req, timeout=20) as r:
    tokens = json.load(r)
tmp = TOKEN_FILE.with_suffix(".tmp")
tmp.write_text(json.dumps({"refresh_token": tokens["refresh_token"]}), encoding="utf-8")
os.replace(tmp, TOKEN_FILE)
print("Connected to Spotify. Restart the helper (deck_server.py) so it starts using it.")
