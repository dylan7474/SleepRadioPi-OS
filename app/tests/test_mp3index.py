import subprocess
from pathlib import Path

import numpy as np
import pytest

from sleepradiopi.audio import mp3index, pcm


@pytest.fixture
def long_mp3(tmp_path: Path) -> Path:
    """Three minutes of VBR MP3 (noise: no two moments alike), with an ID3 tag."""
    path = tmp_path / "book.mp3"
    subprocess.run([*pcm.FFMPEG, "-f", "lavfi", "-i", "anoisesrc=d=180:c=pink:seed=3", "-ac", "2", "-c:a", "libmp3lame", "-q:a", "6",
                    "-metadata", "title=A book", str(path)], check=True)
    return path


def test_the_index_starts_a_long_mp3_at_the_same_place(long_mp3: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(mp3index, "FOLDER", tmp_path / "idx")
    monkeypatch.setattr(mp3index, "MIN_BYTES", 0)
    monkeypatch.setattr(mp3index, "_loaded", {})
    idx = mp3index.file_for(long_mp3)
    idx.parent.mkdir()
    assert mp3index.lookup(long_mp3, 150_000) is None               # (none yet)
    assert mp3index.main([str(long_mp3), str(idx)]) == 0
    entries = mp3index.scan(long_mp3)
    ms = list(entries[0::2])
    assert ms[0] == 0 and ms[-1] >= 170_000
    assert all(9_900 < b - a < 10_100 for a, b in zip(ms, ms[1:]))
    skip, base = mp3index.lookup(long_mp3, 150_000)
    assert 139_000 <= base <= 149_000 and skip > 0

    fast = np.concatenate(list(pcm.decode(long_mp3, 150_000, 152_000)))
    monkeypatch.setattr(mp3index, "lookup", lambda *a: None)
    slow = np.concatenate(list(pcm.decode(long_mp3, 150_000, 152_000)))
    n = min(len(fast), len(slow))
    assert n > 1.9 * pcm.SAMPLE_RATE
    a, b = fast[:n, 0].astype(float), slow[:n, 0].astype(float)
    lag = max(range(-3000, 3001), key=lambda k: np.dot(a[5000:20000], b[5000 + k:20000 + k]))
    assert abs(lag) < 0.03 * pcm.SAMPLE_RATE      # the same moment, give or take the encoder's delay
    assert np.corrcoef(a[5000:20000], b[5000 + lag:20000 + lag])[0, 1] > 0.98   # (ffmpeg trims ~25 ms only at the file's start)


def test_short_or_near_the_start_or_not_mp3_uses_ffmpeg_s_own_seek(tmp_path: Path) -> None:
    assert mp3index.lookup(tmp_path / "a.mp3", 30_000) is None
    assert mp3index.lookup(tmp_path / "a.m4b", 3_600_000) is None
    assert mp3index.lookup(tmp_path / "missing.mp3", 3_600_000) is None


def test_want_makes_the_index_in_the_background(long_mp3: Path, tmp_path: Path, monkeypatch) -> None:
    import time
    monkeypatch.setattr(mp3index, "FOLDER", tmp_path / "idx")
    monkeypatch.setattr(mp3index, "MIN_BYTES", 0)
    mp3index.want([long_mp3, tmp_path / "notes.txt"])
    idx = mp3index.file_for(long_mp3)
    end = time.monotonic() + 30
    while not idx.is_file() and time.monotonic() < end:
        time.sleep(0.1)
    assert idx.is_file() and mp3index.lookup(long_mp3, 100_000) is not None
