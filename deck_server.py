r"""Home Deck helper.

Opens apps on this PC when a PC pad on the Echo is tapped. The deck pages
themselves live on the Echo (sync_echo.py copies them there), so the Echo
works with this PC off. It only opens what is listed in pads.json, and only
for requests that carry the secret key in %USERPROFILE%\.homedeck\.deck-key.

Run:  python deck_server.py
(At login it starts hidden via a Startup shortcut; its log is then %USERPROFILE%\.homedeck\deck.log.)
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
import urllib.error
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

HERE = Path(__file__).parent
PORT = 8765

# The deck key, the Spotify sign-in, Claude usage, the log and the cover picture live in a folder on this PC only
# (%USERPROFILE%\.homedeck), not next to the code, which may be in a folder OneDrive or Dropbox uploads.
# (Not AppData\Local: Python from the Microsoft Store / Python Install Manager gets a private copy of that folder,
# so files put there by other programs can be invisible to it.)
DATA = Path.home() / ".homedeck"
DATA.mkdir(parents=True, exist_ok=True)
for _name in (".deck-key", ".spotify-token", "usage.json", "deck.log", "nowplaying-art.bin"):
    if (HERE / _name).exists() and not (DATA / _name).exists():  # move them from where older versions kept them
        try:
            shutil.move(str(HERE / _name), str(DATA / _name))
        except OSError:
            pass

PADS = json.loads((HERE / "pads.json").read_text(encoding="utf-8"))
# your own settings (weather place, calendar links, adb); settings.example.json is the template. settings.json can sit
# in %USERPROFILE%\.homedeck (best if this folder is synced to the cloud: it holds your calendar links) or here.
SETTINGS_FILE = next((f for f in (DATA / "settings.json", HERE / "settings.json") if f.exists()), HERE / "settings.example.json")
SETTINGS = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
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
_k32.OpenProcess.argtypes, _k32.OpenProcess.restype = [_wt.DWORD, _wt.BOOL, _wt.DWORD], _wt.HANDLE
_k32.QueryFullProcessImageNameW.argtypes = [_wt.HANDLE, _wt.DWORD, _wt.LPWSTR, ctypes.POINTER(_wt.DWORD)]
_k32.CloseHandle.argtypes = [_wt.HANDLE]


class _LastInput(ctypes.Structure):
    _fields_ = [("cbSize", _wt.UINT), ("dwTime", _wt.DWORD)]


def last_input():
    """When the last key or click happened (any program's), in ms since Windows started."""
    li = _LastInput(ctypes.sizeof(_LastInput), 0)
    _u32.GetLastInputInfo(ctypes.byref(li))
    return li.dwTime
_u32.GetForegroundWindow.restype = _wt.HWND
_u32.GetWindow.restype = _wt.HWND


def _exe_of(hwnd):
    pid = _wt.DWORD()
    _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = _k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf, size = ctypes.create_unicode_buffer(32768), _wt.DWORD(32768)  # room for very long install paths
        ok = _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return buf.value.rsplit("\\", 1)[-1].lower() if ok else ""
    finally:
        _k32.CloseHandle(h)


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
    """Bring hwnd to the front. For 2.5 s, if the window that was in front before takes the focus back on its own
    (Brave does this), put hwnd back. The moment you press a key or click anything, it stops and leaves you alone.
    Returns True (in front), False (couldn't) or None (you did something else meanwhile)."""
    for _ in range(3):
        if to_front(hwnd):
            break
        time.sleep(0.2)
    else:
        return False
    mark, fixes, end = last_input(), 0, time.time() + 2.5  # to_front's own key press is the last input now
    while time.time() < end and fixes < 2:
        time.sleep(0.15)
        if last_input() != mark:  # you typed or clicked: your choice wins
            return None
        if _u32.GetForegroundWindow() == was_front:
            fixes += 1
            to_front(hwnd)
            mark = last_input()
    return _u32.GetForegroundWindow() == hwnd


# Pings: when a PC pad's app wants you (it flashes its taskbar button, the way Windows apps say "done" or "your turn"),
# its pad on the deck shows a small silent animation until you open that app. Apps or scripts can also ping a pad
# themselves with POST /ping/<pad id> (for example from a Claude Code hook).
PINGS = {}  # pad id -> when it pinged


def ping_exe(hwnd, on):
    exe = _exe_of(hwnd)
    for pad, want in WINDOWS.items():
        if want == exe:
            if on:
                PINGS[pad] = time.time()
            else:
                PINGS.pop(pad, None)


def shell_hook_loop():
    """Hears Windows' shell messages: a taskbar button flashing (an app asking for you) or a window coming forward."""
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, _wt.HWND, _wt.UINT, _wt.WPARAM, _wt.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", _wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", _wt.HINSTANCE), ("hIcon", _wt.HICON), ("hCursor", _wt.HANDLE), ("hbrBackground", _wt.HBRUSH),
                    ("lpszMenuName", _wt.LPCWSTR), ("lpszClassName", _wt.LPCWSTR)]
    _u32.DefWindowProcW.argtypes, _u32.DefWindowProcW.restype = [_wt.HWND, _wt.UINT, _wt.WPARAM, _wt.LPARAM], LRESULT
    _u32.CreateWindowExW.restype = _wt.HWND
    _u32.CreateWindowExW.argtypes = [_wt.DWORD, _wt.LPCWSTR, _wt.LPCWSTR, _wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, _wt.HWND, _wt.HMENU, _wt.HINSTANCE, _wt.LPVOID]
    shell_msg = _u32.RegisterWindowMessageW("SHELLHOOK")

    def proc(hwnd, msg, wparam, lparam):
        if msg == shell_msg:
            code = wparam & 0xFFFF
            if code == 0x8006:          # HSHELL_FLASH: an app's taskbar button is flashing
                ping_exe(lparam, True)
            elif code in (4, 0x8004):   # HSHELL_WINDOWACTIVATED / RUDEAPPACTIVATED: you opened it, so it's seen
                ping_exe(lparam, False)
        return _u32.DefWindowProcW(hwnd, msg, wparam, lparam)
    keep = WNDPROC(proc)  # held for the life of the thread, or Windows would call freed memory
    wc = WNDCLASSW(lpfnWndProc=keep, lpszClassName="HomeDeckShellHook")
    _u32.RegisterClassW(ctypes.byref(wc))
    hwnd = _u32.CreateWindowExW(0, "HomeDeckShellHook", "Home Deck", 0, 0, 0, 0, 0, None, None, None, None)
    if not hwnd or not _u32.RegisterShellHookWindow(hwnd):
        print("Pings from apps not available (couldn't listen to the taskbar)")
        return
    msg = _wt.MSG()
    while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        _u32.TranslateMessage(ctypes.byref(msg)); _u32.DispatchMessageW(ctypes.byref(msg))


