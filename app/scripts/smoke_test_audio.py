#!/usr/bin/env python3
"""Roadmap milestone 1: confirm the HiFiBerry board shows up as an ALSA
device and plays sound.

Deliberately bypasses the app's own playback code (python-vlc, the ambient
mixer) -- this is the "is the hardware even wired up right" check that
should pass *before* any of that is trusted. See docs/ROADMAP.md.

Usage (on the Pi, inside the venv -- see docs/PI_SETUP.md):

    python3 scripts/smoke_test_audio.py --list-devices
    python3 scripts/smoke_test_audio.py --device 1
    python3 scripts/smoke_test_audio.py --device "hifiberry" --seconds 3 --freq 440

`--device` accepts either a sounddevice index (from --list-devices) or a
case-insensitive substring of the device name -- useful because the
HiFiBerry's exact ALSA index can shift depending on what else is attached.
"""

from __future__ import annotations

import argparse

import numpy as np


def generate_tone(
    freq_hz: float, seconds: float, sample_rate: int, volume: float = 0.2
) -> np.ndarray:
    """A plain sine-wave test tone as float32 samples in [-volume, volume].

    Pure function, no audio-library dependency -- exercised directly by
    tests/test_smoke_audio.py without needing sounddevice/PortAudio
    installed.
    """
    if not 0.0 < volume <= 1.0:
        raise ValueError("volume must be in (0, 1]")
    t = np.linspace(0, seconds, int(sample_rate * seconds), endpoint=False)
    return (volume * np.sin(2 * np.pi * freq_hz * t)).astype(np.float32)


def resolve_device(sd, spec: str | None):
    """Accept a sounddevice index ("1") or a name substring ("hifiberry")."""
    if spec is None:
        return None
    if spec.isdigit():
        return int(spec)
    spec_lower = spec.lower()
    for index, info in enumerate(sd.query_devices()):
        if spec_lower in info["name"].lower():
            return index
    raise SystemExit(f"No output device matching {spec!r} -- try --list-devices")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--device", help="sounddevice index or name substring")
    parser.add_argument("--freq", type=float, default=440.0, help="Hz")
    parser.add_argument("--seconds", type=float, default=2.0)
    parser.add_argument("--sample-rate", type=int, default=44_100)
    parser.add_argument("--volume", type=float, default=0.2, help="0-1")
    args = parser.parse_args()

    try:
        import sounddevice as sd
    except ImportError as exc:
        raise SystemExit(
            "sounddevice not installed -- this script only runs on the Pi, "
            "inside the venv set up per docs/PI_SETUP.md"
        ) from exc

    if args.list_devices:
        print(sd.query_devices())
        return

    device = resolve_device(sd, args.device)
    tone = generate_tone(args.freq, args.seconds, args.sample_rate, args.volume)
    print(
        f"Playing {args.freq:.0f} Hz for {args.seconds:.1f}s "
        f"on device={device if device is not None else '(default)'}..."
    )
    sd.play(tone, args.sample_rate, device=device)
    sd.wait()
    print("Done. Heard it? That's roadmap milestone 1.")


if __name__ == "__main__":
    main()
