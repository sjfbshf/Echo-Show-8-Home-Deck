"""Ping a pad on the deck: its app is done or wants you. Meant for app hooks, so it's quick and never fails loudly.

    pythonw ping_deck.py <pad id>               ping one pad (e.g. claude, chatgpt, terminal)
    pythonw ping_deck.py auto                   for Claude Code hooks: "terminal" when Claude Code runs in a terminal,
                                                "claude" when it runs inside the Claude app
    pythonw ping_deck.py <pad id> -- <command>  run another command first (to keep an app's own notify hook working)

The deck key is read from %USERPROFILE%\\.homedeck\\.deck-key, so it never has to be written into a hook.
"""
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

args = sys.argv[1:]
if "--" in args:  # an app's own notify command comes first, unchanged, with whatever the app passes after it
    cut = args.index("--")
    try:
        subprocess.Popen(args[cut + 1:])
    except OSError:
        pass
    args = args[:cut]  # the rest was the app's command
pad = args[0] if args else "auto"
if pad == "auto":
    pad = "terminal" if os.environ.get("CLAUDE_CODE_ENTRYPOINT") == "cli" else "claude"
try:
    key = (Path.home() / ".homedeck" / ".deck-key").read_text().strip()
    urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:8765/ping/{pad}?k={key}", method="POST"), timeout=2)
except Exception:
    pass  # helper not running: nothing to ping, and a hook must never get in the app's way