BUSY, BUSY_LOCK = set(), threading.Lock()  # pads being opened right now: a second quick tap doesn't open another


def start_pad(pad, target):
    """Open a pad's app. Returns False if Windows couldn't open it; bringing it forward carries on in the background."""
    exe, reuse = WINDOWS.get(pad), pad in REUSE
    with BUSY_LOCK:
        if pad in BUSY:
            return True
        BUSY.add(pad)
    try:
        was_front = _u32.GetForegroundWindow()
        before = app_windows(exe) if exe else []
        if not (reuse and before):  # "reuse": bring back the open window instead (Terminal opens a new one each time)
            os.startfile(target)  # Windows: opens the app, URI or link like double-clicking it
    except OSError as e:
        print(f"Couldn't open {pad}: {e.strerror or type(e).__name__}")
        with BUSY_LOCK:
            BUSY.discard(pad)
        return False
    threading.Thread(target=bring_forward, args=(pad, exe, reuse, before, was_front), daemon=True).start()
    return True


def bring_forward(pad, exe, reuse, before, was_front):
    try:
        if not exe:
            return
        if reuse and before:
            if keep_in_front(before[0], was_front) is False:
                print(f"Couldn't bring {exe} to the front (in front: {_exe_of(_u32.GetForegroundWindow()) or 'unknown'})")
            return
        before, end = set(before), time.time() + 6
        while time.time() < end:
            time.sleep(0.2)
            wins = app_windows(exe)
            new = [w for w in wins if w not in before]
            # a brand-new window wins; an app that was already open (it just shows itself) uses its front window
            pick = new[0] if new else (wins[0] if wins and time.time() > end - 4.5 else None)
            if pick:
                got = keep_in_front(pick, was_front)
                if got is not False:  # in front, or you moved on to something else: done either way
                    return
        print(f"Couldn't bring {exe} to the front (in front: {_exe_of(_u32.GetForegroundWindow()) or 'unknown'})")
    finally:
        with BUSY_LOCK:
            BUSY.discard(pad)


