"""PIN storage/verification for gating security-relevant tray actions.

The PIN is never stored in plaintext: only a salted PBKDF2 hash lives on disk.
"""

import hashlib
import json
import os

PIN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pin.dat")


def has_pin() -> bool:
    return os.path.exists(PIN_PATH)


def _hash(pin: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, 200_000)


def set_pin(pin: str):
    salt = os.urandom(16)
    digest = _hash(pin, salt)
    with open(PIN_PATH, "w", encoding="utf-8") as f:
        json.dump({"salt": salt.hex(), "hash": digest.hex()}, f)


def verify_pin(pin: str) -> bool:
    if not has_pin():
        return True
    with open(PIN_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    salt = bytes.fromhex(data["salt"])
    expected = bytes.fromhex(data["hash"])
    return _hash(pin, salt) == expected
