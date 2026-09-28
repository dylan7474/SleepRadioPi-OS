"""Entry point: run the Broadcast Radio station and its listen-in web page.

    python -m sleepradiopi.main [--config PATH] [--port N]

Settings come from ~/.config/sleepradiopi/config.json (created with the
defaults on first run). Open http://<pi>.local/ to listen.
"""

from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from dataclasses import asdict
from pathlib import Path

from sleepradiopi.audio import pcm
from sleepradiopi.audio.eq import Equalizer
from sleepradiopi.audio.speaker import SpeakerControl, SpeakerOutput, TeeOutput
from sleepradiopi.broadcast.station import Station
from sleepradiopi.config.settings import DEFAULT_PATH, load, save
from sleepradiopi import startup_sound
from sleepradiopi import wifi
from sleepradiopi.updater import Updates
from sleepradiopi.voices import STANDARD_NAME, VoiceJobs
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


def _make_warming_up(station: Station, ready) -> None:
    """Make the start-up sound's spoken line in the DJ's voice, once (per voice),
    after the show is under way. startup_sound.py plays it at the next start."""
    path = startup_sound.speech_file(Path.home(), station.dj_voice)
    if path.is_file():
        return
    while not ready():
        time.sleep(5)
    try:
        audio = station.render_speech(startup_sound.WARMING_UP)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as f:
            f.write(audio.astype("<i2").tobytes())
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        logging.info("start-up line made in the %s voice (%.1f s)", station.dj_voice, len(audio) / pcm.SAMPLE_RATE)
    except Exception:
        logging.exception("couldn't make the start-up line")


def _hotspot() -> dict | None:
    """The radio's own network's details, while it's a hotspot."""
    st = wifi.status()
    return st.get("hotspot") if st.get("mode") == "hotspot" else None


def _announce_hotspot(announcer) -> None:
    """When the radio becomes a hotspot (no saved network in range), say how to
    join it, once each time -- in a new place you'd otherwise never know."""
    was = None
    while True:
        mode = wifi.status().get("mode")
        if mode == "hotspot" and was != "hotspot":
            time.sleep(3)
            announcer.speak()
        was = mode
        time.sleep(10)


def _say_now(station: Station, control, text: str) -> None:
    """Say something on the radio's speaker now (in the background), e.g. an
    update's progress. Needs a voice and a speaker; otherwise it's only logged."""
    logging.info("say: %s", text)
    if control is None or not station._has_voice:
        return

    def run():
        from sleepradiopi.io.announce import Clip
        try:
            control.play_clip(Clip(station.render_speech(text), "notice", "Notice"))
        except Exception:
            logging.exception("couldn't say it")
    threading.Thread(target=run, name="say", daemon=True).start()


def _after_first_song(station: Station, updates: Updates) -> None:
    """Once music is playing after a start-up, confirm a pending update (so the
    boot watchdog doesn't roll it back) and say how it went."""
    while station.current_track is None:
        time.sleep(2)
    updates.on_air()


def _fetch_standard_voice(jobs: VoiceJobs) -> None:
    """A radio with no voice at all (e.g. a fresh card): get the standard one
    once it's online. Tries again every minute until it works."""
    logging.warning("no voice packs: downloading the standard voice when online")
    while True:
        try:
            jobs.download_standard(wait=True)
        except ValueError:            # one is already going (e.g. started from the page)
            pass
        if jobs.status()["state"] == "done":
            return
        time.sleep(60)


def _voice_installed(station: Station, config_file: Path, control, name: str) -> None:
    """A new voice is there. If the radio had no working voice, use it (restart)."""
    from sleepradiopi.config.settings import save_setting
    from sleepradiopi.web.server import RESTART_ENV, _restart_soon
    if station.dj_voice and station.dj_voice in station.voices() and station.dj_voice != name:
        return                         # keep the voice in use; the page can switch
    save_setting(config_file, "broadcast_voice", name)
    logging.info("using the new voice %s", name)
    if os.environ.get(RESTART_ENV):
        _restart_soon(control)


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
    stream = Mp3Output(enabled=settings.web_stream or not settings.speaker_enabled)
    speaker = control = announcer = None
    if settings.speaker_enabled:
        startup_sound.wait_for_it()   # the chime / "warming up" may still be playing
        speaker = SpeakerOutput(settings.speaker_device, mono=settings.speaker_mono,
                                eq=Equalizer(pcm.SAMPLE_RATE, pcm.CHANNELS, settings.speaker_eq,
                                             settings.speaker_highpass_hz))
        station = Station(cfg, tts, TeeOutput(speaker, stream))
        control = SpeakerControl(
            speaker, station.listener_joined, station.listener_left,
            state_file=Path.home() / ".local" / "state" / "sleepradiopi" / "speaker.json",
            default_volume=settings.speaker_volume, config_file=args.config)
        # A long press says the radio's address (for finding the web page away from home).
        # Made only once the show is playing music, so it never holds up the opening.
        on_air = lambda: bool(tts and tts.ready) and station.current_track is not None
        announcer = Announcer(station.render_speech if station._has_voice else None, control.play_clip,
                              voice_ready=on_air, hotspot=_hotspot)
        announcer.start()
        if station._has_voice:          # the sides test's "Left speaker" / "Right speaker"
            control.speech = lambda text: station.render_speech(text) if on_air() else None
        threading.Thread(target=_announce_hotspot, args=(announcer,), name="hotspot-watch",
                         daemon=True).start()
        if station._has_voice:
            threading.Thread(target=_make_warming_up, args=(station, on_air), name="warming-up",
                             daemon=True).start()
        Knob(lambda clicks: control.step(clicks * settings.knob_step), control.toggle,
             on_long_press=announcer.speak).start()
        control.play()   # a bedside radio plays as soon as it's powered
    else:
        station = Station(cfg, tts, stream)
    updates = Updates(settings.update_source, say=lambda text: _say_now(station, control, text))
    threading.Thread(target=_after_first_song, args=(station, updates), name="update-confirm",
                     daemon=True).start()
    jobs = VoiceJobs(voices, Path.home() / "voice-inbox",
                     on_installed=lambda name: _voice_installed(station, args.config, control, name))
    if not station.voices():
        threading.Thread(target=_fetch_standard_voice, args=(jobs,), name="voice-fetch", daemon=True).start()
    serve(station, stream, args.port or settings.http_port, control, args.config, announcer, jobs, updates)


if __name__ == "__main__":
    main()
