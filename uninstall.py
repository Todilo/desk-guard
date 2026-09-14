"""
Uninstall Desk Guard: stops both running processes, removes the Startup
autostart shortcut, and optionally deletes your enrolled face data, trained
model, settings, and PIN. Leaves the script files themselves in place - the
desk-guard folder can be deleted manually afterward if you want.

Usage:
    python uninstall.py
"""

import os
import shutil
import subprocess
import sys

import desk_guard as dg
import pin_auth

STARTUP_SHORTCUT = os.path.join(
    os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "Desk Guard.lnk"
)


def stop_running():
    print("Stopping Desk Guard...")
    subprocess.run([sys.executable, os.path.join(dg.BASE_DIR, "stop_all.py")])


def remove_autostart():
    if os.path.exists(STARTUP_SHORTCUT):
        os.remove(STARTUP_SHORTCUT)
        print("Removed autostart shortcut.")
    else:
        print("No autostart shortcut found (already removed, or never set up).")


def remove_data():
    targets = [
        dg.FACES_DIR, dg.MODEL_PATH, dg.SETTINGS_PATH, pin_auth.PIN_PATH,
        dg.LOG_PATH, dg.LOG_PATH + ".1",
        dg.TRAY_PID_PATH, dg.WATCHDOG_PID_PATH, dg.SHUTDOWN_FLAG_PATH,
    ]
    for path in targets:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            elif os.path.exists(path):
                os.remove(path)
        except Exception as e:
            print(f"Could not remove {path}: {e}")
    print("Removed enrolled face data, trained model, settings, PIN, and logs.")


def main():
    stop_running()
    remove_autostart()

    answer = input(
        "Also delete your enrolled face data, trained model, settings, and PIN? [y/N]: "
    ).strip().lower()
    if answer == "y":
        remove_data()
    else:
        print("Kept face data/model/settings/PIN.")

    print("Done. The desk-guard folder itself (scripts) hasn't been deleted - "
          "remove it manually if you want it gone completely.")


if __name__ == "__main__":
    main()
