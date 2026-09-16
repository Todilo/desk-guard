"""
Desk Guard tray app: run face-based auto-lock monitoring in the background,
toggled on/off from a Windows system tray icon.

Disabling monitoring, quitting, and re-training all require the PIN (once one
is set) so a stranger who sits down can't just right-click their way past
protection before the away/unknown-face timer fires.

Tkinter is not reliably thread-safe on Windows when multiple interpreters run
their own event loops on different threads at once (dialogs render but never
receive real keyboard/mouse input). So a single Tk root lives on the main
thread and drives every dialog; pystray's tray icon runs detached on its own
thread and hands dialog requests to the main thread through a queue.

Usage:
    pythonw.exe tray.py     (no console window)
    python.exe tray.py      (console window, useful while testing)
"""

import ctypes
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import messagebox

from PIL import Image, ImageDraw
import pystray

import desk_guard as dg
import pin_auth

enabled = threading.Event()
enabled.set()
stop_flag = threading.Event()  # controls only the monitor thread (retrain/settings restart it)
shutting_down = threading.Event()  # set only on a real, PIN-verified app quit
_monitor_thread = None

_root = None
_icon = None
_dialog_queue = queue.Queue()

# Camera health, reported by dg.monitor()'s on_camera_status callback. Tracked
# separately from "enabled" so a stalled/unplugged camera shows up as its own
# icon state instead of silently looking identical to "protected and fine".
CAMERA_WARN_AFTER = 20  # seconds of camera failure before the icon turns amber
_camera_ok = True
_camera_bad_since = None

_snooze_timer = None
_snooze_resume_at = None
_camera_warned = False


