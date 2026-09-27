"""Adding voices: download the standard one, or upload your own.

A voice pack is a folder in the voices folder with model.onnx, tokens.txt
and (usually) espeak-ng-data/ -- a Piper/VITS model as sherpa-onnx uses it.
Packs arrive as an archive (.tar.bz2 / .tar.gz / .tgz / .tar.xz / .tar /
.zip) holding one such folder; the model may be called anything.onnx (it's
renamed model.onnx).

The standard voice is Piper's British English "southern_english_female"
(low), packaged by sherpa-onnx; its training data is OpenSLR 83, CC BY-SA
4.0. It's what a radio uses when it has no voice of its own.

On the appliance image the voices folder is on the read-only media
partition, and the station isn't root: it saves the archive under
~/voice-inbox and asks voice-install-watch (root) to install it, by writing
SLEEPRADIOPI_VOICE_REQUEST; the answer comes back in <request>.result.
Without that variable (a desktop) it installs straight into the folder.

    python3 -m sleepradiopi.voices install --archive A --name N --voices DIR
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

STANDARD_NAME = "stock"
STANDARD_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
                "vits-piper-en_GB-southern_english_female-low.tar.bz2")
STANDARD_CREDIT = ("Standard voice: Piper “southern_english_female” (training data OpenSLR 83, "
                   "CC BY-SA 4.0), packaged by sherpa-onnx.")
REQUEST_ENV = "SLEEPRADIOPI_VOICE_REQUEST"
SUFFIXES = (".tar.bz2", ".tbz2", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar", ".zip")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MAX_UPLOAD = 400 * 1024 * 1024
UNPACKERS = {".tar.bz2": ["bzcat"], ".tbz2": ["bzcat"], ".tar.gz": ["gzip", "-dc"], ".tgz": ["gzip", "-dc"],
             ".tar.xz": ["xz", "-dc"], ".txz": ["xz", "-dc"]}


def check_name(name) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValueError("a voice's name: lower-case letters, digits, - and _ (up to 32)")
    return name


def archive_suffix(filename: str) -> str:
    low = filename.lower()
    for suffix in SUFFIXES:
        if low.endswith(suffix):
            return suffix
    raise ValueError("send a .tar.bz2, .tar.gz, .tar.xz, .tar or .zip of the voice folder")


# --- installing (root on the appliance) ----------------------------------------------------

def _safe_members(tar: tarfile.TarFile):
    for m in tar:
        if m.name.startswith("/") or ".." in Path(m.name).parts or not (m.isfile() or m.isdir()):
            continue                     # no absolute paths, no climbing out, no links or devices
        yield m


def unpack(archive: Path, into: Path) -> None:
    into.mkdir(parents=True)
    suffix = archive_suffix(archive.name)
    if suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                parts = Path(info.filename).parts
                if info.filename.startswith("/") or ".." in parts:
                    continue
                z.extract(info, into)
        return
    if suffix == ".tar":
        with tarfile.open(archive, "r:") as tar:
            for m in _safe_members(tar):
                tar.extract(m, into, filter="data")
        return
    # bz2/xz aren't built into the image's Python: stream through the command-line tools
    proc = subprocess.Popen([*UNPACKERS[suffix], str(archive)], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for m in _safe_members(tar):
                tar.extract(m, into, filter="data")
        # tar stops at its end marker; read the padding after it too, or the
        # unpacker dies writing into a closed pipe and looks like a failure
        while proc.stdout.read(1 << 16):
            pass
    except tarfile.TarError as e:
        proc.kill()
        proc.wait()
        raise ValueError(f"the archive is damaged or isn't what its name says ({e})") from None
    except BaseException:
        proc.kill()                        # keep the real error, not the unpacker's
        proc.wait()
        raise
    finally:
        proc.stdout.close()
    if proc.wait() != 0:
        raise ValueError("the archive couldn't be unpacked (damaged, or not what its name says)")


def find_pack(root: Path) -> Path:
    """The folder in an unpacked archive that holds the model and tokens."""
    for d in [root, *sorted(p for p in root.rglob("*") if p.is_dir())]:
        if (d / "tokens.txt").is_file() and any(d.glob("*.onnx")):
            return d
    raise ValueError("no voice in it: it needs a .onnx model and tokens.txt in one folder")


def normalise(pack: Path) -> None:
    """Rename the model to model.onnx (and its .onnx.json to match)."""
    models = sorted(pack.glob("*.onnx"))
    if len(models) != 1:
        raise ValueError("a voice folder should hold one .onnx model")
    model = models[0]
    if model.name != "model.onnx":
        cfg = model.with_name(model.name + ".json")
        model.rename(pack / "model.onnx")
        if cfg.is_file():
            cfg.rename(pack / "model.onnx.json")
    if (pack / "model.onnx").stat().st_size < 1_000_000:
        raise ValueError("model.onnx is too small to be a voice")


def install(archive: Path, name: str, voices: Path) -> Path:
    """Unpack archive as voices/<name> (replacing one of that name). Returns it."""
    check_name(name)
    work = voices / f".{name}.new"
    old = voices / f".{name}.old"
    for d in (work, old):
        shutil.rmtree(d, ignore_errors=True)
    try:
        unpack(archive, work)
        pack = find_pack(work)
        normalise(pack)
        target = voices / name
        if target.exists():
            target.rename(old)
        pack.rename(target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)
    return voices / name


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Install a voice pack archive.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install")
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--voices", type=Path, required=True)
    p.add_argument("--inbox", type=Path, help="only install archives from here")
    args = parser.parse_args(argv)
    try:
        if args.inbox and args.archive.resolve().parent != args.inbox.resolve():
            raise ValueError("not an archive from the inbox")
        path = install(args.archive, args.name, args.voices)
        print(json.dumps({"ok": True, "name": args.name, "path": str(path)}))
        return 0
    except Exception as e:                     # reported to the page, not a traceback
        print(json.dumps({"ok": False, "name": args.name, "error": str(e)}))
        return 1


# --- the station's side: downloads, uploads, and asking for the install -------------------

class VoiceJobs:
    """One voice job at a time; status() is what the page shows."""

    def __init__(self, voices_dir: Path, inbox: Path, on_installed: Callable[[str], None] = lambda n: None,
                 url: str = STANDARD_URL) -> None:
        self.voices_dir, self.inbox = Path(voices_dir), Path(inbox)
        self.on_installed = on_installed
        self.url = url
        self._lock = threading.Lock()
        self._job = {"state": "idle"}

    def status(self) -> dict:
        with self._lock:
            return dict(self._job)

    def _set(self, **job) -> None:
        with self._lock:
            self._job = job

    def _begin(self, **job) -> None:
        with self._lock:
            if self._job["state"] in ("downloading", "uploading", "installing"):
                raise ValueError("a voice is already being added; wait for it to finish")
            self._job = job

    def _free(self) -> int:
        self.inbox.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self.inbox).free

    def download_standard(self, wait: bool = False) -> None:
        """Download and install the standard voice (in the background)."""
        self._begin(state="downloading", name=STANDARD_NAME, progress=0.0)
        t = threading.Thread(target=self._download, name="voice-download", daemon=True)
        t.start()
        if wait:
            t.join()

    def _download(self) -> None:
        path = self.inbox / (STANDARD_NAME + archive_suffix(self.url))
        try:
            self.inbox.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(self.url, timeout=30) as r:
                total = int(r.headers.get("Content-Length") or 0)
                if total and total + 20_000_000 > self._free():
                    raise ValueError("not enough room on the data partition for the download")
                got = 0
                with open(path, "wb") as f:
                    while chunk := r.read(1 << 20):
                        f.write(chunk)
                        got += len(chunk)
                        if total:
                            self._set(state="downloading", name=STANDARD_NAME, progress=round(got / total, 3))
            log.info("standard voice downloaded (%d MB)", got >> 20)
            self._install(path, STANDARD_NAME)
        except Exception as e:
            log.warning("standard voice download failed: %s", e)
            path.unlink(missing_ok=True)
            self._set(state="error", name=STANDARD_NAME, message=f"Download failed: {e}")

    def receive(self, name: str, filename: str, length: int, read: Callable[[int], bytes]) -> None:
        """An upload: save it (now, from the request), then install it in the background."""
        check_name(name)
        suffix = archive_suffix(filename)
        if not 0 < length <= MAX_UPLOAD:
            raise ValueError(f"a voice archive should be under {MAX_UPLOAD >> 20} MB")
        if length + 20_000_000 > self._free():
            raise ValueError("not enough room on the data partition for that upload")
        self._begin(state="uploading", name=name, progress=0.0)
        path = self.inbox / (name + suffix)
        try:
            left = length
            with open(path, "wb") as f:
                while left:
                    chunk = read(min(left, 1 << 20))
                    if not chunk:
                        raise ValueError("the upload stopped part-way")
                    f.write(chunk)
                    left -= len(chunk)
                    self._set(state="uploading", name=name, progress=round(1 - left / length, 3))
        except Exception as e:
            path.unlink(missing_ok=True)
            self._set(state="error", name=name, message=f"Upload failed: {e}")
            raise
        threading.Thread(target=self._install, args=(path, name), name="voice-install", daemon=True).start()

    def _install(self, archive: Path, name: str) -> None:
        self._set(state="installing", name=name)
        try:
            request = os.environ.get(REQUEST_ENV)
            if request:
                result = self._ask_root(Path(request), archive, name)
            else:
                install(archive, name, self.voices_dir)
                result = {"ok": True}
            if not result.get("ok"):
                raise ValueError(result.get("error") or "it couldn't be installed")
            log.info("voice installed: %s", name)
            self._set(state="done", name=name)
            self.on_installed(name)
        except Exception as e:
            log.warning("voice install failed: %s", e)
            self._set(state="error", name=name, message=f"Not installed: {e}")
        finally:
            archive.unlink(missing_ok=True)

    def _ask_root(self, request: Path, archive: Path, name: str, timeout_s: float = 600) -> dict:
        result = request.with_name(request.name + ".result")
        result.unlink(missing_ok=True)
        tmp = request.with_name(request.name + ".tmp")
        tmp.write_text(f"{name}\n{archive}\n")
        os.replace(tmp, request)
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            if result.exists():
                try:
                    return json.loads(result.read_text().strip().splitlines()[-1])
                except (ValueError, IndexError):
                    return {"ok": False, "error": "no answer from the installer"}
                finally:
                    result.unlink(missing_ok=True)
            time.sleep(0.5)
        return {"ok": False, "error": "the installer didn't answer (is voice-install-watch running?)"}


if __name__ == "__main__":
    sys.exit(main())
