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
import unicodedata
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

HERE = Path(__file__).parent
PORT = 8765

PADS = json.loads((HERE / "pads.json").read_text(encoding="utf-8"))
# your own settings (weather place, calendar links, adb); settings.example.json is the template
SETTINGS = json.loads(((HERE / "settings.json") if (HERE / "settings.json").exists() else (HERE / "settings.example.json")).read_text(encoding="utf-8"))
ALL = PADS["left"] + PADS["right"]
PC_PADS = [p for p in ALL if "pc" in p]
ACTIONS = {p["id"]: p["pc"] for p in PC_PADS}
WINDOWS = {p["id"]: p["window"].lower() for p in PC_PADS if p.get("window")}  # program to bring to the front
REUSE = {p["id"] for p in PC_PADS if p.get("reuse") and p.get("window")}  # bring back the open window instead of opening another
assert len(ACTIONS) == len(PC_PADS), "every PC pad needs its own unique id"
assert all(p["url"].startswith("https://") or (HERE / p["url"]).is_file() for p in ALL if "pc" not in p), "Echo pads need an https:// link or a page in this folder"

# Windows virtual-key codes for the media buttons under the clock
MEDIA_KEYS = {"prev": 0xB1, "play": 0xB3, "next": 0xB0, "voldown": 0xAE, "volup": 0xAF, "mute": 0xAD}


def press(vk):
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, 2, 0)  # 2 = key up


# Bringing an opened app to the front. Windows doesn't let a background program (this helper) put another app's
# window in front, so apps opened from the deck could appear behind whatever you were using. After opening one,
# the helper finds its window (by the pad's "window": the program name, like "Obsidian.exe") and pulls it forward.
from ctypes import wintypes as _wt
_u32, _k32 = ctypes.windll.user32, ctypes.windll.kernel32
_EnumProc = ctypes.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
for _f, _args in {"GetWindowThreadProcessId": [_wt.HWND, ctypes.c_void_p], "IsWindowVisible": [_wt.HWND], "GetWindowTextLengthW": [_wt.HWND],
                  "GetWindow": [_wt.HWND, _wt.UINT], "GetWindowLongW": [_wt.HWND, ctypes.c_int], "IsIconic": [_wt.HWND],
                  "ShowWindow": [_wt.HWND, ctypes.c_int], "BringWindowToTop": [_wt.HWND], "SetForegroundWindow": [_wt.HWND],
                  "SetWindowPos": [_wt.HWND, _wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, _wt.UINT],
                  "AttachThreadInput": [_wt.DWORD, _wt.DWORD, _wt.BOOL], "EnumWindows": [_EnumProc, _wt.LPARAM],
                  "SendMessageTimeoutW": [_wt.HWND, _wt.UINT, _wt.WPARAM, _wt.LPARAM, _wt.UINT, _wt.UINT, ctypes.c_void_p]}.items():
    getattr(_u32, _f).argtypes = _args  # 64-bit window handles must not be cut down to 32 bits
_u32.GetWindowThreadProcessId.restype = _wt.DWORD
_u32.GetForegroundWindow.restype = _wt.HWND
_u32.GetWindow.restype = _wt.HWND


def _exe_of(hwnd):
    pid = _wt.DWORD()
    _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = _k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    buf, size = ctypes.create_unicode_buffer(260), _wt.DWORD(260)
    ok = _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
    _k32.CloseHandle(h)
    return buf.value.rsplit("\\", 1)[-1].lower() if ok else ""


def app_windows(exe):
    """The app's main windows, frontmost first (visible, titled, not tool windows)."""
    found = []

    def each(hwnd, _):
        if _u32.IsWindowVisible(hwnd) and _u32.GetWindowTextLengthW(hwnd) and not _u32.GetWindow(hwnd, 4) \
                and not (_u32.GetWindowLongW(hwnd, -20) & 0x80) and _exe_of(hwnd) == exe:  # no owner, not WS_EX_TOOLWINDOW
            found.append(hwnd)
        return True
    _u32.EnumWindows(_EnumProc(each), 0)
    return found


