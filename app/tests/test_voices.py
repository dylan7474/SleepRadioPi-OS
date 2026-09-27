import io
import json
import os
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi import voices as v
from sleepradiopi.web.server import make_handler

from test_artist_radio import _station

MODEL = b"\x08" * 2_000_000                 # big enough to pass as a model


def _pack(root: Path, folder="vits-piper-en_GB-x-low", model_name="en_GB-x-low.onnx") -> Path:
    d = root / folder
    (d / "espeak-ng-data").mkdir(parents=True)
    (d / model_name).write_bytes(MODEL)
    (d / (model_name + ".json")).write_text("{}")
    (d / "tokens.txt").write_text("_ 0\n")
    (d / "espeak-ng-data" / "en_dict").write_bytes(b"x")
    return d


def _archive(tmp_path: Path, suffix: str) -> Path:
    src = tmp_path / "src"
    _pack(src)
    out = tmp_path / f"voice{suffix}"
    if suffix == ".zip":
        with zipfile.ZipFile(out, "w") as z:
            for f in src.rglob("*"):
                z.write(f, f.relative_to(src))
    else:
        tar_path = tmp_path / "voice.tar"
        with tarfile.open(tar_path, "w") as t:
            t.add(src / "vits-piper-en_GB-x-low", arcname="vits-piper-en_GB-x-low")
        if suffix == ".tar":
            return tar_path.rename(out)
        tool = {".tar.bz2": "bzip2", ".tar.gz": "gzip", ".tar.xz": "xz"}[suffix]
        subprocess.run([tool, "-k", str(tar_path)], check=True)
        Path(str(tar_path) + {".tar.bz2": ".bz2", ".tar.gz": ".gz", ".tar.xz": ".xz"}[suffix]).rename(out)
    return out


@pytest.mark.parametrize("suffix", [".tar.bz2", ".tar.gz", ".tar.xz", ".tar", ".zip"])
def test_install_every_kind_of_archive(tmp_path: Path, suffix: str) -> None:
    voices = tmp_path / "voices"
    voices.mkdir()
    got = v.install(_archive(tmp_path, suffix), "newvoice", voices)
    assert got == voices / "newvoice"
    assert (got / "model.onnx").read_bytes() == MODEL and (got / "model.onnx.json").is_file()
    assert (got / "tokens.txt").is_file() and (got / "espeak-ng-data" / "en_dict").is_file()
    assert sorted(p.name for p in voices.iterdir()) == ["newvoice"]     # no leftovers


def test_install_replaces_a_voice_of_the_same_name(tmp_path: Path) -> None:
    voices = tmp_path / "voices"
    (voices / "stock").mkdir(parents=True)
    (voices / "stock" / "old.txt").write_text("old")
    v.install(_archive(tmp_path, ".tar"), "stock", voices)
    assert not (voices / "stock" / "old.txt").exists() and (voices / "stock" / "model.onnx").is_file()


def test_bad_archives_are_refused_and_leave_nothing(tmp_path: Path) -> None:
    voices = tmp_path / "voices"
    voices.mkdir()
    empty = tmp_path / "empty.tar"
    with tarfile.open(empty, "w") as t:
        data = b"hello"
        info = tarfile.TarInfo("notes/readme.txt"); info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError, match="no voice"):
        v.install(empty, "x", voices)
    evil = tmp_path / "evil.tar"
    with tarfile.open(evil, "w") as t:
        for name in ("../escape.txt", "/etc/evil.txt"):
            info = tarfile.TarInfo(name); info.size = 5
            t.addfile(info, io.BytesIO(b"owned"))
    with pytest.raises(ValueError):
        v.install(evil, "x", voices)
    assert not (tmp_path / "escape.txt").exists() and list(voices.iterdir()) == []
    with pytest.raises(ValueError):
        v.install(_archive(tmp_path, ".tar"), "../up", voices)                # bad name
    with pytest.raises(ValueError):
        v.archive_suffix("voice.rar")


