"""Copy the deck onto the Echo, so its pages work with this PC off.

Run after editing any page, pads.json or settings.json (Echo plugged in by USB):
    python sync_echo.py

It writes config.js (this PC's address, the deck key, your weather place and calendar links) and
pads.js (from pads.json), pushes them with the pages to the Echo, and points
Fully's start page at the copy on the Echo. Only the PC pads still need the PC.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from deck_server import HERE, KEY, PADS, PORT, SETTINGS, lan_ip

ADB = SETTINGS.get("adb") or shutil.which("adb")  # set "adb" in settings.json if adb isn't on your PATH
if not ADB:
    sys.exit("Can't find adb. Install Android platform-tools, or put adb's full path in settings.json.")
ECHO_DIR = "/sdcard/Android/data/de.ozerov.fully/files/home-deck"  # Fully's own folder: readable without extra permissions
START_URL = f"file://{ECHO_DIR}/index.html"
PREFS = "/data/data/de.ozerov.fully/shared_prefs/de.ozerov.fully_preferences.xml"
PAGES = ["index.html", "timer.html", "weather.html", "calendar.html", "google.html", "alarm.js", "wheel.js"]
if not (HERE / "settings.json").exists():
    print("No settings.json yet, so using settings.example.json. Copy it to settings.json and fill in your own place.")


def adb(*args):
    return subprocess.run([ADB, *args], check=True, capture_output=True, text=True).stdout


def main():
    cals = [c for c in SETTINGS.get("calendars", []) if c.startswith("https://")]
    pads = {side: [{k: (True if k == "pc" else v) for k, v in p.items()} for p in PADS[side]] for side in ("left", "right")}  # the PC commands stay on the PC
    config = {"pc": f"http://{lan_ip()}:{PORT}", "key": KEY, "calendars": cals,
              "weather": SETTINGS.get("weather"), "usage": SETTINGS.get("claude_usage", True)}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "config.js").write_text("window.DECK = " + json.dumps(config) + ";\n", encoding="utf-8")
        (tmp / "pads.js").write_text("window.PADS = " + json.dumps(pads) + ";\n", encoding="utf-8")
        adb("shell", f"mkdir -p {ECHO_DIR}")
        for f in [HERE / p for p in PAGES] + [tmp / "config.js", tmp / "pads.js"]:
            adb("push", str(f), f"{ECHO_DIR}/{f.name}")

    # point Fully at the copy on the screen by editing its settings file while it's stopped.
    # That needs root (an unlocked Echo has it). On a normal tablet it's skipped and you set the Start URL once in Fully.
    adb("shell", "am force-stop de.ozerov.fully")
    try:
        prefs = adb("shell", f"cat {PREFS}")
        if "<map" not in prefs:  # some adb versions print "Permission denied" but still exit 0
            raise subprocess.CalledProcessError(1, "cat")
        if START_URL not in prefs:
            prefs = re.sub(r'<string name="startURL">.*?</string>', f'<string name="startURL">{START_URL}</string>', prefs)
            with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8", newline="\n") as t:
                t.write(prefs.replace("\r\n", "\n"))
            adb("push", t.name, "/data/local/tmp/fully.xml")
            adb("shell", f"cat /data/local/tmp/fully.xml > {PREFS} && rm /data/local/tmp/fully.xml")  # cat keeps the file's owner
            Path(t.name).unlink()
    except subprocess.CalledProcessError:
        print(f"No root, so Fully's start page wasn't changed. In Fully, set Settings > Web Content > Start URL to:\n  {START_URL}")
    # Fully asks for this once its start page is a file; granting it here skips the dialog
    try:
        adb("shell", "pm grant de.ozerov.fully android.permission.READ_EXTERNAL_STORAGE")
    except subprocess.CalledProcessError:
        print("Couldn't grant Fully storage access. Allow it when Fully asks.")
    adb("shell", "am start -n de.ozerov.fully/.MainActivity")  # always bring Fully back
    print(f"Synced {len(PAGES) + 2} files to the Echo ({len(cals)} calendar link(s)). The deck now opens from the Echo itself.")


if __name__ == "__main__":
    main()