def to_front(hwnd):
    if _u32.IsIconic(hwnd):
        # un-minimise the way the taskbar does (WM_SYSCOMMAND, SC_RESTORE), then let the animation finish
        _u32.SendMessageTimeoutW(hwnd, 0x0112, 0xF120, 0, 0x2, 1000, None)
        time.sleep(0.3)
    me, fg = _k32.GetCurrentThreadId(), _u32.GetWindowThreadProcessId(_u32.GetForegroundWindow(), None)
    target = _u32.GetWindowThreadProcessId(hwnd, None)
    # joining the input of the app in front lets this helper hand the focus over (the usual way around the rule)
    for t in {fg, target} - {me}:
        _u32.AttachThreadInput(me, t, True)
    # Windows only lets the program that sent the last key or click choose the front window, so the helper
    # "presses" key 0xE8, which no keyboard has and no app reacts to (unlike the usual trick, Alt, which opens menus)
    _u32.keybd_event(0xE8, 0, 0, 0)
    _u32.keybd_event(0xE8, 0, 2, 0)
    _u32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x3)   # HWND_TOPMOST for a moment (SWP_NOMOVE | SWP_NOSIZE)...
    _u32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x43)  # ...then back to normal, now on top (| SWP_SHOWWINDOW)
    _u32.BringWindowToTop(hwnd)
    _u32.SetForegroundWindow(hwnd)
    for t in {fg, target} - {me}:
        _u32.AttachThreadInput(me, t, False)
    return _u32.GetForegroundWindow() == hwnd


def keep_in_front(hwnd, was_front):
    """Bring hwnd to the front and keep it there for 2.5 s against the window that was in front before
    (some apps, Brave for one, take the focus back a moment later). Clicking anything else is left alone."""
    for _ in range(3):
        if to_front(hwnd):
            break
        time.sleep(0.2)
    else:
        return False
    fixes, end = 0, time.time() + 2.5
    while time.time() < end and fixes < 2:
        time.sleep(0.15)
        now = _u32.GetForegroundWindow()
        if now == was_front:  # the old window grabbed it back, not something you chose
            fixes += 1
            to_front(hwnd)
    return _u32.GetForegroundWindow() == hwnd


def open_in_front(target, exe, reuse=False):
    was_front = _u32.GetForegroundWindow()
    before = app_windows(exe) if exe else []
    if reuse and before:  # "reuse": just bring back the window that's already open (Terminal would make a new one each time)
        if not keep_in_front(before[0], was_front):
            print(f"Couldn't bring {exe} to the front (in front: {_exe_of(_u32.GetForegroundWindow()) or 'unknown'})")
        return
    before = set(before)
    os.startfile(target)  # Windows: opens the app, URI or link like double-clicking it
    if not exe:
        return
    end = time.time() + 6
    while time.time() < end:
        time.sleep(0.2)
        wins = app_windows(exe)
        new = [w for w in wins if w not in before]
        # a brand-new window wins; an app that was already open (it just shows itself) uses its front window
        pick = new[0] if new else (wins[0] if wins and time.time() > end - 4.5 else None)
        if pick and keep_in_front(pick, was_front):
            return
    print(f"Couldn't bring {exe} to the front (in front: {_exe_of(_u32.GetForegroundWindow()) or 'unknown'})")


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
    tmp = HERE / "usage.json.tmp"  # written aside, then swapped in whole, so /usage never reads half a file
    tmp.write_text(json.dumps(out), encoding="utf-8")
    os.replace(tmp, HERE / "usage.json")
    return out


def usage_loop():
    while True:
        try:
            refresh_usage()
        except Exception as e:  # logs the kind of problem only, never the sign-in
            print(f"Claude usage not updated: {type(e).__name__} {getattr(e, 'code', '')}")
        time.sleep(300)


# What the PC is playing, for the deck's now-playing card: nowplaying.ps1 reads Windows' media controls
# (the same info as the volume pop-up) and prints a line about every second. Its cover picture goes to ART.
NOW = {"playing": False}
NP_PROC = None  # the running nowplaying.ps1; the deck's skip keys and progress line send it commands
NP_LOCK = threading.Lock()  # one command at a time, so two taps can't mix their lines
ART = HERE / "nowplaying-art.bin"


# Covers and lengths for songs Windows knows nothing about (Spotify read from its window title), found with
# Deezer's or Apple's free search. Only the artist and song name are sent.
# SONGS: {"artist|song": {"no": cover number or 0, "len": seconds or 0, "tried": when}}; COVER_IMG: {cover number: image}.
# The deck asks /art?v=<number>, so it always gets the picture that belongs to the song it shows.
SONGS, COVER_IMG = {}, {}
COVER_NO = iter(range(1000, 10**9))  # cover numbers from 1000 never clash with nowplaying.ps1's


