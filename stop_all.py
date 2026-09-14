"""
Stop the tray app and watchdog cleanly for maintenance or updates, without
triggering a lock or auto-relaunch. Killing either process directly (e.g. in
Task Manager) is what the watchdog and tray are each designed to react to as
a hostile kill - this script sets the same authorized-shutdown flag a PIN-
verified Quit would, so neither reacts.

Usage:
    python stop_all.py
"""

import ctypes
import time

import desk_guard as dg

PROCESS_TERMINATE = 0x0001


def force_kill(pid):
    if not pid or not dg.pid_alive(pid):
        return
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if handle:
        ctypes.windll.kernel32.TerminateProcess(handle, 0)
        ctypes.windll.kernel32.CloseHandle(handle)


def main():
    dg.mark_authorized_shutdown()
    print("Shutdown flag set. Waiting for the watchdog to exit...")

    wd_pid = dg.read_pid(dg.WATCHDOG_PID_PATH)
    for _ in range(10):
        if not dg.pid_alive(wd_pid):
            break
        time.sleep(0.5)

    tray_pid = dg.read_pid(dg.TRAY_PID_PATH)
    if dg.pid_alive(tray_pid):
        print(f"Stopping tray.py (pid {tray_pid})...")
        force_kill(tray_pid)

    time.sleep(0.5)
    force_kill(dg.read_pid(dg.WATCHDOG_PID_PATH))  # in case it respawned tray meanwhile
    dg.clear_shutdown_flag()
    print("Stopped.")


if __name__ == "__main__":
    main()
