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
from sleepradiopi.config.settings import DEFAULT_PATH, load, save, save_setting
from sleepradiopi import startup_sound
from sleepradiopi import wifi
from sleepradiopi.updater import Updates
from sleepradiopi.voices import STANDARD_NAME, VoiceJobs
from sleepradiopi.io.announce import Announcer, beep
from sleepradiopi.io.knob import Chord, Knob
from sleepradiopi.io import service as service_mod
from sleepradiopi.io import presets as presets_mod
from sleepradiopi.playback import radio as radio_mod
from sleepradiopi.media import MediaLibrary
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
    while not station.music_started:      # a song, or an internet radio station
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


def _speaker_stuck() -> None:
    """The sound card won't open (a stuck driver: only a restart clears it) --
    restart the radio, at most once an hour, so it never sits there silent
    with nobody to pull the plug."""
    from sleepradiopi.config import power
    flag = Path.home() / ".local" / "state" / "sleepradiopi" / "speaker-restart"
    try:
        if time.time() - flag.stat().st_mtime < 3600:
            logging.error("speaker stuck, but restarted for that within the hour: not again")
            return
    except OSError:
        pass
    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.touch()
    except OSError:
        pass
    logging.error("speaker stuck: restarting the radio")
    power.request_restart()


def _service_menu(station: Station, control, presets, config_file: Path, ready=lambda: True):
    """The service menu's voice and actions (io/service.py). While it's open the
    show is paused and the speaker plays only the menu (MenuSound): its words,
    and a soft tick while they're being made. The fixed lines are made once
    per voice (in the background, after the show starts) and kept on disk, so
    they play at once; only the status report is made fresh."""
    import hashlib
    import shutil
    import numpy as np
    from sleepradiopi.audio import pcm
    from sleepradiopi.broadcast.station import _tick
    from sleepradiopi.config import power, reset
    from sleepradiopi.io.announce import Clip, addresses, spoken_ip
    from sleepradiopi.updater import this_version
    from sleepradiopi.web.server import _restart_soon
    volume_file = Path.home() / ".local" / "state" / "sleepradiopi" / "speaker.json"
    cache = Path.home() / ".cache" / "sleepradiopi" / "service-menu"
    made: dict = {}                   # the status report (made fresh), in memory
    making = threading.Lock()
    sound = service_mod.MenuSound(_tick(), pcm.SAMPLE_RATE, pcm.CHANNELS)
    held = [False]                    # the menu has the speaker
    was_playing = [False]
    said = [0]                        # the latest say(): an older one's words aren't played

    def is_status(text):
        return text.startswith("Status report.")

    def kept(text):
        name = f"{station.dj_voice}-{hashlib.sha1(text.encode()).hexdigest()[:16]}.raw"
        return cache / (f"status-{name}" if is_status(text) else name)

    def render(text):
        path = kept(text)
        try:
            return np.frombuffer(path.read_bytes(), dtype=np.int16).reshape(-1, pcm.CHANNELS)
        except (OSError, ValueError):
            pass
        with making:                  # one at a time, so a line being made ahead is used, not made twice
            if text in made:
                return made[text]
            audio = station.render_speech(text)
            if text in service_mod.FIXED or is_status(text):
                try:
                    cache.mkdir(parents=True, exist_ok=True)
                    if is_status(text):          # (only the latest status report is kept)
                        for old in cache.glob("status-*.raw"):
                            old.unlink(missing_ok=True)
                    tmp = path.with_suffix(".tmp")
                    tmp.write_bytes(audio.astype("<i2").tobytes())
                    os.replace(tmp, path)
                except OSError:
                    logging.exception("service menu: couldn't keep a line")
            else:
                made.clear()
                made[text] = audio
            return audio

    def make_fixed_lines():
        """Once per voice: the fixed lines, made while the show plays; then the
        status report, remade whenever what it says changes (checked every
        10 minutes), so pressing 3 answers at once."""
        while not ready():
            time.sleep(10)
        for text in service_mod.FIXED:
            if not kept(text).is_file():
                try:
                    render(text)
                    time.sleep(5)     # (leave the voice free for the show between them)
                except Exception:
                    logging.exception("service menu: couldn't make a line")
                    return
        while True:
            try:
                text = f"{status()} {service_mod.ROLLBACK_ASK}"
                if not kept(text).is_file():
                    render(text)
            except Exception:
                logging.exception("service menu: couldn't make the status report")
            time.sleep(600)

    def say(text):
        logging.info("service menu: %s", text)
        if not held[0]:               # (not in the menu: over the show, as a notice)
            presets._clip(beep(), "Service menu")
            if station._has_voice:
                threading.Thread(target=lambda: control.play_clip(Clip(render(text), "button", "Service menu")),
                                 name="service-say", daemon=True).start()
            return
        said[0] += 1
        me = said[0]
        sound.put(beep())
        if not station._has_voice:
            return
        sound.making += 1

        def run():
            try:
                audio = render(text)
                if said[0] == me:
                    sound.add(np.concatenate([pcm.silence(0.2), audio]))
            except Exception:
                logging.exception("service menu: couldn't say it")
            finally:
                sound.making -= 1
        threading.Thread(target=run, name="service-say", daemon=True).start()

    def busy():
        return sound.talking

    def prepare(text):
        if station._has_voice:
            try:
                render(text)
            except Exception:
                logging.exception("service menu: couldn't make its words")

    def on_open():
        was_playing[0] = not control.paused
        control.pause()               # the show stops while the menu is open
        held[0] = True
        control.speaker.set_hold(sound)

    def on_close(resume):
        if not resume:
            return                    # (a restart: stay quiet until it happens)

        def run():
            time.sleep(0.3)
            end = time.monotonic() + 30
            while sound.talking and time.monotonic() < end:
                time.sleep(0.1)       # its last words ("Cancelled...")
            held[0] = False
            control.speaker.set_hold(None)
            if was_playing[0]:
                control.play()        # back to what was playing
        threading.Thread(target=run, name="service-close", daemon=True).start()

    def quiet_then_say(words):
        """Say the last words (the show is already paused) and return once
        they've been said -- before a restart."""
        logging.info("service menu: %s", words)
        control.save_now()
        say(words)
        time.sleep(0.3)
        end = time.monotonic() + 60
        while sound.talking and time.monotonic() < end:
            time.sleep(0.05)

    def restart(words):
        quiet_then_say(words)
        if not power.request_restart():
            _restart_soon(control)    # (a desktop: at least restart the station)

    def forget_wifi():
        reset.forget_wifi(wifi.NETWORKS)
        try:
            wifi.ask("hotspot")
        except OSError:
            logging.warning("service menu: no Wi-Fi manager to ask")

    def status():
        try:
            free = shutil.disk_usage(station.music_dir).free / 1e9
        except OSError:
            free = None
        return service_mod.status_text(addresses(), wifi.status(), this_version(), free, len(station.tracks),
                                       spoken_ip)

    def rollback(words):
        quiet_then_say(words)
        st = power.rollback_status() if power.request_rollback() else None
        return bool(st and st.get("ok"))  # (not: the menu says so, and the show carries on)

    def factory_reset(words):
        quiet_then_say(words)
        reset.factory_reset(config_file, wifi.NETWORKS, volume_file)
        try:
            wifi.ask("hotspot")
        except OSError:
            pass
        os._exit(75)                  # the supervisor restarts the station with the settings as they came

    if station._has_voice:
        threading.Thread(target=make_fixed_lines, name="service-lines", daemon=True).start()
    return service_mod.ServiceMenu(say, {"restart": restart, "wifi": forget_wifi, "status": status,
                                         "rollback": rollback, "reset": factory_reset}, prepare=prepare, busy=busy,
                                   on_open=on_open, on_close=on_close)


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
    # (next to the music by default: /media/audiobooks on the radio, whose config names /media/music)
    cfg["audiobooks_folder"] = Path(settings.audiobooks_folder or cfg["music_folder"].parent / "audiobooks").expanduser()
    cfg["book_cache"] = Path.home() / ".cache" / "sleepradiopi" / "books.json"
    cfg["book_positions"] = Path.home() / ".local" / "state" / "sleepradiopi" / "book-positions.json"
    cfg["podcast_cache"] = Path.home() / ".cache" / "sleepradiopi" / "podcasts"
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
        # (the start-up sound chimes, then ticks, until the speaker first opens: SpeakerOutput._open)
        speaker = SpeakerOutput(settings.speaker_device, mono=settings.speaker_mono,
                                eq=Equalizer(pcm.SAMPLE_RATE, pcm.CHANNELS, settings.speaker_eq,
                                             settings.speaker_highpass_hz))
        station = Station(cfg, tts, TeeOutput(speaker, stream))
        speaker.on_stuck = _speaker_stuck
        control = SpeakerControl(
            speaker, station.listener_joined, station.listener_left,
            state_file=Path.home() / ".local" / "state" / "sleepradiopi" / "speaker.json",
            default_volume=settings.speaker_volume, config_file=args.config,
            noise=(settings.noise_on, settings.noise_kind, settings.noise_mix))
        # A long press says the radio's address (for finding the web page away from home).
        # Made only once the show is playing music, so it never holds up the opening.
        on_air = lambda: bool(tts and tts.ready) and station.music_started
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
        presets = presets_mod.Presets(station, control, args.config, settings.buttons, announcer)
        # An audiobook steps back a minute after the sleep timer, and pauses the radio at its end.
        station.paused_by_sleep = lambda: control.slept
        station.on_book_end = control.pause
        buttons = {code: (lambda i=i: presets.press(i), lambda i=i: presets.hold(i))
                   for code, i in presets_mod.KEYCODES.items()}
        # The service menu: hold buttons 1 and 4 together for 5 s.
        menu = presets.menu = _service_menu(station, control, presets, args.config, ready=on_air)
        from sleepradiopi.broadcast.station import _tick
        tick = _tick()
        chord = Chord({2, 5}, service_mod.HOLD_S, on_start=menu.holding, on_fire=menu.open,
                      on_tick=lambda: presets._clip(tick, "Button"))   # a tick a second, counting down
        # (SLEEPRADIOPI_INPUT_DIR: somewhere else to look for the knob and buttons -- e.g. an empty
        # folder for a test run on a desktop, whose keyboard would otherwise count as buttons 1-4)
        knob = Knob(lambda clicks: control.step(clicks * settings.knob_step), control.toggle,
                    devices=Path(os.environ.get("SLEEPRADIOPI_INPUT_DIR", "/dev/input")),
                    on_long_press=announcer.speak, buttons=buttons, chord=chord)
        presets.keys = knob.keys         # the page's buttons go down and up through the same timers
        knob.start()
        control.play()   # a bedside radio plays as soon as it's powered
    else:
        station = Station(cfg, tts, stream)
        presets = presets_mod.Presets(station, None, args.config, settings.buttons)
    # Podcasts: the shows followed are settings; their episode lists refresh in the background.
    station.podcasts.save = lambda shows: save_setting(args.config, "podcasts", shows)
    station.podcasts.keep_fresh()
    # A station or album playing instead of the show is kept over a restart.
    station.on_source = lambda source: save_setting(args.config, "stream_source", source)
    updates = Updates(settings.update_source, say=lambda text: _say_now(station, control, text))
    threading.Thread(target=_after_first_song, args=(station, updates), name="update-confirm",
                     daemon=True).start()
    jobs = VoiceJobs(voices, Path.home() / "voice-inbox",
                     on_installed=lambda name: _voice_installed(station, args.config, control, name))
    if not station.voices():
        threading.Thread(target=_fetch_standard_voice, args=(jobs,), name="voice-fetch", daemon=True).start()
    # Internet radio's station search: a copy of the directory, kept fresh.
    directory = radio_mod.Directory(Path.home() / ".cache" / "sleepradiopi" / "stations.tsv")
    directory.keep_fresh()
    # The web page's music library manager (upload / delete); the library is rescanned after.
    media = MediaLibrary({"music": cfg["music_folder"], "jingles": cfg["jingles_folder"],
                          "audiobooks": cfg["audiobooks_folder"]},
                         on_changed=station.reload_library)
    serve(station, stream, args.port or settings.http_port, control, args.config, announcer, jobs, updates,
          presets, directory, media)


if __name__ == "__main__":
    main()
