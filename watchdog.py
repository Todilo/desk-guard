"""
Desk Guard watchdog: keeps the tray monitor process alive and locks the
workstation immediately if it's killed outside of an authorized, PIN-gated
quit (e.g. someone ending it from Task Manager to dodge the lock timers).

This is meant to autostart instead of tray.py directly - it launches tray.py
itself and supervises it. Killing tray.py alone no longer disables protection;
killing this watchdog alone leaves tray.py's own detection running untouched.

Usage:
    pythonw.exe watchdog.py     (no console window; this is what autostart uses)
    python.exe watchdog.py      (console window, useful while testing)
"""

import os
import subprocess
import sys
import time

import desk_guard as dg

CHECK_INTERVAL = 2.0

# Circuit breaker: if tray.py keeps dying right after being relaunched (a bad
# settings.json, a corrupted model file, a future bug - anything that makes it
# crash on every startup), locking + relaunching every couple seconds would
# turn a bug into a lockout with no way back in. So after a handful of rapid
# failures, stop locking and back off exponentially instead - still retrying
# quietly (so it self-heals once the underlying problem is fixed) but never
# hammering the lock screen forever over a malfunction rather than a kill.
FAILURE_WINDOW = 600  # seconds of failures that count toward the threshold
MAX_FAILURES_BEFORE_BACKOFF = 5
BACKOFF_START = 60
BACKOFF_MAX = 1800


def launch_tray():
    cmd = dg.launch_command("tray.py", "Desk Guard.exe")
    proc = subprocess.Popen(cmd, cwd=dg.BASE_DIR)
    dg.log(f"[watchdog] launched tray (pid {proc.pid})")
    return proc.pid


def main():
    dg.write_pid(dg.WATCHDOG_PID_PATH)
    dg.clear_shutdown_flag()

    tray_pid = dg.read_pid(dg.TRAY_PID_PATH)
    if not dg.pid_alive(tray_pid):
        tray_pid = launch_tray()

    dg.log("[watchdog] started")

    failure_times = []
    backoff = 0.0
    backoff_until = 0.0

    try:
        while True:
            time.sleep(CHECK_INTERVAL)

            if dg.is_shutdown_authorized():
                # Don't clear the flag here - tray.py may still be alive and
                # mid-shutdown (e.g. stop_all.py hasn't force-killed it yet).
                # Clearing it too early let tray's own watchdog-guard thread
                # see "no flag + watchdog gone" and relaunch a second
                # watchdog, which then spawned a second tray.py. Only a fresh
                # startup (both main()s do this) should ever clear the flag.
                dg.log("[watchdog] authorized shutdown detected, exiting")
                break

            now = time.time()
            failure_times[:] = [t for t in failure_times if now - t < FAILURE_WINDOW]
            if not failure_times:
                backoff = 0.0

            if dg.pid_alive(tray_pid):
                continue

            failure_times.append(now)

            if len(failure_times) > MAX_FAILURES_BEFORE_BACKOFF:
                if now < backoff_until:
                    continue  # cooling down - no lock, no relaunch yet
                backoff = min(max(backoff * 2, BACKOFF_START), BACKOFF_MAX)
                backoff_until = now + backoff
                dg.log(f"[watchdog] tray.py has crashed {len(failure_times)} times in "
                       f"{FAILURE_WINDOW}s - this looks like a malfunction, not a kill. "
                       f"Backing off {backoff:.0f}s without locking; check desk_guard.log. "
                       "Trying a quiet relaunch now.")
                dg.queue_notification(
                    "Desk Guard",
                    f"Tray app keeps crashing ({len(failure_times)}x) - backed off {backoff:.0f}s "
                    "without locking. Check desk_guard.log.",
                )
                tray_pid = launch_tray()
                continue

            dg.log("[watchdog] tray process disappeared without authorization "
                    "- locking workstation and relaunching")
            dg.queue_notification(
                "Desk Guard",
                "Tray app was ended unexpectedly - workstation locked and protection restarted.",
            )
            dg.lock_workstation()
            tray_pid = launch_tray()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            os.remove(dg.WATCHDOG_PID_PATH)
        except Exception:
            pass


if __name__ == "__main__":
    main()
