"""Local music folder scanning + playback.

Mirrors SleepRadio's rule: any subfolder containing audio files is an
"album". Tags/duration read via mutagen (replaces MediaMetadataRetriever).
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Album:
    path: Path
    title: str
    tracks: list[Path]


def scan_music_folder(root: Path) -> list[Album]:
    """Walk root and return one Album per subfolder containing audio files.

    TODO: port the folder-walk + audio-extension filtering from
    MusicRepository.kt's local-music path.
    """
    raise NotImplementedError
