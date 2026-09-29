"""Updates over the internet, from GitHub releases, started from the web page.

A release (made by scripts/release.sh) has the system image (rootfs.squashfs)
and a manifest.json: {"version", "notes", "rootfs": {"file", "size",
"sha256"}, "kernel_sha256", "config_txt": {"file", "sha256"}}. The page
checks for one; Install hands it to the root helper (update-watch), which
does what scripts/update.sh does from the PC:

  1. refuse if the kernel changed (it lives on the shared boot partition:
     that needs the card in a PC, as ever);
  2. stream the image straight into the root slot the Pi isn't running from
     (p2 or p3), then read it back and check its SHA-256;
  3. bring config.txt up to date if it changed;
  4. point cmdline.txt at the new slot, note the update as pending, reboot.

After the reboot the station confirms it once music is playing. If it hasn't
within CONFIRM_S, the boot watchdog (also root) points cmdline.txt back at
the old slot and reboots: a broken update undoes itself. Either way the
station says what happened, once. The DJ talks through it: downloading,
restarting, "updated to version 1.2".

The download is checked against the manifest's SHA-256 (both come from the
same release, over HTTPS).
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_SOURCE = "github:dylan7474/SleepRadioPi-OS"
VERSION_FILE = Path("/etc/sleepradiopi-version")
RUN = Path("/run/sleepradiopi")
REQUEST, STATUS, GO = RUN / "update-request", RUN / "update-status", RUN / "update-reboot"
STATE = Path("/data/radio/.update")        # pending.json, result.json (the station can write here)
CONFIRM_S = 300                           # the new system must be on air within this
SLOT_BYTES = 256 * 1024 * 1024
UA = {"User-Agent": "SleepRadioPi-updater"}


def this_version(path: Path = VERSION_FILE) -> str:
    try:
        return path.read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def _get(url: str, timeout: float = 20):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)


def download(url: str, size: int, write: Callable[[bytes], None],
             progress: Callable[[int], None] = lambda got: None, attempts: int = 8) -> int:
    """Fetch exactly size bytes, handing them to write() in order. GitHub's
    download servers sometimes close the connection early -- and Python then
    just sees the end -- so a short download carries on from where it
    stopped (an HTTP Range request), up to attempts times. Returns the bytes got."""
    got = 0
    for attempt in range(attempts):
        headers = dict(UA)
        if got:
            headers["Range"] = f"bytes={got}-"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                if got and r.status != 206:           # the server ignored the Range
                    log.warning("the server can't resume a download (%d of %d bytes)", got, size)
                    return got                        # the caller's checksum refuses it
                while chunk := r.read(1 << 20):
                    if got + len(chunk) > size:
                        raise ValueError("the download is bigger than the release says")
                    write(chunk)
                    got += len(chunk)
                    progress(got)
        except (OSError, urllib.error.URLError, http.client.HTTPException) as e:
            log.warning("download interrupted at %d of %d bytes: %s", got, size, e)
        if got == size:
            return got
        log.warning("download stopped at %d of %d bytes; resuming (%d)", got, size, attempt + 1)
        time.sleep(min(2 ** attempt, 30))
    return got


def fetch_manifest(source: str = DEFAULT_SOURCE) -> dict:
    """The latest release's manifest, with absolute URLs filled in.
    source: "github:owner/repo" or a manifest.json URL (for testing)."""
    if source.startswith("github:"):
        repo = source.split(":", 1)[1]
        try:
            with _get(f"https://api.github.com/repos/{repo}/releases/latest") as r:
                release = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise ValueError("no releases have been published yet") from None
            raise
        assets = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
        if "manifest.json" not in assets:
            raise ValueError(f"release {release.get('tag_name')} has no manifest.json")
        base, manifest_url = None, assets["manifest.json"]
    else:
        assets, manifest_url = {}, source
        base = source.rsplit("/", 1)[0]
    with _get(manifest_url) as r:
        m = json.load(r)
    for key in ("version", "rootfs", "kernel_sha256"):
        if key not in m:
            raise ValueError(f"the release's manifest has no {key}")

    def url(name: str) -> str:
        return assets[name] if name in assets else f"{base}/{name}"
    m["rootfs"]["url"] = url(m["rootfs"]["file"])
    if m.get("config_txt"):
        m["config_txt"]["url"] = url(m["config_txt"]["file"])
    return m


def newer(release: str, current: str) -> bool:
    """Is the release different from (and, when both are numbers, newer than) this radio?"""
    def nums(v: str):
        m = re.match(r"v?(\d+(?:\.\d+)*)$", v.strip())
        return tuple(int(x) for x in m.group(1).split(".")) if m else None
    a, b = nums(release), nums(current)
    if a is not None and b is not None:
        return a > b
    return release.strip().lstrip("v") != current.strip().lstrip("v").split("-")[0]


# --- the root side (update-watch) ---------------------------------------------------------

class Paths:
    """Where things are; tests point these at a temporary folder."""

    def __init__(self, boot: Path = Path("/boot"), proc_cmdline: Path = Path("/proc/cmdline"),
                 state: Path = STATE, status: Path = STATUS, go: Path = GO,
                 device: Callable[[str], Path] = lambda slot: Path(f"/dev/mmcblk0{slot}"),
                 remount: Callable[[str], None] | None = None,
                 rollback_status: Path = RUN / "rollback-status") -> None:
        self.boot, self.proc_cmdline, self.state = boot, proc_cmdline, state
        self.rollback_status = rollback_status
        self.status, self.go, self.device = status, go, device
        self.remount = remount or (lambda mode: subprocess.run(["mount", "-o", f"remount,{mode}", str(boot)],
                                                               check=True))


def _sha256_file(path: Path, limit: int | None = None) -> str:
    h, left = hashlib.sha256(), limit
    with open(path, "rb") as f:
        while left is None or left > 0:
            chunk = f.read(1 << 20 if left is None else min(1 << 20, left))
            if not chunk:
                break
            h.update(chunk)
            if left is not None:
                left -= len(chunk)
    return h.hexdigest()


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass


def booted_slot(p: Paths) -> str:
    m = re.search(r"root=/dev/mmcblk0(p[23])\b", p.proc_cmdline.read_text())
    if not m:
        raise ValueError("not booted from root slot p2 or p3")
    return m.group(1)


def _set_root(p: Paths, old: str, new: str) -> None:
    """The one write that matters: cmdline.txt, replaced atomically."""
    cmdline = p.boot / "cmdline.txt"
    text = cmdline.read_text()
    if f"root=/dev/mmcblk0{old}" not in text:
        raise ValueError("cmdline.txt doesn't say what it boots")
    p.remount("rw")
    try:
        tmp = p.boot / "cmdline.new"
        tmp.write_text(text.replace(f"root=/dev/mmcblk0{old}", f"root=/dev/mmcblk0{new}"))
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, cmdline)
        os.sync()
    finally:
        p.remount("ro")


def install(req: dict, p: Paths = Paths(), status=None) -> str:
    """Write the release into the spare slot and switch to it. Returns the slot."""
    def say(state, **kw):
        _write_json(p.status, {"state": state, "version": req["version"], **kw})
        if status:
            status(state, kw)
    kernel = p.boot / "Image"
    if kernel.is_file() and _sha256_file(kernel) != req["kernel_sha256"]:
        raise ValueError("this update changes the kernel: it needs the card in a PC (scripts/flash.sh)")
    cur = booted_slot(p)
    new = "p3" if cur == "p2" else "p2"
    size, want = int(req["rootfs"]["size"]), req["rootfs"]["sha256"]
    if not 0 < size <= SLOT_BYTES:
        raise ValueError("the image doesn't fit a root slot")
    dev = p.device(new)
    say("downloading", progress=0.0)
    got, h = 0, hashlib.sha256()
    with open(dev, "r+b" if dev.exists() else "wb") as out:
        def write(chunk: bytes) -> None:
            h.update(chunk)
            out.write(chunk)
        got = download(req["rootfs"]["url"], size, write,
                       lambda n: say("downloading", progress=round(n / size, 3)))
        out.flush()
        os.fsync(out.fileno())
    if got != size or h.hexdigest() != want:
        raise ValueError("the download doesn't match the release's checksum; nothing was changed")
    say("verifying")
    if _sha256_file(dev, size) != want:                 # what's actually on the card
        raise ValueError("the written image doesn't read back right; nothing was changed")
    cfg = req.get("config_txt")
    if cfg and _sha256_file(p.boot / "config.txt") != cfg["sha256"]:
        with _get(cfg["url"]) as r:
            data = r.read()
        if hashlib.sha256(data).hexdigest() != cfg["sha256"]:
            raise ValueError("config.txt doesn't match the release's checksum; nothing was changed")
        p.remount("rw")
        try:
            tmp = p.boot / "config.new"
            tmp.write_bytes(data)
            os.replace(tmp, p.boot / "config.txt")
            os.sync()
        finally:
            p.remount("ro")
    _write_json(p.state / "pending.json", {"from": cur, "to": new, "version": req["version"],
                                           "previous": req.get("current", "unknown"), "at": time.time()})
    _set_root(p, cur, new)
    say("ready", slot=new)
    return new


def watchdog(p: Paths = Paths(), confirm_s: float = CONFIRM_S, reboot=lambda: subprocess.run(["reboot"])) -> str:
    """At boot (root): if an update is pending, wait for the station to confirm
    it; if it doesn't in time, go back to the previous slot. Returns what it did."""
    pending_file = p.state / "pending.json"
    try:
        pending = json.loads(pending_file.read_text())
    except (OSError, ValueError):
        return "nothing pending"
    if booted_slot(p) != pending["to"]:              # it never booted the new one
        _write_json(p.state / "result.json", {"ok": False, "version": pending["version"],
                                              "reason": "the new version didn't start"})
        pending_file.unlink(missing_ok=True)
        return "not booted: gave up"
    end = time.monotonic() + confirm_s
    while time.monotonic() < end:
        if not pending_file.exists():
            return "confirmed"
        time.sleep(2)
    _set_root(p, pending["to"], pending["from"])
    _write_json(p.state / "result.json", {"ok": False, "version": pending["version"],
                                          "reason": "the new version didn't come on air"})
    pending_file.unlink(missing_ok=True)
    log.warning("update not confirmed: back to %s", pending["from"])
    reboot()
    return "rolled back"


