# Desk Guard

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Latest release](https://img.shields.io/github/v/release/Todilo/desk-guard)](https://github.com/Todilo/desk-guard/releases/latest)

Watches the webcam and locks the Windows workstation when it can't confirm
you're the one sitting there. Runs as a system tray app that autostarts at
login.

**Windows only** (uses `ctypes`/`win32` APIs throughout - workstation locking,
foreground/focus handling, process supervision). Not tested on other
platforms and not expected to work there without real changes.

- Locks automatically when your face isn't recognized, or no one's there
- Local-only face detection/recognition (OpenCV YuNet + LBPH) - no cloud,
  no accounts, no telemetry
- PIN-gated controls with lockout on repeated wrong guesses
- Kill-resistant: a watchdog process locks + relaunches if the tray app is
  killed outside an authorized quit
- Windows notifications for locks, camera issues, and watchdog events
- Snooze, live settings, re-training, all from the tray icon

## Install

1. Download `DeskGuardSetup.exe` from the
   [latest release](https://github.com/Todilo/desk-guard/releases/latest)
2. Run it — no admin rights needed
3. On the finish screen, enroll your face (opens a quick webcam capture) and
   start Desk Guard

That's it - nothing else to install.

> **Windows may show a SmartScreen warning** ("Windows protected your PC")
> since this installer isn't code-signed — that costs money and isn't worth
> it for a free personal tool. Click **More info → Run anyway** if you trust
> the source (or just read the code and build it yourself - see
> [Development](#development)).

## How it works

Every `interval` seconds, it grabs a webcam frame and runs face detection
(OpenCV's YuNet model, which handles off-angle / side-lit faces much better
than a classic Haar cascade — useful when the camera isn't square-on to you).
Each detected face is compared against your enrolled face using an LBPH
recognizer.

State machine per check:

- **Your face is seen** → all timers reset, nothing happens.
- **A face is seen that doesn't match yours** → an "unrecognized face" timer
  starts. If it persists for `grace` seconds, the workstation locks.
- **No face at all is seen** (you walked away) → an "away" timer starts. If it
  persists for `away_grace` seconds, the workstation locks.
- After locking, monitoring pauses for `cooldown` seconds (the camera is
  usually unavailable on the lock screen anyway) before resuming.

Seeing your own face at the same time as an unrecognized one (e.g. showing
someone your screen) does **not** lock — your face being present always wins.

### Default timings

| Setting      | Default | Meaning                                                        |
|--------------|---------|-----------------------------------------------------------------|
| `interval`   | 1.0s    | How often the webcam is sampled                                 |
| `threshold`  | 65      | LBPH match distance; lower = stricter match to your enrolled face |
| `grace`      | 5.0s    | How long an unrecognized face must persist before locking        |
| `away_grace` | 10.0s   | How long no face at all must persist before locking (0 disables) |
| `cooldown`   | 15.0s   | Pause after a lock before monitoring resumes                     |

Change these anytime from the tray menu (**Settings...**) — no PIN-less
back door: changing settings requires the PIN once one is set, since loosening
the timers is itself a way to defeat protection. Changes take effect
immediately.

## How it protects you

### PIN protection

A stranger who sits down at the desk could otherwise just right-click the
tray icon and turn monitoring off before the grace timer ever fires. Once you
set a PIN (tray menu → **Set/change PIN...**), it's required to:

- disable monitoring (turning it **back on** is always free — no security
  reason to gate that)
- re-train the face model
- change settings
- quit the app

The PIN is never stored in plaintext — only a salted PBKDF2-SHA256 hash is
kept locally. Until a PIN is set, these actions work without a prompt so
you're not locked out before configuring one.

**Lockout on repeated wrong guesses:** 3 incorrect attempts trigger a 30-second
lockout before another attempt is accepted; each further wrong attempt after
that doubles the wait (60s, 120s, 240s, capped at 300s). This resets to zero
after a correct entry. Without this, someone at the desk could just keep
guessing as fast as they could type.

### Kill-resistance (watchdog)

Turning off monitoring "properly" still leaves an obvious hole: someone could
just kill the tray process in Task Manager instead of using the menu. To
close that, Desk Guard actually runs as **two** cooperating processes — a
small watchdog with no window or tray icon of its own, and the tray app
itself.

If the tray app disappears **without** an authorized quit having happened
(PIN verified, "Quit" clicked), the watchdog immediately **locks the
workstation** and relaunches it. A normal quit doesn't trigger this. The
watchdog is protected the same way in reverse, so the pair doesn't quietly
degrade over time.

Both processes remain fully visible in Task Manager, properly labeled "Desk
Guard" / "Desk Guard Watchdog" — this isn't process hiding, just making a
quick kill counterproductive (it locks the machine, and gets auto-relaunched)
rather than effective.

**If you ever need to stop everything yourself** (for maintenance or before
uninstalling), don't just kill the processes in Task Manager — that's exactly
what triggers a lock. Uninstalling via the installer's own uninstaller
handles this safely and automatically.

**Limit:** this defends against someone with only standard desktop access.
Anyone with admin rights can still stop both processes (e.g. via an elevated
`taskkill`), remove the Startup entry, or boot into safe mode. It's meant to
stop casual snooping, not a determined attacker with admin access to the
machine.

#### Crash-loop protection (won't lock you out)

If the tray app crashed on every single startup (a corrupted model file, a
bad settings value, a future bug — not someone killing it), a naive watchdog
would lock the workstation every couple of seconds forever, since from its
point of view each crash looks exactly like an unauthorized kill. That would
turn a bug into being locked out of your own machine with no way back in.

To prevent that, the watchdog counts failures: the first few unexpected
disappearances within a 10-minute window still lock + relaunch as described
above (this is what actually deters/reacts to someone killing it). But once
more than 5 happen in that window, it stops locking entirely and switches to
quiet retries with exponential backoff (starting at 1 minute, capped at 30),
just logging what's happening. If the app stabilizes for a while, the failure
count ages out and normal behavior resumes automatically — no manual reset
needed.

## Autostart

Already set up if you kept "start at login" checked during install. To
change it later, re-run the installer, or manage the shortcut directly in
`shell:startup`.

## Using Desk Guard

### Tray menu reference

- **Monitoring enabled** — checkbox; toggling off requires the PIN
- **Snooze monitoring** — submenu (5/15/10/30 min, PIN-gated); disables
  monitoring temporarily and re-enables it automatically when the timer runs
  out, for things like screen-sharing without needing to remember to turn
  protection back on
- **Settings...** — edit the timings above (PIN-gated)
- **Re-train face model...** — capture more samples of your face and retrain
  (PIN-gated; pauses monitoring while the camera is used for enrollment)
- **Set/change PIN...** — set a PIN for the first time, or change an existing
  one (requires the current PIN to change)
- **About / How it works...** — shows this app's current live settings and a
  summary of the above, from the tray
- **Quit** — PIN-gated; stops the app and its watchdog cleanly

### Tray icon states

The icon and its hover tooltip reflect live status, not just on/off:

| Icon    | Meaning                                                       |
|---------|-----------------------------------------------------------------|
| 🟢 green  | Monitoring enabled, camera working                              |
| 🟡 amber  | Monitoring enabled, but the camera has been unreachable for over 20s (unplugged, driver issue, in use by another app) |
| ⚪ gray   | Monitoring disabled (manually, or snoozed — tooltip shows which and, if snoozed, when it resumes) |

### Notifications

Windows toast notifications (via the tray icon) fire for events you didn't
directly trigger yourself, so you don't have to dig through logs to notice
them:

- A lock was triggered (unrecognized face / away)
- The camera has been unavailable for more than 20 seconds
- The detection loop crashed and was restarted
- The watchdog detected the tray app being killed outside an authorized
  quit, and locked + relaunched it
- The crash-loop circuit breaker kicked in (backing off instead of locking)

Actions you initiate yourself from the menu (toggling, snoozing, retraining,
changing settings) don't get a separate toast — the menu interaction and the
tray tooltip/icon are already your feedback for those.

## Known limitations

- Face recognition can false-trigger in poor lighting or unusual angles; if
  that happens, either raise the match threshold a bit or re-enroll with
  more/better samples covering your normal head position.
- The webcam is normally inaccessible while Windows is already locked, so
  the loop just backs off and retries — this is expected, not a bug.
- This is a personal-desk deterrent, not enterprise security software: an
  attacker with admin rights or physical access to disable Startup items /
  boot into safe mode can still get around it.

## Credits

Face detection uses the [YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet)
model from OpenCV Zoo (bundled in this repo for convenience) - see that repo
for its own license terms.

## License

MIT - see [LICENSE](LICENSE).

---

## Development

The rest of this doc is for building Desk Guard from source or modifying
it - not needed just to use the app (see Install above).

### Source layout

| File / folder                | Purpose                                              |
|-------------------------------|-------------------------------------------------------|
| `desk_guard.py`                | Core detection/lock logic, shared helpers, CLI (`enroll` / `run`) |
| `tray.py`                       | System tray app: icon, menu, dialogs, monitor thread   |
| `watchdog.py`                   | Supervises `tray.py`; locks + relaunches on unauthorized kill |
| `stop_all.py`                    | Stops both processes cleanly for maintenance (no lock/relaunch) |
| `uninstall.py`                    | Stops everything, removes autostart, optionally deletes your data |
| `pin_auth.py`                   | PIN hashing/verification                               |
| `faces/owner/`                  | Enrolled face photos (grayscale crops) - gitignored, personal data |
| `owner_model.yml`               | Trained LBPH face-recognition model - gitignored, personal data |
| `settings.json`                 | Saved timing configuration - gitignored, personal data |
| `pin.dat`                       | Salted PIN hash - gitignored, personal data            |
| `desk_guard.log` (+ `.log.1`)   | Activity log; rotates at 5 MB, one backup kept - gitignored |
| `tray.pid` / `watchdog.pid`     | PID files the two processes use to watch each other - gitignored |
| `shutdown.flag`                 | Transient marker written on an authorized quit - gitignored |
| `pending_notification.json`      | Transient note from the watchdog for the tray app to show as a toast on next start - gitignored |
| `face_detection_yunet_2023mar.onnx` | Face detection model                              |
| `requirements.txt`               | Pinned dependency versions for `pip install -r`       |
| `icon.ico`                         | App icon, embedded into both `.exe`s                  |
| `version_tray.txt` / `version_watchdog.txt` | Version-resource info (sets each exe's displayed name/product) |
| `DeskGuard.spec` / `DeskGuardWatchdog.spec` | PyInstaller build specs                    |
| `installer/DeskGuard.iss`          | Inno Setup script that builds `DeskGuardSetup.exe`     |
| `installer/next_steps.txt`         | Shown at the end of the installer                     |

### Running from source

Requires Python 3.12+ with tkinter (the standard python.org Windows
installer includes it; the Microsoft Store build does not):

```
pip install -r requirements.txt
python desk_guard.py enroll
pythonw.exe watchdog.py
```

Path resolution, cross-launching between the tray/watchdog pair, all of it
detects whether it's running frozen (packaged) or from source and behaves
accordingly, so both modes stay usable side by side.

### Packaged .exe vs. running from source

The two always-running components are built as standalone, custom-branded
executables via PyInstaller - `DeskGuard.exe` (from `tray.py`) and
`DeskGuardWatchdog.exe` (from `watchdog.py`). This is what makes Task
Manager and Windows notifications show "Desk Guard" instead of a generic
"Python" (which is what you'd see running the raw `.py` files under
`pythonw.exe`, since that's `python.exe`'s own embedded product name).

Both binaries look for their data (faces, model, settings, PIN, logs) in
their own folder, so they must stay next to `face_detection_yunet_2023mar.onnx`
and the rest of the app files. They detect and correctly cross-launch each
other by exe name.

Enrollment is also part of the packaged exe - `DeskGuard.exe --enroll` runs
the same capture/train flow as `python desk_guard.py enroll`, with no Python
needed (the tray's **Re-train face model...** menu item uses this when
frozen). `DeskGuard.exe --stop-all` likewise runs the same logic as
`stop_all.py`.

**Rebuilding after a code change:**
```
pyinstaller --noconfirm "DeskGuard.spec"
pyinstaller --noconfirm "DeskGuardWatchdog.spec"
```
Then copy the two `.exe`s out of `dist\` into the app folder (overwriting the
old ones), after stopping any running instance first (`python stop_all.py` -
Windows locks a running `.exe`'s file, so it can't be overwritten while live).
The `.spec` files already encode the icon, version info, bundled detector
model, and `cv2.face` hidden import used for the original build.

### Building the installer

Built with [Inno Setup](https://jrsoftware.org/isinfo.php) from
`installer/DeskGuard.iss`, which bundles the two packaged `.exe`s plus the
Python source files (for the optional from-source/maintenance path above):

```
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\DeskGuard.iss
```

Output: `installer\output\DeskGuardSetup.exe`. Build the two app `.exe`s
first - the installer just packages whatever's currently sitting in the repo
root.

The generated uninstaller stops both processes (via `taskkill`, in
watchdog-then-tray order so it doesn't trigger a lock) before removing
files, and leaves personal data (faces, model, settings, PIN, logs) in place
by design - use `uninstall.py` or delete the install folder manually for a
full wipe.

### CLI reference

```
python desk_guard.py enroll [--count N]
python desk_guard.py run [--interval S] [--threshold N] [--grace S] [--away-grace S] [--cooldown S]
```

`enroll` opens a webcam preview and captures face samples (default 30),
adding to any existing ones and retraining on the full set. `run` is the
plain command-line monitor (used by the tray app internally, and directly
runnable for testing without a tray icon). Its defaults come from
`settings.json`; flags override for a one-off run without saving.
