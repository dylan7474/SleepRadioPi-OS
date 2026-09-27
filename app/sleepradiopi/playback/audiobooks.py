"""Audiobook playback with per-book resume position.

Mirrors SleepRadio's audiobook handling: a folder of book files/folders,
resume position persisted (there: Room DB; here: the settings store in
sleepradiopi.config).
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Audiobook:
    path: Path
    title: str
    resume_position_ms: int = 0


def scan_audiobooks_folder(root: Path) -> list[Audiobook]:
    """TODO: port the folder scan from MusicRepository.kt's audiobook path."""
    raise NotImplementedError