SQUASHFS_MAGIC = b"hsqs"


def rollback(p: Paths = Paths(), reboot=lambda: subprocess.run(["reboot"])) -> str:
    """(root; the service menu's "go back to the previous version") Boot the
    other root slot, if it holds a system, and reboot. Writes ROLLBACK_STATUS."""
    cur = booted_slot(p)
    other = "p3" if cur == "p2" else "p2"
    try:
        with open(p.device(other), "rb") as f:
            ok = f.read(4) == SQUASHFS_MAGIC
    except OSError:
        ok = False
    if not ok:
        _write_json(p.rollback_status, {"ok": False, "reason": "no previous version"})
        return "nothing to go back to"
    (p.state / "pending.json").unlink(missing_ok=True)     # (not an update to confirm)
    _write_json(p.state / "result.json", {"ok": True, "rolled_back": True})
    _set_root(p, cur, other)
    _write_json(p.rollback_status, {"ok": True, "slot": other})
    log.warning("going back to the previous version in %s", other)
    reboot()
    return f"back to {other}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Install an update (root), or watch a new one boot.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("install")        # reads REQUEST
    sub.add_parser("watchdog")
    sub.add_parser("rollback")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.cmd == "watchdog":
        print(watchdog())
        return 0
    if args.cmd == "rollback":
        print(rollback())
        return 0
    try:
        req = json.loads(REQUEST.read_text())
        REQUEST.unlink(missing_ok=True)
        slot = install(req)
        end = time.monotonic() + 30                  # let the station say it's restarting
        while not GO.exists() and time.monotonic() < end:
            time.sleep(0.5)
        GO.unlink(missing_ok=True)
        print(f"installed {req['version']} into {slot}; rebooting", flush=True)
        subprocess.run(["reboot"])
        return 0
    except Exception as e:
        _write_json(STATUS, {"state": "error", "message": str(e)})
        print(f"update failed: {e}", flush=True)
        return 1


