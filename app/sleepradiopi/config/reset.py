"""Factory reset: every setting back to how it came.

Keeps what belongs to this radio rather than to its owner's choices -- where
its music, jingles, audiobooks and voices are, the speaker being on, its
sound card and pins, where updates come from -- plus the voice in use (so
the DJ can still talk you through setting it up) and the speaker tuning
(mono/stereo, EQ, low cut: they suit the cabinet, not a person). Everything
else in the settings goes, the web page's password included; so do the
networks added on the page (and a renamed hotspot) and the remembered
volume. Music, audiobooks (and where each was left), voices and caches stay.
The network on the card itself (/boot/wpa_supplicant.conf) is part of the
image, so it stays too: the radio is as it was when it was flashed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sleepradiopi.config.atomic import write_atomic
from sleepradiopi.config.backup import LOCAL

log = logging.getLogger(__name__)

KEEP = (LOCAL - {"web_password", "stream_source"}) | {"broadcast_voice", "speaker_mono", "speaker_eq",
                                                        "speaker_highpass_hz"}


def forget_wifi(networks: Path) -> None:
    """Forget the networks added on the page, and the hotspot's own name/password."""
    networks.unlink(missing_ok=True)
    log.warning("reset: the saved Wi-Fi networks are forgotten")


def factory_reset(config_file: Path, networks: Path, volume_file: Path | None) -> list[str]:
    """Reset the settings. Returns the settings that were kept."""
    try:
        raw = json.loads(config_file.read_text())
    except (OSError, ValueError):
        raw = {}
    kept = {k: v for k, v in raw.items() if k in KEEP}
    write_atomic(config_file, json.dumps(kept, indent=2))
    forget_wifi(networks)
    if volume_file is not None:
        volume_file.unlink(missing_ok=True)
    log.warning("FACTORY RESET: settings back to how they came (kept: %s)", ", ".join(sorted(kept)) or "nothing")
    return sorted(kept)
