"""Radios that know each other (sleepradiopi/sync.py): Other radios on the desktop, pairing, and two-way sync.

GET /api/sync: this radio, the radios it's paired with (and how the last sync went), and the others heard on the
network. Asked by another radio: GET /api/sync/hello, GET /api/sync/state, POST /api/sync/paired {"id", "name",
"port"}, /api/sync/unpaired {"id"}, /api/sync/adopt {"id", "merged", "was"} (its merge, to take up). From the page: POST
/api/sync/pair {"address"}, /api/sync/unpair {"id"}, /api/sync/now {"id"}, /api/sync/allow {"id"} (a held sync
let through once).
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("sleepradiopi.web.server")


def build(ctx):
    sync = getattr(ctx, "sync", None)

    def get(self, path) -> bool:
        if sync is None or not path.startswith("/api/sync"):
            return False
        if path == "/api/sync":
            self._send(json.dumps(sync.overview()).encode(), "application/json")
        elif path == "/api/sync/voice":      # a voice pack, as a tar (for a paired radio that hasn't got it)
            self._send_voice()
        elif path == "/api/sync/hello":
            self._send(json.dumps(sync.hello()).encode(), "application/json")
        elif path == "/api/sync/state":
            from urllib.parse import parse_qs, urlparse
            for_id = parse_qs(urlparse(self.path).query).get("for", [""])[0]
            self._send(json.dumps(sync.state(for_id, self.client_address[0].removeprefix("::ffff:"))).encode(), "application/json")
        else:
            return False
        return True

    def post(self, path) -> bool:
        if sync is None or not path.startswith("/api/sync/"):
            return False
        action = path[len("/api/sync/"):]
        try:
            body = self._body()
            if not isinstance(body, dict):
                raise ValueError("send {...}")
            if action == "paired":           # (another radio, pairing with this one: where it is is where it asked from)
                port = body.get("port")
                if not isinstance(body.get("id"), str) or not isinstance(port, int) or not 0 < port < 65536:
                    raise ValueError("send {\"id\", \"name\", \"port\"}")
                ip = self.client_address[0].removeprefix("::ffff:")
                sync.paired_with({"id": body["id"], "name": str(body.get("name", ""))[:60],
                                  "address": ip if port == 80 else f"{ip}:{port}"})
                reply = {"ok": True, "id": sync.id}
            elif action == "unpaired":
                sync.unpaired(str(body.get("id", "")))
                reply = {"ok": True}
            elif action == "adopt":          # (a paired radio worked out the merge: take it up, if nothing changed here)
                reply = {"ok": sync.adopt(str(body.get("id", "")), body.get("merged"), str(body.get("was", "")),
                                          self.client_address[0].removeprefix("::ffff:"))}
            elif action == "pair":
                reply = sync.pair(str(body.get("address", "")))
            elif action == "unpair":
                sync.unpair(str(body.get("id", "")))
                reply = {"ok": True}
            elif action in ("now", "allow"):
                if action == "allow":
                    sync.allow_once(str(body.get("id", "")))
                reply = sync.sync_now(str(body.get("id", "")))
            else:
                return False
        except (OSError, ValueError, KeyError, TypeError) as e:
            self._error(str(getattr(e, "reason", None) or e) or e.__class__.__name__)
            return True
        self._send(json.dumps(reply).encode(), "application/json")
        return True

    def _send_voice(self) -> None:
        """GET /api/sync/voice?name=: the pack's folder as a plain tar, streamed (its length worked out first by
        writing it to nowhere: a Zero hasn't the memory to hold an 80 MB voice)."""
        import tarfile
        from pathlib import Path
        from urllib.parse import parse_qs, urlparse
        from sleepradiopi import voices as voices_mod
        name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
        try:
            voices_mod.check_name(name)
        except ValueError as e:
            self._error(str(e))
            return
        if sync.voice_jobs is None or name not in sync.voice_names():
            self._error("there's no voice of that name here")
            return
        pack = Path(sync.voice_jobs.voices_dir) / name

        class Out:
            def __init__(self, sink=None):
                self.n, self.sink = 0, sink

            def write(self, b):
                self.n += len(b)
                if self.sink is not None:
                    self.sink.write(b)
                return len(b)

        def tar(out):
            with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as t:
                t.add(pack, arcname=name, recursive=True)
        size = Out()
        tar(size)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-tar")
        self.send_header("Content-Length", str(size.n))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            tar(Out(self.wfile))
        except (BrokenPipeError, ConnectionResetError):
            pass
        log.info("sync: voice %s sent to %s", name, self.client_address[0])

    return {"methods": {"_send_voice": _send_voice}, "get": get, "post": post}
