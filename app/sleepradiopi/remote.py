"""Remote access: reaching the radio's web page from anywhere, through a
Cloudflare Tunnel (cloudflared, outbound only: nothing opens on the router or
the network the radio is on).

OFF unless someone switches it on from the page (Settings, Remote access) and
gives it the tunnel's token. Nothing here runs the tunnel: the station isn't
root and must never wait for the network. It keeps two files in
SLEEPRADIOPI_REMOTE_DIR (/data/cloudflared on the appliance image),

    token     the tunnel token (mode 600, never sent back or put in a backup)
    enabled   present = the tunnel should run

and the root helper remote-access-watch (usr/sbin) starts or stops cloudflared
to match, once the clock is set and the station has had time to start. It
writes what it's doing to STATE_FILE; cloudflared answers /ready on METRICS
once it is connected to Cloudflare.

Without SLEEPRADIOPI_REMOTE_DIR, or without cloudflared installed (a desktop
run, an old image), there is no Remote access: available() is False.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from sleepradiopi.config.atomic import write_atomic

ENV = "SLEEPRADIOPI_REMOTE_DIR"
BINARY = Path(os.environ.get("SLEEPRADIOPI_CLOUDFLARED", "/usr/bin/cloudflared"))
STATE_FILE = Path(os.environ.get("SLEEPRADIOPI_REMOTE_STATE", "/run/sleepradiopi/remote-status"))
METRICS = "http://127.0.0.1:20241/ready"


def _dir() -> Path | None:
    path = os.environ.get(ENV)
    return Path(path) if path else None


def available() -> bool:
    d = _dir()
    return d is not None and d.parent.is_dir() and BINARY.exists()


def parse_token(text: object) -> str:
    """The token from what was pasted: the token alone, or the whole command
    Cloudflare shows ("cloudflared service install eyJ...", "sudo cloudflared
    ..."). A tunnel token is base64 of {"a": account, "t": tunnel, "s": secret}.
    Raises ValueError (with words for the page) if it isn't one."""
    if not isinstance(text, str) or len(text) > 2000:
        raise ValueError("that isn't a tunnel token")
    words = text.split()
    if not words:
        raise ValueError("paste the tunnel token (it starts eyJ)")
    token = words[-1]
    try:
        info = json.loads(base64.b64decode(token + "=" * (-len(token) % 4), validate=True))
        if not (isinstance(info, dict) and all(isinstance(info.get(k), str) and info[k] for k in "ats")):
            raise ValueError
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise ValueError("that isn't a tunnel token: it starts eyJ and is one long word") from None
    return token


def token_set() -> bool:
    d = _dir()
    return d is not None and (d / "token").is_file() and (d / "token").stat().st_size > 0


def enabled() -> bool:
    d = _dir()
    return d is not None and (d / "enabled").exists()


def set_token(text: object) -> None:
    token = parse_token(text)
    d = _dir()
    if d is None:
        raise ValueError("this radio has no remote access")
    d.mkdir(parents=True, exist_ok=True)
    write_atomic(d / "token", token + "\n", 0o600)     # (nothing else can read it; the tunnel runs as this user)


def set_enabled(on: bool) -> None:
    d = _dir()
    if d is None:
        raise ValueError("this radio has no remote access")
    if on:
        if not token_set():
            raise ValueError("give it a tunnel token first")
        d.mkdir(parents=True, exist_ok=True)
        if not enabled():                             # (a write to the card only when it changes)
            write_atomic(d / "enabled", "1\n")
    else:
        (d / "enabled").unlink(missing_ok=True)


def forget() -> None:
    set_enabled(False)
    d = _dir()
    if d is not None:
        (d / "token").unlink(missing_ok=True)


def _connected() -> bool:
    try:
        with urllib.request.urlopen(METRICS, timeout=1.5) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def status() -> dict:
    """{"available", "token_set", "enabled", "state", "connected"}. state is the helper's own words
    ("off", "waiting for the clock", "waiting for the radio to start", "starting", "running"); connected is
    whether cloudflared says it has reached Cloudflare. Never the token."""
    if not available():
        return {"available": False}
    on = enabled() and token_set()
    try:
        state = STATE_FILE.read_text().strip() or "off"
    except OSError:
        state = "off"
    if not on:
        state = "off"
    return {"available": True, "token_set": token_set(), "enabled": enabled(), "state": state,
            "connected": on and state == "running" and _connected()}
