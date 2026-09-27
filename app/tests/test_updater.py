import functools
import hashlib
import json
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi import updater as up


@pytest.fixture
def release(tmp_path: Path):
    """A release served over HTTP, and a fake Pi: boot partition, slots, state."""
    rel = tmp_path / "release"
    rel.mkdir()
    image = bytes(range(256)) * 4096                    # 1 MB "rootfs"
    (rel / "rootfs.squashfs").write_bytes(image)
    (rel / "config.txt").write_text("dtparam=audio=off\n# new\n")
    kernel = b"kernel-bytes"
    manifest = {"version": "1.2.0", "notes": "Faster start.",
                "rootfs": {"file": "rootfs.squashfs", "size": len(image), "sha256": hashlib.sha256(image).hexdigest()},
                "kernel_sha256": hashlib.sha256(kernel).hexdigest(),
                "config_txt": {"file": "config.txt", "sha256": hashlib.sha256((rel / "config.txt").read_bytes()).hexdigest()}}
    (rel / "manifest.json").write_text(json.dumps(manifest))
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(rel))
    handler.log_message = lambda *a: None
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "cmdline.txt").write_text("console=serial0 root=/dev/mmcblk0p2 rootfstype=squashfs ro\n")
    (boot / "config.txt").write_text("dtparam=audio=off\n")
    (boot / "Image").write_bytes(kernel)
    (tmp_path / "proc_cmdline").write_text("console=serial0 root=/dev/mmcblk0p2 rootfstype=squashfs ro")
    for slot in ("p2", "p3"):
        (tmp_path / f"slot_{slot}").write_bytes(b"\xff" * (2 * len(image)))   # bigger than the image, like a partition
    remounts = []
    paths = up.Paths(boot=boot, proc_cmdline=tmp_path / "proc_cmdline", state=tmp_path / "state",
                     status=tmp_path / "run" / "update-status", go=tmp_path / "run" / "update-reboot",
                     device=lambda s: tmp_path / f"slot_{s}", remount=remounts.append)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/manifest.json"
    yield url, paths, image, remounts, tmp_path
    httpd.shutdown()


def test_versions() -> None:
    assert up.newer("1.2.0", "v1.1.9") and up.newer("v1.10", "1.9") and not up.newer("1.2.0", "v1.2.0")
    assert not up.newer("1.0.0", "1.2.0")
    assert up.newer("1.2.0", "v1.1.0-3-gabc123-dirty") is True       # a dev build: offer the release
    assert up.newer("1.2.0", "unknown")


def test_manifest_from_a_url(release) -> None:
    url, *_ = release
    m = up.fetch_manifest(url)
    assert m["version"] == "1.2.0" and m["rootfs"]["url"].endswith("/rootfs.squashfs")
    assert m["config_txt"]["url"].endswith("/config.txt")


def test_install_writes_the_spare_slot_and_switches(release) -> None:
    url, paths, image, remounts, tmp = release
    req = {**up.fetch_manifest(url), "current": "v1.1.0"}
    assert up.install(req, paths) == "p3"
    assert (tmp / "slot_p3").read_bytes()[:len(image)] == image           # written into the other slot
    assert (tmp / "slot_p2").read_bytes()[:4] == b"\xff" * 4              # the running one untouched
    assert "root=/dev/mmcblk0p3" in (paths.boot / "cmdline.txt").read_text()
    assert "# new" in (paths.boot / "config.txt").read_text()
    assert remounts.count("rw") == remounts.count("ro") == 2              # boot back to read-only each time
    pending = json.loads((paths.state / "pending.json").read_text())
    assert pending == {**pending, "from": "p2", "to": "p3", "version": "1.2.0", "previous": "v1.1.0"}
    assert json.loads(paths.status.read_text())["state"] == "ready"


def test_a_changed_kernel_or_a_bad_download_changes_nothing(release) -> None:
    url, paths, image, remounts, tmp = release
    req = up.fetch_manifest(url)
    with pytest.raises(ValueError, match="kernel"):
        up.install({**req, "kernel_sha256": "0" * 64}, paths)
    bad = {**req, "rootfs": {**req["rootfs"], "sha256": "0" * 64}}
    with pytest.raises(ValueError, match="checksum"):
        up.install(bad, paths)
    assert "root=/dev/mmcblk0p2" in (paths.boot / "cmdline.txt").read_text()   # still boots the old one
    assert not (paths.state / "pending.json").exists() and remounts == []


