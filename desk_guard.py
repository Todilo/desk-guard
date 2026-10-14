"""
Desk Guard: locks the Windows workstation if an unrecognized face is seen
at the webcam while your own (enrolled) face is not present.

Usage:
    python desk_guard.py enroll            capture your face and train the model
    python desk_guard.py run               start monitoring and auto-lock
"""

import argparse
import ctypes
import json
import os
import shutil
import sys
import time

import cv2

# When frozen (PyInstaller), sys.executable is the .exe itself and bundled
# read-only resources are extracted to a temp dir (sys._MEIPASS) that's wiped
# after each run - so writable app data (faces/, model, settings, logs, pid
# files) must live next to the exe instead, while bundled resources (the
# detector model) must be read from _MEIPASS. Running from source, both are
# just this script's own directory, as before.
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
    RESOURCE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = BASE_DIR

FACES_DIR = os.path.join(BASE_DIR, "faces", "owner")
MODEL_PATH = os.path.join(BASE_DIR, "owner_model.yml")
DETECTOR_MODEL_PATH = os.path.join(RESOURCE_DIR, "face_detection_yunet_2023mar.onnx")
LOG_PATH = os.path.join(BASE_DIR, "desk_guard.log")
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")
TRAY_PID_PATH = os.path.join(BASE_DIR, "tray.pid")
WATCHDOG_PID_PATH = os.path.join(BASE_DIR, "watchdog.pid")
SHUTDOWN_FLAG_PATH = os.path.join(BASE_DIR, "shutdown.flag")
NOTIFY_PATH = os.path.join(BASE_DIR, "pending_notification.json")

DEFAULT_SETTINGS = {
    "interval": 1.0,
    "threshold": 65.0,
    "grace": 5.0,
    "away_grace": 10.0,
    "cooldown": 15.0,
}


LOG_MAX_BYTES = 5 * 1024 * 1024  # rotate at 5 MB, keep one backup (10 MB total cap)


def _rotate_log_if_needed():
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            backup_path = LOG_PATH + ".1"
            if os.path.exists(backup_path):
                os.remove(backup_path)
            os.rename(LOG_PATH, backup_path)
    except Exception:
        pass


def log(msg: str):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line)
    except Exception:
        pass  # no console under pythonw
    try:
        _rotate_log_if_needed()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                settings.update(json.load(f))
        except Exception:
            pass
    return settings


def save_settings(settings: dict):
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


