"""An optional password for the web page.

Off until someone sets one (on the page's Settings, or over ssh). It's kept
in the config as "web_password": a salted PBKDF2-SHA256 hash, never the
password; it's never put in the settings backup file. Logging in gives a
cookie that's good for 30 days: "<expiry>.<HMAC>", signed with a key made
from the hash, so it survives restarts and changing or removing the password
logs everyone out.

The config is re-read when it changes, so clearing the password over ssh
works at once:

    python3 -m sleepradiopi.config.auth [--config PATH] clear   # no password
    python3 -m sleepradiopi.config.auth [--config PATH] set     # asks for one

(on the appliance image: `sleepradio-password clear`). Requests from the
radio itself (127.0.0.1) never need the password.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from pathlib import Path

from sleepradiopi.config.settings import DEFAULT_PATH, save_setting

KEY = "web_password"
ITERATIONS = 60_000          # ~0.3 s on a Pi Zero 2 W: fine for a login, slow for guessing
COOKIE = "sleepradio_session"
SESSION_S = 30 * 24 * 3600
MIN_LEN, MAX_LEN = 4, 128


def hash_password(password: str, salt: bytes | None = None, iterations: int = ITERATIONS) -> dict:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return {"salt": salt.hex(), "hash": digest.hex(), "iterations": iterations}


def check_length(password) -> str:
    if not isinstance(password, str) or not MIN_LEN <= len(password) <= MAX_LEN:
        raise ValueError(f"a password must be {MIN_LEN}-{MAX_LEN} characters")
    return password


class Auth:
    def __init__(self, config_file: Path | None) -> None:
        self.config_file = config_file
        self._lock = threading.Lock()
        self._stamp = None
        self._entry: dict | None = None

    def _current(self) -> dict | None:
        """The stored hash, re-read whenever the config file changes."""
        if self.config_file is None:
            return None
        try:
            st = self.config_file.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            return None
        with self._lock:
            if stamp != self._stamp:
                try:
                    entry = json.loads(self.config_file.read_text()).get(KEY)
                except (OSError, ValueError):
                    entry = None
                ok = isinstance(entry, dict) and all(isinstance(entry.get(k), str) for k in ("salt", "hash"))
                self._entry = entry if ok else None
                self._stamp = stamp
            return self._entry

    @property
    def protected(self) -> bool:
        return self._current() is not None

    def check(self, password: str) -> bool:
        entry = self._current()
        if entry is None or not isinstance(password, str):
            return False
        got = hash_password(password, bytes.fromhex(entry["salt"]), int(entry.get("iterations", ITERATIONS)))
        return hmac.compare_digest(got["hash"], entry["hash"])

    def _sign(self, entry: dict, expiry: int) -> str:
        key = bytes.fromhex(entry["hash"]) + bytes.fromhex(entry["salt"])
        return hmac.new(key, str(expiry).encode(), hashlib.sha256).hexdigest()

    def token(self) -> str:
        entry = self._current()
        expiry = int(time.time()) + SESSION_S
        return f"{expiry}.{self._sign(entry, expiry)}"

    def valid(self, token: str | None) -> bool:
        entry = self._current()
        if entry is None:
            return True
        if not token or "." not in token:
            return False
        expiry, sig = token.split(".", 1)
        if not expiry.isdigit() or int(expiry) < time.time():
            return False
        return hmac.compare_digest(sig, self._sign(entry, int(expiry)))

    def set_password(self, password: str | None) -> None:
        """A new password (ValueError if too short/long), or None for none."""
        if self.config_file is None:
            raise ValueError("no config file to keep a password in")
        entry = None if password is None else hash_password(check_length(password))
        save_setting(self.config_file, KEY, entry)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Set or clear the web page's password.")
    parser.add_argument("--config", type=Path, default=DEFAULT_PATH)
    parser.add_argument("action", choices=["clear", "set", "status"])
    args = parser.parse_args(argv)
    auth = Auth(args.config)
    if args.action == "status":
        print("password set" if auth.protected else "no password")
    elif args.action == "clear":
        auth.set_password(None)
        print("password cleared: the web page is open")
    else:
        first = getpass.getpass("New password: ")
        if first != getpass.getpass("Again: "):
            print("they don't match; nothing changed", file=sys.stderr)
            return 1
        try:
            auth.set_password(first)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        print("password set")
    return 0


if __name__ == "__main__":
    sys.exit(main())