KEY_FILE = DATA / ".deck-key"
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
    now = datetime.now(timezone.utc)
    extra = u.get("extra_usage") or {}
    out = {"updated": now.isoformat(), "five_hour": pick(u.get("five_hour")), "weekly": pick(u.get("seven_day")),
           # the in-depth usage screen: every limit (model-specific ones like Fable or Opus appear here when your plan
           # has them), where this week's usage went, and usage credits (cents)
           "limits": [{k: x.get(k) for k in ("kind", "group", "scope", "percent", "severity", "resets_at", "is_active")} for x in u.get("limits") or []],
           "breakdown": [{k: x.get(k) for k in ("key", "display_name", "percent", "percent_of_limit")} for x in (u.get("seven_day_breakdown") or {}).get("rows") or []],
           "credits": {k: extra.get(k) for k in ("is_enabled", "used_credits", "monthly_limit", "currency", "decimal_places", "spend_limit_reached")} if extra else None}
    # history for the 7-day chart: one point per update, kept for 8 days
    hist_file = DATA / "usage-history.json"
    try:
        hist = json.loads(hist_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        hist = []
    cutoff = now.timestamp() - 8 * 86400
    hist = [h for h in hist if h["t"] > cutoff] + [{"t": round(now.timestamp()), "s": (out["five_hour"] or {}).get("percent"), "w": (out["weekly"] or {}).get("percent")}]
    tmp = DATA / "usage-history.json.tmp"
    tmp.write_text(json.dumps(hist), encoding="utf-8")
    os.replace(tmp, hist_file)
    tmp = DATA / "usage.json.tmp"  # written aside, then swapped in whole, so /usage never reads half a file
    tmp.write_text(json.dumps(out), encoding="utf-8")
    os.replace(tmp, DATA / "usage.json")
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
ART = DATA / "nowplaying-art.bin"


# Covers and lengths for songs Windows knows nothing about (Spotify read from its window title), found with
# Deezer's or Apple's free search. Only the artist and song name are sent.
# SONGS: {"artist|song": {"no": cover number or 0, "len": seconds or 0, "tried": when}}; COVER_IMG: {cover number: image}.
# The deck asks /art?v=<id>, so it always gets the picture that belongs to the song it shows. Ids are "c<n>" for
# covers kept here and "w<n>" for the one Windows gave (nowplaying.ps1's file), so the two can never be mixed up.
SONGS, COVER_IMG = OrderedDict(), OrderedDict()  # both bounded: oldest dropped first
COVER_NO = iter(range(1, 10**12))
CACHE_LOCK = threading.Lock()


def keep_cover(img):
    """Store a cover picture and return its id. Only the 80 newest are kept."""
    with CACHE_LOCK:
        cid = f"c{next(COVER_NO)}"
        COVER_IMG[cid] = img
        while len(COVER_IMG) > 80:
            COVER_IMG.popitem(last=False)
        return cid


def song_info(artist, title):
    """What's known about this song so far. Searched in the background; a miss is tried again after 5 minutes."""
    key = f"{artist}|{title}"
    info = SONGS.get(key) or {"no": 0, "len": 0, "tried": 0}
    if info["no"] and info["no"] not in COVER_IMG:  # its picture was dropped from the cache: fetch it again
        info = {"no": 0, "len": info["len"], "tried": 0}
    if not info["no"] and time.time() - info["tried"] > 300:
        info = SONGS[key] = {"no": 0, "len": info["len"], "tried": time.time()}
        while len(SONGS) > 300:  # misses are remembered too, but not forever
            SONGS.popitem(last=False)
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
            SONGS[key] = {"no": keep_cover(img), "len": found[1], "tried": time.time()}
    except Exception as e:
        print(f"No cover found: {type(e).__name__}")


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
SP_TOKEN_FILE = DATA / ".spotify-token"
SP = {"access": None, "until": 0.0, "state": None, "at": 0.0, "seek_ok": True, "art": {}, "device": None, "dead": False}
SP_LOCK = threading.Lock()  # one token renewal at a time


def sp_access():
    with SP_LOCK:
        if SP["access"] and time.time() < SP["until"] - 60:
            return SP["access"]
        if SP["dead"]:
            raise RuntimeError("Spotify needs signing in again")
        refresh = json.loads(SP_TOKEN_FILE.read_text(encoding="utf-8"))["refresh_token"]
        req = urllib.request.Request("https://accounts.spotify.com/api/token", data=urlencode({
            "grant_type": "refresh_token", "refresh_token": refresh, "client_id": SP_ID}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                t = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (400, 401) and b"invalid_grant" in e.read():  # removed, or unused for months: retrying won't help
                SP["dead"] = True
                print("Spotify's sign-in has expired or was removed. Run: python spotify_login.py, then restart the helper.")
            raise
        if t.get("refresh_token"):  # Spotify may hand out a new renewal token; keep the newest (written whole)
            tmp = SP_TOKEN_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"refresh_token": t["refresh_token"]}), encoding="utf-8")
            os.replace(tmp, SP_TOKEN_FILE)
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
    cid = SP["art"].get(url)
    if not cid or cid not in COVER_IMG:  # new, or dropped from the cache since
        with urllib.request.urlopen(url, timeout=10) as r:
            cid = keep_cover(r.read())
        SP["art"][url] = cid
        if len(SP["art"]) > 200:
            SP["art"] = {u: c for u, c in SP["art"].items() if c in COVER_IMG}
    return cid


