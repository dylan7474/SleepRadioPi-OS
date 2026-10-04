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


def retire_artist_radio(station: Station, config_file: Path | None, presets, settings) -> bool:
    """Artist radio is retired (2026-10-01): whatever was saved as one -- the radio
    left on it, a button, a programme block -- becomes that artist's one-artist
    theme (Station.theme_for_artist: "The Beatles" -> theme "Beatles", still
    announced "Beatles Radio"). Saved. True if anything changed."""
    before = len(station.profiles)
    changed = False
    if settings.broadcast_artist:
        if not settings.broadcast_profile:
            station.set_profile(station.theme_for_artist(settings.broadcast_artist))
        if config_file is not None:
            save_setting(config_file, "broadcast_artist", None)
            save_setting(config_file, "broadcast_profile", station.profile)
        changed = True
    if presets is not None and presets.artists_to_themes(station.theme_for_artist):
        changed = True
    sched = getattr(station, "scheduler", None)
    if sched is not None and sched.artists_to_themes(station.theme_for_artist):
        if config_file is not None:
            save_setting(config_file, "programmes", sched.programmes)
        changed = True
    if len(station.profiles) != before and config_file is not None:
        save_setting(config_file, "profiles", station.profiles)
    if changed:
        logging.getLogger(__name__).info("artist radio retired: now %s", [p["name"] for p in station.profiles])
    return changed


def ensure_default_theme(station: Station, config_file: Path | None, media=None, presets=None) -> bool:
    """There's always a theme called Default (all my music unless given artists; its
    jingles are the start-up and fill-in ones when the theme playing has none). The
    "Sleep Radio" theme of all my music made before it becomes Default, taking its
    jingles folder, buttons and programmes along; otherwise Default is made, first.
    Messages with no theme become Default's. True if anything changed (saved)."""
    from sleepradiopi.broadcast.profiles import DEFAULT
    from sleepradiopi.config import brand
    from sleepradiopi.media import MediaError
    changed = False
    if not any(p["name"].lower() == DEFAULT.lower() for p in station.profiles):
        old = next((p for p in station.profiles if p.get("all") and p["name"].lower() == brand.name.lower()), None)
        if old is not None:
            name = old["name"]
            station._rename(old, DEFAULT)
            root = Path(station.jingles_dir)
            if media is not None and (root / name).is_dir() and not (root / DEFAULT).exists():
                try:
                    media.rename("jingles", name, DEFAULT)
                except MediaError as e:
                    logging.warning("Default's jingles folder: %s", e)
                finally:
                    media.done()
            if presets is not None:
                presets.rename_theme(name, DEFAULT)
            sched = getattr(station, "scheduler", None)
            if sched is not None and sched.rename_theme(name, DEFAULT) and config_file is not None:
                save_setting(config_file, "programmes", sched.programmes)
            logging.info("themes: %s is Default now", name)
        else:
            station.set_profiles([{"name": DEFAULT, "artists": [], "all": True}] + station.profiles)
            logging.info("themes: Default made")
        changed = True
    if any(not m.get("station") for m in station.messages.cfg["list"]):
        cfg = station.messages.cfg
        station.messages.set({**cfg, "list": [m if m.get("station") else {**m, "station": DEFAULT} for m in cfg["list"]]})
        if config_file is not None:
            save_setting(config_file, "messages", station.messages.cfg)
        changed = True
    if not station.artist and not station.profile:
        station.set_profile(DEFAULT)
        changed = True
    if changed and config_file is not None:
        save_setting(config_file, "profiles", station.profiles)
        save_setting(config_file, "broadcast_profile", station.profile)
    return changed


