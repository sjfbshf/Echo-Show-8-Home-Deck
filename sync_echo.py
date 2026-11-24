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

from deck_server import HERE, KEY, PADS, PORT, SETTINGS, SETTINGS_FILE, lan_ip

ADB = SETTINGS.get("adb") or shutil.which("adb")  # set "adb" in settings.json if adb isn't on your PATH
if not ADB:
    sys.exit("Can't find adb. Install Android platform-tools, or put adb's full path in settings.json.")
ECHO_DIR = "/sdcard/Android/data/de.ozerov.fully/files/home-deck"  # Fully's own folder: readable without extra permissions
START_URL = f"file://{ECHO_DIR}/index.html"
PREFS = "/data/data/de.ozerov.fully/shared_prefs/de.ozerov.fully_preferences.xml"
PAGES = ["index.html", "timer.html", "weather.html", "calendar.html", "google.html", "alarm.js", "wheel.js"]
if SETTINGS_FILE.name != "settings.json":
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
        new = ECHO_DIR + ".new"
        adb("shell", f"rm -rf {new} && mkdir -p {new}")
        files = [HERE / p for p in PAGES] + [tmp / "config.js", tmp / "pads.js"]
        for f in files:
            adb("push", str(f), f"{new}/{f.name}")
        if adb("shell", f"ls {new} | wc -l").strip() != str(len(files)):
            raise SystemExit("Not every file reached the Echo; the deck there wasn't changed. Run sync_echo.py again.")
        adb("shell", f"rm -rf {ECHO_DIR}.old; [ -d {ECHO_DIR} ] && mv {ECHO_DIR} {ECHO_DIR}.old; mv {new} {ECHO_DIR} && rm -rf {ECHO_DIR}.old")

    # point Fully at the copy on the screen by editing its settings file while it's stopped.
    # That needs root (an unlocked Echo has it). On a normal tablet it's skipped and you set the Start URL once in Fully.
    adb("shell", "am force-stop de.ozerov.fully")
    try:
        prefs = adb("shell", f"cat {PREFS}")
        if "<map" not in prefs:  # some adb versions print "Permission denied" but still exit 0
            raise subprocess.CalledProcessError(1, "cat")
        if START_URL not in prefs:
            line = f'<string name="startURL">{START_URL}</string>'
            if re.search(r'<string name="startURL">.*?</string>', prefs):
                prefs = re.sub(r'<string name="startURL">.*?</string>', line, prefs)
            else:  # a fresh Fully has no start page saved yet: add one
                prefs = prefs.replace("</map>", f"    {line}\n</map>")
            with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8", newline="\n") as t:
                t.write(prefs.replace("\r\n", "\n"))
            try:
                adb("push", t.name, "/data/local/tmp/fully.xml")
                # the new file goes next to the real one with the same owner, is checked, and only then replaces
                # it in one step, so Fully's settings are never left half-written
                who = adb("shell", f"stat -c %u:%g {PREFS}").strip()
                adb("shell", f"cp /data/local/tmp/fully.xml {PREFS}.new && chown {who} {PREFS}.new && chmod 660 {PREFS}.new"
                             f" && (restorecon {PREFS}.new 2>/dev/null; true)")
                if START_URL not in adb("shell", f"cat {PREFS}.new") or "</map>" not in adb("shell", f"tail -c 20 {PREFS}.new"):
                    raise RuntimeError("the new settings file didn't check out")
                adb("shell", f"mv {PREFS}.new {PREFS}")
            except subprocess.CalledProcessError as e:
                raise RuntimeError(f"copying the settings failed ({(e.stderr or '').strip()[:80]})") from e
            finally:  # never leave copies of Fully's settings behind
                Path(t.name).unlink(missing_ok=True)
                subprocess.run([ADB, "shell", f"rm -f /data/local/tmp/fully.xml {PREFS}.new"], capture_output=True)
    except subprocess.CalledProcessError:
        print(f"No root, so Fully's start page wasn't changed. In Fully, set Settings > Web Content > Start URL to:\n  {START_URL}")
    except RuntimeError as e:
        print(f"Couldn't change Fully's start page: {e}. Its settings were left as they were.\n"
              f"In Fully, set Settings > Web Content > Start URL to:\n  {START_URL}")
    # Fully asks for this once its start page is a file; granting it here skips the dialog
    try:
        adb("shell", "pm grant de.ozerov.fully android.permission.READ_EXTERNAL_STORAGE")
    except subprocess.CalledProcessError:
        print("Couldn't grant Fully storage access. Allow it when Fully asks.")
    adb("shell", "am start -n de.ozerov.fully/.MainActivity")  # always bring Fully back
    print(f"Synced {len(PAGES) + 2} files to the Echo ({len(cals)} calendar link(s)). The deck now opens from the Echo itself.")


if __name__ == "__main__":
    main()