def spotify_loop():
    """While Spotify is the one playing, ask it every 1.5 s what it's doing."""
    while True:
        if SP["dead"]:  # signed out: nothing to ask until spotify_login.py is run again
            time.sleep(60)
            continue
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
    # only the same song (name and artist) playing on this computer: not your phone, say
    def same(a, b):
        pa, pb = plain(a), plain(b)
        return (pa == pb) if pa and pb else a.strip().lower() == b.strip().lower()  # names with no Latin letters
    artists = [x.get("name", "") for x in item.get("artists") or []]
    window_artist = now.get("artist", "").split(",")[0]
    if not same(item.get("name", ""), now.get("title", "")) or not any(same(a, window_artist) for a in artists):
        return False
    device = st.get("device") or {}
    if device.get("type") != "Computer":
        return False
    SP["device"] = device.get("id")
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
        query = {"position_ms": int(seconds * 1000)}
        if SP["device"]:
            query["device_id"] = SP["device"]  # this PC, so a phone that just took over isn't moved
        sp_call("PUT", "/me/player/seek", query)
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
                if now.get("keys"):  # Spotify read from its window title
                    # exact details straight from Spotify when connected; otherwise look up the cover and length
                    # (Deezer/iTunes only see the song's name when Spotify can't tell us) and keep time ourselves
                    if not from_spotify(now):
                        info = song_info(now.get("artist", ""), now.get("title", ""))
                        pos = spotify_position(f"{now.get('artist')}|{now.get('title')}", True)
                        if info["len"]:
                            pos %= info["len"]  # on repeat, the same song starts over
                        now.update(art=info["no"], length=round(info["len"], 1), position=round(pos, 1), estimated=True)
                else:
                    spotify_position(None, False)  # Spotify paused or gone: stop its clock
                    if now.get("art"):
                        now["art"] = f"w{now['art']}"  # Windows' own picture
                NOW = now  # swapped whole, so a request never sees half of it
            print("Now playing reader stopped; restarting it")
        except Exception as e:
            print(f"Now playing not available: {type(e).__name__}")
        NOW = {"playing": False}
        time.sleep(10)