def jingle_folders(media, station: Station) -> None:
    """Each theme's jingles folder, Jingles/<theme>, made if it's missing; the old
    Power-on folder's jingles, and loose ones at the top, go into Default's (its
    jingles are the start-up ones)."""
    from sleepradiopi.broadcast.library import AUDIO_EXTENSIONS
    from sleepradiopi.broadcast.profiles import DEFAULT
    from sleepradiopi.media import MediaError
    root = Path(station.jingles_dir)
    missing = [p["name"] for p in station.profiles if not (root / p["name"]).is_dir()]
    audio = lambda d: sorted(f.name for f in d.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS) if d.is_dir() else []
    loose, old = audio(root), root / "Power-on"
    if not missing and not loose and not old.is_dir():
        return
    try:
        for name in missing:
            media.mkdir("jingles", "", name)
        if (loose or old.is_dir()) and not (root / DEFAULT).is_dir():
            media.mkdir("jingles", "", DEFAULT)
        for name in loose:
            media.move("jingles", name, "jingles", DEFAULT)
        for name in audio(old):
            media.move("jingles", "Power-on/" + name, "jingles", DEFAULT)
        if old.is_dir():
            media.delete("jingles", "Power-on")
        logging.info("jingles: folders made for %s; %d moved into %s", missing or "none", len(loose), DEFAULT)
    except MediaError as e:
        logging.warning("jingles folders: %s", e)
    finally:
        media.done()                      # (read-only again; a move rescans the jingles)


def _make_warming_up(station: Station, ready) -> None:
    """Make the start-up sound's spoken line in the DJ's voice, once (per voice),
    after the show is under way. startup_sound.py plays it at the next start."""
    path = startup_sound.speech_file(Path.home(), station.dj_voice)
    if path.is_file():
        return
    while not ready():
        time.sleep(5)
    try:
        audio = station.render_speech(startup_sound.warming_up())
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


def _wifi_lines(host: str) -> list[str]:
    """The Wi-Fi set-up's spoken lines, made ahead (the announcer keeps them on disk)."""
    from sleepradiopi.io import announce
    spot = wifi.status().get("hotspot")
    if not spot:                          # (no Wi-Fi manager: not the appliance)
        return []
    return [announce.announcement([], host, spot), announce.failed_text(spot), announce.JOINING, announce.JOINED]


def _wifi_event(announcer, was: dict | None, st: dict) -> None:
    """Say what the Wi-Fi is doing while it's being set up: a tune at once, and
    the words as soon as they're ready. Becoming a hotspot (no network set up,
    or asked for) says how to join it, once each time -- in a new place you'd
    otherwise never know; a join asked for from the hotspot says it's joining,
    then that it's on (and its new address) or that it couldn't. Everything
    else -- a drop in the night, the regular retries -- stays silent."""
    from sleepradiopi.io import announce
    mode = st.get("mode")
    if mode == (was or {}).get("mode"):
        return
    if mode == "hotspot":
        if st.get("again"):
            return
        if st.get("failed"):
            announcer.say([announce.failed_text(st["hotspot"])], announce.cue("failed"), wait=20)
        else:
            announcer.say([announcer.text], announce.cue("setup"), wait=20)
    elif st.get("setup") and was is not None:     # (seen happening, not found so at start-up)
        if mode == "connecting":
            announcer.say([announce.JOINING], announce.cue("joining"), wait=20)
        elif mode == "station":
            announcer.say([announce.JOINED, announcer.text], announce.cue("joined"), wait=20)


def _wifi_watch(announcer) -> None:
    was = None
    while True:
        st = wifi.status()
        try:
            _wifi_event(announcer, was, st)
        except Exception:
            logging.exception("wifi watch")
        was = st
        time.sleep(1)


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


def _lock_in_memory() -> None:
    """Keep the station's code and data (as they are now) in RAM. When the
    voice reloads it reads its ~60 MB model, which pushed parts of the station
    out of memory; reading them back in stalled it long enough for the speaker
    to run dry (a blip in the music). Only what's mapped now is locked, so
    later allocations can still go to the compressed swap. Needs the memlock
    limit raised (the appliance's sleepradiopi-station does); else a no-op."""
    import ctypes
    import ctypes.util
    MCL_CURRENT, MCL_ONFAULT = 1, 4       # (ONFAULT: only pages already in RAM -- the ~100 MB
    try:                                  #  in use, not the ~240 MB of address space reserved)
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        if libc.mlockall(MCL_CURRENT | MCL_ONFAULT) != 0:
            logging.info("memory not locked (errno %d): fine on a desktop", ctypes.get_errno())
            return
        logging.info("station memory locked in RAM (%s)", next((line.split(":")[1].strip()
                     for line in open("/proc/self/status") if line.startswith("VmLck")), "?"))
    except (OSError, AttributeError):
        pass


