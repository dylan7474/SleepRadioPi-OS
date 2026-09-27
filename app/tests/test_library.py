import os
from pathlib import Path

from sleepradiopi.broadcast import library
from sleepradiopi.broadcast.library import scan_music


def _library(root: Path) -> Path:
    music = root / "music"
    for rel in ("Nick Drake/Pink Moon/01 - Pink Moon.mp3", "Nick Drake/Pink Moon/02 - Place to Be.mp3"):
        p = music / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"not really audio")
    return music


def _count_reads(monkeypatch) -> list:
    calls = []
    real = library._tags
    monkeypatch.setattr(library, "_tags", lambda p: calls.append(p) or real(p))
    return calls


def test_second_scan_reads_no_tags(tmp_path: Path, monkeypatch) -> None:
    music, cache = _library(tmp_path), tmp_path / "tags.json"
    calls = _count_reads(monkeypatch)
    first = scan_music(music, cache)
    assert len(calls) == 2 and cache.exists()
    second = scan_music(music, cache)
    assert len(calls) == 2
    assert second == first
    assert [t.title for t in second] == ["Pink Moon", "Place to Be"]
    assert {t.artist for t in second} == {"Nick Drake"}


def test_changed_and_removed_files(tmp_path: Path, monkeypatch) -> None:
    music, cache = _library(tmp_path), tmp_path / "tags.json"
    scan_music(music, cache)
    calls = _count_reads(monkeypatch)
    changed = music / "Nick Drake/Pink Moon/01 - Pink Moon.mp3"
    changed.write_bytes(b"re-tagged, a different size")
    os.utime(changed, ns=(1, 1))
    (music / "Nick Drake/Pink Moon/02 - Place to Be.mp3").unlink()
    tracks = scan_music(music, cache)
    assert calls == [changed]
    assert [t.title for t in tracks] == ["Pink Moon"]
    assert "Place to Be" not in cache.read_text()


def test_bad_cache_is_ignored(tmp_path: Path) -> None:
    music, cache = _library(tmp_path), tmp_path / "tags.json"
    cache.write_text("{half a file")
    assert len(scan_music(music, cache)) == 2


def test_no_cache_path_still_scans(tmp_path: Path) -> None:
    assert len(scan_music(_library(tmp_path))) == 2