def write_pid(path: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


def read_pid(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None


def pid_alive(pid) -> bool:
    if not pid:
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    exit_code = ctypes.c_ulong()
    ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
    ctypes.windll.kernel32.CloseHandle(handle)
    STILL_ACTIVE = 259
    return exit_code.value == STILL_ACTIVE


def mark_authorized_shutdown():
    try:
        with open(SHUTDOWN_FLAG_PATH, "w", encoding="utf-8") as f:
            f.write("authorized")
    except Exception:
        pass


def clear_shutdown_flag():
    try:
        if os.path.exists(SHUTDOWN_FLAG_PATH):
            os.remove(SHUTDOWN_FLAG_PATH)
    except Exception:
        pass


def is_shutdown_authorized() -> bool:
    return os.path.exists(SHUTDOWN_FLAG_PATH)


def find_python_exe(name: str = "python.exe"):
    # sys.executable is the packaged .exe itself once frozen, so it can't be
    # used to locate a real Python install (needed for retrain()'s enroll
    # subprocess, which isn't packaged). PATH is reliable either way; the
    # AppData scan is just a fallback if PATH ever isn't set for some reason.
    found = shutil.which(name)
    if found:
        return found
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python")
    if os.path.isdir(base):
        for entry in sorted(os.listdir(base), reverse=True):
            candidate = os.path.join(base, entry, name)
            if os.path.exists(candidate):
                return candidate
    return None


def launch_command(script_name: str, exe_name: str):
    """Command to launch the sibling component (tray<->watchdog). When
    packaged (PyInstaller), that's the branded exe expected next to this one;
    running from source, it's pythonw.exe running the given script - same as
    before packaging existed."""
    if getattr(sys, "frozen", False):
        return [os.path.join(BASE_DIR, exe_name)]
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    return [pythonw, os.path.join(BASE_DIR, script_name)]


def queue_notification(title: str, message: str):
    # watchdog.py has no tray icon of its own to call notify() on, so it
    # leaves a note here for tray.py to show as a toast the next time it starts.
    try:
        with open(NOTIFY_PATH, "w", encoding="utf-8") as f:
            json.dump({"title": title, "message": message, "time": time.time()}, f)
    except Exception:
        pass


def pop_pending_notification():
    try:
        if not os.path.exists(NOTIFY_PATH):
            return None, None
        with open(NOTIFY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        os.remove(NOTIFY_PATH)
        if time.time() - data.get("time", 0) < 300:  # ignore stale notes (machine was off/asleep)
            return data.get("title"), data.get("message")
    except Exception:
        pass
    return None, None


def get_detector():
    # YuNet handles off-angle / side-lit faces far better than a Haar cascade,
    # which matters for a laptop cam that isn't square-on to the user.
    if not os.path.exists(DETECTOR_MODEL_PATH):
        sys.exit(f"Missing face detection model: {DETECTOR_MODEL_PATH}")
    detector = cv2.FaceDetectorYN_create(
        DETECTOR_MODEL_PATH, "", (320, 320), score_threshold=0.7, nms_threshold=0.3, top_k=5000
    )
    return detector


def detect_faces(detector, frame):
    h, w = frame.shape[:2]
    detector.setInputSize((w, h))
    _, results = detector.detect(frame)
    faces = []
    if results is None:
        return faces
    for r in results:
        x, y, fw, fh = r[:4].astype(int)
        x, y = max(x, 0), max(y, 0)
        fw = min(fw, w - x)
        fh = min(fh, h - y)
        if fw > 0 and fh > 0:
            faces.append((x, y, fw, fh))
    return faces


def open_camera():
    cam = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if not cam.isOpened():
        cam = cv2.VideoCapture(0)
    return cam


def enroll(count: int):
    os.makedirs(FACES_DIR, exist_ok=True)
    detector = get_detector()
    cam = open_camera()
    if not cam.isOpened():
        sys.exit("Could not open webcam.")

    print(f"Capturing {count} face samples. Look at the camera, move your head slightly "
          "(tilt/turn a little) between shots for better coverage. Press 'q' to abort.")

    captured = 0
    existing = len(os.listdir(FACES_DIR)) if os.path.isdir(FACES_DIR) else 0
    last_capture = 0.0
    consecutive_read_failures = 0
    start_time = time.time()
    MAX_READ_FAILURES = 100  # a camera that can't deliver a frame at all, not just no face
    MAX_ENROLL_SECONDS = 180  # backstop so a stalled camera/face can't hang this forever

    while captured < count:
        if time.time() - start_time > MAX_ENROLL_SECONDS:
            print(f"Enrollment timed out after {MAX_ENROLL_SECONDS}s.")
            break

        ok, frame = cam.read()
        if not ok:
            consecutive_read_failures += 1
            if consecutive_read_failures > MAX_READ_FAILURES:
                cam.release()
                cv2.destroyAllWindows()
                sys.exit("Camera stopped responding during enrollment.")
            continue
        consecutive_read_failures = 0

        faces = detect_faces(detector, frame)

        display = frame.copy()
        for (x, y, w, h) in faces:
            cv2.rectangle(display, (x, y), (x + w, y + h), (0, 255, 0), 2)
        cv2.putText(display, f"Captured {captured}/{count}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
        cv2.imshow("Desk Guard - Enroll", display)

        now = time.time()
        if len(faces) == 1 and (now - last_capture) > 0.4:
            x, y, w, h = faces[0]
            face_img = cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY)
            face_img = cv2.resize(face_img, (200, 200))
            out_path = os.path.join(FACES_DIR, f"owner_{existing + captured:03d}.jpg")
            cv2.imwrite(out_path, face_img)
            captured += 1
            last_capture = now

        if cv2.waitKey(1) & 0xFF == ord("q"):
            print("Aborted.")
            break

    cam.release()
    cv2.destroyAllWindows()

    if captured == 0:
        sys.exit("No samples captured, nothing to train.")

    train_model()
    print(f"Enrollment complete. {captured} new samples captured, model saved to {MODEL_PATH}")


def train_model():
    images, labels = [], []
    for fname in os.listdir(FACES_DIR):
        if not fname.lower().endswith((".jpg", ".png")):
            continue
        path = os.path.join(FACES_DIR, fname)
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        images.append(img)
        labels.append(0)  # single class: "owner"

    if not images:
        sys.exit("No training images found. Run enroll first.")

    recognizer = cv2.face.LBPHFaceRecognizer_create()
    recognizer.train(images, __import__("numpy").array(labels))
    recognizer.save(MODEL_PATH)


def lock_workstation():
    ctypes.windll.user32.LockWorkStation()


def monitor(interval: float, threshold: float, grace: float, away_grace: float, cooldown: float,
            is_enabled=lambda: True, is_stopped=lambda: False, on_camera_status=lambda ok: None,
            on_lock=lambda reason: None):
    """Core detect/lock loop. is_enabled/is_stopped let callers (e.g. the tray
    app) pause or stop monitoring from another thread without duplicating this
    logic. on_camera_status(bool) reports whether the last camera access
    succeeded, so a caller can surface "camera unavailable" separately from
    "no face seen" (e.g. a tray icon warning state). on_lock(reason) fires
    right before locking, so a caller can show a toast/notification."""
    detector = get_detector()
    recognizer = cv2.face.LBPHFaceRecognizer_create()
    recognizer.read(MODEL_PATH)

    unknown_since = None
    away_since = None
    cam = None

    try:
        while not is_stopped():
            if not is_enabled():
                if cam is not None:
                    cam.release()
                    cam = None
                unknown_since = None
                away_since = None
                time.sleep(0.5)
                continue

            if cam is None or not cam.isOpened():
                cam = open_camera()
                if not cam.isOpened():
                    # likely locked / camera unavailable — back off and retry
                    on_camera_status(False)
                    time.sleep(max(interval, 2.0))
                    continue

            ok, frame = cam.read()
            if not ok or frame is None:
                on_camera_status(False)
                time.sleep(interval)
                continue

            on_camera_status(True)
            faces = detect_faces(detector, frame)

            owner_present = False
            unknown_present = False

            for (x, y, w, h) in faces:
                face_img = cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY)
                face_img = cv2.resize(face_img, (200, 200))
                _, confidence = recognizer.predict(face_img)
                # LBPH confidence is a distance: lower = better match
                if confidence <= threshold:
                    owner_present = True
                else:
                    unknown_present = True

            now = time.time()
            locking = False
            lock_reason = None

            if owner_present:
                unknown_since = None
                away_since = None
            elif unknown_present:
                away_since = None
                if unknown_since is None:
                    unknown_since = now
                    log(f"unrecognized face detected, will lock in {grace}s if it persists")
                elif now - unknown_since >= grace:
                    lock_reason = "unrecognized face"
                    log(f"locking workstation ({lock_reason})")
                    locking = True
            else:
                unknown_since = None
                if away_grace > 0:
                    if away_since is None:
                        away_since = now
                    elif now - away_since >= away_grace:
                        lock_reason = "no face / away"
                        log(f"locking workstation ({lock_reason})")
                        locking = True

            if locking:
                on_lock(lock_reason)
                lock_workstation()
                unknown_since = None
                away_since = None
                cam.release()
                cam = None
                time.sleep(cooldown)
                continue

            time.sleep(interval)
    finally:
        if cam is not None:
            cam.release()


def run(interval: float, threshold: float, grace: float, away_grace: float, cooldown: float):
    if not os.path.exists(MODEL_PATH):
        sys.exit("No trained model found. Run 'python desk_guard.py enroll' first.")

    log("Desk Guard running. Ctrl+C to stop.")
    log(f"interval={interval}s threshold={threshold} grace={grace}s "
        f"away_grace={away_grace}s cooldown={cooldown}s")

    try:
        monitor(interval, threshold, grace, away_grace, cooldown)
    except KeyboardInterrupt:
        log("Stopped.")
    finally:
        cv2.destroyAllWindows()


def main():
    parser = argparse.ArgumentParser(description="Lock the PC when an unrecognized face is seen.")
    sub = parser.add_subparsers(dest="command", required=True)

    enroll_p = sub.add_parser("enroll", help="capture your face and train the model")
    enroll_p.add_argument("--count", type=int, default=30, help="number of samples to capture")

    defaults = load_settings()
    run_p = sub.add_parser("run", help="start monitoring")
    run_p.add_argument("--interval", type=float, default=defaults["interval"], help="seconds between checks")
    run_p.add_argument("--threshold", type=float, default=defaults["threshold"],
                        help="LBPH distance threshold; lower = stricter match required")
    run_p.add_argument("--grace", type=float, default=defaults["grace"],
                        help="seconds an unknown face must persist before locking")
    run_p.add_argument("--away-grace", type=float, default=defaults["away_grace"],
                        help="seconds with no face at all before locking (0 to disable)")
    run_p.add_argument("--cooldown", type=float, default=defaults["cooldown"],
                        help="seconds to pause monitoring after a lock is triggered")

    args = parser.parse_args()

    if args.command == "enroll":
        enroll(args.count)
    elif args.command == "run":
        run(args.interval, args.threshold, args.grace, args.away_grace, args.cooldown)


if __name__ == "__main__":
    main()