def song_info(artist, title):
    """What's known about this song so far. Searched in the background; a miss is tried again after 5 minutes."""
    key = f"{artist}|{title}"
    info = SONGS.get(key) or {"no": 0, "len": 0, "tried": 0}
    if not info["no"] and time.time() - info["tried"] > 300:
        info = SONGS[key] = {"no": 0, "len": 0, "tried": time.time()}
        threading.Thread(target=fetch_song, args=(key, artist, title), daemon=True).start()
    return info


def plain(text):
    """For comparing names: no accents, case, punctuation, "(feat. ...)" or " - Remastered" style extras."""
    text = unicodedata.normalize("NFKD", text.split(" - ")[0])
    text = re.sub(r"[\(\[].*?[\)\]]", "", "".join(c for c in text if not unicodedata.combining(c)))
    return re.sub(r"[^a-z0-9]", "", text.lower())


def get_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "home-deck"}), timeout=10) as r:
        return json.load(r)


def look_up(artist, title):
    """(cover address, length in seconds) of this exact song, or None. A wrong cover is worse than none, so names must match."""
    want, who = plain(title), plain(artist.split(",")[0])
    def same(t, a):
        return plain(t) == want and bool(who) and (who in plain(a) or plain(a) in who)
    who_name = artist.split(",")[0]
    short = re.sub(r"\s*[\(\[].*?[\)\]]", "", title).strip()  # searches often miss "Song (Lunch Break Freestyle)" but find "Song"
    for term in dict.fromkeys([f"{who_name} {title}", f"{who_name} {short}"]):
        # Deezer first (its catalogue is close to Spotify's), then Apple's iTunes search; both free with no key
        for x in get_json("https://api.deezer.com/search?" + urlencode({"q": term, "limit": 10})).get("data", []):
            if same(x.get("title", ""), x.get("artist", {}).get("name", "")) and x.get("album", {}).get("cover_xl"):
                return x["album"]["cover_xl"], x.get("duration") or 0
        for x in get_json("https://itunes.apple.com/search?" + urlencode({"term": term, "entity": "song", "limit": 10})).get("results", []):
            if same(x.get("trackName", ""), x.get("artistName", "")) and x.get("artworkUrl100"):
                return x["artworkUrl100"].replace("100x100bb", "600x600bb"), (x.get("trackTimeMillis") or 0) / 1000
    return None


def fetch_song(key, artist, title):
    try:
        found = look_up(artist, title)
        if found:
            with urllib.request.urlopen(found[0], timeout=10) as r:
                img = r.read()
            no = next(COVER_NO)
            COVER_IMG[no] = img
            SONGS[key] = {"no": no, "len": found[1], "tried": time.time()}
    except Exception as e:
        print(f"No cover found: {type(e).__name__}")
    if len(COVER_IMG) > 100:  # forget old songs' pictures
        for n in sorted(COVER_IMG)[:50]:
            COVER_IMG.pop(n, None)
        for k, v in list(SONGS.items()):
            if v["no"] and v["no"] not in COVER_IMG:
                SONGS.pop(k, None)


# Spotify read from its window title gives no position, so the helper keeps its own clock: time played since the
# song first showed up, paused while Spotify is paused. It's an estimate: it can't see jumps made inside Spotify.
CLOCK = {"key": None, "played": 0.0, "since": None, "paused_at": 0.0}


def spotify_position(key, playing):
    now = time.time()
    if CLOCK["key"] != key and key is not None:
        CLOCK.update(key=key, played=0.0, since=now if playing else None)
    elif playing and CLOCK["since"] is None:
        CLOCK["since"] = now
    elif not playing and CLOCK["since"] is not None:
        CLOCK["played"] += now - CLOCK["since"]; CLOCK["since"] = None; CLOCK["paused_at"] = now
    if CLOCK["since"] is None and CLOCK["key"] and now - CLOCK["paused_at"] > 1800:
        CLOCK["key"] = None  # paused for half an hour: start fresh next time
    return CLOCK["played"] + (now - CLOCK["since"] if CLOCK["since"] is not None else 0)


