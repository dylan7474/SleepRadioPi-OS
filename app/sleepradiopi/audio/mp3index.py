"""Where the time is in a long MP3, so a book resumes at once.

ffmpeg's -ss into an MP3 with no table of contents reads every frame up to
the place: on a Zero, 1 s an hour in, 2-6 s three hours into a long book, the
show silent all that while (and a button's words waiting behind it). So a long
MP3 gets an index, made once in the background: the byte where each ~10 s
starts. A start then skips straight to the nearest one before it
(-skip_initial_bytes) and ffmpeg's -ss covers the last few seconds.
(Within ~25 ms: ffmpeg trims the encoder's delay only at the file's start.)

The index is made in its own process at low priority (scanning a 350 MB file
in Python would hold the show's threads up), one file at a time, and kept as a
small file named after the MP3's path, size and time. Until it's made, a start
seeks the old way.
"""

from __future__ import annotations

import hashlib
import logging
import mmap
import os
import queue
import subprocess
import sys
import threading
from array import array
from pathlib import Path

log = logging.getLogger(__name__)

FOLDER = Path.home() / ".cache" / "sleepradiopi" / "mp3index"
STEP_MS = 10_000            # an entry every ~10 s
PREROLL_MS = 1_000          # at least this much decoded before the place (the first frames after a cut
                            # lean on the one before: a click, if heard)
MIN_START_MS = 60_000       # nearer the start, ffmpeg's own seek is quick enough
MIN_BYTES = 20_000_000      # smaller files aren't worth an index
VERSION = 1
DROP_EVERY = 8 << 20        # the scan lets go of what it's read (not filling the cache the radio needs)