# --- the station's side --------------------------------------------------------------

class Updates:
    """Check / install from the page; confirm and announce after the reboot.
    say(text) speaks on the radio (in the background)."""

    def __init__(self, source: str = DEFAULT_SOURCE, say: Callable[[str], None] = lambda t: None,
                 state: Path = STATE, run: Path = RUN, version_file: Path = VERSION_FILE,
                 restart_delay_s: float = 9) -> None:
        self.source, self.say = source, say
        self.restart_delay_s = restart_delay_s    # time to say "restarting" before it does
        self.state, self.version_file = state, version_file
        self.request, self.status_file, self.go = run / "update-request", run / "update-status", run / "update-reboot"
        self._lock = threading.Lock()
        self._job = {"state": "idle"}
        self.available: dict | None = None
        try:                    # made by the station (radio), so it can confirm updates
            self.state.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    @property
    def version(self) -> str:
        return this_version(self.version_file)

    def status(self) -> dict:
        with self._lock:
            job = dict(self._job)
        if job["state"] in ("requested", "downloading", "verifying", "ready"):
            try:
                job.update(json.loads(self.status_file.read_text()))
            except (OSError, ValueError):
                pass
        return {"version": self.version, "job": job,
                "available": None if self.available is None else
                {"version": self.available["version"], "notes": self.available.get("notes", "")}}

    def check(self) -> dict:
        m = fetch_manifest(self.source)
        with self._lock:
            self.available = m if newer(m["version"], self.version) else None
            self._job = {"state": "checked"}
        return self.status()

    def install(self) -> dict:
        if self.available is None:
            raise ValueError("check for an update first")
        with self._lock:
            if self._job["state"] in ("requested", "downloading", "verifying", "ready"):
                raise ValueError("an update is already under way")
            self._job = {"state": "requested"}
        m = self.available
        self.status_file.unlink(missing_ok=True)
        _write_json(self.request, {"version": m["version"], "rootfs": m["rootfs"], "current": self.version,
                                   "kernel_sha256": m["kernel_sha256"], "config_txt": m.get("config_txt")})
        self.say(f"Downloading an update for Sleep Radio: version {m['version']}. "
                 "The music carries on while it downloads.")
        threading.Thread(target=self._follow, name="update", daemon=True).start()
        return self.status()

    def _follow(self) -> None:
        """Watch the root helper; speak at the end."""
        end = time.monotonic() + 1800
        while time.monotonic() < end:
            try:
                st = json.loads(self.status_file.read_text())
            except (OSError, ValueError):
                st = {}
            if st.get("state") == "ready":
                with self._lock:
                    self._job = {"state": "ready"}
                self.say("The update is ready. Sleep Radio is restarting now, and will be back in about a minute.")
                time.sleep(self.restart_delay_s)
                self.go.touch()
                return
            if st.get("state") == "error":
                with self._lock:
                    self._job = {"state": "error", "message": st.get("message", "the update failed")}
                self.say("The update didn't work, so nothing has changed.")
                return
            time.sleep(1)
        with self._lock:
            self._job = {"state": "error", "message": "the update helper didn't answer"}

    def on_air(self) -> None:
        """Once music is playing after a start-up: confirm a pending update,
        and say how the last one went (once)."""
        pending = self.state / "pending.json"
        if pending.exists():
            try:
                version = json.loads(pending.read_text())["version"]
            except (OSError, ValueError, KeyError):
                version = self.version
            _write_json(self.state / "result.json", {"ok": True, "version": version})
            pending.unlink(missing_ok=True)
            log.info("update to %s confirmed", version)
        result = self.state / "result.json"
        try:
            r = json.loads(result.read_text())
        except (OSError, ValueError):
            return
        result.unlink(missing_ok=True)
        if r.get("rolled_back"):
            self.say(f"Sleep Radio has gone back to its previous version, {self.version}.")
        elif r.get("ok"):
            self.say(f"Sleep Radio has been updated to version {r.get('version', self.version)}.")
        else:
            self.say("The last update didn't work, so Sleep Radio went back to the version it had before.")


if __name__ == "__main__":
    sys.exit(main())
