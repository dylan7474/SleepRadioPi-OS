"""Power-cut-safe file replacement.

On the appliance image the data partition is the only thing written at run
time, and the plug can be pulled at any moment. Writing a file in place can
leave it truncated; instead write a temp file next to it, fsync it, rename it
over the old one (atomic on the same filesystem) and fsync the directory so
the rename itself is on disk. After a power cut you get the old file or the
new one, never half of either.
"""

import os
import tempfile
from pathlib import Path


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # a temp file of its own: two writers at once used to share one name and could mix
    # their text (a settings file that then couldn't be read)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