def _after_first_song(station: Station, updates: Updates) -> None:
    """Once music is playing after a start-up, confirm a pending update (so the
    boot watchdog doesn't roll it back) and say how it went; and lock the
    station in memory (everything it needs is loaded by then)."""
    while not station.music_started:      # a song, or an internet radio station
        time.sleep(2)
    _lock_in_memory()
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
    menu_ref: list = []               # (the menu, once made: which kind of status question it asks)
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
                text = f"{status()} {menu_ref[0].rollback_ask if menu_ref else service_mod.ROLLBACK_ASK}"
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
    menu = service_mod.ServiceMenu(say, {"restart": restart, "wifi": forget_wifi, "status": status,
                                         "rollback": rollback, "reset": factory_reset}, prepare=prepare, busy=busy,
                                   on_open=on_open, on_close=on_close)
    menu_ref.append(menu)
    return menu


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
    from sleepradiopi.config import brand
    brand.set_name(brand.name_for(asdict(settings)))   # "Sleep Radio", "Phonosphere", or station_name

    cfg = asdict(settings)
    cfg["music_folder"] = Path(settings.music_folder or MEDIA / "music").expanduser()
    cfg["jingles_folder"] = Path(settings.jingles_folder or MEDIA / "jingles").expanduser()
    # (next to the music by default: /media/audiobooks on the radio, whose config names /media/music)
    cfg["audiobooks_folder"] = Path(settings.audiobooks_folder or cfg["music_folder"].parent / "audiobooks").expanduser()
    # On demand: played only when asked for, never in the show (/media/ondemand on the radio)
    cfg["ondemand_folder"] = cfg["music_folder"].parent / "ondemand"
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
    speaker = control = announcer = lamps = None
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
            noise=(settings.noise_on, settings.noise_kind, settings.noise_mix),
            knob_mode=settings.knob_mode, knob_step=settings.knob_step)
        # A long press says the radio's address (for finding the web page away from home).
        # Made only once the show is playing music, so it never holds up the opening.
        on_air = lambda: bool(tts and tts.ready) and station.music_started
        announcer = Announcer(station.render_speech if station._has_voice else None, control.play_clip,
                              voice_ready=on_air, hotspot=_hotspot,
                              keep=Path.home() / ".cache" / "sleepradiopi" / "wifi-lines",
                              key=lambda: f"{station.dj_voice}|{station.config.announcer_speed}|{station.announcer_volume}")
        announcer.ahead = lambda: _wifi_lines(announcer.host)
        announcer.start()
        if station._has_voice:          # the sides test's "Left speaker" / "Right speaker"
            control.speech = lambda text: station.render_speech(text) if on_air() else None
        threading.Thread(target=_wifi_watch, args=(announcer,), name="wifi-watch", daemon=True).start()
        if station._has_voice:
            threading.Thread(target=_make_warming_up, args=(station, on_air), name="warming-up",
                             daemon=True).start()
        cathedral = settings.hardware == "cathedral"
        presets = presets_mod.Presets(station, control, args.config, settings.buttons, announcer,
                                      settings.buttons_night, settings.buttons_bank, settings.buttons_auto,
                                      count=presets_mod.MAX_N if cathedral else presets_mod.N, selector=cathedral,
                                      slots={"knob_long": settings.knob_long, "power_on": settings.power_on})
        presets.hotspot = _hotspot
        presets.keep_auto()
        if station._has_voice and not cathedral:
            # A button with steps says the one it landed on: the names are made ahead, in the
            # background (again if the voice, its speed or its volume changes); pips until then.
            from sleepradiopi.io.button_names import Names
            presets.names = Names(station.render_speech, Path.home() / ".cache" / "sleepradiopi" / "buttons",
                                  key=lambda: f"{station.dj_voice}|{station.config.announcer_speed}|{station.announcer_volume}",
                                  ready=on_air)
            presets.bake()
        # An audiobook steps back a minute after the sleep timer, and pauses the radio at its end.
        station.paused_by_sleep = lambda: control.slept
        station.speaker_paused = lambda: control.paused
        station.on_book_end = control.pause
        menu = presets.menu = _service_menu(station, control, presets, args.config, ready=on_air)
        menu.selector = cathedral
        from sleepradiopi.broadcast.station import _tick
        tick = _tick()
        raw_keys = {}
        if cathedral:
            # The Phonosphere: a 6-way rotary selector (a position counts once the knob
            # settles there) and a hidden button on the back -- a press swaps day and
            # night (or confirms in the service menu), a 5 s hold opens the service menu.
            raw_keys = {code: (lambda i=i: presets.selector_down(i), lambda i=i: presets.selector_up(i))
                        for code, i in presets_mod.KEYCODES.items() if i < presets.count}
            buttons = {presets_mod.BACK_KEY: (presets.back_press, presets.back_hold, presets_mod.BACK_HOLD_S)}
            chord = []
        else:
            buttons = {code: (lambda i=i: presets.press(i), lambda i=i: presets.hold(i))
                       for code, i in presets_mod.KEYCODES.items() if i < presets.count}
            # The service menu: hold buttons 1 and 4 together for 5 s.
            chord = [Chord({2, 5}, service_mod.HOLD_S, on_start=menu.holding, on_fire=menu.open,
                           on_tick=lambda: presets._clip(tick, "Button")),   # a tick a second, counting down
                     # buttons 2 and 3 held: swap the day and night sets of buttons
                     Chord(set(presets_mod.BANK_KEYS), presets_mod.BANK_HOLD_S, on_start=lambda: None,
                           on_fire=presets.toggle_bank, on_tick=lambda: presets._clip(tick, "Button"))]
        # (SLEEPRADIOPI_INPUT_DIR: somewhere else to look for the knob and buttons -- e.g. an empty
        # folder for a test run on a desktop, whose keyboard would otherwise count as buttons 1-4)
        from sleepradiopi.io.announce import Clip as _Clip
        from sleepradiopi.io.seek import KnobSeek
        station.seeker = KnobSeek(station, control, _Clip)   # (the page's knob uses it too)
        knob = Knob(control.knob, control.toggle,
                    devices=Path(os.environ.get("SLEEPRADIOPI_INPUT_DIR", "/dev/input")),
                    on_long_press=presets.knob_long, buttons=buttons, chord=chord, raw_keys=raw_keys,
                    on_held_turn=station.seeker.turn)
        presets.keys = knob.keys         # the page's buttons go down and up through the same timers
        if cathedral:                    # the VU needle and the grille's glow (hardware PWM)
            from sleepradiopi.io.lamps import Lamps
            lamps = Lamps(speaker, control, bank=lambda: presets.bank, glow_day=settings.glow_day,
                          glow_night=settings.glow_night, meter_trim_db=settings.meter_trim_db)
            lamps.start()
        knob.start()
    else:
        station = Station(cfg, tts, stream)
        control = None
        cathedral = settings.hardware == "cathedral"
        presets = presets_mod.Presets(station, None, args.config, settings.buttons, None,
                                      settings.buttons_night, settings.buttons_bank, settings.buttons_auto,
                                      count=presets_mod.MAX_N if cathedral else presets_mod.N, selector=cathedral)
        presets.keep_auto()
    # Programmes: running orders the radio plays by itself (and may start at a set time).
    from sleepradiopi.broadcast.programmes import Scheduler
    def play_jingle(name: str) -> None:        # a programme's jingle, over whatever's on
        if control is None:
            return
        path = (cfg["jingles_folder"] / name).resolve()
        if cfg["jingles_folder"].resolve() not in path.parents or not path.is_file():
            logging.getLogger(__name__).warning("programme: no jingle %s", name)
            return
        def run():
            import numpy as np
            from sleepradiopi.audio import pcm
            from sleepradiopi.io.announce import Clip
            audio = np.concatenate(list(pcm.decode(path)) or [pcm.silence(0.1)])
            control.play_clip(Clip(audio, "jingle", path.stem))
        threading.Thread(target=run, name="programme-jingle", daemon=True).start()
    presets.jingle = play_jingle              # (a jingle on a button, too)
    def time_signal(at, pips: bool, speak: bool) -> None:
        """A programme's time signal for [at]: the pips (five short, the long one on
        the minute) and/or the time said. Made ahead (the voice is slow on a Zero),
        then played on the dot; given up if it's more than half a minute late."""
        if control is None:
            return
        def run():
            import datetime as dt
            import numpy as np
            from sleepradiopi.audio import pcm
            from sleepradiopi.io.announce import Clip, pips as pips_audio
            parts = []
            if pips:
                parts.append(pips_audio())
            if speak and station._has_voice:
                try:
                    if pips:
                        parts.append(pcm.silence(0.3))
                    parts.append(station.render_speech(station.builder.time_line(at.time())))
                except Exception:
                    logging.getLogger(__name__).exception("couldn't make the time")
            if not parts:
                return
            start = at - dt.timedelta(seconds=5) if pips else at
            wait = (start - dt.datetime.now()).total_seconds()
            if wait < -30:
                logging.getLogger(__name__).warning("time signal for %s too late: skipped", at.strftime("%H:%M"))
                return
            if wait > 0:
                time.sleep(wait)
            control.play_clip(Clip(np.concatenate(parts), "time", "The time"))
        threading.Thread(target=run, name="time-signal", daemon=True).start()
    scheduler = Scheduler(station, wake=control.play if control else None,
                          sleep=(lambda: control.set_sleep(1)) if control else None,
                          pause=control.pause if control else None,
                          say=lambda text: _say_now(station, control, text),
                          jingle=play_jingle, action=presets.scheduled)
    try:
        scheduler.set_programmes(settings.programmes)
    except ValueError as e:              # a hand-edited config: don't stop the station
        logging.getLogger(__name__).warning("programmes ignored: %s", e)
    scheduler.on_change = lambda progs: save_setting(args.config, "programmes", progs)
    scheduler.quiet = settings.programme_mode
    scheduler.time_signal = time_signal
    if control is not None:
        scheduler.paused = lambda: control.paused
    station.scheduler = scheduler
    scheduler.start()
    # Podcasts: the shows followed are settings; their episode lists refresh in the background.
    station.podcasts.save = lambda shows: save_setting(args.config, "podcasts", shows)
    station.podcasts.keep_fresh()
    # A station or album playing instead of the show is kept over a restart.
    station.on_source = lambda source: save_setting(args.config, "stream_source", source)
    if control is not None and not settings.programme_mode:
        # a bedside radio plays as soon as it's powered (in programme mode: quiet until one's on):
        # what's been chosen for switch-on, if anything, else what was on last
        presets.power_on()
        control.play()
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
                          "audiobooks": cfg["audiobooks_folder"], "ondemand": cfg["ondemand_folder"]},
                         on_changed=station.reload_library,
                         tag_log=Path.home() / ".local" / "state" / "sleepradiopi" / "original-tags.json")
    # Every station has its jingles folder: made a minute after start-up (not to slow it), and
    # whenever the themes change
    def folders(delay=0.0):
        def run():
            time.sleep(delay)
            jingle_folders(media, station)
        threading.Thread(target=run, name="jingle-folders", daemon=True).start()
    ensure_default_theme(station, args.config, media, presets)   # (always there: Default)
    presets.pin_shows("Default")          # (buttons set to the old main show: Default)
    retire_artist_radio(station, args.config, presets, settings)
    station.on_profiles = folders
    folders(60.0)
    serve(station, stream, args.port or settings.http_port, control, args.config, announcer, jobs, updates,
          presets, directory, media, lamps)


if __name__ == "__main__":
    main()
