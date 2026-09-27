"""Power-cut-safe file replacement.

On the appliance image the data partition is the only thing written at run
time, and the plug can be pulled at any moment. Writing a file in place can
leave it truncated; instead write a temp file next to it, fsync it, rename it
over the old one (atomic on the same filesystem) and fsync the directory so
the rename itself is on disk. After a power cut you get the old file or the
new one, never half of either.
"""

import os
from pathlib import Path


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
