"""Entry point: run the Broadcast Radio station and its listen-in web page.

    python -m sleepradiopi.main [--config PATH] [--port N]

Settings come from ~/.config/sleepradiopi/config.json (created with the
defaults on first run). Open http://<pi>.local/ to listen.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import asdict
from pathlib import Path

from sleepradiopi.audio import pcm
from sleepradiopi.audio.eq import Equalizer
from sleepradiopi.audio.speaker import SpeakerControl, SpeakerOutput, TeeOutput
from sleepradiopi.broadcast.station import Station
from sleepradiopi.config.settings import DEFAULT_PATH, load, save
from sleepradiopi.io.announce import Announcer
from sleepradiopi.io.knob import Knob
from sleepradiopi.tts.worker import TtsWorker
from sleepradiopi.web.server import serve
from sleepradiopi.web.stream import Mp3Output

REPO = Path(__file__).resolve().parent.parent
MEDIA = Path.home() / "media"
# The 70s DJ hooks ship with the app, as in SleepRadio (a copy of its
# app/src/main/assets/dj_hooks_70s.txt); hooks_file in the settings overrides.
BUNDLED_HOOKS = Path(__file__).resolve().parent / "data" / "dj_hooks_70s.txt"


def main() -> None:
    parser = argparse.ArgumentParser(description="Sleep Radio broadcast station")
    parser.add_argument("--config", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--port", type=int, help="override http_port from the config")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not args.config.exists():
        save(args.config, load(args.config))
        logging.info("wrote default settings to %s", args.config)
    settings = load(args.config)

    cfg = asdict(settings)
    cfg["music_folder"] = Path(settings.music_folder or MEDIA / "music").expanduser()
    cfg["jingles_folder"] = Path(settings.jingles_folder or MEDIA / "jingles").expanduser()
    voices = Path(settings.voices_folder or REPO / "voices").expanduser()
    cfg["hooks_file"] = str(Path(settings.hooks_file).expanduser() if settings.hooks_file else BUNDLED_HOOKS)
    cfg["scan_cache"] = Path.home() / ".cache" / "sleepradiopi" / "scans.json"
    cfg["voices_dir"] = voices
    cfg["tag_cache"] = Path.home() / ".cache" / "sleepradiopi" / "tags.json"

    # One voice only: two don't fit a Pi Zero 2 W's RAM alongside the stream.
    tts = TtsWorker(voices, settings.broadcast_voice) if settings.broadcast_voice else None
    stream = Mp3Output()
    speaker = control = announcer = None
    if settings.speaker_enabled:
        speaker = SpeakerOutput(settings.speaker_device, mono=settings.speaker_mono,
                                eq=Equalizer(pcm.SAMPLE_RATE, pcm.CHANNELS, settings.speaker_eq,
                                             settings.speaker_highpass_hz))
        station = Station(cfg, tts, TeeOutput(speaker, stream))
        control = SpeakerControl(
            speaker, station.listener_joined, station.listener_left,
            state_file=Path.home() / ".local" / "state" / "sleepradiopi" / "speaker.json",
            default_volume=settings.speaker_volume, config_file=args.config)
        # A long press says the radio's address (for finding the web page away from home).
        announcer = Announcer(station.render_speech if station._has_voice else None, control.play_clip,
                              voice_ready=lambda: bool(tts and tts.ready))
        announcer.start()
        Knob(lambda clicks: control.step(clicks * settings.knob_step), control.toggle,
             on_long_press=announcer.speak).start()
        control.play()   # a bedside radio plays as soon as it's powered
    else:
        station = Station(cfg, tts, stream)
    serve(station, stream, args.port or settings.http_port, control, args.config, announcer)


if __name__ == "__main__":
    main()