class Deck(BaseHTTPRequestHandler):
    timeout = 15  # seconds to send the request; idle or stuck connections are closed
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
            f = DATA / "usage.json"
            return self._send(200, f.read_bytes() if f.exists() else b"{}", "application/json")
        if url.path == "/usagehistory":  # only the usage screen asks for this (it's the biggest answer here)
            f = DATA / "usage-history.json"
            return self._send(200, f.read_bytes() if f.exists() else b"[]", "application/json")
        if url.path == "/nowplaying":
            pings = [p for p, t in list(PINGS.items()) if time.time() - t < 6 * 3600]  # a ping fades after 6 hours unseen
            return self._send(200, json.dumps({**NOW, "pings": pings}).encode(), "application/json")
        if url.path == "/art":
            v = parse_qs(url.query).get("v", [""])[0]
            try:
                if v.startswith("c"):
                    img = COVER_IMG[v]
                elif v.startswith("w") and v == NOW.get("art"):  # Windows' picture, only while it's still that one
                    img = ART.read_bytes()
                else:
                    raise KeyError(v)
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
        if url.path.startswith("/ping/"):  # an app or script asking for a pad's ping (?clear=1 takes it off)
            pad = url.path.removeprefix("/ping/")
            if pad not in ACTIONS:
                return self._send(404, b"Unknown pad")
            if parse_qs(url.query).get("clear"):
                PINGS.pop(pad, None)
            elif not (WINDOWS.get(pad) and _exe_of(_u32.GetForegroundWindow()) == WINDOWS[pad]):  # you're already looking at it
                PINGS[pad] = time.time()
            return self._send(204)
        target = ACTIONS.get(url.path.removeprefix("/run/"))
        if target is None:
            return self._send(404, b"Unknown pad")
        PINGS.pop(url.path.removeprefix("/run/"), None)  # opening the app from the deck counts as seeing it
        if not start_pad(url.path.removeprefix("/run/"), target):
            return self._send(502, b"Couldn't open it")  # the deck then says so, instead of "Opened"
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
        log = DATA / "deck.log"
        if log.exists() and log.stat().st_size > 5_000_000:  # keep it from growing forever: one old copy is kept
            os.replace(log, DATA / "deck.log.1")
        sys.stdout = sys.stderr = open(log, "a", buffering=1, encoding="utf-8")
    ip = lan_ip_or_none()
    print(f"Home Deck helper is running at http://{ip}:{PORT} (the Echo's PC pads call it)." if ip
          else f"Home Deck helper is running on port {PORT} (no network yet).")
    print("Keep this window open. Press Ctrl+C to stop.")
    if SETTINGS.get("claude_usage", True):
        threading.Thread(target=usage_loop, daemon=True).start()
    threading.Thread(target=shell_hook_loop, daemon=True).start()
    if SETTINGS.get("now_playing", True):
        threading.Thread(target=now_playing_loop, daemon=True).start()
        if SP_ID and SP_TOKEN_FILE.exists():
            threading.Thread(target=spotify_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Deck).serve_forever()