def test_cli_prints_a_result_and_checks_the_inbox(tmp_path: Path, capsys) -> None:
    voices = tmp_path / "voices"
    voices.mkdir()
    arc = _archive(tmp_path, ".tar.gz")
    assert v.main(["install", "--archive", str(arc), "--name", "me", "--voices", str(voices),
                   "--inbox", str(tmp_path / "elsewhere")]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert v.main(["install", "--archive", str(arc), "--name", "me", "--voices", str(voices),
                   "--inbox", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "name": "me", "path": str(voices / "me")}


def _wait(jobs, states=("done", "error"), timeout=10):
    end = time.monotonic() + timeout
    while jobs.status()["state"] not in states and time.monotonic() < end:
        time.sleep(0.05)
    return jobs.status()


def test_download_the_standard_voice(tmp_path: Path) -> None:
    arc = _archive(tmp_path, ".tar.bz2")
    voices = tmp_path / "voices"
    voices.mkdir()
    installed = []
    jobs = v.VoiceJobs(voices, tmp_path / "inbox", installed.append, url=arc.as_uri())
    jobs.download_standard(wait=True)
    assert _wait(jobs)["state"] == "done" and installed == ["stock"]
    assert (voices / "stock" / "model.onnx").is_file() and list((tmp_path / "inbox").iterdir()) == []
    bad = v.VoiceJobs(voices, tmp_path / "inbox", url=(tmp_path / "missing.tar.bz2").as_uri())
    bad.download_standard(wait=True)
    assert bad.status()["state"] == "error" and "Download failed" in bad.status()["message"]


def test_upload_goes_through_the_root_helper(tmp_path: Path, monkeypatch) -> None:
    """The station writes a request; a stand-in for voice-install-watch answers it."""
    request = tmp_path / "run" / "voice-install"
    request.parent.mkdir()
    monkeypatch.setenv(v.REQUEST_ENV, str(request))
    voices, inbox = tmp_path / "voices", tmp_path / "inbox"
    voices.mkdir()

    def watcher():
        while not request.exists():
            time.sleep(0.05)
        name, archive = request.read_text().split("\n")[:2]
        request.unlink()
        rc = v.main(["install", "--archive", archive, "--name", name, "--voices", str(voices), "--inbox", str(inbox)])
        request.with_name("voice-install.result").write_text(json.dumps({"ok": rc == 0, "name": name}))
    threading.Thread(target=watcher, daemon=True).start()
    jobs = v.VoiceJobs(voices, inbox)
    data = _archive(tmp_path, ".zip").read_bytes()
    stream = io.BytesIO(data)
    jobs.receive("personal", "my voice.zip", len(data), stream.read)
    assert _wait(jobs)["state"] == "done"
    assert (voices / "personal" / "model.onnx").is_file() and list(inbox.iterdir()) == []


def test_upload_is_checked_first(tmp_path: Path) -> None:
    jobs = v.VoiceJobs(tmp_path / "voices", tmp_path / "inbox")
    for name, filename, length in (("Bad Name", "a.zip", 10), ("ok", "a.rar", 10), ("ok", "a.zip", 0),
                                   ("ok", "a.zip", v.MAX_UPLOAD + 1)):
        with pytest.raises(ValueError):
            jobs.receive(name, filename, length, io.BytesIO(b"x" * 10).read)
    with pytest.raises(ValueError, match="stopped"):
        jobs.receive("ok", "a.zip", 100, io.BytesIO(b"short").read)
    assert jobs.status()["state"] == "error"


def test_web_api(tmp_path: Path) -> None:
    voices = tmp_path / "voices"
    voices.mkdir()
    st = _station(tmp_path, voices_dir=voices)
    jobs = v.VoiceJobs(voices, tmp_path / "inbox", url=_archive(tmp_path, ".tar.xz").as_uri())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st, None, None, None, None, jobs))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def req(path, body=None, ctype="application/json"):
        r = urllib.request.Request(base + path, data=body, method="GET" if body is None else "POST",
                                   headers={"Content-Type": ctype})
        with urllib.request.urlopen(r) as resp:
            return json.load(resp)
    try:
        state = req("/api/voices")
        assert state["voices"] == [] and not state["has_standard"] and "CC BY-SA" in state["credit"]
        req("/api/voices/standard", b"{}")
        assert _wait(jobs)["state"] == "done" and req("/api/voices")["has_standard"]
        (tmp_path / "up").mkdir()
        data = _archive(tmp_path / "up", ".tar.gz").read_bytes()
        got = req("/api/voices/upload?name=mine&file=mine.tar.gz", data, "application/octet-stream")
        assert got["job"]["state"] in ("installing", "done")
        assert _wait(jobs)["state"] == "done" and "mine" in req("/api/voices")["voices"]
        with pytest.raises(urllib.error.HTTPError) as err:
            req("/api/voices/upload?name=BAD&file=x.zip", b"123", "application/octet-stream")
        assert err.value.code == 400
    finally:
        httpd.shutdown()


def test_trailing_padding_after_the_tar_end_is_read(tmp_path: Path) -> None:
    """Real archives often have lots of zero blocks after the end marker; if
    they're left unread the unpacker gets a broken pipe and fails."""
    src = tmp_path / "src"
    _pack(src)
    tar_path = tmp_path / "padded.tar"
    with tarfile.open(tar_path, "w") as t:
        t.add(src / "vits-piper-en_GB-x-low", arcname="v")
    with open(tar_path, "ab") as f:
        f.write(bytes(4 * 1024 * 1024))        # far more padding than any pipe buffer
    subprocess.run(["gzip", "-k", str(tar_path)], check=True)
    voices = tmp_path / "voices"
    voices.mkdir()
    arc = tmp_path / "padded.tar.gz"
    assert v.install(arc, "padded", voices) == voices / "padded"