def test_confirmed_after_the_reboot(release) -> None:
    url, paths, image, remounts, tmp = release
    up.install(up.fetch_manifest(url), paths)
    (tmp / "proc_cmdline").write_text("root=/dev/mmcblk0p3 ro")         # it booted the new one
    said = []
    u = up.Updates(url, said.append, state=paths.state, run=tmp / "run", version_file=tmp / "version")
    t = threading.Thread(target=lambda: said.append(up.watchdog(paths, confirm_s=3, reboot=lambda: said.append("REBOOT"))))
    t.start()
    time.sleep(0.5)
    u.on_air()                                                            # music is playing: confirm
    t.join()
    assert "updated to version 1.2.0" in said[0] and said[-1] == "confirmed"
    assert "REBOOT" not in said and "root=/dev/mmcblk0p3" in (paths.boot / "cmdline.txt").read_text()
    u.on_air()
    assert len(said) == 2                                                 # said once


def test_not_confirmed_goes_back(release) -> None:
    url, paths, image, remounts, tmp = release
    up.install(up.fetch_manifest(url), paths)
    (tmp / "proc_cmdline").write_text("root=/dev/mmcblk0p3 ro")
    rebooted = []
    assert up.watchdog(paths, confirm_s=0.5, reboot=lambda: rebooted.append(1)) == "rolled back"
    assert rebooted == [1] and "root=/dev/mmcblk0p2" in (paths.boot / "cmdline.txt").read_text()
    (tmp / "proc_cmdline").write_text("root=/dev/mmcblk0p2 ro")         # back on the old one
    said = []
    up.Updates(url, said.append, state=paths.state, run=tmp / "run", version_file=tmp / "v").on_air()
    assert "went back" in said[0]


def test_the_station_side(release) -> None:
    url, paths, image, remounts, tmp = release
    (tmp / "version").write_text("v1.1.0\n")
    said = []
    u = up.Updates(url, said.append, state=paths.state, run=tmp / "run", version_file=tmp / "version",
                   restart_delay_s=0.1)
    assert u.status()["version"] == "v1.1.0"
    with pytest.raises(ValueError):
        u.install()                                                       # check first
    st = u.check()
    assert st["available"] == {"version": "1.2.0", "notes": "Faster start."}
    (tmp / "run").mkdir(exist_ok=True)
    u.install()
    req = json.loads((tmp / "run" / "update-request").read_text())
    assert req["version"] == "1.2.0" and req["current"] == "v1.1.0" and "Downloading" in said[0]
    # the root helper's part, then the station follows it to the end
    paths.status = tmp / "run" / "update-status"
    up.install(req, paths)
    end = time.monotonic() + 15
    while not (tmp / "run" / "update-reboot").exists() and time.monotonic() < end:
        time.sleep(0.2)
    assert (tmp / "run" / "update-reboot").exists() and "restarting" in said[-1]
    (tmp / "version").write_text("1.2.0\n")
    assert u.check()["available"] is None                                 # up to date now


def _flaky_server(data: bytes, cut_after: int, cuts: int = 1, ranges: bool = True):
    """Serves data, but closes the first `cuts` responses after cut_after bytes
    (as GitHub's download servers sometimes do); honours Range requests."""
    from http.server import BaseHTTPRequestHandler
    state = {"cuts": cuts, "requests": []}

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            rng = self.headers.get("Range")
            state["requests"].append(rng)
            start = int(rng.split("=")[1].rstrip("-")) if (rng and ranges) else 0
            body = data[start:]
            self.send_response(206 if start else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if state["cuts"] > 0:
                state["cuts"] -= 1
                self.wfile.write(body[:cut_after])
                self.wfile.flush()
                self.close_connection = True
                return
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/rootfs.squashfs", state


def test_a_download_cut_short_is_resumed(monkeypatch) -> None:
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    data = bytes(range(256)) * 20000                       # ~5 MB
    httpd, url, state = _flaky_server(data, cut_after=3_000_000, cuts=2)
    try:
        out = bytearray()
        got = up.download(url, len(data), out.extend)
        assert got == len(data) and bytes(out) == data
        assert state["requests"][0] is None and state["requests"][1].startswith("bytes=")   # resumed
    finally:
        httpd.shutdown()


def test_a_download_that_cant_resume_gives_up_cleanly(monkeypatch) -> None:
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    data = bytes(1_000_000)
    httpd, url, state = _flaky_server(data, cut_after=100_000, cuts=99, ranges=False)
    try:
        out = bytearray()
        got = up.download(url, len(data), out.extend, attempts=3)
        assert got < len(data)                             # install() then refuses on the checksum
    finally:
        httpd.shutdown()


def test_install_survives_a_cut_download(release, monkeypatch) -> None:
    monkeypatch.setattr(up.time, "sleep", lambda s: None)
    url, paths, image, remounts, tmp = release
    req = up.fetch_manifest(url)
    httpd, flaky_url, state = _flaky_server(image, cut_after=len(image) // 3, cuts=1)
    try:
        req["rootfs"]["url"] = flaky_url
        assert up.install(req, paths) == "p3"
        assert (tmp / "slot_p3").read_bytes()[:len(image)] == image
    finally:
        httpd.shutdown()