def make_dot(color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((8, 8, 56, 56), fill=color)
    return img


ICON_ON = make_dot((40, 180, 80, 255))
ICON_OFF = make_dot((140, 140, 140, 255))
ICON_WARN = make_dot((230, 160, 30, 255))


def on_camera_status(ok: bool):
    global _camera_ok, _camera_bad_since
    if ok:
        _camera_ok = True
        _camera_bad_since = None
    else:
        if _camera_ok:
            _camera_bad_since = time.time()
        _camera_ok = False


def update_icon_tick():
    # Self-rescheduling: if anything in the body throws (a transient Windows
    # API failure, a stale icon handle after long uptime, etc.) without this
    # try/finally, the function aborts before reaching its own reschedule
    # call below and this timer permanently stops - freezing the tray icon
    # in whatever state it last rendered, forever. Same class of bug already
    # fixed once for _process_dialog_queue.
    global _camera_warned
    try:
        if _icon is not None:
            if not enabled.is_set():
                _icon.icon = ICON_OFF
                if _snooze_resume_at is not None:
                    remaining_min = max(0, int((_snooze_resume_at - time.time()) // 60) + 1)
                    _icon.title = f"Desk Guard - snoozed, resumes in ~{remaining_min} min"
                else:
                    _icon.title = "Desk Guard - monitoring disabled"
            elif not _camera_ok and _camera_bad_since and (time.time() - _camera_bad_since) > CAMERA_WARN_AFTER:
                _icon.icon = ICON_WARN
                _icon.title = "Desk Guard - camera unavailable"
                if not _camera_warned:
                    _camera_warned = True
                    _icon.notify(
                        "Camera has been unavailable for a while - protection may not be active.",
                        "Desk Guard",
                    )
            else:
                _icon.icon = ICON_ON
                _icon.title = "Desk Guard"
                _camera_warned = False
    except Exception:
        dg.log(f"update_icon_tick error:\n{traceback.format_exc()}")
    finally:
        if _root is not None:
            _root.after(2000, update_icon_tick)


_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32


def _force_foreground(win):
    # A background process (this tray app is never the foreground window) can't
    # normally steal keyboard focus on Windows: SetForegroundWindow alone is
    # blocked by the foreground-lock. Borrowing the foreground thread's input
    # queue via AttachThreadInput is the reliable way around that.
    hwnd = win.winfo_id()
    fg_hwnd = _user32.GetForegroundWindow()
    cur_thread = _kernel32.GetCurrentThreadId()
    fg_thread = _user32.GetWindowThreadProcessId(fg_hwnd, None)

    attached = False
    if fg_thread and fg_thread != cur_thread:
        attached = bool(_user32.AttachThreadInput(fg_thread, cur_thread, True))
    try:
        _user32.SetForegroundWindow(hwnd)
        _user32.BringWindowToTop(hwnd)
        _user32.SetFocus(hwnd)
    finally:
        if attached:
            _user32.AttachThreadInput(fg_thread, cur_thread, False)


# ---- dialogs: only ever run on the main thread, via the queue below ----

def _ask_pin_on_main_thread(prompt: str):
    result = {"value": None}
    dialog = tk.Toplevel(_root)
    dialog.title("Desk Guard")
    dialog.attributes("-topmost", True)
    dialog.resizable(False, False)
    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

    tk.Label(dialog, text=prompt, padx=20, pady=10).pack()
    var = tk.StringVar()
    entry = tk.Entry(dialog, textvariable=var, show="*", width=24)
    entry.pack(padx=20, pady=5)

    def submit(event=None):
        result["value"] = var.get()
        dialog.destroy()

    def cancel(event=None):
        result["value"] = None
        dialog.destroy()

    entry.bind("<Return>", submit)
    dialog.bind("<Escape>", cancel)

    btn_frame = tk.Frame(dialog)
    btn_frame.pack(pady=(0, 10))
    tk.Button(btn_frame, text="OK", command=submit, width=8).pack(side="left", padx=5)
    tk.Button(btn_frame, text="Cancel", command=cancel, width=8).pack(side="left", padx=5)

    dialog.update_idletasks()
    w, h = dialog.winfo_width(), dialog.winfo_height()
    sw, sh = dialog.winfo_screenwidth(), dialog.winfo_screenheight()
    dialog.geometry(f"+{(sw - w) // 2}+{(sh - h) // 3}")

    dialog.deiconify()
    dialog.lift()
    _force_foreground(dialog)
    dialog.focus_force()
    entry.focus_set()
    dialog.grab_set()

    dialog.wait_window()
    return result["value"]


def _show_message_on_main_thread(text: str, error: bool):
    _force_foreground(_root)
    if error:
        messagebox.showerror("Desk Guard", text, parent=_root)
    else:
        messagebox.showinfo("Desk Guard", text, parent=_root)


SETTINGS_FIELDS = [
    ("interval", "Check interval (seconds)"),
    ("threshold", "Match threshold (lower = stricter)"),
    ("grace", "Unrecognized-face grace (seconds)"),
    ("away_grace", "Away grace - no face at all (seconds, 0 = off)"),
    ("cooldown", "Cooldown after a lock (seconds)"),
]


def _settings_dialog_on_main_thread(current: dict):
    result = {"value": None}
    dialog = tk.Toplevel(_root)
    dialog.title("Desk Guard Settings")
    dialog.attributes("-topmost", True)
    dialog.resizable(False, False)
    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

    vars_ = {}
    for row, (key, label) in enumerate(SETTINGS_FIELDS):
        tk.Label(dialog, text=label).grid(row=row, column=0, sticky="w", padx=10, pady=5)
        var = tk.StringVar(value=str(current[key]))
        vars_[key] = var
        tk.Entry(dialog, textvariable=var, width=10).grid(row=row, column=1, padx=10, pady=5)

    error_label = tk.Label(dialog, text="", fg="red")
    error_label.grid(row=len(SETTINGS_FIELDS), column=0, columnspan=2)

    def submit(event=None):
        try:
            new_settings = {key: float(vars_[key].get()) for key, _ in SETTINGS_FIELDS}
        except ValueError:
            error_label.config(text="All fields must be numbers.")
            return
        if new_settings["interval"] <= 0:
            error_label.config(text="Check interval must be greater than 0.")
            return
        if any(new_settings[k] < 0 for k in ("grace", "away_grace", "cooldown", "threshold")):
            error_label.config(text="Values must be zero or positive.")
            return
        result["value"] = new_settings
        dialog.destroy()

    def cancel(event=None):
        dialog.destroy()

    dialog.bind("<Escape>", cancel)
    btn_frame = tk.Frame(dialog)
    btn_frame.grid(row=len(SETTINGS_FIELDS) + 1, column=0, columnspan=2, pady=10)
    tk.Button(btn_frame, text="Save", command=submit, width=8).pack(side="left", padx=5)
    tk.Button(btn_frame, text="Cancel", command=cancel, width=8).pack(side="left", padx=5)

    dialog.update_idletasks()
    w, h = dialog.winfo_width(), dialog.winfo_height()
    sw, sh = dialog.winfo_screenwidth(), dialog.winfo_screenheight()
    dialog.geometry(f"+{(sw - w) // 2}+{(sh - h) // 3}")

    dialog.deiconify()
    dialog.lift()
    _force_foreground(dialog)
    dialog.focus_force()
    dialog.grab_set()

    dialog.wait_window()
    return result["value"]


def _about_dialog_on_main_thread(sections):
    # Unlike the PIN/Settings dialogs, this is read-only and mouse-only - no
    # typed input, so it doesn't need the foreground-forcing/grab dance those
    # need to receive keyboard input. Mouse clicks are delivered by screen
    # position regardless of which window Windows considers "foreground", so
    # a plain topmost window is enough (and far less likely to misbehave).
    dg.log("about: building window")
    bg = "#f7f7f5"
    dialog = tk.Toplevel(_root)
    dialog.title("Desk Guard - About")
    dialog.attributes("-topmost", True)
    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
    dialog.configure(bg=bg)

    text_widget = tk.Text(dialog, wrap="word", padx=18, pady=16, bg=bg,
                           relief="flat", borderwidth=0, highlightthickness=0,
                           font=("Consolas", 9))
    text_widget.tag_configure("title", font=("Segoe UI", 16, "bold"), spacing3=2)
    text_widget.tag_configure("subtitle", font=("Segoe UI", 9, "italic"),
                               foreground="#666666", spacing3=12)
    text_widget.tag_configure("header", font=("Consolas", 10, "bold"),
                               foreground="#1a1a1a", spacing1=10, spacing3=3)
    text_widget.tag_configure("body", font=("Consolas", 9), foreground="#2b2b2b")

    for tag, line in sections:
        text_widget.insert("end", line + "\n", tag)

    text_widget.config(state="disabled")
    text_widget.pack(fill="both", expand=True)

    button_bar = tk.Frame(dialog, bg=bg)
    button_bar.pack(fill="x", pady=(0, 12))
    tk.Button(button_bar, text="Close", command=dialog.destroy, width=10).pack()

    w, h = 560, 500
    sw, sh = dialog.winfo_screenwidth(), dialog.winfo_screenheight()
    dialog.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 3}")

    dg.log("about: showing window")
    dialog.deiconify()
    dialog.lift()
    dg.log("about: entering wait_window")
    dialog.wait_window()
    dg.log("about: closed")


def _process_dialog_queue():
    # A bug in any one dialog must never stop this from being rescheduled -
    # that would silently brick every future tray action that needs a dialog
    # (including PIN prompts), with no console to see why.
    try:
        while True:
            try:
                kind, kwargs, result_holder, done = _dialog_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if kind == "ask_pin":
                    result_holder["value"] = _ask_pin_on_main_thread(kwargs["prompt"])
                elif kind == "message":
                    _show_message_on_main_thread(kwargs["text"], kwargs["error"])
                elif kind == "settings":
                    result_holder["value"] = _settings_dialog_on_main_thread(kwargs["current"])
                elif kind == "about":
                    _about_dialog_on_main_thread(kwargs["sections"])
                elif kind == "quit":
                    _root.quit()
            except Exception:
                dg.log(f"dialog error ({kind}):\n{traceback.format_exc()}")
            finally:
                done.set()
    finally:
        _root.after(50, _process_dialog_queue)


def _submit_dialog(kind: str, **kwargs):
    result_holder = {}
    done = threading.Event()
    _dialog_queue.put((kind, kwargs, result_holder, done))
    done.wait()
    return result_holder.get("value")


def ask_pin(prompt: str):
    return _submit_dialog("ask_pin", prompt=prompt)


def show_message(text: str, error: bool = False):
    _submit_dialog("message", text=text, error=error)


def open_settings_dialog(current: dict):
    return _submit_dialog("settings", current=current)


def show_about():
    _submit_dialog("about", sections=build_about_sections())


def build_about_sections():
    """Returns a list of (tag, line) pairs for the About window's Text widget,
    where tag selects the visual style (title/subtitle/header/body)."""
    s = dg.load_settings()
    has_pin = pin_auth.has_pin()

    sections = [
        ("title", "Desk Guard"),
        ("subtitle", "Automatic, camera-based desk lock"),
        ("body", ""),
        ("body", "Watches the webcam and locks this PC when it can't confirm"),
        ("body", "you're the one sitting there."),
        ("body", ""),
        ("header", "Current timings"),
        ("body", f"  Check interval            {s['interval']:.1f}s"),
        ("body", "     how often it samples the webcam"),
        ("body", f"  Unrecognized-face grace   {s['grace']:.1f}s"),
        ("body", "     how long a mismatched face must persist before locking"),
        ("body", f"  Away grace                {s['away_grace']:.1f}s"),
        ("body", "     how long no face at all before locking (0 = disabled)"),
        ("body", f"  Cooldown after lock       {s['cooldown']:.1f}s"),
        ("body", "     pause before resuming checks after a lock"),
        ("body", f"  Match threshold           {s['threshold']:.0f}"),
        ("body", "     lower = stricter match to your enrolled face"),
        ("body", ""),
        ("header", "PIN protection"),
        ("body", f"  {'Configured' if has_pin else 'NOT SET - configure one from this menu'}"),
        ("body", "  Required for: disabling monitoring, re-training, changing"),
        ("body", "  settings, and quitting."),
        ("body", ""),
        ("header", "Kill-resistance"),
        ("body", "  A separate watchdog process relaunches this app and"),
        ("body", "  immediately locks the workstation if it's ended outside an"),
        ("body", "  authorized, PIN-verified quit (e.g. from Task Manager)."),
        ("body", ""),
        ("header", "Autostart"),
        ("body", "  Enabled via a Startup-folder shortcut (runs invisibly,"),
        ("body", "  no console window)."),
        ("body", ""),
        ("header", "Files (in the app folder)"),
        ("body", "  faces/owner/       your enrolled face photos"),
        ("body", "  owner_model.yml    trained face-recognition model"),
        ("body", "  settings.json      saved timing configuration"),
        ("body", "  pin.dat            salted PIN hash (never the PIN itself)"),
        ("body", "  desk_guard.log     activity log"),
        ("body", "  README.md          full documentation"),
    ]
    return sections


PIN_LOCKOUT_THRESHOLD = 3  # failures allowed before the first lockout
PIN_LOCKOUT_BASE = 30      # seconds
PIN_LOCKOUT_MAX = 300

_pin_failures = 0
_pin_lockout_until = 0.0


def require_pin(action_label: str) -> bool:
    global _pin_failures, _pin_lockout_until

    if not pin_auth.has_pin():
        return True

    now = time.time()
    if now < _pin_lockout_until:
        remaining = int(_pin_lockout_until - now) + 1
        show_message(f"Too many incorrect PIN attempts. Try again in {remaining}s.", error=True)
        return False

    pin = ask_pin(f"Enter PIN to {action_label}:")
    if pin is None:
        return False

    if pin_auth.verify_pin(pin):
        _pin_failures = 0
        return True

    _pin_failures += 1
    dg.log(f"PIN check failed for action: {action_label} (failure #{_pin_failures})")

    if _pin_failures >= PIN_LOCKOUT_THRESHOLD:
        delay = min(PIN_LOCKOUT_BASE * (2 ** (_pin_failures - PIN_LOCKOUT_THRESHOLD)), PIN_LOCKOUT_MAX)
        _pin_lockout_until = now + delay
        dg.log(f"PIN lockout: {delay:.0f}s after {_pin_failures} consecutive failures")
        show_message(f"Too many incorrect PIN attempts. Locked out for {delay:.0f}s.", error=True)
    else:
        show_message("Incorrect PIN.", error=True)
    return False


def set_pin_action(icon, item):
    if pin_auth.has_pin() and not require_pin("change the PIN"):
        return
    pin1 = ask_pin("Enter new PIN:")
    if not pin1:
        return
    pin2 = ask_pin("Confirm new PIN:")
    if pin1 != pin2:
        show_message("PINs did not match. PIN not changed.", error=True)
        return
    pin_auth.set_pin(pin1)
    dg.log("PIN set/changed from tray")
    show_message("PIN updated.")


def on_lock(reason: str):
    if _icon is not None:
        _icon.notify(f"Workstation locked ({reason}).", "Desk Guard")


def monitor_loop():
    s = dg.load_settings()
    try:
        dg.monitor(
            s["interval"], s["threshold"], s["grace"], s["away_grace"], s["cooldown"],
            is_enabled=enabled.is_set,
            is_stopped=stop_flag.is_set,
            on_camera_status=on_camera_status,
            on_lock=on_lock,
        )
    except SystemExit as e:
        # e.g. get_detector()/model files missing - sys.exit(), not an Exception
        dg.log(f"monitor thread exited: {e}")
    except Exception:
        dg.log(f"monitor thread crashed:\n{traceback.format_exc()}")


def start_monitor_thread():
    global _monitor_thread
    stop_flag.clear()
    _monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    _monitor_thread.start()


def stop_monitor_thread():
    stop_flag.set()
    if _monitor_thread is not None:
        _monitor_thread.join(timeout=5)


def monitor_supervisor_loop():
    # A silent crash here means protection silently stops working with no
    # sign of it - worse than restarting. Same crash-loop guard as the
    # watchdog: back off instead of respawning forever if it keeps dying
    # immediately (e.g. a bad settings value). Skips restarting while
    # stop_flag is set for an intentional pause (retrain/settings/quit
    # already manage that themselves).
    failure_times = []
    backoff = 0.0
    backoff_until = 0.0
    while not shutting_down.is_set():
        time.sleep(5)
        if shutting_down.is_set() or stop_flag.is_set():
            continue
        if _monitor_thread is not None and _monitor_thread.is_alive():
            continue

        now = time.time()
        failure_times[:] = [t for t in failure_times if now - t < 600]
        if not failure_times:
            backoff = 0.0
        failure_times.append(now)

        if len(failure_times) > 5:
            if now < backoff_until:
                continue
            backoff = min(max(backoff * 2, 30), 900)
            backoff_until = now + backoff
            dg.log(f"monitor thread crashed {len(failure_times)} times recently - "
                   f"backing off {backoff:.0f}s between restarts")
            if _icon is not None:
                _icon.notify(
                    f"Detection keeps crashing ({len(failure_times)}x) - check desk_guard.log.",
                    "Desk Guard",
                )

        dg.log("monitor thread not running, restarting it")
        start_monitor_thread()


def toggle_enabled(icon, item):
    if enabled.is_set():
        if not require_pin("disable monitoring"):
            return
        enabled.clear()
        dg.log("monitoring disabled from tray (PIN verified)")
    else:
        cancel_snooze()
        enabled.set()
        dg.log("monitoring enabled from tray")
    icon.icon = ICON_ON if enabled.is_set() else ICON_OFF


def cancel_snooze():
    global _snooze_timer, _snooze_resume_at
    if _snooze_timer is not None:
        _snooze_timer.cancel()
        _snooze_timer = None
    _snooze_resume_at = None


def resume_from_snooze():
    global _snooze_timer, _snooze_resume_at
    _snooze_timer = None
    _snooze_resume_at = None
    if not shutting_down.is_set():
        enabled.set()
        dg.log("monitoring auto-resumed after snooze")


def start_snooze(minutes: int):
    global _snooze_timer, _snooze_resume_at
    cancel_snooze()
    enabled.clear()
    _snooze_resume_at = time.time() + minutes * 60
    dg.log(f"monitoring snoozed for {minutes} minutes from tray (PIN verified)")
    _snooze_timer = threading.Timer(minutes * 60, resume_from_snooze)
    _snooze_timer.daemon = True
    _snooze_timer.start()


def snooze_action_for(minutes: int):
    def handler(icon, item):
        if not require_pin(f"disable monitoring for {minutes} minutes"):
            return
        start_snooze(minutes)
    return handler


def enroll_command():
    """Command to run the enroll flow. When frozen, that's this same exe
    relaunched with --enroll (no Python needed at all); running from source,
    it's python.exe (not pythonw.exe, for console feedback) running this
    script with --enroll."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--enroll"]
    python_exe = dg.find_python_exe("python.exe")
    if python_exe is None:
        return None
    return [python_exe, os.path.abspath(__file__), "--enroll"]


def retrain(icon, item):
    if not require_pin("re-train the face model"):
        return

    dg.log("retrain requested from tray")
    was_enabled = enabled.is_set()
    enabled.clear()
    icon.icon = ICON_OFF
    stop_monitor_thread()

    cmd = enroll_command()
    if cmd is None:
        show_message(
            "Couldn't find a Python install to run enrollment with. "
            "Install Python and make sure python.exe is on PATH, or run "
            "'python desk_guard.py enroll' manually.",
            error=True,
        )
        start_monitor_thread()
        if was_enabled:
            enabled.set()
            icon.icon = ICON_ON
        return

    show_message("Starting enrollment. A camera preview window will open — "
                  "look at the camera and move your head slightly until it finishes.")
    subprocess.run(cmd, cwd=dg.BASE_DIR)
    dg.log("retrain finished, resuming monitoring")

    start_monitor_thread()
    if was_enabled:
        enabled.set()
        icon.icon = ICON_ON


def settings_action(icon, item):
    if not require_pin("change settings"):
        return
    current = dg.load_settings()
    new_settings = open_settings_dialog(current)
    if new_settings is None:
        return
    dg.save_settings(new_settings)
    dg.log(f"settings updated from tray: {new_settings}")
    stop_monitor_thread()
    start_monitor_thread()
    show_message("Settings updated.")


def quit_app(icon, item):
    if not require_pin("quit Desk Guard"):
        return
    dg.log("tray app quitting (PIN verified)")
    dg.mark_authorized_shutdown()
    shutting_down.set()
    cancel_snooze()
    stop_monitor_thread()
    icon.stop()
    _dialog_queue.put(("quit", {}, {}, threading.Event()))


def watchdog_guard_loop():
    # Resilience, not the security boundary: if the watchdog crashes or is
    # killed on its own, relaunch it so kill-resistance doesn't quietly lapse.
    # Killing tray.py itself is what the watchdog defends against, not this,
    # so there's no lock risk here - just a simple cap so a broken watchdog.py
    # can't get spammed-relaunched forever.
    # Uses shutting_down (not stop_flag) since stop_flag also toggles for
    # ordinary retrain/settings restarts of the monitor thread alone.
    failure_times = []
    backoff_until = 0.0
    while not shutting_down.is_set():
        time.sleep(5)
        if dg.is_shutdown_authorized() or shutting_down.is_set():
            continue

        now = time.time()
        failure_times[:] = [t for t in failure_times if now - t < 600]

        wd_pid = dg.read_pid(dg.WATCHDOG_PID_PATH)
        if dg.pid_alive(wd_pid):
            continue

        failure_times.append(now)
        if len(failure_times) > 5:
            if now < backoff_until:
                continue
            backoff_until = now + 300
            dg.log("watchdog keeps disappearing - backing off 300s before retrying")

        dg.log("watchdog missing, relaunching")
        subprocess.Popen(dg.launch_command("watchdog.py", "DeskGuardWatchdog.exe"), cwd=dg.BASE_DIR)


def main():
    global _root, _icon

    if not os.path.exists(dg.MODEL_PATH):
        # sys.exit()'s message goes nowhere under a windowed/no-console exe -
        # the app would just silently vanish on a fresh install with no
        # explanation. Show it instead.
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        _force_foreground(root)
        messagebox.showerror(
            "Desk Guard",
            "No trained face model found.\n\n"
            "Run 'python desk_guard.py enroll' from the app folder first to "
            "capture your face, then start Desk Guard again.",
            parent=root,
        )
        root.destroy()
        # Not a crash - a first-run condition. Mark it authorized so the
        # watchdog cleanly stops too instead of treating this exit as a kill
        # and locking + relaunching into the same dialog forever.
        dg.mark_authorized_shutdown()
        sys.exit(1)

    dg.write_pid(dg.TRAY_PID_PATH)
    dg.clear_shutdown_flag()

    _root = tk.Tk()
    _root.withdraw()
    _root.after(50, _process_dialog_queue)
    _root.after(2000, update_icon_tick)

    start_monitor_thread()
    threading.Thread(target=watchdog_guard_loop, daemon=True).start()
    threading.Thread(target=monitor_supervisor_loop, daemon=True).start()

    snooze_menu = pystray.Menu(
        pystray.MenuItem("5 minutes", snooze_action_for(5)),
        pystray.MenuItem("10 minutes", snooze_action_for(10)),
        pystray.MenuItem("15 minutes", snooze_action_for(15)),
        pystray.MenuItem("30 minutes", snooze_action_for(30)),
    )
    menu = pystray.Menu(
        pystray.MenuItem("Monitoring enabled", toggle_enabled, checked=lambda item: enabled.is_set()),
        pystray.MenuItem("Snooze monitoring", snooze_menu),
        pystray.MenuItem("Settings...", settings_action),
        pystray.MenuItem("Re-train face model...", retrain),
        pystray.MenuItem("Set/change PIN...", set_pin_action),
        pystray.MenuItem("About / How it works...", lambda icon, item: show_about()),
        pystray.MenuItem("Quit", quit_app),
    )
    _icon = pystray.Icon("DeskGuard", ICON_ON, "Desk Guard", menu)
    dg.log("tray app started")
    _icon.run_detached()
    _root.after(1500, check_pending_notification)  # let the tray icon register with the shell first

    _root.mainloop()


def check_pending_notification():
    title, message = dg.pop_pending_notification()
    if message and _icon is not None:
        _icon.notify(message, title or "Desk Guard")


if __name__ == "__main__":
    if "--enroll" in sys.argv:
        count = 30
        if "--count" in sys.argv:
            try:
                count = int(sys.argv[sys.argv.index("--count") + 1])
            except (IndexError, ValueError):
                pass
        dg.enroll(count)
    elif "--stop-all" in sys.argv:
        import stop_all
        stop_all.main()
    else:
        main()