# MPEG audio layer III: kbit/s by bitrate index (MPEG-1, then MPEG-2 / 2.5); sample rates by version bits
_KBPS = ((0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
         (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160))
_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _frame(m, pos: int) -> tuple[int, int, int] | None:
    """(bytes, samples, sample rate) of a layer III frame header at pos, or None."""
    if pos + 4 > len(m) or m[pos] != 0xFF:
        return None
    b1, b2 = m[pos + 1], m[pos + 2]
    version, layer = (b1 >> 3) & 3, (b1 >> 1) & 3
    bitrate, rate_i = b2 >> 4, (b2 >> 2) & 3
    if b1 & 0xE0 != 0xE0 or version == 1 or layer != 1 or bitrate in (0, 15) or rate_i == 3:
        return None
    rate = _RATES[version][rate_i]
    kbps = _KBPS[0 if version == 3 else 1][bitrate]
    pad = (b2 >> 1) & 1
    if version == 3:
        return 144_000 * kbps // rate + pad, 1152, rate
    return 72_000 * kbps // rate + pad, 576, rate


def _after_id3(m) -> int:
    pos = 0
    while m[pos:pos + 3] == b"ID3" and pos + 10 <= len(m):
        size = (m[pos + 6] << 21) | (m[pos + 7] << 14) | (m[pos + 8] << 7) | m[pos + 9]
        pos += 10 + size + (10 if m[pos + 5] & 0x10 else 0)
    return pos


def _sync(m, pos: int) -> int | None:
    """The next frame from pos: a header whose next frame has a header too."""
    while True:
        pos = m.find(b"\xff", pos)
        if pos < 0:
            return None
        f = _frame(m, pos)
        if f is not None and (pos + f[0] >= len(m) or _frame(m, pos + f[0]) is not None):
            return pos
        pos += 1


def scan(path: Path) -> array:
    """The index: ms, byte, ms, byte... -- where each ~STEP_MS starts."""
    out = array("q")
    with open(path, "rb") as f:
        fd = f.fileno()
        size = os.fstat(fd).st_size
        if not size:
            return out
        with mmap.mmap(fd, 0, access=mmap.ACCESS_READ) as m:
            m.madvise(mmap.MADV_SEQUENTIAL)
            pos = _sync(m, _after_id3(m))
            samples, rate, first, next_ms, dropped = 0, 0, True, 0, 0
            while pos is not None and pos < size:
                f = _frame(m, pos)
                if f is None:
                    pos = _sync(m, pos + 1)
                    continue
                length, n, rate = f
                if first:                    # a Xing / Info / VBRI frame holds no sound
                    first = False
                    if any(m.find(tag, pos, min(pos + 64, size)) >= 0 for tag in (b"Xing", b"Info", b"VBRI")):
                        pos += length
                        continue
                ms = samples * 1000 // rate
                if ms >= next_ms:
                    out.extend((ms, pos))
                    next_ms = ms + STEP_MS
                samples += n
                pos += length
                if pos - dropped >= DROP_EVERY:
                    end = pos - pos % mmap.PAGESIZE
                    m.madvise(mmap.MADV_DONTNEED, dropped, end - dropped)
                    os.posix_fadvise(fd, dropped, end - dropped, os.POSIX_FADV_DONTNEED)
                    dropped = end
    return out


def file_for(path: Path) -> Path | None:
    try:
        st = path.stat()
    except OSError:
        return None
    key = f"{VERSION}|{path.resolve()}|{st.st_size}|{st.st_mtime_ns}"
    return FOLDER / (hashlib.sha1(key.encode()).hexdigest()[:20] + ".idx")


_loaded: dict[Path, array] = {}
_lock = threading.Lock()
_queue: queue.Queue = queue.Queue()
_queued: set[Path] = set()
_thread: threading.Thread | None = None


def lookup(path: Path, start_ms: int) -> tuple[int, int] | None:
    """(byte, ms) to start a decode from for start_ms -- at least PREROLL_MS
    before it -- or None (not worth it, or no index yet: then one's made)."""
    if start_ms < MIN_START_MS or path.suffix.lower() != ".mp3":
        return None
    idx = file_for(path)
    if idx is None:
        return None
    with _lock:
        entries = _loaded.get(idx)
    if entries is None:
        try:
            entries = array("q")
            with open(idx, "rb") as f:
                entries.frombytes(f.read())
        except OSError:
            want([path])
            return None
        with _lock:
            if len(_loaded) > 8:
                _loaded.clear()
            _loaded[idx] = entries
    target = start_ms - PREROLL_MS
    lo, hi = 0, len(entries) // 2 - 1          # the last entry at or before target
    if hi < 0 or entries[0] > target:
        return None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if entries[2 * mid] <= target:
            lo = mid
        else:
            hi = mid - 1
    return entries[2 * lo + 1], entries[2 * lo]


def want(paths) -> None:
    """Have indexes made (in the background, one at a time) for the long MP3s
    among paths that don't have one."""
    global _thread
    for path in paths:
        path = Path(path)
        if path.suffix.lower() != ".mp3":
            continue
        try:
            if path.stat().st_size < MIN_BYTES:
                continue
        except OSError:
            continue
        idx = file_for(path)
        with _lock:
            if idx is None or idx.is_file() or path in _queued:
                continue
            _queued.add(path)
            if _thread is None:
                _thread = threading.Thread(target=_run, name="mp3-index", daemon=True)
                _thread.start()
        _queue.put(path)


def _run() -> None:
    while True:
        path = _queue.get()
        try:
            idx = file_for(path)
            if idx is not None and not idx.is_file():
                idx.parent.mkdir(parents=True, exist_ok=True)
                proc = subprocess.Popen([sys.executable, "-m", __name__, str(path), str(idx)],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                try:
                    os.setpriority(os.PRIO_PROCESS, proc.pid, 19)
                except OSError:
                    pass
                _, err = proc.communicate()
                if proc.returncode == 0:
                    log.info("mp3 index made: %s", path.name)
                else:
                    log.warning("mp3 index: couldn't make one for %s: %s", path.name,
                                err.decode(errors="replace").strip()[-200:])
        except Exception:
            log.exception("mp3 index: %s", path)
        finally:
            with _lock:
                _queued.discard(path)


def main(argv: list[str]) -> int:
    """python -m sleepradiopi.audio.mp3index MP3 OUT: write the index of MP3 to OUT."""
    src, out = Path(argv[0]), Path(argv[1])
    entries = scan(src)
    if not entries:
        print("no MPEG layer III frames found", file=sys.stderr)
        return 1
    tmp = out.with_name(out.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(entries.tobytes())
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
