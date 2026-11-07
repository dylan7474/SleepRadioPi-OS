"""Power-cut-safe file replacement.

On the appliance image the data partition is the only thing written at run
time, and the plug can be pulled at any moment. Writing a file in place can
leave it truncated; instead write a temp file next to it, fsync it, rename it
over the old one (atomic on the same filesystem) and fsync the directory so
the rename itself is on disk. After a power cut you get the old file or the
new one, never half of either.
"""

import os
import re
import tempfile
import time
from pathlib import Path


_TMP = re.compile(r"\.[A-Za-z0-9_]{8}\.tmp$")      # (what tempfile.mkstemp makes below: name.XXXXXXXX.tmp)


def sweep_tmp(folder: Path, older_than_s: float = 600.0, now: float | None = None) -> list[str]:
    """Remove the temp files a write left behind when the radio died part-way through it (an out-of-memory kill, a
    power cut), in folder only; -> their names. Only those older than older_than_s, so a write going on now is safe."""
    now = time.time() if now is None else now
    gone = []
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return gone
    for e in entries:
        try:
            if e.is_file(follow_symlinks=False) and _TMP.search(e.name) and now - e.stat().st_mtime > older_than_s:
                os.unlink(e.path)
                gone.append(e.name)
        except OSError:
            pass
    return gone


def write_atomic(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # a temp file of its own: two writers at once used to share one name and could mix
    # their text (a settings file that then couldn't be read)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