# Spotify's official Web API (optional; set up with spotify_login.py). When connected, a Spotify song gets its
# exact position, length and cover from Spotify itself, and the deck can jump in it (that needs Premium).
# Only a renewal token is kept, in .spotify-token; nothing is stored in settings.json but the app's public Client ID.
SP_ID = (SETTINGS.get("spotify_client_id") or "").strip()
SP_TOKEN_FILE = HERE / ".spotify-token"
SP = {"access": None, "until": 0.0, "state": None, "at": 0.0, "seek_ok": True, "art": {}}


def sp_access():
    if SP["access"] and time.time() < SP["until"] - 60:
        return SP["access"]
    refresh = json.loads(SP_TOKEN_FILE.read_text(encoding="utf-8"))["refresh_token"]
    req = urllib.request.Request("https://accounts.spotify.com/api/token", data=urlencode({
        "grant_type": "refresh_token", "refresh_token": refresh, "client_id": SP_ID}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=10) as r:
        t = json.load(r)
    if t.get("refresh_token"):  # Spotify may hand out a new renewal token; keep the newest
        SP_TOKEN_FILE.write_text(json.dumps({"refresh_token": t["refresh_token"]}), encoding="utf-8")
    SP["access"], SP["until"] = t["access_token"], time.time() + t.get("expires_in", 3600)
    return SP["access"]


def sp_call(method, path, query=None):
    url = "https://api.spotify.com/v1" + path + ("?" + urlencode(query) if query else "")
    req = urllib.request.Request(url, method=method, data=b"" if method != "GET" else None,
                                 headers={"Authorization": "Bearer " + sp_access()})
    with urllib.request.urlopen(req, timeout=8) as r:
        body = r.read()
    try:
        return json.loads(body) if body else None
    except ValueError:  # playback commands answer with a bare id or nothing, not JSON
        return None


def sp_cover(url):
    """A number for Spotify's cover picture (fetched once), for /art?v=."""
    if url not in SP["art"]:
        with urllib.request.urlopen(url, timeout=10) as r:
            img = r.read()
        no = next(COVER_NO)
        COVER_IMG[no] = img
        SP["art"][url] = no
    return SP["art"][url]


def spotify_loop():
    """While Spotify is the one playing, ask it every 1.5 s what it's doing."""
    while True:
        if NOW.get("keys"):
            try:
                SP["state"], SP["at"] = sp_call("GET", "/me/player"), time.time()
            except Exception as e:
                SP["state"] = None
                print(f"Spotify not reached: {type(e).__name__} {getattr(e, 'code', '')}")
                time.sleep(10)
        time.sleep(1.5)


def from_spotify(now):
    """Fill a window-title Spotify reading with Spotify's own exact details, when connected."""
    st = SP["state"]
    if not st or time.time() - SP["at"] > 5 or not st.get("item"):
        return False
    item = st["item"]
    # only if it's the same song the window shows (it could be playing on your phone, say)
    if plain(item.get("name", "")) != plain(now.get("title", "")):
        return False
    pos = (st.get("progress_ms") or 0) / 1000 + ((time.time() - SP["at"]) if st.get("is_playing") else 0)
    images = item.get("album", {}).get("images") or []
    try:
        art = sp_cover(images[0]["url"]) if images else 0
    except Exception:
        art = 0
    seekable = SP["seek_ok"] and not st.get("actions", {}).get("disallows", {}).get("seeking")
    now.update(position=round(pos, 1), length=round(item.get("duration_ms", 0) / 1000, 1), art=art or now.get("art", 0),
               seek=bool(seekable), estimated=False, via="spotify")
    return True


def sp_seek(seconds):
    try:
        sp_call("PUT", "/me/player/seek", {"position_ms": int(seconds * 1000)})
        SP["at"] = 0  # ask again soon, so the card shows the new spot
    except Exception as e:
        if getattr(e, "code", 0) == 403:  # Spotify allows jumping only with Premium
            SP["seek_ok"] = False
            print("Spotify said no to jumping (it needs Premium)")
        else:
            print(f"Spotify jump failed: {type(e).__name__} {getattr(e, 'code', '')}")


def now_playing_loop():
    global NOW, NP_PROC
    while True:
        try:
            NP_PROC = p = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(HERE / "nowplaying.ps1"), "-ArtFile", str(ART)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
            for line in p.stdout:
                try:
                    now = json.loads(line)
                except ValueError:
                    continue
                if now.get("keys"):  # Spotify read from its window title: look up its cover and length, and keep time
                    info = song_info(now.get("artist", ""), now.get("title", ""))
                    pos = spotify_position(f"{now.get('artist')}|{now.get('title')}", True)
                    if info["len"]:
                        pos %= info["len"]  # on repeat, the same song starts over
                    now.update(art=info["no"], length=round(info["len"], 1), position=round(pos, 1), estimated=True)
                    from_spotify(now)  # exact details straight from Spotify, when connected
                else:
                    spotify_position(None, False)  # Spotify paused or gone: stop its clock
                NOW = now  # swapped whole, so a request never sees half of it
            print("Now playing reader stopped; restarting it")
        except Exception as e:
            print(f"Now playing not available: {type(e).__name__}")
        NOW = {"playing": False}
        time.sleep(10)


class Deck(BaseHTTPRequestHandler):
    def _authorized(self, query):
        given = query.get("k", [""])[0]
        return bool(given) and secrets.compare_digest(given, KEY)  # a missing or empty key never matches

    # keep the key out of deck.log
    def log_request(self, code="-", size="-"):
        if code == 200 and self.path.startswith(("/nowplaying", "/art")):
            return  # asked every second; logging them would fill deck.log
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
        if url.path == "/nowplaying":
            return self._send(200, json.dumps(NOW).encode(), "application/json")
        if url.path == "/art":
            try:
                v = int(parse_qs(url.query).get("v", ["0"])[0])
            except ValueError:
                v = 0
            try:
                img = COVER_IMG[v] if v >= 1000 else ART.read_bytes()  # a looked-up cover, or the one Windows gave
            except (KeyError, OSError):
                return self._send(404, b"No picture")
            return self._send(200, img, "image/png" if img[:4] == b"\x89PNG" else "image/jpeg")
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
        if url.path in ("/np/next", "/np/prev", "/np/seek"):  # skip or jump in what's playing
            cmd = url.path.removeprefix("/np/")
            if cmd == "seek" and NOW.get("via") == "spotify":  # Spotify connected: it jumps itself
                try:
                    to = max(0.0, float(parse_qs(url.query).get("to", [""])[0]))
                except ValueError:
                    return self._send(400, b"Bad time")
                threading.Thread(target=sp_seek, args=(to,), daemon=True).start()
                return self._send(204)
            if cmd == "seek":
                try:
                    cmd = f"seek {max(0.0, float(parse_qs(url.query).get('to', [''])[0])):.1f}"
                except ValueError:
                    return self._send(400, b"Bad time")
            try:
                with NP_LOCK:
                    proc = NP_PROC  # one reference, in case the reader restarts meanwhile
                    proc.stdin.write(cmd + "\n"); proc.stdin.flush()
            except (AttributeError, OSError):
                return self._send(503, b"Now playing isn't running")
            return self._send(204)
        target = ACTIONS.get(url.path.removeprefix("/run/"))
        if target is None:
            return self._send(404, b"Unknown pad")
        pad = url.path.removeprefix("/run/")
        threading.Thread(target=open_in_front, args=(target, WINDOWS.get(pad), pad in REUSE), daemon=True).start()
        self._send(204)


def lan_ip():
    # connecting a UDP socket sends nothing; it just picks the Wi-Fi address
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]


def lan_ip_or_none():
    try:
        return lan_ip()
    except OSError:  # no network yet (e.g. right after login): still start; the Echo connects once Wi-Fi is up
        return None


if __name__ == "__main__":
    if sys.stderr is None:  # started hidden with pythonw at login: log to a file instead
        sys.stdout = sys.stderr = open(HERE / "deck.log", "a", buffering=1, encoding="utf-8")
    ip = lan_ip_or_none()
    print(f"Home Deck helper is running at http://{ip}:{PORT} (the Echo's PC pads call it)." if ip
          else f"Home Deck helper is running on port {PORT} (no network yet).")
    print("Keep this window open. Press Ctrl+C to stop.")
    if SETTINGS.get("claude_usage", True):
        threading.Thread(target=usage_loop, daemon=True).start()
    if SETTINGS.get("now_playing", True):
        threading.Thread(target=now_playing_loop, daemon=True).start()
        if SP_ID and SP_TOKEN_FILE.exists():
            threading.Thread(target=spotify_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Deck).serve_forever()
