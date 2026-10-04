"""The Broadcast Radio station: one continuous show, rendered in real time.

A port of the Broadcast half of SleepRadio's PlaybackConnection.kt. The
show runs on one producer thread that writes 16-bit stereo PCM to an
Output (the MP3 web stream today; the HiFiBerry via ALSA later), and the
Output paces it to real time.

What happens in the gap after each track is planned when the track starts
(ShowClock decides link / ident / time check; jingles every N tracks), and
its speech is synthesised then, while the track plays -- the personal voice
is slower than realtime on a Pi Zero, so nothing is made on demand if it
can be helped. The two things that depend on *when* they're heard are made
just in time instead:

  * the time check is worded PREFETCH_S before the track ends, from the
    real end time (the stream is realtime with no pause, so that projection
    is exact -- the fix the Android app needed JIT wording for). A skip ends
    the track early, so it words the gap again from the real time then;
  * a news bulletin is fetched and synthesised up to 15 minutes ahead, but
    only read if its :00/:30 window is open when the gap actually comes,
    with its time line ("It's just gone ten o'clock") worded at prefetch.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections import Counter, deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from collections.abc import Callable
from typing import Protocol

import numpy as np

from sleepradiopi.audio import pcm
from sleepradiopi.config.clock import clock_trusted
from sleepradiopi.playback import radio as radio_mod
from sleepradiopi.playback.audiobooks import KEPT, BookLibrary, KeptLibrary, Positions
from sleepradiopi.playback import podcasts as pod_mod
from sleepradiopi.tts.worker import TtsWorker

from .library import scan_jingles, scan_music
from .models import BroadcastConfig, BroadcastTrack, Chattiness, JingleClip, LinkKind
from .news import DueNews, NewsRepository, NewsSlot, NewsSchedule, QuietHours, build_bulletin_body, bulletin_time_line
from . import playlists as playlists_mod
from . import profiles as profiles_mod
from .birthdays import BirthdayWishes, wish_text
from .messages import Messages
from .script_builder import DjScriptBuilder, ShowClock, artist_station_name
from .selector import BroadcastSelector, HookPool, parse_hooks

log = logging.getLogger(__name__)

LOOKAHEAD = 3             # tracks picked (and loudness-scanned) ahead
PREFETCH_S = 75.0         # word the time check / news time line this long before a track ends
                          # (from the real end time; long enough to wake the voice, ~17 s, and say it)
                          # (room for a TTS worker recycle, ~15 s, to finish first)
STARTUP_JINGLE_MAX_S = 20.0   # the opening ident: longer ones (most jingles) wait for the first gap,
                              # so the first song isn't held back after a slow start-up
SPEECH_PAD_S = 0.25       # breath of silence either side of the DJ
SPEECH_WAIT_S = 45.0      # give up on a line that still isn't synthesised after this
SEEK_BOOK_MS = 30_000     # the knob's press-and-turn: a click in a book or podcast
SEEK_SONG_MS = 10_000     # ...and in a song (an album's, a playlist's)
SOURCE_SCAN_WAIT_S = 1.5  # an album or playlist starts at once: its first song needn't wait to be measured
FILL_JINGLE_MAX_S = 40.0  # the DJ isn't ready (power-on, back to the show, a gap): one of the
                          # jingles this short instead of a wait, then the music, as a real station
                          # would (the user's shortest are ~31-38 s; the rest 55 s+)
GAP_GRACE_S = 2.0         # in a gap, a line not made by the song's end gets this long, then it's skipped


def _tick() -> np.ndarray:
    """A soft, short "tock" (like a clock), int16 stereo, well below the music."""
    t = np.arange(int(0.04 * pcm.SAMPLE_RATE)) / pcm.SAMPLE_RATE
    wave = np.sin(2 * np.pi * 1100 * t) * 0.6 + np.sin(2 * np.pi * 550 * t) * 0.4
    x = (0.12 * 32767 * wave * np.exp(-t * 120)).astype(np.int16)
    return np.repeat(x[:, None], pcm.CHANNELS, axis=1)
PER_LINE_ESTIMATE_S = 4.0  # rough length of a spoken line, for wording a clock after one
SPEED_MIN, SPEED_MAX = 0.5, 1.5   # DJ / news speech speed (x the voice's own pace), from the page
RADIO_PREBUFFER_S = 1.0   # internet radio: audio in hand before it plays (and after a stall)
RADIO_GIVE_UP_S = 45.0    # no sound from a station this long: back to the show (Wi-Fi can be slow at boot)
RADIO_RETRY_S = 3.0       # wait between reconnects
BOOK_SAVE_S = 30.0        # an audiobook's place is saved this often while it plays (and on pause)
BOOK_BACK_SLEEP_MS = 60_000   # resuming after the sleep timer: a minute back (you'd dozed off)
BOOK_BACK_PAUSE_MS = 5_000    # ...after an ordinary pause: a few seconds
BOOK_SEEK_MS = 60_000     # the page's rewind / fast-forward


class Output(Protocol):
    def start(self) -> None: ...
    def write(self, block: np.ndarray) -> None: ...
    def stop(self) -> None: ...


@dataclass
class Speech:
    text: str
    voice: str
    future: Future


@dataclass
class ClockStep:
    speech: Speech | None = None

    def discard(self) -> None:
        if self.speech is not None:
            self.speech.future.cancel()  # a no-op if the synth has already started
            self.speech = None


@dataclass
class NewsItem:
    due: DueNews
    headlines: list[str]
    body: Speech
    time_line: Speech | None = None


@dataclass
class Step:
    kind: str  # "say" | "clock" | "jingle" | "news"
    speech: Speech | None = None
    clock: ClockStep | None = None
    jingle: JingleClip | None = None
    news: NewsItem | None = None
    message: tuple | None = None   # a message's Messages.played() record: due again if it isn't said

    def describe(self) -> str:
        if self.kind == "say":
            return f'say("{self.speech.text}")'
        return self.kind


@dataclass
class OnAir:
    kind: str       # "track" | "dj" | "jingle" | "news" | "wait" | "radio"
    title: str
    artist: str = ""
    album: str = ""
    started: float = field(default_factory=time.time)
    duration_s: float = 0.0


PREVIOUS_RESTART_S = 5          # Previous later than this into a track restarts it

def _size(path: Path) -> int:
    """A file's size in bytes (0 if it has gone)."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _natural(name: str) -> list:
    """'10 - x' after '9 - x': digits compare as numbers."""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", name)]


def artist_key(artist: str) -> str:
    """"The Beatles", "Beatles", "beatles " -> "beatles"."""
    key = " ".join(artist.lower().split())
    return key[4:] if key.startswith("the ") and len(key) > 4 else key


def _album_names(tracks: list[BroadcastTrack], rel: str, root: str, deep: bool = False) -> dict:
    """An album's title and artist: from the tags where most tracks agree, else
    the folder. On demand has no set layout, so there it's the folder's own
    name, and its artist only if the tags give one."""
    parts = [p for p in rel.split("/") if p]
    name = parts[-1] if parts else ("On demand" if root == "ondemand" else "Music")
    titles = Counter(t.album.strip() for t in tracks if t.album.strip())
    artists = Counter(t.artist.strip() for t in tracks if t.artist.strip())
    artist, n = artists.most_common(1)[0] if artists else ("", 0)
    if root == "ondemand":
        tagged = [t for t in tracks if t.album.strip() and t.album.strip() != t.path.parent.name]
        title = name if deep or not tagged else titles.most_common(1)[0][0]
        folder_artist = parts[0] if len(parts) >= 2 else ""   # (read_track's guess from Artist/Album/x)
        return {"title": title, "artist": artist if artist != folder_artist and n >= 0.6 * len(tracks) else ""}
    title = name if deep else (titles.most_common(1)[0][0] if titles else name)
    return {"title": title, "artist": artist if n >= 0.6 * len(tracks) else ("Various artists" if artists else "")}


class Station:
    def __init__(self, cfg: dict, tts: TtsWorker | None, output: Output) -> None:
        self.music_dir: Path = cfg["music_folder"]
        self.jingles_dir: Path = cfg["jingles_folder"]
        self.dj_voice: str | None = cfg["broadcast_voice"]
        news_voice = cfg["news_voice"]
        self.news_voice: str = self.dj_voice if news_voice in (None, "same") else news_voice
        self.announcer_volume: float = cfg["broadcast_announcer_volume"]
        self.grace_s: float = cfg["listener_grace_s"]
        chattiness = Chattiness.from_id(cfg["broadcast_chattiness"])
        self.config = BroadcastConfig(
            tracks_per_link=chattiness.tracks_per_link,
            announce_every_track=chattiness == Chattiness.MAXIMUM,
            announcer_speed=cfg["broadcast_announcer_speed"],
            news_speed=cfg["news_speed"],
            jingle_every=cfg["broadcast_jingle_every"] if cfg["broadcast_jingle_enabled"] else 0,
            dj_hooks_enabled=cfg["broadcast_dj_hooks"],
            news_enabled=cfg["news_enabled"],
            news_quiet_hours=cfg["news_quiet_hours"],
            news_quiet_start_min=cfg["news_quiet_start_min"],
            news_quiet_end_min=cfg["news_quiet_end_min"],
        )
        self.tts = tts
        self.output = output
        self.scans = pcm.ScanCache(cfg["scan_cache"])

        self.chattiness = chattiness.ident
        self.dj_on = bool(cfg.get("broadcast_dj", True))   # off: music only -- no speech at all (links, time, news, messages)
        self.time_checks = bool(cfg.get("broadcast_time_checks", True))
        self.voices_dir: Path | None = cfg.get("voices_dir")
        self._hook_pool = None                # loaded even when off, so they can be turned on
        if cfg["hooks_file"] and Path(cfg["hooks_file"]).is_file():
            self._hook_pool = HookPool(parse_hooks(Path(cfg["hooks_file"]).read_text()))
        self.builder = DjScriptBuilder(hooks=self._hook_pool if self.config.dj_hooks_enabled else None)
        self.news_schedule = NewsSchedule(
            QuietHours.of_minutes(self.config.news_quiet_start_min, self.config.news_quiet_end_min)
            if self.config.news_quiet_hours else None)
        self.news_repo = NewsRepository()
        quiet = (QuietHours.of_minutes(self.config.news_quiet_start_min, self.config.news_quiet_end_min)
                 if self.config.news_quiet_hours else None)
        try:
            self.birthdays = BirthdayWishes(cfg.get("birthdays") or [], quiet)
        except ValueError as e:              # a hand-edited config: don't stop the station
            log.warning("birthdays ignored: %s", e)
            self.birthdays = BirthdayWishes([], quiet)
        try:
            self.messages = Messages(cfg.get("messages"))
        except ValueError as e:
            log.warning("messages ignored: %s", e)
            self.messages = Messages()

        self._tag_cache = cfg.get("tag_cache")
        self._renamed_artists: set[str] = set()   # (artists whose tags were just changed: follow_artist)
        self.tracks = scan_music(self.music_dir, self._tag_cache)
        # On demand: anything played only when asked for (storms, radio shows, long
        # classical pieces...), any layout. Never in the show, the song search or lists.
        self.ondemand_dir: Path | None = cfg.get("ondemand_folder")
        self.ondemand_tracks = self._scan_ondemand()
        try:
            self.playlists: list[dict] = playlists_mod.validate(cfg.get("playlists") or [])
        except ValueError as e:              # a hand-edited config: don't stop the station
            log.warning("playlists ignored: %s", e)
            self.playlists = []
        self._path_index: dict | None = None   # (root, path in it) -> track, for playlists
        self.selector = BroadcastSelector(self.tracks)
        self.artist: str | None = None      # artist radio: only this artist's tracks
        self.profile: str | None = None     # ...or only the artists on this list
        try:
            self.profiles: list[dict] = profiles_mod.validate(cfg.get("profiles") or [])
        except ValueError as e:              # a hand-edited config: don't stop the station
            log.warning("profiles ignored: %s", e)
            self.profiles = []
        # Each station has its own jingles: Jingles/<theme>, or Jingles/<the radio's name> for the
        # main show (and artist radio); loaded for the station that's playing (_load_jingles).
        self.jingles: list[JingleClip] = []
        self._jingles_from: Path | None = None
        self._default_jingles: list[JingleClip] = []    # Default's: start-up and fill-ins when the station has none
        self._jingle_paths: set[Path] = set()
        self._jingle_bag: deque[JingleClip] = deque()
        self.on_profiles: Callable[[], None] | None = None
        self._last_jingle: JingleClip | None = None

        self._tts_pool = ThreadPoolExecutor(1, thread_name_prefix="speech")
        self._scan_pool = ThreadPoolExecutor(1, thread_name_prefix="scan")
        self._scan_futures: dict[Path, Future] = {}

        self._lock = threading.Lock()
        self._listeners = 0
        self._stop_timer: threading.Timer | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._queue: deque[BroadcastTrack] = deque()
        self._show_clock = ShowClock(self.config)
        self._tracks_since_jingle = 0
        self._news_prep_key: str | None = None
        self._news_ready: NewsItem | None = None
        self._opening: tuple[str, list[Step], BroadcastTrack] | None = None
        self._first_open = True               # the first welcome since power-on
        self._last_opening: tuple[str, list[Step], BroadcastTrack] | None = None
        self._plan: list[Step] = []
        self._gap_decision = None
        self._in_gap = False
        self._n_requested = 0                # requests at the front of the queue
        self._pending: deque[BroadcastTrack] = deque()   # requests made during a gap
        self._albums: list[dict] = []        # album runs being played start to finish
        self._album_index: list[dict] | None = None
        self._next_source: dict | None = None  # "Play next" with no DJ: tuned when the song (or album) ends
        self._played: deque[BroadcastTrack] = deque(maxlen=30)   # the show's songs, for Previous
        self._jump: BroadcastTrack | None = None   # Previous / a playlist now: play this at once, no gap
        self._album_jump: int | None = None        # Previous in an album: this track next
        self._file_seek: float | None = None       # the knob's press-and-turn: this far into the song (ms)
        self._retune = False                       # another station chosen mid-show: it opens now
        self._file_pos: tuple = (None, 0)          # the song playing, and how far into its file (ms)
        self._file_trim = 0                        # where its sound starts in the file (its scan's start)
        self._gap_is_album = False           # this gap is between two tracks of an album
        self.current_track: BroadcastTrack | None = None
        self._took_request = False
        self._opening_requested = False      # the prepared opening's first song was asked for
        self._skip_for: OnAir | None = None

        self.on_air: OnAir | None = None
        # Streaming: a source played instead of the show -- an internet radio
        # station {"kind": "radio", "name", "url"} or one of the library's albums
        # straight through {"kind": "album", "folder", "title", "artist", "track"}.
        self._source: dict | None = None
        self._switch = threading.Event()     # the source changed: whatever plays gives way
        self._radio_heard = False            # a station has made a sound since start-up
        self._in_music = False               # the music show is playing (not a station, album or book)
        self.radio_title: str | None = None  # the station's now-playing, if it sends one
        self.radio_stream = None             # the open stream, while a station plays (a receiver's can be retuned)
        self.monitor = None                  # playback/monitor.Monitor, if main.py gave it one
        self.radio_playing = False           # its sound is on air (not tuning in / reconnecting)
        self.source_error: str | None = None # why the last source stopped
        # Called with the source (or None) when it changes, so it can be saved
        # and resumed after a restart (an album at the track it was on).
        self.on_source: Callable[[dict | None], None] | None = None
        # Audiobooks: their own folder; each book's place kept on the writable storage.
        self.books = BookLibrary(cfg.get("audiobooks_folder") or Path("/nonexistent"), cfg.get("book_cache"))
        self.book_positions = Positions(cfg.get("book_positions"))
        # ...and the On demand things set to remember their place: played as books (their keys start with KEPT)
        cache = cfg.get("book_cache")
        self.kept = KeptLibrary(cfg.get("ondemand_folder") or Path("/nonexistent"),
                                Path(cache).with_name("kept.json") if cache else None, cfg.get("ondemand_keep") or [])
        self.books_scanning = True
        threading.Thread(target=self._scan_books, name="books-scan", daemon=True).start()
        self._book_seek: int | None = None
        self.book_now: dict | None = None    # the book playing: place, chapter, length
        self.paused_by_sleep: Callable[[], bool] = lambda: False   # (wired to the speaker)
        self.speaker_paused: Callable[[], bool] = lambda: False    # (likewise: the radio is paused)
        self.on_book_end: Callable[[], None] | None = None          # pause the radio at the end (a book, the
                                                                    # last podcast, something played from the desktop)
        # Podcasts: the shows (settings), their episode lists (cached), each episode's place.
        self.podcasts = pod_mod.Podcasts(cfg.get("podcasts") or [], cfg.get("podcast_cache"), self.book_positions)
        if cfg.get("stream_source"):
            try:
                self._source = self._check_source(cfg["stream_source"])
            except ValueError as e:
                log.warning("stream_source ignored: %s", e)
        self.next_track: BroadcastTrack | None = None
        self.gap_plan: list[str] = []
        self.history: deque[dict] = deque(maxlen=12)
        self.shows_started = 0
        # The DJ's radio-wide settings, kept apart from what's in force: a theme can set its own
        # (profiles.THEME_SETTINGS), and anything it doesn't set is these (_apply_settings).
        self.base = {"chattiness": self.chattiness, "time_checks": self.time_checks,
                     "news": self.config.news_enabled, "jingle_every": self.config.jingle_every,
                     "dj_hooks": self.config.dj_hooks_enabled}
        self.profiles = self.complete_settings(self.profiles)   # (each theme its own full set)
        if cfg.get("broadcast_profile"):
            self._use_selection(profile=cfg["broadcast_profile"])
        elif self.profiles:                   # (an old broadcast_artist: main makes it a theme)
            self._use_selection()             # (no theme chosen: Default)
        self._load_jingles()
        self._prepare_opening()

    # --- listeners / show lifecycle ---------------------------------------------------

    def listener_joined(self) -> None:
        with self._lock:
            self._listeners += 1
            if self._stop_timer:
                self._stop_timer.cancel()
                self._stop_timer = None
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run_show, name="show", daemon=True)
                self._thread.start()

    def listener_left(self) -> None:
        with self._lock:
            self._listeners = max(0, self._listeners - 1)
            if self._listeners == 0 and self._stop_timer is None:
                self._stop_timer = threading.Timer(self.grace_s, self._end_show)
                self._stop_timer.daemon = True
                self._stop_timer.start()

    def _end_show(self) -> None:
        with self._lock:
            self._stop_timer = None
            if self._listeners == 0:
                log.info("no listeners for %.0fs: ending the show", self.grace_s)
                self._stop.set()

    @property
    def is_on_air(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def skip(self) -> bool:
        """Cut short whatever is on air (a track, jingle, DJ line or bulletin) and
        go straight on to what comes after it. False if there's nothing to skip."""
        on_air = self.on_air
        if not self.is_on_air or on_air is None or on_air.kind in ("radio", "book", "episode"):
            return False
        self._skip_for = on_air
        log.info("skip: %s %s", on_air.kind, on_air.title[:70])
        return True

    def _skipped(self, on_air: OnAir) -> bool:
        return self._skip_for is on_air

    def _halted(self) -> bool:
        """Stop what's playing: the show is ending, or the source changed."""
        return self._stop.is_set() or self._switch.is_set()

    def _run_show(self) -> None:
        self.shows_started += 1
        self.output.start()
        back = False                      # back to the show from a station, album, book or podcast
        try:
            while not self._stop.is_set():
                self._switch.clear()
                source = self._source
                if source is None:
                    back, self._retune = back or self._retune, False
                    self._run_music(back)
                    back = False
                elif source["kind"] in ("album", "playlist"):
                    self._run_album(source)
                elif source["kind"] == "book":
                    self._run_book(source)
                elif source["kind"] == "episode":
                    self._run_episode(source)
                else:
                    self._run_radio(source)
                if source is not None:
                    back = True
        except Exception:
            log.exception("show crashed")
        finally:
            self.output.stop()
            self.on_air = None
            self.gap_plan = []
            log.info("show ended")
            if self._opening is None:
                self._prepare_opening()

    def _run_music(self, back: bool = False) -> None:
        """The Broadcast show, until it ends or the source changes."""
        self._in_music = True
        try:
            self._run_music_show(back)
        finally:
            self._in_music = False

    def _opening_steps(self, steps: list[Step], why: str) -> list[Step]:
        """The welcome, at power-on or back to the show (from a station, an album...),
        is often not made yet: on a Zero the DJ's voice has to load (~20 s), then
        speak it (30-60 s), and waiting sounded broken. So if it isn't ready: one of
        the short jingles (if jingles are on) and straight into the music, as a real
        station would; the DJ joins at the next gap. A welcome that is ready is said
        as usual. The jingle follows the jingles on/off setting only -- on a theme or
        artist radio too (the user's choice)."""
        ready = all(s.speech.future.done() for s in steps if s.kind == "say")
        power_on = self._start_up_jingle() if why == "power-on" else None
        if power_on is not None:              # (the station's own, else a loose one in the jingles folder)
            log.info("power-on: %s%s", power_on.path.name, ", then the welcome" if ready else "; the welcome isn't ready")
            if not ready:
                for step in steps:
                    if step.kind == "say":
                        step.speech.future.cancel()
            return [Step("jingle", jingle=power_on)] + (steps if ready else [])
        if ready:
            return steps
        for step in steps:
            if step.kind == "say":
                step.speech.future.cancel()   # (if it hasn't started: the Zero's CPU is the music's now)
        clip = self._fill_jingle()
        log.info("%s: the welcome isn't ready; %s", why, "a short jingle, then the music" if clip else "straight to the music")
        return [Step("jingle", jingle=clip)] if clip else []

    def _fill_jingle(self) -> JingleClip | None:
        """A jingle to play while the DJ isn't ready: one of the station's own (when
        jingles are on; a short one if it has any), else one of Default's (likewise).
        None: a gap it is (no jingles to be had)."""
        if self.config.jingle_every <= 0:     # (jingles off on this station: none at all)
            return None
        own = self.jingles
        short = lambda js: [j for j in js if 0 < j.duration_s <= FILL_JINGLE_MAX_S]
        # (its own before Default's, even a long one: Dylan's 58 s jingle lost to Default's short ones)
        pick = short(own) or own or short(self._default_jingles) or self._default_jingles
        return random.choice(pick) if pick else None

    def _run_music_show(self, back: bool = False) -> None:
        self._show_clock.reset()
        self._tracks_since_jingle = 0
        if not self.tracks:
            log.error("no music in %s: nothing to broadcast", self.music_dir)
            while not self._halted():
                self._write(pcm.silence(0.5))
            return
        opened = False
        try:
            steps, first = self._take_opening()
            opening = self._last_opening
            if back or self._first_open:      # (power-on: the start-up sound has chimed)
                steps = self._opening_steps(steps, "back to the show" if back else "power-on")
            self._first_open = False
            self._run_steps(steps)
            track = first
            with self._lock:
                jump, self._jump = self._jump, None
                if jump is not None:          # (a playlist started during the welcome)
                    self._queue.insert(self._n_requested, first)
                    track = jump
            while not self._halted():
                opened = True
                self._play_track(track)
                if self._halted() or self._take_next_source():
                    break
                with self._lock:
                    jump, self._jump = self._jump, None
                if jump is not None:          # Previous (or a playlist started): straight to it
                    track = jump
                    continue
                with self._lock:
                    self._in_gap = True       # a request now plays after the announced next song
                self._run_gap()
                with self._lock:
                    jump, self._jump = self._jump, None
                    track = jump or self._take_next()
                    self._in_gap = False
                    self._place_pending()
        finally:
            if self._switch.is_set() and not self._stop.is_set():
                with self._lock:
                    self._in_gap = False
                    self._place_pending()
                self.on_air = None
                self.gap_plan = []
                self.next_track = None
                if not opened and not self._retune:   # its lines may be half made (slow on a Zero): keep them
                    self._opening = opening
                else:
                    self._prepare_opening()   # ready for coming back from the station

    # --- streaming: internet radio and albums ---------------------------------------------

    def _check_source(self, source: dict) -> dict:
        """A clean copy of a source; ValueError if it isn't one (or the album isn't here)."""
        if not isinstance(source, dict):
            raise ValueError("a source is a station or an album")
        if source.get("kind", "radio") == "radio":
            return {"kind": "radio", **radio_mod.validate_station(source)}
        if source["kind"] == "album":
            root, deep = source.get("root") or "music", source.get("deep") is True
            album = self._album_by_folder(source.get("folder"), root, deep)
            if album is None:
                raise ValueError("that isn't in the library")
            track = source.get("track", 0)
            track = track if isinstance(track, int) and not isinstance(track, bool) else 0
            out = {"kind": "album", "folder": album["folder"], "title": album["title"],
                   "artist": album["artist"], "track": max(0, min(track, len(album["tracks"]) - 1))}
            if root != "music":
                out["root"] = root
            if deep:
                out["deep"] = True
            if source.get("one") is True:     # just that one track
                out["one"] = True
                out["title"] = album["tracks"][out["track"]].title
            if source.get("then") == "pause":
                out["then"] = "pause"
            if isinstance(source.get("offset_ms"), int) and source["offset_ms"] > 0:
                out["offset_ms"] = source["offset_ms"]
            return out
        if source["kind"] == "playlist":
            refs = source.get("refs")
            refs = [list(r) for r in refs if isinstance(r, (list, tuple)) and len(r) == 2
                    and all(isinstance(x, str) for x in r)] if isinstance(refs, list) else []
            refs = [r for r in refs if self._by_ref(*r) is not None]
            if not refs:
                raise ValueError("that playlist has nothing on the radio to play")
            name = source.get("name") if isinstance(source.get("name"), str) else "Playlist"
            track = source.get("track", 0)
            track = track if isinstance(track, int) and not isinstance(track, bool) else 0
            out = {"kind": "playlist", "name": name, "title": name, "artist": "", "refs": refs,
                   "shuffle": source.get("shuffle") is True, "track": max(0, min(track, len(refs) - 1))}
            if source.get("then") == "pause":
                out["then"] = "pause"
            if isinstance(source.get("offset_ms"), int) and source["offset_ms"] > 0:
                out["offset_ms"] = source["offset_ms"]
            return out
        if source["kind"] == "episode":
            show = self.podcasts.show(source.get("show")) if isinstance(source.get("show"), str) else None
            eps = self.podcasts.episodes(show["id"]) if show else []
            ep = next((e for e in eps if e["guid"] == source.get("guid")), None)
            if ep is None:
                raise ValueError("that episode isn't in a podcast you follow")
            return {"kind": "episode", "show": show["id"], "show_title": show["title"], "guid": ep["guid"],
                    "title": ep["title"], "url": ep["url"], "type": ep["type"], "bytes": ep["bytes"],
                    "duration_ms": ep["duration_ms"]}
        if source["kind"] == "book":
            key = source.get("key")
            book = self._book(key) if isinstance(key, str) else None
            at = (self.kept.root / key[len(KEPT):]) if isinstance(key, str) and key.startswith(KEPT) else \
                (self.books.root / key) if isinstance(key, str) and key else None
            if book is None and not (at is not None and ".." not in key.split("/") and at.exists()):
                raise ValueError("that book isn't in the audiobooks folder")
            return {"kind": "book", "key": key, "title": book.title if book else Path(key).stem,
                    "author": book.author if book else ""}
        raise ValueError(f"unknown kind of source {source.get('kind')!r}")

    def tune(self, source: dict | None) -> None:
        """Play a source instead of the show -- an internet radio station
        ({"name", "url"}, kind "radio" by default) or an album straight through
        ({"kind": "album", "folder"}) -- or None to go back to the show. Heard at
        once if the radio is playing. ValueError if it isn't one."""
        if source is not None and source.get("kind") == "album" and source.get("root") == "ondemand" \
                and KEPT + str(source.get("folder")) in {KEPT + p for p in self.kept.paths} \
                and self.kept.get(KEPT + source["folder"]) is not None and not source.get("one") and not source.get("track"):
            source = {"kind": "book", "key": KEPT + source["folder"]}        # it remembers its place: as a book
        source = self._check_source(source) if source is not None else None
        with self._lock:
            self._source = source
            self.source_error = None
            self._switch.set()
        log.info("streaming: %s", "back to the show" if source is None else
                 f"{source['kind']} {source.get('name') or source['title']}")
        self._notify_source()

    def album_source(self, album_id=None, root="music", folder=None, deep=False, track=0, one=False) -> dict:
        """A source for an album: an id from the search or list, or a folder;
        from `track` on (or, with `one`, only that track)."""
        if album_id is not None:
            albums = self.albums()
            if isinstance(album_id, bool) or not isinstance(album_id, int) or not 0 <= album_id < len(albums):
                raise ValueError("no such album")
            a = albums[album_id]
            root, folder, deep = a["root"], a["folder"], False
        return self._check_source({"kind": "album", "root": root, "folder": folder, "deep": deep is True,
                                    "track": track, "one": one is True})

    def play_album(self, album_id=None, root="music", folder=None, deep=False, track=0,
                   shuffle: bool = False, then: str | None = None) -> dict:
        """Play an album or folder straight through instead of the show (from
        `track` on): no DJ, jingles or news. Back to the show when it ends --
        or, then="pause", the radio pauses. shuffle (an artist's folder, say):
        its songs in a random order, once each."""
        src = self.album_source(album_id, root, folder, deep, track)
        if shuffle:
            album = self._source_album(src)
            tracks = list(album["tracks"])
            random.shuffle(tracks)
            self.play_tracks_as(src["title"], tracks, True, then)
            return self._source
        key = self.kept.key_for(src["folder"]) if src.get("root") == "ondemand" else None
        if key is not None:                     # it remembers its place (or is in a folder that does): as a book
            whole = key == KEPT + src["folder"] and not src.get("track")
            file = None if whole else self._source_album(src)["tracks"][src.get("track") or 0].path
            if self._kept_play(key, file) is not None:
                return self._source
        self._next_source = None
        self.tune({**src, **({"then": "pause"} if then == "pause" else {})})
        return self._source

    def play_track(self, root: str, path: str, then: str | None = None) -> dict:
        """One track now. From the music: in the show at once (whatever's on),
        then the show carries on, the DJ as set. From On demand: that track on
        its own, no DJ, then the show. then="pause" (dropped on the desktop's
        radio): that track on its own, no DJ, then the radio pauses."""
        t = self._by_ref(root, path) if isinstance(path, str) else None
        if t is None:
            raise ValueError("that track isn't in the library")
        key = self.kept.key_for(path) if root == "ondemand" else None
        if key is not None and self._kept_play(key, t.path) is not None:      # it remembers its place: as a book
            log.info("play now: %s (from where it was left)", t.title)
            return {"title": t.title, "artist": t.artist}
        if then == "pause":                     # (from the desktop: that song on its own, then quiet)
            self.play_tracks_as(t.title, [t], False, "pause")
        elif root == "ondemand":
            folder = path.rsplit("/", 1)[0] if "/" in path else ""
            album = self._album_by_folder(folder, "ondemand")
            self._next_source = None
            self.tune(self.album_source(root="ondemand", folder=folder, track=album["tracks"].index(t), one=True))
        else:
            self._play_now([t])
        log.info("play now: %s", t.title)
        return {"title": t.title, "artist": t.artist}

    def play_next(self, album_id=None, root="music", folder=None, deep=False, track=0) -> dict:
        """Play next: music albums go into the show, introduced by the DJ; On
        demand plays with no DJ once the song (or the album) playing ends."""
        src = self.album_source(album_id, root, folder, deep)
        if src.get("root", "music") == "music":
            album = self._album_by_folder(src["folder"], "music", src.get("deep", False))
            return {**self._request_album(album), "dj": True}
        playing = self._source
        if not self.is_on_air or (playing is not None and playing["kind"] in ("radio", "book", "episode")):
            self.tune(src)                       # nothing to wait for: it plays (when the radio's on)
            return {"title": src["title"], "artist": src["artist"], "now": True, "dj": False}
        with self._lock:
            self._next_source = src
        log.info("up next (no DJ): %s", src["title"])
        return {"title": src["title"], "artist": src["artist"], "now": False, "dj": False}

    def _take_next_source(self) -> bool:
        """(show thread) a song or album has ended: a queued Play next takes over."""
        with self._lock:
            src, self._next_source = self._next_source, None
            if src is None:
                return False
            self._source = src
            self.source_error = None
            self._switch.set()
        log.info("streaming: album %s (played next)", src["title"])
        self._notify_source()
        return True

    def next_source_status(self) -> dict | None:
        src = self._next_source
        return None if src is None else {"title": src["title"], "artist": src["artist"]}

    @property
    def source(self) -> dict | None:
        return self._source

    def _notify_source(self) -> None:
        if self.on_source is not None:
            try:
                self.on_source(dict(self._source) if self._source else None)
            except Exception:
                log.exception("couldn't save the source")

    @property
    def music_started(self) -> bool:
        """Something (a song, or a station) has played since start-up."""
        return self.current_track is not None or self._radio_heard

    def _give_up_radio(self, tuned: dict, why: str) -> None:
        with self._lock:
            if self._source is tuned:
                self._source = None
                self.source_error = f"{tuned['name']}: {why}"
        log.warning("radio: giving up on %s (%s): back to the show", tuned["name"], why)
        # (still saved: after a restart, e.g. with the Wi-Fi back, it's tried again)

    def _album_by_folder(self, folder, root="music", deep=False) -> dict | None:
        """The album in a folder; with deep, everything under the folder (e.g. a
        box set's CD1, CD2 or a series' seasons) as one, in path order."""
        if not isinstance(folder, str) or root not in ("music", "ondemand"):
            return None
        if not deep:
            return next((a for a in self.albums() if a["root"] == root and a["folder"] == folder), None)
        prefix = folder + "/" if folder else ""
        parts = [a for a in self.albums() if a["root"] == root and (a["folder"] == folder or a["folder"].startswith(prefix))]
        if not parts:
            return None
        parts.sort(key=lambda a: [_natural(p) for p in a["folder"].split("/")])
        tracks = [t for a in parts for t in a["tracks"]]
        return {"root": root, "folder": folder, "deep": True, "tracks": tracks,
                **_album_names(tracks, folder, root, deep=True)}

    def _run_album(self, source: dict) -> None:
        """Play an album (or a playlist) from source["track"] to the end, like a
        record: no DJ, jingles or news; Skip goes to the next track. Then back to
        the show."""
        album = self._source_album(source)
        if album is None:                       # (checked when tuned; gone after a library change)
            with self._lock:
                if self._source is source:
                    self._source = None
            return
        tracks = album["tracks"]
        self.gap_plan = []
        self.history.appendleft({"kind": "album", "text": f"{album['title']} — {album['artist']}",
                                 "at": time.time()})
        i = source.get("track", 0)
        self._album_jump = None
        while i < len(tracks):
            if self._halted():
                return
            t = tracks[i]
            if i + 1 < len(tracks):
                self._scan(tracks[i + 1].path)      # measured while this one plays
            self.next_track = tracks[i + 1] if i + 1 < len(tracks) else None
            with self._lock:
                if self._source is not source:
                    return
                source["track"] = i
            self._notify_source()                   # resumes at this track after a restart
            self.current_track = t
            self.history.appendleft({"kind": "track", "text": f"{t.title} — {t.artist}", "at": time.time()})
            log.info("%s %s, track %d: %s by %s", source["kind"], album["title"], i + 1, t.title, t.artist)
            self._play_file(t.path, OnAir("track", t.title, t.artist, t.album), scan_wait_s=SOURCE_SCAN_WAIT_S,
                            start_ms=int(source.pop("offset_ms", 0) or 0), keep_place=True)
            if self._halted():                   # (paused long enough that the show ended: keep the place)
                with self._lock:
                    if self._source is source and self._file_pos[0] == t.path:
                        source["offset_ms"] = int(self._file_pos[1])
                self._notify_source()
                return
            jump, self._album_jump = self._album_jump, None
            if jump is None and source.get("one"):
                break
            i = i + 1 if jump is None else jump
            if source.get("stop") and jump is None:   # (a playlist stopped: this song was its last)
                break
        if self._halted():
            return
        log.info("%s finished: %s%s", source["kind"], album["title"],
                 "; pausing" if source.get("then") == "pause" else "")
        with self._lock:
            if self._source is source:
                self._source = None
        self.next_track = None
        if source.get("then") == "pause":     # (played from the desktop: done, quiet -- the knob plays the theme)
            with self._lock:
                self._next_source = None
            self._notify_source()
            if self.on_book_end is not None:
                self.on_book_end()
            return
        if not self._take_next_source():
            self._notify_source()

    def _source_album(self, source: dict) -> dict | None:
        """An album or playlist source's tracks, as {"title", "artist", "tracks"}."""
        if source["kind"] == "playlist":
            tracks = [t for r in source["refs"] if (t := self._by_ref(*r)) is not None]
            return {"title": source["name"], "artist": "", "tracks": tracks} if tracks else None
        return self._album_by_folder(source["folder"], source.get("root", "music"), source.get("deep", False))

    def _run_radio(self, tuned: dict) -> None:
        """Play a station until the source changes, the show ends, or it stays
        silent for RADIO_GIVE_UP_S (then the show plays instead)."""
        name = tuned["name"]
        on_air = self.on_air = OnAir("radio", "Tuning in…", name)
        self.next_track = None
        self.gap_plan = []
        self.radio_title = None
        self.history.appendleft({"kind": "radio", "text": name, "at": time.time()})
        leveller = radio_mod.Leveller()
        last_sound = time.monotonic()
        why = "no sound from the station"
        while not self._halted():
            stream = self.radio_stream = radio_mod.open_stream(tuned["url"])     # (the rig on the desktop tunes it: web/server.py)
            try:
                stream.start()
                playing = False
                while not self._halted():
                    if stream.title != self.radio_title and stream.title is not None:
                        self.radio_title = stream.title
                        self.history.appendleft({"kind": "radio", "text": f"{stream.title} ({name})",
                                                 "at": time.time()})
                    filling = not playing and stream.buffered_s < RADIO_PREBUFFER_S and not stream.ended
                    block = None if filling else stream.read(0.05)
                    if block is None:
                        playing = self.radio_playing = False
                        if stream.ended and not stream.buffered_s:
                            why = stream.ended
                            break
                        if time.monotonic() - last_sound > RADIO_GIVE_UP_S:
                            self._give_up_radio(tuned, why if stream.ended else "no sound from the station")
                            return
                        self._write(pcm.silence(0.05))
                        continue
                    if not playing:
                        playing = self.radio_playing = True
                        on_air.title = self.radio_title or name
                        on_air.started = time.time()
                    self._radio_heard = True
                    last_sound = time.monotonic()
                    on_air.title = self.radio_title or name
                    self._write(leveller.process(block))
            finally:
                stream.close()
                self.radio_stream = None
                self.radio_playing = False
            if self._halted():
                break
            on_air.title = "Reconnecting…"
            log.info("radio: %s: %s; reconnecting", name, why)
            deadline = time.monotonic() + RADIO_RETRY_S
            while time.monotonic() < deadline and not self._halted():
                self._write(pcm.silence(0.1))
            if time.monotonic() - last_sound > RADIO_GIVE_UP_S:
                self._give_up_radio(tuned, why)
                return
        self.radio_title = None

    # --- output -----------------------------------------------------------------------

    def _write(self, block: np.ndarray) -> None:
        monitor = self.monitor                       # (rooms heard over whatever's playing: playback/monitor.py)
        if monitor is not None:
            block = monitor.mix(block, getattr(self.radio_stream, "url", None))
        self.output.write(block)

    def _await(self, fut: Future, limit_s: float = SPEECH_WAIT_S, skip_for: OnAir | None = None):
        """Wait for background work while keeping the stream fed with silence, so a slow
        synthesis is a pause on air rather than a dropped connection. A skip of
        skip_for (what the page shows meanwhile) gives up waiting."""
        deadline = time.monotonic() + limit_s
        while not fut.done():
            if self._halted() or time.monotonic() > deadline:
                return None
            if skip_for is not None and self._skipped(skip_for):
                return None
            self._write(pcm.silence(0.1))
        try:
            return fut.result()
        except Exception as e:
            log.warning("background job failed: %s", e)
            return None

    # --- speech ----------------------------------------------------------------------

    def _say(self, text: str, voice: str | None = None, speed: float | None = None) -> Speech:
        voice = voice or self.dj_voice
        speed = speed or self.config.announcer_speed

        def job() -> np.ndarray:
            t0 = time.monotonic()
            samples, rate = self.tts.synth(voice, text, speed)
            out = pcm.speech_pcm(samples, rate, self.announcer_volume)
            log.info("synth %s %.1fs of speech in %.1fs: %s", voice, len(out) / pcm.SAMPLE_RATE,
                     time.monotonic() - t0, text[:70])
            return out

        return Speech(text, voice, self._tts_pool.submit(job))

    def news_now(self) -> np.ndarray | None:
        """The latest top stories read now (a preset button), in the news voice,
        as int16 stereo; None if there are none (e.g. offline). Blocking: a
        minute or so on a Zero."""
        if not self._has_voice:
            return None
        headlines = self.news_repo.headlines_for(NewsSlot.TOP_OF_HOUR)
        body = build_bulletin_body(NewsSlot.TOP_OF_HOUR, headlines)
        if body is None:
            return None
        samples, rate = self.tts.synth(self.news_voice, body, self.config.news_speed)
        self.news_repo.mark_read(headlines)
        self.history.appendleft({"kind": "news", "text": "The news, by request", "at": time.time()})
        return pcm.speech_pcm(samples, rate, self.announcer_volume)

    def render_speech(self, text: str) -> np.ndarray:
        """Say text in the DJ voice, as int16 stereo, now (blocking): for things
        outside the show, like the spoken address. Needs a voice."""
        samples, rate = self.tts.synth(self.dj_voice, text, self.config.announcer_speed)
        return pcm.speech_pcm(samples, rate, self.announcer_volume)

    @property
    def _has_voice(self) -> bool:
        return self.tts is not None and self.dj_voice is not None

    def _speak(self, speech: Speech, kind: str = "dj") -> bool:
        """Say a line. False only if it was skipped (or the show stopped)."""
        waiting = None
        if not speech.future.done():     # not made yet: say so on the page, and let Skip end the wait
            waiting = self.on_air = OnAir("wait", "Getting the next bit ready…")
        audio = self._await(speech.future, skip_for=waiting)
        if audio is None:
            if waiting is not None and self._skipped(waiting):
                log.info("skipped a line that wasn't ready: %s", speech.text[:70])
                return False
            log.warning("dropped line (not ready): %s", speech.text)
            return True
        on_air = self.on_air = OnAir(kind, speech.text, duration_s=len(audio) / pcm.SAMPLE_RATE)
        self.history.appendleft({"kind": kind, "text": speech.text, "at": time.time()})
        self._write(pcm.silence(SPEECH_PAD_S))
        for block in pcm.blocks(audio):
            if self._halted():
                return False
            if self._skipped(on_air):
                self._write(pcm.silence(SPEECH_PAD_S))
                return False
            self._write(block)
        self._write(pcm.silence(SPEECH_PAD_S))
        return True

    # --- tracks ----------------------------------------------------------------------

    def _scan(self, path: Path) -> Future:
        fut = self._scan_futures.get(path)
        if fut is None:
            fut = self._scan_futures[path] = self._scan_pool.submit(self.scans.scan, path)
        return fut

    def _refill(self) -> None:
        while len(self._queue) < LOOKAHEAD:
            t = self.selector.next_track()
            if t is None:
                return
            self._queue.append(t)
            self._scan(t.path)

    def _take_next(self) -> BroadcastTrack:
        self._refill()
        t = self._queue.popleft()
        self._took_request = self._n_requested > 0
        if self._took_request:
            self._n_requested -= 1
        self._refill()
        return t

    # --- requests: "play next" from the web page ---------------------------------------

    def search(self, query: str, limit: int = 40) -> list[dict]:
        """Tracks whose title, artist or album has every word of the query."""
        words = query.lower().split()
        if not words:
            return []
        out = []
        for i, t in enumerate(self.tracks):
            hay = f"{t.title} {t.artist} {t.album}".lower()
            if all(w in hay for w in words):
                out.append({"id": i, "title": t.title, "artist": t.artist, "album": t.album,
                            "path": t.path.relative_to(self.music_dir).as_posix()})
        out.sort(key=lambda r: (r["artist"].lower(), r["album"].lower(), r["title"].lower()))
        return out[:limit]

    def request(self, track_id: int) -> dict:
        """Play this track next (after any earlier requests). While a track
        plays, the gap's talk is re-worded for it; once the gap has started,
        it plays after the song already announced. Off air, the show opens
        with it. Requests are kept at the front of the queue (_n_requested)."""
        if not 0 <= track_id < len(self.tracks):
            raise ValueError("no such track")
        t = self.tracks[track_id]
        prepare = replanned = False
        with self._lock:
            on_air = self._thread is not None and self._thread.is_alive()
            if on_air and self._in_gap:
                self._pending.append(t)       # placed once the announced song starts
                after_announced = True
            else:
                after_announced = False
                pos = self._n_requested
                self._queue.insert(pos, t)
                self._n_requested += 1
                if on_air and pos == 0 and self._gap_decision is not None:
                    self._replan_gap(t)
                    replanned = True
                elif not on_air and not (self._opening is not None and self._opening_requested):
                    self._opening = None      # re-open the show with the request
                    prepare = True
            self._scan(t.path)
            position = len(self.requests()) - 1
        log.info("request: %s by %s (%s)", t.title, t.artist,
                 "after the announced song" if after_announced else f"position {position}")
        if prepare and self.tracks:
            self._prepare_opening()
        return {"position": position, "after_announced": after_announced, "replanned": replanned,
                "title": t.title, "artist": t.artist}

    # --- Previous: like a CD player ---------------------------------------------------

    def previous(self) -> bool:
        """More than PREVIOUS_RESTART_S into a track: start it again; sooner, the
        one before (in the show, then the one cut short again). In a gap
        (the DJ, a jingle, the news): the song that's just ended. Stations,
        books and podcasts have nothing to go back to: False."""
        on_air = self.on_air
        if not self.is_on_air or on_air is None or on_air.kind in ("radio", "book", "episode", "wait"):
            return False
        into = time.time() - on_air.started
        src = self._source
        if src is not None:                   # an album or On demand folder, straight through
            if src.get("kind") not in ("album", "playlist"):
                return False
            i = src.get("track", 0)
            self._album_jump = i if on_air.kind != "track" or into > PREVIOUS_RESTART_S else max(0, i - 1)
            self._skip_for = on_air
            log.info("previous: album track %d", self._album_jump + 1)
            return True
        cur = self.current_track
        if cur is None:
            return False
        with self._lock:
            if on_air.kind == "track" and into <= PREVIOUS_RESTART_S:
                before = next((t for t in reversed(self._played) if t != cur), None)
                if before is not None:
                    self._queue.appendleft(cur)   # then the one cut short, again
                    self._n_requested += 1
                    target = before
                else:
                    target = cur
            else:
                target = cur                  # restart it, or (in a gap) the song just ended
            self._jump = target
        self._skip_for = on_air
        log.info("previous: %s", target.title)
        return True

    # --- playlists: your own lists of tracks ----------------------------------------------

    def _ref(self, t: BroadcastTrack) -> list[str] | None:
        for root, (base, _) in self._roots().items():
            if base is not None:
                try:
                    return [root, t.path.relative_to(base).as_posix()]
                except ValueError:
                    pass
        return None

    def _by_ref(self, root: str, path: str) -> BroadcastTrack | None:
        if self._path_index is None:
            self._path_index = {tuple(r): t for _, (_, ts) in self._roots().items() for t in ts
                                if (r := self._ref(t)) is not None}
        return self._path_index.get((root, path))

    def set_playlists(self, entries: list[dict]) -> None:
        self.playlists = playlists_mod.validate(entries)

    def rename_playlist(self, old: str, new: str) -> str:
        """Rename a playlist (one playing keeps playing, under its new name).
        Returns the new name as saved; ValueError if there's none called old or
        the new name is taken."""
        p = playlists_mod.find(self.playlists, old)
        if p is None:
            raise ValueError("there's no playlist of that name")
        lists = [{"name": new if q is p else q["name"], "tracks": q["tracks"]} for q in self.playlists]
        self.playlists = playlists_mod.validate(lists)
        name = next(q["name"] for q, was in zip(self.playlists, lists) if was["tracks"] is p["tracks"])
        src = self._source
        if src is not None and src["kind"] == "playlist" and src["name"].lower() == p["name"].lower():
            src["name"] = src["title"] = name
            self._notify_source()
        log.info("playlist renamed: %s -> %s", p["name"], name)
        return name

    def playlist_view(self, p: dict) -> dict:
        tracks = []
        for root, path in p["tracks"]:
            t = self._by_ref(root, path)
            tracks.append({"root": root, "path": path, "title": t.title if t else Path(path).stem,
                           "artist": t.artist if t else "", "missing": t is None})
        return {"name": p["name"], "tracks": tracks}

    def refs_for(self, root=None, path=None, folder=None, deep=False, now=False) -> list[list[str]]:
        """What to add to a playlist: one track (root + path), a folder (+ deep:
        everything under it), or the song playing now."""
        if now:
            t = self.current_track
            ref = self._ref(t) if t is not None else None
            if ref is None:
                raise ValueError("no song is playing")
            return [ref]
        if folder is not None:
            album = self._album_by_folder(folder, root or "music", deep)
            if album is None:
                raise ValueError("that folder isn't in the library")
            return [r for t in album["tracks"] if (r := self._ref(t)) is not None]
        if not isinstance(path, str) or self._by_ref(root or "music", path) is None:
            raise ValueError("that track isn't in the library")
        return [[root or "music", path]]

    def add_to_playlist(self, name: str, refs: list[list[str]]) -> dict:
        """Add tracks to the end of a playlist (made if it isn't there)."""
        lists = [dict(p, tracks=list(p["tracks"])) for p in self.playlists]
        p = playlists_mod.find(lists, name)
        if p is None:
            p = {"name": name, "tracks": []}
            lists.append(p)
        p["tracks"].extend(refs)
        self.set_playlists(lists)
        return playlists_mod.find(self.playlists, name)

    def play_playlist(self, name: str, shuffle: bool = False, then: str | None = None) -> dict:
        """Play a playlist now, in order or shuffled, straight through like an
        album: at once, no DJ, jingles or news (those are Theme Radio's); then
        back to the show. Replaces a playlist already playing."""
        p = playlists_mod.find(self.playlists, name)
        if p is None:
            raise ValueError("there's no playlist of that name")
        tracks = [t for root, path in p["tracks"] if (t := self._by_ref(root, path)) is not None]
        if not tracks:
            raise ValueError(f"{p['name']} has nothing on the radio to play")
        if shuffle:
            random.shuffle(tracks)
        self.play_tracks_as(p["name"], tracks, shuffle, then)
        log.info("playlist: %s (%d tracks%s)", p["name"], len(tracks), ", shuffled" if shuffle else "")
        return {"name": p["name"], "tracks": len(tracks), "shuffle": shuffle}

    def _play_now(self, tracks: list[BroadcastTrack]) -> None:
        """These tracks at once, through the show's queue (the DJ as set), then
        the show: cutting in if the show's on, back from a station or album if
        one's playing, or opening the show with them if it's off."""
        playing = self._source is not None
        with self._lock:
            show_on = self.is_on_air and not playing
            rest = tracks[1:] if show_on else tracks
            for i, t in enumerate(rest):
                self._queue.insert(i, t)
            self._n_requested += len(rest)
            self._pending = deque(t for t in self._pending if t not in tracks)
            if show_on:
                self._jump = tracks[0]        # at once, whatever's on (a song or the DJ)
            elif not self.is_on_air:
                if self._opening is not None:
                    self._queue.insert(len(rest), self._opening[2])   # (its song goes back)
                self._opening = None
        for t in tracks[:2]:
            self._scan(t.path)
        if show_on:
            self._skip_for = self.on_air
        elif playing:
            self._next_source = None
            self._opening = None              # the show comes back opening with it
            self.tune(None)
        else:
            self._prepare_opening()

    # --- real lengths (for the desktop's timelines) -----------------------------------

    def track_seconds(self, t: BroadcastTrack) -> float | None:
        """A track's length, from its file's tags (cached)."""
        cache = self.__dict__.setdefault("_lengths", {})
        if t.path not in cache:
            from .library import _tags
            tags = _tags(t.path)
            cache[t.path] = float(getattr(getattr(tags, "info", None), "length", 0) or 0) or None
        return cache[t.path]

    def minutes_of(self, item: dict) -> float | None:
        """Minutes of a track, an album or folder (deep: all under it) or a playlist."""
        from . import playlists as pl
        k = item.get("kind") if isinstance(item, dict) else None
        if k == "track":
            t = self._by_ref(item.get("root", "music"), item.get("path"))
            tracks = [t] if t else []
        elif k == "album":
            a = self._album_by_folder(item.get("folder"), item.get("root", "music"), item.get("deep") is True)
            tracks = list(a["tracks"]) if a else []
        elif k == "playlist":
            p = pl.find(self.playlists, item.get("name"))
            tracks = [t for r, path in (p["tracks"] if p else []) if (t := self._by_ref(r, path))]
        else:
            return None
        secs = [self.track_seconds(t) for t in tracks]
        known = [s for s in secs if s]
        if not known:
            return None
        return round(sum(known) / 60 * len(secs) / len(known), 1)   # (any unreadable ones: the average)

    def rename_refs(self, old_root: str, old: str, new_root: str, new: str) -> bool:
        """A file or folder was moved: playlists that pointed into it follow it."""
        def moved(root, path):
            if root == old_root and (path == old or path.startswith(old + "/")):
                return new_root, new + path[len(old):]
            return root, path
        if old_root == "ondemand":               # (what remembered its place: still does, if it's still in On demand)
            kept = [m[1] for p in self.kept.paths if (m := moved("ondemand", p))[0] == "ondemand"]
            if kept != self.kept.paths:
                self.kept.paths = sorted(kept)
                self.kept.scan()
        changed = False
        lists = []
        for p in self.playlists:
            tracks = [list(moved(r, path)) for r, path in p["tracks"]]
            changed |= tracks != p["tracks"]
            lists.append({"name": p["name"], "tracks": tracks})
        if changed:
            self.set_playlists(lists)
        return changed

    def play_tracks_as(self, name: str, tracks: list[BroadcastTrack], shuffle: bool = False,
                       then: str | None = None) -> None:
        """Tracks now, as a playlist of this name (a playlist, or a programme's
        block): straight through, no DJ or jingles; playlist_status() has it."""
        refs = [r for t in tracks if (r := self._ref(t)) is not None]
        self._next_source = None
        self.tune({"kind": "playlist", "name": name, "refs": refs, "shuffle": shuffle,
                   **({"then": "pause"} if then == "pause" else {})})

    def stop_playlist(self) -> bool:
        """The rest of the playlist is dropped; the song playing finishes."""
        src = self._source
        if src is None or src["kind"] != "playlist" or src.get("stop"):
            return False
        src["stop"] = True
        log.info("playlist stopped")
        return True

    def playlist_status(self) -> dict | None:
        src = self._source
        if src is None or src["kind"] != "playlist":
            return None
        left = 0 if src.get("stop") else len(src["refs"]) - src.get("track", 0) - 1
        return {"name": src["name"], "left": left, "shuffle": src.get("shuffle", False)}

    # --- albums: played start to finish -----------------------------------------------

    def _scan_ondemand(self) -> list[BroadcastTrack]:
        if self.ondemand_dir is None:
            return []
        cache = None if self._tag_cache is None else Path(self._tag_cache).with_name("tags-ondemand.json")
        return scan_music(self.ondemand_dir, cache)

    def _roots(self) -> dict[str, tuple[Path | None, list[BroadcastTrack]]]:
        return {"music": (self.music_dir, self.tracks), "ondemand": (self.ondemand_dir, self.ondemand_tracks)}

    def albums(self) -> list[dict]:
        """Every folder with audio in it, in music (Artist/Album/NN - Title) and On
        demand (any layout): its tracks in file order, named from the tags where
        they agree, else from the folder. Cached (the library changes only
        through reload_library)."""
        if self._album_index is None:
            index = []
            for root, (base, root_tracks) in self._roots().items():
                folders: dict[Path, list[BroadcastTrack]] = {}
                for t in root_tracks:
                    folders.setdefault(t.path.parent, []).append(t)
                for folder, tracks in folders.items():
                    tracks.sort(key=lambda t: _natural(t.path.name))
                    try:
                        rel = folder.relative_to(base).as_posix()
                    except ValueError:
                        rel = str(folder)
                    rel = "" if rel == "." else rel
                    index.append({"root": root, "folder": rel, "tracks": tracks,
                                  **_album_names(tracks, rel, root)})
            index.sort(key=lambda a: (a["root"] != "music", artist_key(a["artist"]), a["title"].lower()))
            for i, a in enumerate(index):
                a["id"] = i
            self._album_index = index
        return self._album_index

    def browse(self, root: str, path: str = "") -> dict:
        """One folder of music or On demand, for the page's folder view: the
        folders in it that hold audio (at any depth), and its own tracks if any."""
        if root not in ("music", "ondemand"):
            raise ValueError("root is music or ondemand")
        path = "/".join(p for p in (path or "").split("/") if p)
        if ".." in path.split("/"):
            raise ValueError("that isn't a folder in the library")
        prefix = path + "/" if path else ""
        subs: dict[str, dict] = {}
        here = None
        for a in self.albums():
            if a["root"] != root:
                continue
            if a["folder"] == path:
                here = a
            elif a["folder"].startswith(prefix):
                name = a["folder"][len(prefix):].split("/")[0]
                sub = subs.setdefault(name, {"name": name, "tracks": 0, "bytes": 0, "own": False, "inner": set()})
                sub["tracks"] += len(a["tracks"])
                sub["bytes"] += self._album_bytes(a)
                if a["folder"] == prefix + name:
                    sub["own"] = True           # it has tracks of its own
                else:                           # the folders inside it (holding audio somewhere)
                    sub["inner"].add(a["folder"][len(prefix + name) + 1:].split("/")[0])
        folders = sorted(subs.values(), key=lambda f: _natural(f["name"]))
        for f in folders:
            f["folders"] = len(f.pop("inner"))
        album = None
        if here is not None:
            album = {**self._album_info(here),
                     "list": [{"n": i, "title": t.title, "artist": t.artist, "path": self._ref(t)[1], "bytes": _size(t.path)}
                              for i, t in enumerate(here["tracks"])]}
        return {"root": root, "path": path, "folders": folders, "album": album}

    @staticmethod
    def _album_bytes(a: dict) -> int:
        """The size of an album's files, worked out once (the album index is
        rebuilt when the library changes, which starts it again)."""
        if "bytes" not in a:
            a["bytes"] = sum(_size(t.path) for t in a["tracks"])
        return a["bytes"]

    @staticmethod
    def _album_info(a: dict) -> dict:
        return {"id": a.get("id"), "root": a["root"], "folder": a["folder"], "deep": a.get("deep", False),
                "title": a["title"], "artist": a["artist"], "tracks": len(a["tracks"])}

    def all_albums(self) -> list[dict]:
        """The A-Z list for the page (no tracks)."""
        return [self._album_info(a) for a in self.albums()]

    def search_albums(self, query: str, limit: int = 20) -> list[dict]:
        words = query.lower().split()
        if not words:
            return []
        hits = [a for a in self.albums()
                if all(w in f"{a['title']} {a['artist']} {a['folder']}".lower() for w in words)]
        return [self._album_info(a) for a in hits[:limit]]

    def request_album(self, album_id: int) -> dict:
        """Queue a whole album to play next, in order, straight through: the DJ
        introduces it and back-announces it, with nothing in between."""
        albums = self.albums()
        if isinstance(album_id, bool) or not isinstance(album_id, int) or not 0 <= album_id < len(albums):
            raise ValueError("no such album")
        if albums[album_id]["root"] != "music":
            return self.play_next(album_id)
        return self._request_album(albums[album_id])

    def _request_album(self, album: dict) -> dict:
        run = {"title": album["title"], "artist": album["artist"], "tracks": list(album["tracks"])}
        with self._lock:
            self._albums.append(run)          # before the requests, so the gap is worded for it
        index = {t.path: i for i, t in enumerate(self.tracks)}
        for t in run["tracks"]:
            self.request(index[t.path])
        log.info("album: %s by %s (%d tracks)", run["title"], run["artist"], len(run["tracks"]))
        return {"title": run["title"], "artist": run["artist"], "tracks": len(run["tracks"])}

    def stop_album(self) -> bool:
        """Drop the rest of the album(s) from the queue; the track playing finishes."""
        with self._lock:
            had_next, self._next_source = self._next_source is not None, None
            if not self._albums:
                return had_next
            in_albums = {t for run in self._albums for t in run["tracks"]}
            front = list(self._queue)[:self._n_requested]
            keep = [t for t in front if t not in in_albums]
            rest = list(self._queue)[self._n_requested:]
            self._queue = deque(keep + rest)
            self._n_requested = len(keep)
            self._pending = deque(t for t in self._pending if t not in in_albums)
            self._albums.clear()
            on_air = self._thread is not None and self._thread.is_alive()
            self._refill()
            if on_air and not self._in_gap and self._queue:
                if self._gap_decision is not None:
                    self._replan_gap(self._queue[0])
                else:                          # was straight on inside the album: talk again
                    self._gap_is_album = False
                    self._plan = self._build_gap(LinkKind.LINK, False, [], self.current_track, self._queue[0])
                    self.next_track = self._queue[0]
                    self.gap_plan = [s.describe() for s in self._plan]
        log.info("album stopped")
        return True

    def album_status(self) -> dict | None:
        """The album playing (or about to), for the page."""
        for run in self._albums:
            t = self.current_track
            if run.get("started") and t in run["tracks"]:
                return {"title": run["title"], "artist": run["artist"],
                        "track": run["tracks"].index(t) + 1, "of": len(run["tracks"])}
        if self._albums:
            run = self._albums[0]
            return {"title": run["title"], "artist": run["artist"], "track": 0, "of": len(run["tracks"])}
        return None

    def _album_of(self, t: BroadcastTrack | None) -> dict | None:
        """The started album this track belongs to."""
        return next((r for r in self._albums if r.get("started") and t in r["tracks"]), None) if t else None

    def _album_inside(self, prev: BroadcastTrack, nxt: BroadcastTrack) -> bool:
        run = self._album_of(prev)
        if run is None or not run.get("started") or nxt not in run["tracks"]:
            return False
        return run["tracks"].index(nxt) == run["tracks"].index(prev) + 1

    def _album_start(self, prev: BroadcastTrack | None, nxt: BroadcastTrack) -> dict | None:
        run = next((r for r in self._albums if not r.get("started") and r["tracks"][0] == nxt), None)
        return run

    def _album_end(self, prev: BroadcastTrack | None) -> dict | None:
        run = next((r for r in self._albums if r.get("started") and r["tracks"][-1] == prev), None)
        return run

    def _place_pending(self) -> None:
        """(show thread, under the lock) after the gap: queue requests made during it."""
        while self._pending:
            self._queue.insert(self._n_requested, self._pending.popleft())
            self._n_requested += 1

    def requests(self) -> list[dict]:
        """Requested songs still to come, in order."""
        queued = list(self._queue)[:self._n_requested] + list(self._pending)
        if self._opening is not None and self._opening_requested:
            queued.insert(0, self._opening[2])
        in_albums = {t for run in self._albums for t in run["tracks"]}
        return [{"title": t.title, "artist": t.artist, "album": t in in_albums} for t in queued]

    def _play_file(self, path: Path, on_air: OnAir, near_end=None, scan_wait_s: float = 60,
                   start_ms: int = 0, keep_place: bool = False) -> None:
        """Play a file to its end (or until skipped). near_end(end_at, again) is called
        once PREFETCH_S before the end -- and again, with again=True, if a skip then
        makes that projected end wrong. scan_wait_s: how long to wait for its loudness
        scan (a whole decode: tens of seconds for a long track on a Zero); not ready
        by then, it plays as it is (the scan carries on, cached for next time).
        start_ms: from there in the file. keep_place (an album's or playlist's song):
        paused, it stops and waits where it was (as a book does) rather than play on
        into nothing, and the knob's press-and-turn can move it (_file_seek).
        self._file_pos is where it's got to, in the file (ms)."""
        fut = self._scan(path)
        scan = self._await(fut, scan_wait_s) or pcm.NO_SCAN
        if scan is pcm.NO_SCAN and not fut.done():
            log.info("not measured yet, playing as it is: %s", path.name)
        if path not in self._jingle_paths:  # jingles recur; tracks' futures can go
            self._scan_futures.pop(path, None)
        playable_s = scan.playable_ms / 1000
        if not playable_s and path not in self._jingle_paths:   # (not measured: its length from its tags)
            playable_s = self.track_seconds(BroadcastTrack(path, "", "")) or 0
        on_air.duration_s = playable_s
        self.on_air = on_air
        pos = max(start_ms, scan.start_ms)       # (ms in the file)
        self._file_seek = None
        self._file_trim = scan.start_ms          # (where the song's sound starts: position 0 for the page)
        fired = near_end is None
        reword = False                           # moved after the gap was worded: word it again
        while True:
            on_air.started = time.time() - (pos - scan.start_ms) / 1000
            self._file_pos = (path, pos)
            moved = False
            for block in pcm.decode(path, pos, scan.end_ms):
                if self._halted():
                    return
                if self._skipped(on_air):
                    if fired and near_end is not None:
                        near_end(datetime.now(), True)
                    break                        # (moved stays False: done)
                paused = keep_place and self._listeners == 0 and self.speaker_paused()
                if self._file_seek is not None or paused:
                    if paused:                   # wait here (the knob may move it meanwhile)
                        while self._listeners == 0 and not self._halted():
                            time.sleep(0.2)
                        if self._halted():
                            return
                    if self._file_seek is not None:
                        pos, self._file_seek = self._file_seek, None
                    moved = True
                    break
                self._write(pcm.apply_gain(block, scan.gain))
                pos += len(block) * 1000 / pcm.SAMPLE_RATE
                self._file_pos = (path, pos)
                left = playable_s - (pos - scan.start_ms) / 1000
                if not fired and playable_s and left <= PREFETCH_S:
                    fired = True
                    near_end(datetime.now() + timedelta(seconds=left), reword)
                    reword = False
            if not moved:
                break
            if fired and near_end is not None:   # (moved: the end is somewhere else now)
                left = playable_s - (pos - scan.start_ms) / 1000
                if left <= PREFETCH_S:
                    near_end(datetime.now() + timedelta(seconds=max(0, left)), True)
                else:
                    fired, reword = False, True
        if not fired:
            near_end(datetime.now(), False)

    def _track_started(self, track: BroadcastTrack) -> None:
        self.current_track = track
        self._played.append(track)
        for run in self._albums:              # an album counts as under way from its first track
            if not run.get("started") and run["tracks"][0] == track:
                run["started"] = True
                break

    def _play_track(self, track: BroadcastTrack) -> None:
        self._track_started(track)
        self._refill()
        self.next_track = self._queue[0] if self._queue else None
        plan = self._plan_gap(track, self.next_track)
        self._plan = plan
        self.gap_plan = [s.describe() for s in plan]
        self._maybe_prepare_news()
        log.info("now: %s by %s | after it: %s", track.title, track.artist, self.gap_plan or "straight on")
        self.history.appendleft({"kind": "track", "text": f"{track.title} — {track.artist}", "at": time.time()})
        self._play_file(track.path, OnAir("track", track.title, track.artist, track.album),
                        near_end=lambda end_at, again: self._prefetch_gap(plan, end_at, again))

    # --- the gap between tracks ---------------------------------------------------------

    def _jingle_due(self) -> bool:
        if self.config.jingle_every <= 0 or not self.jingles:
            return False
        self._tracks_since_jingle += 1
        if self._tracks_since_jingle < self.config.jingle_every:
            return False
        self._tracks_since_jingle = 0
        return True

    def _next_jingle(self) -> JingleClip | None:
        if not self.jingles:
            return None
        if not self._jingle_bag:
            bag = self.jingles[:]
            random.shuffle(bag)
            if len(bag) > 1 and bag[0] == self._last_jingle:
                bag.append(bag.pop(0))
            self._jingle_bag.extend(bag)
        self._last_jingle = self._jingle_bag.popleft()
        return self._last_jingle

    # --- DJ settings (the page's DJ card) -------------------------------------------------

    def voices(self) -> list[str]:
        """The voice packs in the voices folder (folders with a model.onnx)."""
        if self.voices_dir is None or not Path(self.voices_dir).is_dir():
            return []
        return sorted(d.name for d in Path(self.voices_dir).iterdir() if (d / "model.onnx").is_file())

    def dj_settings(self) -> dict:
        b = self.theme_settings()              # (the theme playing's: each theme has its own)
        t = self.settings_theme()
        return {"voice": self.dj_voice, "voices": self.voices(), "chattiness": b["chattiness"],
                "theme": t["name"] if t else None,
                "chattiness_options": [c.ident for c in Chattiness],
                "dj_hooks": bool(b["dj_hooks"] and self._hook_pool), "hooks_available": self._hook_pool is not None,
                "jingle_every": b["jingle_every"], "jingles_available": self._jingles_available(),
                "news_enabled": b["news"], "time_checks": b["time_checks"], "dj_on": self.dj_on,
                "dj_speed": self.config.announcer_speed, "news_speed": self.config.news_speed}

    def _jingles_available(self) -> bool:
        if self.jingles:
            return True
        from .library import _audio_files
        return bool(_audio_files(Path(self.jingles_dir)))     # (in any station's folder)

    def jingles_folder(self, profile: str | None = None) -> Path | None:
        """A theme's jingles: Jingles/<theme> (no shared jingles: the user's choice).
        None for artist radio, which has none. (No themes at all, which a radio
        never has for long: Jingles/<the radio's name>.)"""
        name = profile if profile is not None else self.profile
        if name:
            return Path(self.jingles_dir) / name
        if self.artist or self.profiles:
            return None
        from sleepradiopi.config import brand
        return Path(self.jingles_dir) / brand.name

    def _start_up_jingle(self) -> JingleClip | None:
        """At power-on: one of the playing theme's jingles (a short one if it has
        any), else one of Default's (the theme that's always there)."""
        folder = self.jingles_folder()
        own = (self.jingles or (scan_jingles(folder) if folder else [])) if self.config.jingle_every > 0 else []
        short = [j for j in own if 0 < j.duration_s <= FILL_JINGLE_MAX_S]
        pick = short or own or self._default_jingles
        return random.choice(pick) if pick else None

    def _load_jingles(self, force: bool = False) -> None:
        """The playing station's jingles (when jingles are on), and the power-on ones;
        again only when the station's folder changes (or force: they were changed)."""
        folder = self.jingles_folder()
        if folder == self._jingles_from and not force:
            return
        self._jingles_from = folder
        self.jingles = scan_jingles(folder) if self.config.jingle_every and folder else []
        self._default_jingles = scan_jingles(Path(self.jingles_dir) / profiles_mod.DEFAULT)
        self._jingle_paths = {j.path for j in self.jingles + self._default_jingles}
        self._jingle_bag.clear()
        for j in self.jingles + self._default_jingles:     # (a handful of short files: loudness up front)
            self._scan(j.path)

    def set_dj(self, chattiness: str | None = None, dj_hooks: bool | None = None,
               jingle_every: int | None = None, news_enabled: bool | None = None,
               dj_speed: float | None = None, news_speed: float | None = None,
               dj_on: bool | None = None, time_checks: bool | None = None) -> None:
        """Change the DJ live (from the next gap on): the radio-wide settings (a theme
        playing may set its own chattiness, time checks, news, jingles and hooks). ValueError if a value is wrong.
        The speeds (1 = the voice's own pace, higher = faster) apply to lines made
        from now on; one or two may already be made at the old speed."""
        for name, speed in (("dj_speed", dj_speed), ("news_speed", news_speed)):
            if speed is not None and (isinstance(speed, bool) or not isinstance(speed, (int, float))
                                      or not SPEED_MIN <= speed <= SPEED_MAX):
                raise ValueError(f"{name} must be {SPEED_MIN}-{SPEED_MAX}")
        if dj_speed is not None:
            self.config.announcer_speed = round(float(dj_speed), 2)
        if news_speed is not None:
            self.config.news_speed = round(float(news_speed), 2)
        if chattiness is not None and chattiness not in [c.ident for c in Chattiness]:
            raise ValueError(f"chattiness must be one of {[c.ident for c in Chattiness]}")
        if jingle_every is not None and not 0 <= jingle_every <= 50:
            raise ValueError("jingles: 0 (off) to every 50 tracks")
        for key, value in (("chattiness", chattiness), ("dj_hooks", None if dj_hooks is None else bool(dj_hooks)),
                           ("jingle_every", jingle_every), ("news", None if news_enabled is None else bool(news_enabled)),
                           ("time_checks", None if time_checks is None else bool(time_checks))):
            if value is not None:                     # (the theme playing's, or Default's on artist radio)
                t = self.settings_theme()
                if t is not None:
                    t["settings"] = {**self.theme_settings(), **(t.get("settings") or {}), key: value}
                else:
                    self.base[key] = value
        if jingle_every is not None:
            self._tracks_since_jingle = 0
        self._apply_settings()
        if dj_on is not None and bool(dj_on) != self.dj_on:
            self.dj_on = bool(dj_on)
            if not self.is_on_air:
                self._opening = None           # the welcome: made again (with or without the DJ)
                self._prepare_opening()
        log.info("DJ %s: chattiness %s, hooks %s, jingles every %s, news %s, speed %s, news speed %s",
                 "on" if self.dj_on else "off", self.chattiness, self.builder.hooks is not None,
                 self.config.jingle_every or "off",
                 self.config.news_enabled, self.config.announcer_speed, self.config.news_speed)

    def settings_theme(self) -> dict | None:
        """The theme whose DJ settings are in force: the one playing, else (artist radio)
        Default."""
        want = (self.profile or profiles_mod.DEFAULT).lower()
        return next((p for p in self.profiles if p["name"].lower() == want), None)

    def theme_settings(self) -> dict:
        """The DJ settings in force: each theme has its own full set (no master
        settings); base (the old radio-wide values) only fills a gap."""
        t = self.settings_theme()
        return {**self.base, **((t or {}).get("settings") or {})}

    def complete_settings(self, lists: list[dict]) -> list[dict]:
        """Each theme has its own full set of DJ settings: one missing some (a new theme)
        gets Default's (or, for Default, the old radio-wide values)."""
        default = next((p for p in lists if p["name"].lower() == profiles_mod.DEFAULT.lower()), None)
        base = {**self.base, **((default or {}).get("settings") or {})} if hasattr(self, "base") else None
        if base is None:
            return lists
        return [{**p, "settings": {**base, **(p.get("settings") or {})}} for p in lists]

    def _apply_settings(self) -> None:
        """Put into force the DJ settings of the theme playing (theme_settings)."""
        eff = self.theme_settings()
        c = next(c for c in Chattiness if c.ident == eff["chattiness"])
        self.chattiness = c.ident
        self.config.tracks_per_link = c.tracks_per_link
        self.config.announce_every_track = c == Chattiness.MAXIMUM
        self.time_checks = bool(eff["time_checks"])
        self.config.news_enabled = bool(eff["news"])
        self.builder.hooks = self._hook_pool if eff["dj_hooks"] else None
        self.config.dj_hooks_enabled = bool(eff["dj_hooks"] and self._hook_pool)
        was = self.config.jingle_every
        self.config.jingle_every = int(eff["jingle_every"])
        if self.config.jingle_every and not was:     # (jingles weren't loaded while off)
            self._load_jingles(force=True)

    def _plan_gap(self, prev: BroadcastTrack, nxt: BroadcastTrack | None) -> list[Step]:
        """onBroadcastTrackStarted: what fills the gap after [prev]. The decisions
        (link or time check, jingle, birthday, message) are made once here and kept, so a
        request for the next song can re-word the gap without making them again."""
        self._gap_is_album = nxt is not None and self._album_inside(prev, nxt)
        if self._gap_is_album:               # like a record: straight on to the next track
            self._gap_decision = None
            return []
        kind = self._show_clock.on_track_started(datetime.now().time())
        if kind == LinkKind.TIME_CHECK and (not clock_trusted() or not self.time_checks):
            kind = LinkKind.LINK   # offline, the clock may be hours out: say no times
        # Jingles follow the jingles on/off setting, on a theme or artist radio too.
        jingle_due = self._jingle_due()      # (on a theme or artist radio too: the user's choice)
        people = []
        if self._has_voice and self.dj_on:
            now = datetime.now()
            people = self.birthdays.due(now, clock_trusted())
            if people:
                self.birthdays.wished(now)
                log.info("birthday wish planned for %s", ", ".join(p["name"] for p in people))
        self._gap_decision = (kind, jingle_due, people, prev, None)   # (a message is added near the end)
        steps = self._build_gap(kind, jingle_due, people, prev, nxt)
        finished = self._album_end(prev)
        if finished is not None:              # its back-announcement is planned: done with it
            self._albums.remove(finished)
        return steps

    def _build_gap(self, kind: LinkKind, jingle_due: bool, people: list, prev: BroadcastTrack,
                   nxt: BroadcastTrack | None, message: str | None = None) -> list[Step]:
        b, voice = self.builder, self._has_voice and self.dj_on   # (the DJ off: no talk)
        starts = self._album_start(prev, nxt) if nxt is not None else None
        ends = self._album_end(prev)
        if voice and (starts or ends):        # into or out of an album
            steps = [Step("say", self._say(b.album_outro(ends) if ends else b.outro_line(prev)))]
            if jingle_due:
                steps.append(Step("jingle"))
            if starts:
                steps.append(Step("say", self._say(b.album_intro(starts, nxt))))
            elif nxt is not None:
                steps.append(Step("say", self._say(b.intro_line(nxt))))
            if message:
                steps.insert(0, Step("say", self._say(message)))
            if people:
                steps.insert(0, Step("say", self._say(wish_text(people, b.station))))
            return steps
        steps: list[Step] = []
        talky = kind in (LinkKind.LINK, LinkKind.TIME_CHECK)
        if jingle_due and voice and talky:
            steps.append(Step("say", self._say(b.outro_line(prev))))
            if kind == LinkKind.TIME_CHECK:
                steps.append(Step("clock", clock=ClockStep()))
            steps.append(Step("jingle"))
            steps.append(Step("say", self._say(b.intro_line(nxt))))
        elif jingle_due:
            steps.append(Step("jingle"))
        elif voice and kind == LinkKind.TIME_CHECK:
            if self.config.announce_every_track:
                steps.append(Step("say", self._say(b.outro_line(prev))))
            steps.append(Step("clock", clock=ClockStep()))
            if nxt is not None:
                steps.append(Step("say", self._say(b.intro_line(nxt))))
        elif voice and kind != LinkKind.NONE:
            text = b.build(kind, prev, nxt, announce_every_track=self.config.announce_every_track)
            if text:
                steps.append(Step("say", self._say(text)))
        if voice and message:                 # first thing in the gap (after a birthday wish)
            steps.insert(0, Step("say", self._say(message)))
        if voice and people:                  # first thing in the gap
            steps.insert(0, Step("say", self._say(wish_text(people, b.station))))
        return steps

    def _replan_gap(self, nxt: BroadcastTrack) -> None:
        """The next song changed (a request) while a track plays: re-word the
        gap's lines for it. Clock, jingle and news steps are kept as they are
        (a time check may already be worded); only the talk is redone."""
        kind, jingle_due, people, prev, message = self._gap_decision
        old = self._plan
        new = self._build_gap(kind, jingle_due, people, prev, nxt, message)
        for i, step in enumerate(new):
            if step.kind != "say" and i < len(old) and old[i].kind == step.kind:
                new[i] = old[i]
        for step in old:
            if step.kind == "say" and step not in new:
                step.speech.future.cancel()   # a no-op if it's already made
        self._plan = new
        self.next_track = nxt
        self.gap_plan = [s.describe() for s in new]
        log.info("next changed by request: %s by %s | after it: %s", nxt.title, nxt.artist, self.gap_plan)

    def set_birthdays(self, entries: list[dict]) -> None:
        """Replace the birthday list (validated; ValueError if it's wrong)."""
        self.birthdays.set(entries)
        log.info("birthdays: %d on the list", len(self.birthdays.entries))

    def set_messages(self, cfg: dict) -> None:
        """Replace the messages and their times (validated; ValueError if wrong)."""
        self.messages.set(cfg)
        log.info("messages: %d on the list, %s", len(self.messages.cfg["list"]),
                 "on" if self.messages.cfg["on"] else "off")

    def _prefetch_gap(self, plan: list[Step], end_at: datetime, again: bool = False) -> None:
        """PREFETCH_S before the track ends: word anything time-dependent from the real
        end time, so it's synthesised by the time the gap arrives. [again]: the track was
        skipped after that, so throw away what was worded and word it from now."""
        if not self.dj_on:                    # the DJ off: nothing to word
            return
        news = self._news_ready
        if again:
            for step in plan:
                if step.kind == "clock":
                    step.clock.discard()
            if news is not None and news.time_line is not None:
                news.time_line.future.cancel()
                news.time_line = None
        if news and news.time_line is None:
            due = self.news_schedule.due_at(end_at)
            if due and due.key == news.due.key:
                news.time_line = self._say(bulletin_time_line(news.due.mark, end_at),
                                           self.news_voice, self.config.news_speed)
        self._maybe_add_message(end_at)
        offset = 0.0
        for step in plan:
            if step.kind == "say":
                offset += PER_LINE_ESTIMATE_S
            elif step.kind == "clock" and step.clock.speech is None:
                step.clock.speech = self._say(self.builder.time_line((end_at + timedelta(seconds=offset)).time()))
            elif step.kind == "jingle":
                break

    def _maybe_add_message(self, end_at: datetime) -> None:
        """Near the end of a track: if a message is due when the gap comes, put it
        first in the gap (after a birthday wish). Not in an album, and not when
        the news is due in this gap (the message then waits for the next one)."""
        decision = self._gap_decision
        if decision is None or decision[4] is not None or not self._has_voice or not self.dj_on or self._gap_is_album:
            return
        if self.config.news_enabled and self._news_ready is not None \
                and self.news_schedule.due_at(end_at) is not None:
            return
        message = self.messages.due(end_at, clock_trusted(), self.profile)   # (the radio-wide ones, and this theme's)
        if not message:
            return
        record = self.messages.played(end_at)
        self._gap_decision = decision[:4] + (message,)
        step = Step("say", self._say(message), message=record)
        plan = self._plan
        plan.insert(1 if decision[2] and plan and plan[0].kind == "say" else 0, step)
        self.gap_plan = [s.describe() for s in plan]
        log.info("message in the next gap: %s", message[:60])

    def _run_gap(self) -> None:
        plan = self._plan
        news = self._news_ready
        if news is not None and self.config.news_enabled and self.dj_on and not self._gap_is_album:
            due = self.news_schedule.due_at(datetime.now())
            if due is not None and due.key == news.due.key and not news.body.future.done():
                # Still being made (a skip can bring the gap early): don't sit in
                # silence for it -- it's read at the next gap if it's still due.
                log.info("news not ready yet: at the next gap")
            elif due is not None and due.key == news.due.key:
                self.news_schedule.mark_read(due)
                self._news_ready = None
                had_jingle = any(s.kind == "jingle" for s in plan)
                plan = [Step("news", news=news)]
                if had_jingle:
                    plan.append(Step("jingle"))
                if self._has_voice and self.dj_on:
                    plan.append(Step("say", self._say(self.builder.intro_line(self.next_track))))
                log.info("gap (news): %s", [s.describe() for s in plan])
        self._run_steps(plan, gap=True)

    def _run_steps(self, steps: list[Step], gap: bool = False) -> None:
        """gap: between songs, where a line that isn't made in time isn't waited
        for (it was "Getting the next bit ready", then the line much later): it's
        skipped -- a message comes round again at the next gap -- and a short jingle
        fills in, once, if the gap had none, then the next song."""
        filled = any(s.kind == "jingle" for s in steps)

        def fill() -> bool:
            """(a gap) Something isn't ready: a jingle while it's made, once a gap."""
            nonlocal filled
            # (not two gaps running: the counter is 1 at the gap after one with a jingle)
            recent = self._tracks_since_jingle < 2
            clip = None if filled or recent else self._fill_jingle()
            filled = filled or clip is not None
            if clip is not None:
                self._tracks_since_jingle = 0     # (it counts as the jingle: "every 4" stays every 4)
                self._run_steps([Step("jingle", jingle=clip)])
            return clip is not None

        for step in steps:
            if self._halted() or self._jump is not None:
                return
            if not self.dj_on and step.kind in ("say", "clock", "news"):
                continue                      # the DJ went off after this was planned: music only
            if step.kind == "clock" and step.clock.speech is None:   # no prefetch (unknown track length)
                step.clock.speech = self._say(self.builder.time_line())
            speech = step.speech if step.kind == "say" else step.clock.speech if step.kind == "clock" else None
            if gap and speech is not None and self._await(speech.future, GAP_GRACE_S) is None and not self._halted():
                failed = speech.future.done()    # (it failed: nothing to wait for, so no jingle for it)
                jingled = False if failed else fill()
                if failed or not speech.future.done():   # (failed, or still not made after the jingle)
                    speech.future.cancel()    # (a no-op if it's being made: it's just not used)
                    if step.message is not None:
                        self.messages.unplayed(step.message)
                    log.info("gap: the DJ isn't ready (%s)%s", speech.text[:60],
                             "; a jingle instead" if jingled else "; on to the music")
                    continue
                log.info("gap: a jingle while the DJ got ready (%s)", speech.text[:60])
            if step.kind == "say":
                self._speak(step.speech)
            elif step.kind == "clock":
                self._speak(step.clock.speech)
            elif step.kind == "jingle":
                clip = step.jingle or self._next_jingle()
                if clip is not None:
                    name = clip.path.stem.replace("_", " ")
                    self.history.appendleft({"kind": "jingle", "text": name, "at": time.time()})
                    self._play_file(clip.path, OnAir("jingle", name))
            elif step.kind == "news":
                self._read_news(step.news, fill if gap else None)

    # --- news --------------------------------------------------------------------------

    def _maybe_prepare_news(self) -> None:
        # News is scheduled by the clock (and needs the internet anyway).
        if not (self.config.news_enabled and self.dj_on and self.tts is not None and clock_trusted()):
            return
        due = self.news_schedule.prep_at(datetime.now())
        if due is None or due.key == self._news_prep_key:
            return
        self._news_prep_key = due.key
        self._news_ready = None

        def prepare() -> None:
            headlines = self.news_repo.headlines_for(due.slot)
            body = build_bulletin_body(due.slot, headlines)
            if body is None:
                log.info("news %s: no stories (offline or nothing new)", due.slot.value)
                self._news_prep_key = None  # retry at the next track start
                return
            speech = self._say(body, self.news_voice, self.config.news_speed)
            self._news_ready = NewsItem(due, headlines, speech)
            log.info("news %s for %s prepared: %d stories", due.slot.value, due.mark.strftime("%H:%M"),
                     len(headlines))

        threading.Thread(target=prepare, name="news-prep", daemon=True).start()

    def _read_news(self, news: NewsItem, fill: Callable[[], bool] | None = None) -> None:
        """The bulletin: its time line, then the stories. fill (in a gap): the time line
        isn't made yet -- a jingle while it's made (once); still not made: straight
        to the stories, never a wait in silence."""
        if news.time_line is None:
            news.time_line = self._say(bulletin_time_line(news.due.mark, datetime.now()),
                                       self.news_voice, self.config.news_speed)
        line = news.time_line
        if fill is not None and self._await(line.future, GAP_GRACE_S) is None and not self._halted():
            fill()
            if not line.future.done():
                line.future.cancel()
                log.info("news: the time line isn't ready; straight to the stories")
                line = None
        if line is None or self._speak(line, "news"):  # a skip in the time line skips the whole bulletin
            self._speak(news.body, "news")
        self.news_repo.mark_read(news.headlines)

    # --- opening ----------------------------------------------------------------------

    # --- audiobooks -------------------------------------------------------------------------

    def _book(self, key: str):
        """A book by its key: one of the audiobooks, or an On demand thing that remembers its place."""
        return self.kept.get(key) if key.startswith(KEPT) else self.books.get(key)

    def set_keep(self, path: str, keep: bool) -> list[str]:
        """An On demand file or folder remembers its place from now on (or stops:
        from the start each time, as On demand does). ValueError if it isn't there."""
        path = "/".join(p for p in path.split("/") if p) if isinstance(path, str) else ""
        if not path or ".." in path.split("/") or self.ondemand_dir is None or not (Path(self.ondemand_dir) / path).exists():
            raise ValueError("that isn't in On demand")
        paths = [p for p in self.kept.paths if p != path] + ([path] if keep else [])
        self.kept.paths = sorted(paths)
        self.kept.scan()
        log.info("on demand: %s %s", path, "remembers its place" if keep else "starts from the beginning")
        return self.kept.paths

    def kept_list(self) -> list[dict]:
        """The On demand things that remember their place, with where each was left."""
        out = []
        for path in self.kept.paths:
            book = self.kept.get(KEPT + path)
            pos = self.book_positions.get(KEPT + path)
            out.append({"path": path, "key": KEPT + path, "title": book.title if book else Path(path).stem,
                        "total_ms": book.total_ms if book else 0, "pos_ms": pos,
                        "done": self.book_positions.done(KEPT + path), "chapters": len(book.chapters) if book else 0})
        return out

    def _kept_play(self, key: str, file: Path | None = None) -> dict | None:
        """Play a kept thing as the book it is, from where it was left -- or, a file
        in a kept folder chosen: from that file (unless that's where it was left).
        None if it hasn't been read yet (it then plays the plain way)."""
        book = self.kept.get(key)
        if book is None or not book.chapters:
            return None
        if file is not None:
            i = next((n for n, c in enumerate(book.chapters) if c.path == file), None)
            left_in = book.chapters[book.at(self.book_positions.get(key))[0]].path
            if i is not None and left_in != file:
                self.book_positions.set(key, book.chapters[i].offset_ms)
        self._next_source = None
        self.tune({"kind": "book", "key": key})
        return self._source

    def _scan_books(self) -> None:
        try:
            self.books.scan()
            self.kept.scan()
        except Exception:
            log.exception("audiobooks: scan failed")
        finally:
            self.books_scanning = False

    @staticmethod
    def _book_state(book, pos_ms: int) -> dict:
        i, _ = book.at(pos_ms)
        return {"key": book.key, "pos_ms": int(pos_ms), "total_ms": book.total_ms, "chapter": i + 1,
                "chapters": len(book.chapters), "chapter_title": book.chapters[i].title}

    def book_seek(self, delta_ms: int | None = None, to_ms: int | None = None) -> dict:
        """Move the book playing (or the one tuned, while paused): by delta_ms
        (e.g. -60000 for a minute back) or to to_ms. ValueError if no book."""
        src = self._source
        if src and src.get("kind") == "episode":
            return self._episode_seek(src, delta_ms, to_ms)
        if not src or src.get("kind") != "book":
            raise ValueError("no audiobook or podcast is on")
        book = self._book(src["key"])
        if book is None:
            raise ValueError("that book isn't in the audiobooks folder")
        now = self.book_now["pos_ms"] if self.book_now and self.book_now["key"] == book.key \
            else self.book_positions.get(book.key)
        target = to_ms if to_ms is not None else now + (delta_ms or 0)
        target = int(max(0, min(target, book.total_ms - 1000)))
        if self.book_now and self.book_now["key"] == book.key and self.on_air and self.on_air.kind == "book":
            self._book_seek = target             # the player picks it up at once
            self.book_now = self._book_state(book, target)
        else:
            self.book_positions.set(book.key, target)
        log.info("audiobook: to %d:%02d of %s", target // 60000, target // 1000 % 60, book.title)
        return self._book_state(book, target)

    def knob_seek(self, clicks: int) -> dict | None:
        """The knob turned while held: back (clicks < 0) or on through what's playing --
        a book or podcast SEEK_BOOK_MS a click, a song SEEK_SONG_MS (within it).
        None if there's nothing to move (a station). Else as seek()."""
        if not clicks:
            return None
        src = self._source
        unit = SEEK_BOOK_MS if src is not None and src["kind"] in ("book", "episode") else SEEK_SONG_MS
        return self.seek(delta_ms=clicks * unit)

    def seek(self, to_ms: int | None = None, delta_ms: int | None = None) -> dict | None:
        """Move through what's playing: to to_ms from its start, or by delta_ms -- a
        book or podcast (as book_seek), or a song: an album's or playlist's (paused
        too: it plays on from there), or the show's (the DJ's time check is worded
        again for the new end). Playing, it jumps at once. None if there's nothing
        to move (a station, a jingle, the DJ talking, nothing on). Else {"pos_ms",
        "total_ms", "path", "ms"}: path and ms are where that is in the file (for a
        snatch of it while paused; path None for a podcast: it's streamed)."""
        src = self._source
        if src is not None and src["kind"] in ("book", "episode"):
            st = self.book_seek(delta_ms=delta_ms, to_ms=to_ms)
            total = st.get("total_ms", 0)
            if src["kind"] == "episode":
                return {"pos_ms": st["pos_ms"], "total_ms": total, "path": None, "ms": st["pos_ms"]}
            book = self._book(src["key"])
            i, into = book.at(st["pos_ms"])
            ch = book.chapters[i]
            return {"pos_ms": st["pos_ms"], "total_ms": total, "path": ch.path, "ms": ch.start_ms + into}
        if src is not None and src["kind"] not in ("album", "playlist"):
            return None
        on_air = self.on_air
        if src is not None:                      # an album's or playlist's song: playing or paused
            album = self._source_album(src)
            if album is None:
                return None
            t = album["tracks"][min(src.get("track", 0), len(album["tracks"]) - 1)]
            path = t.path
        else:                                    # the show: only a song that's on now
            if on_air is None or on_air.kind != "track" or not self.is_on_air or self._file_pos[0] is None:
                return None
            path = self._file_pos[0]
        here = self._file_pos[0] == path
        scan = self.scans.get(path)
        trim = self._file_trim if here else (scan.start_ms if scan else 0)
        length = int(((on_air.duration_s if here and on_air is not None and on_air.duration_s else 0)
                      or (scan.playable_ms / 1000 if scan else 0)
                      or (self.track_seconds(BroadcastTrack(path, "", "")) or 0)) * 1000)
        now = self._file_seek if self._file_seek is not None and here else \
            (self._file_pos[1] if here else trim + (src or {}).get("offset_ms", 0))
        now = now - trim                         # (from the song's start)
        target = int(to_ms if to_ms is not None else now + (delta_ms or 0))
        target = max(0, min(target, length - 1000) if length else target)
        playing = here and on_air is not None and on_air.kind == "track" and self.is_on_air \
            and not self.speaker_paused()
        if playing or (here and src is None):
            self._file_seek = trim + target
        else:
            with self._lock:
                src["offset_ms"] = trim + target
            if here:                             # (waiting, paused, inside it: it picks this up on play)
                self._file_seek = trim + target
                self._file_pos = (path, trim + target)
            self._notify_source()
        log.info("seek: %s to %d:%02d", path.stem, target // 60000, target // 1000 % 60)
        return {"pos_ms": target, "total_ms": length, "path": path, "ms": trim + target}

    def position(self) -> dict | None:
        """Where the song, book or podcast playing is, for the page's bar:
        {"pos_ms", "total_ms"}; None if it can't be moved (a station, a jingle,
        the DJ talking, nothing on)."""
        src = self._source
        if src is not None and src["kind"] in ("book", "episode"):
            now = self.book_now or {}
            return {"pos_ms": int(now.get("pos_ms", 0)), "total_ms": int(now.get("total_ms") or 0)} if now else None
        on_air = self.on_air
        path, pos = self._file_pos
        if on_air is None or on_air.kind != "track" or path is None:
            if src is not None and src["kind"] in ("album", "playlist") and "offset_ms" in src:
                return {"pos_ms": int(src["offset_ms"]), "total_ms": 0}
            return None
        return {"pos_ms": int(max(0, pos - self._file_trim)), "total_ms": int((on_air.duration_s or 0) * 1000)}

    def snatch(self, path: Path, ms: int, length_ms: int = 300) -> np.ndarray | None:
        """A moment of a file from ms (levelled as it would play, faded in and out):
        where the knob's press-and-turn has got to, heard while paused."""
        scan = self.scans.get(path) or pcm.NO_SCAN
        blocks = []
        for block in pcm.decode(path, int(ms), int(ms + length_ms)):
            blocks.append(pcm.apply_gain(block, scan.gain))
        if not blocks:
            return None
        audio = np.concatenate(blocks).astype(np.float32)
        n = min(len(audio) // 2, int(pcm.SAMPLE_RATE * 0.03))
        if n:
            ramp = np.linspace(0, 1, n, dtype=np.float32)[:, None]
            audio[:n] *= ramp
            audio[-n:] *= ramp[::-1]
        return audio.astype(np.int16)

    def _run_book(self, source: dict) -> None:
        """Read a book from where it was left, chapter by chapter, with no DJ.
        Paused, it stops at once and keeps its place (a minute back after the
        sleep timer); at the end it pauses the radio rather than waking anyone."""
        deadline = time.monotonic() + 120
        book = self._book(source["key"])
        while book is None and self.books_scanning and time.monotonic() < deadline and not self._halted():
            self._write(pcm.silence(0.2))        # still reading the folder (just after start-up)
            book = self._book(source["key"])
        if book is None or not book.chapters:
            with self._lock:
                if self._source is source:
                    self._source = None
                    self.source_error = f"{source.get('title') or source['key']}: not in the audiobooks folder"
            return
        key = book.key
        pos = self.book_positions.get(key)
        if pos >= book.total_ms - 500:
            pos = 0                              # finished last time: from the start
        self._book_seek = None
        leveller = radio_mod.Leveller()
        on_air = self.on_air = OnAir("book", book.title, book.author)
        self.next_track = None
        self.gap_plan = []
        self.history.appendleft({"kind": "book", "text": f"{book.title}" + (f" — {book.author}" if book.author else ""),
                                 "at": time.time()})
        saved_at = time.monotonic()
        try:
            while not self._halted():
                i, into = book.at(pos)
                ch = book.chapters[i]
                self.book_now = self._book_state(book, pos)
                on_air.artist = ch.title if len(book.chapters) > 1 else book.author
                on_air.duration_s = book.total_ms / 1000
                on_air.started = time.time() - pos / 1000
                restart = False
                for block in pcm.decode(ch.path, ch.start_ms + into, ch.end_ms):
                    if self._halted():
                        return
                    if self._book_seek is not None:
                        pos, self._book_seek = self._book_seek, None
                        restart = True
                        break
                    if self._listeners == 0:     # paused: keep the place, and wait
                        back = BOOK_BACK_SLEEP_MS if self.paused_by_sleep() else BOOK_BACK_PAUSE_MS
                        pos = max(0, int(pos) - back)
                        self.book_positions.set(key, pos)
                        self.book_now = self._book_state(book, pos)
                        log.info("audiobook paused: %s, resumes at %d:%02d", book.title, pos // 60000, pos // 1000 % 60)
                        while self._listeners == 0 and not self._halted():
                            time.sleep(0.2)
                        restart = True
                        break
                    self._radio_heard = True     # (music_started: confirms an update)
                    self._write(leveller.process(block))
                    pos += len(block) * 1000 / pcm.SAMPLE_RATE     # (exact: whole ms would drift ~30 s an hour)
                    self.book_now["pos_ms"] = int(pos)
                    if time.monotonic() - saved_at > BOOK_SAVE_S:
                        self.book_positions.set(key, pos)
                        saved_at = time.monotonic()
                if restart:
                    continue
                pos = ch.offset_ms + ch.length_ms          # on to the next chapter
                if i + 1 >= len(book.chapters):
                    log.info("audiobook finished: %s", book.title)
                    self.book_positions.set(key, book.total_ms)
                    self.book_now = self._book_state(book, book.total_ms)
                    if self.on_book_end is not None:
                        self.on_book_end()
                    while not self._halted() and self._listeners > 0:
                        self._write(pcm.silence(0.2))
                    return
        finally:
            if self.book_now and self.book_now["key"] == key and pos < book.total_ms:
                self.book_positions.set(key, pos)
            self.book_now = None

    # --- podcasts ------------------------------------------------------------------------

    @staticmethod
    def _episode_state(src: dict, pos_ms: int) -> dict:
        return {"key": pod_mod.episode_key(src["show"], src["guid"]), "pos_ms": int(pos_ms),
                "total_ms": src.get("duration_ms") or 0, "chapter": 1, "chapters": 1,
                "chapter_title": src.get("show_title", "")}

    def play_episode(self, show: str, guid: str | None = None, start: str | None = None) -> dict:
        """An episode of a show you follow (guid None: the one a podcast button
        would play -- from `start` on, the first not yet heard; without one, the
        one you're part-way through, else the newest unheard)."""
        if guid is None:
            ep = self.podcasts.pick(show, start)
            if ep is None:
                raise ValueError("no episodes yet: is the radio online?")
            guid = ep["guid"]
        self.tune({"kind": "episode", "show": show, "guid": guid})
        return self._source

    def _episode_seek(self, src: dict, delta_ms, to_ms) -> dict:
        key = pod_mod.episode_key(src["show"], src["guid"])
        now = self.book_now["pos_ms"] if self.book_now and self.book_now["key"] == key else self.book_positions.get(key)
        total = (self.book_now or {}).get("total_ms") or src.get("duration_ms") or 0
        target = to_ms if to_ms is not None else now + (delta_ms or 0)
        target = int(max(0, min(target, total - 1000) if total else target))
        if self.book_now and self.book_now["key"] == key and self.on_air and self.on_air.kind == "episode":
            self._book_seek = target
            self.book_now = {**self.book_now, "pos_ms": target}
        else:
            self.book_positions.set(key, target)
        return {**self._episode_state(src, target), "total_ms": total}

    @staticmethod
    def _episode_bytes(url: str) -> int:
        """The episode's size, from a one-byte request (for starting part-way through)."""
        import re as _re
        import urllib.request
        try:
            req = urllib.request.Request(url, headers={"User-Agent": radio_mod.USER_AGENT, "Range": "bytes=0-0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                m = _re.match(r"bytes \d+-\d+/(\d+)", r.headers.get("Content-Range") or "")
                return int(m.group(1)) if m else int(r.headers.get("Content-Length") or 0) if r.status == 200 else 0
        except (OSError, ValueError):
            return 0

    def _run_episode(self, source: dict) -> None:
        """Play a podcast episode from its place, with no DJ: streamed (https is
        fetched in Python), starting part-way through with an HTTP Range for mp3
        (others are skipped forward). Pause, the sleep timer's step back, seeking
        and the end are as for an audiobook; a dropped connection reconnects at
        the same place."""
        key = pod_mod.episode_key(source["show"], source["guid"])
        total = source.get("duration_ms") or 0
        pos = self.book_positions.get(key)
        if self.book_positions.done(key) or (total and pos >= total - 30_000):
            pos = 0                              # heard: from the start
        mp3 = pod_mod.is_mp3(source)
        nbytes = source.get("bytes") or 0
        self._book_seek = None
        leveller = radio_mod.Leveller()
        on_air = self.on_air = OnAir("episode", source["title"], source.get("show_title", ""))
        on_air.duration_s = total / 1000
        self.next_track = None
        self.gap_plan = []
        self.history.appendleft({"kind": "episode", "text": f"{source['title']} — {source.get('show_title', '')}",
                                 "at": time.time()})
        saved_at = time.monotonic()
        last_sound = time.monotonic()
        self.book_now = self._episode_state(source, pos)
        try:
            while not self._halted():
                headers, skip_ms = {}, 0
                if pos > 0 and mp3:
                    nbytes = nbytes or self._episode_bytes(source["url"])
                    if nbytes and total:
                        headers = {"Range": f"bytes={int(nbytes * pos / total)}-"}
                    else:
                        skip_ms = pos
                elif pos > 0:
                    skip_ms = pos
                decoder = list(radio_mod.DECODER)
                if skip_ms:
                    decoder[decoder.index("-i"):decoder.index("-i")] = ["-ss", f"{skip_ms / 1000:.3f}"]
                stream = radio_mod.RadioStream(source["url"], decoder=decoder, headers=headers)
                restart = False
                try:
                    stream.start()
                    playing = False
                    while not self._halted():
                        if self._book_seek is not None:
                            pos, self._book_seek = self._book_seek, None
                            restart = True
                            break
                        if self._listeners == 0:     # paused: keep the place, and wait
                            back = BOOK_BACK_SLEEP_MS if self.paused_by_sleep() else BOOK_BACK_PAUSE_MS
                            pos = max(0, int(pos) - back)
                            self.book_positions.set(key, pos)
                            self.book_now = self._episode_state(source, pos)
                            while self._listeners == 0 and not self._halted():
                                time.sleep(0.2)
                            restart = True
                            break
                        filling = not playing and stream.buffered_s < RADIO_PREBUFFER_S and not stream.ended
                        block = None if filling else stream.read(0.05)
                        if block is None:
                            playing = False
                            if stream.ended and not stream.buffered_s:
                                break
                            if time.monotonic() - last_sound > RADIO_GIVE_UP_S:
                                with self._lock:
                                    if self._source is source:
                                        self._source = None
                                        self.source_error = f"{source['title']}: {stream.ended or 'no sound from the podcast'}"
                                return
                            self._write(pcm.silence(0.05))
                            continue
                        playing = True
                        last_sound = time.monotonic()
                        self._radio_heard = True
                        self._write(leveller.process(block))
                        pos += len(block) * 1000 / pcm.SAMPLE_RATE
                        if not total and stream.total_bytes:  # no length in the feed: from the size (~128 kbps)
                            total = stream.total_bytes * 8 // 128
                        total = max(total, int(pos)) if total else total   # (feeds' lengths can be a little short)
                        self.book_now = {**self._episode_state(source, pos), "total_ms": total}
                        on_air.duration_s = total / 1000
                        if time.monotonic() - saved_at > BOOK_SAVE_S:
                            self.book_positions.set(key, pos)
                            saved_at = time.monotonic()
                finally:
                    stream.close()
                if restart or self._halted():
                    continue
                if total and pos < total - 60_000:   # cut off mid-episode: carry on from here
                    log.info("podcast: %s broke off at %d:%02d; reconnecting", source["title"],
                             int(pos) // 60000, int(pos) // 1000 % 60)
                    deadline = time.monotonic() + RADIO_RETRY_S
                    while time.monotonic() < deadline and not self._halted():
                        self._write(pcm.silence(0.1))
                    continue
                log.info("podcast finished: %s", source["title"])
                self.book_positions.set(key, int(pos), done=True)
                self.book_now = None
                nxt = self.podcasts.next_after(source["show"], source["guid"])
                if nxt is not None:                   # on to the next newer episode, in order
                    log.info("podcast: on to %s", nxt["title"])
                    self.tune({"kind": "episode", "show": source["show"], "guid": nxt["guid"]})
                    return
                if self.on_book_end is not None:      # that was the latest: pause, as for a book
                    self.on_book_end()
                while not self._halted() and self._listeners > 0:
                    self._write(pcm.silence(0.2))
                return
        finally:
            if self.book_now and self.book_now["key"] == key:
                self.book_positions.set(key, int(pos))
            self.book_now = None

    def play_book(self, key: str) -> dict:
        self.tune({"kind": "book", "key": key})
        return self._source

    def book_list(self) -> list[dict]:
        """Every book, with where it was left; the most recently listened first."""
        out = []
        for b in self.books.all():
            pos = self.book_positions.get(b.key)
            out.append({"key": b.key, "title": b.title, "author": b.author, "total_ms": b.total_ms,
                        "pos_ms": pos, "chapters": len(b.chapters), "at": self.book_positions.when(b.key)})
        out.sort(key=lambda b: (-b["at"], b["title"].lower()))
        return out

    # --- the library changed (the web page's music manager) ---------------------------

    def reload_library(self, kinds: set[str] | None = None) -> dict:
        """Rescan the libraries that changed -- kinds: "music", "ondemand",
        "audiobooks", "jingles"; None: all (only new or changed files' tags are
        read) -- keeping the artist or list playing; songs lined up whose files
        have gone are dropped. The song on air carries on."""
        kinds = set(kinds) if kinds else {"music", "ondemand", "audiobooks", "jingles"}
        songs = bool(kinds & {"music", "ondemand"})
        tracks = scan_music(self.music_dir, self._tag_cache) if "music" in kinds else self.tracks
        ondemand = self._scan_ondemand() if "ondemand" in kinds else self.ondemand_tracks
        if "audiobooks" in kinds:
            self._scan_books()
        elif "ondemand" in kinds:
            self.kept.scan()                     # (their titles come from the tags, which may have been changed)
        with self._lock:
            self.tracks = tracks
            self.ondemand_tracks = ondemand
            if songs:
                self._album_index = None
                self._path_index = None
            self._use_selection(profile=self.profile)
            if "jingles" in kinds:
                self._load_jingles(force=True)
            jingles = self.jingles
            requested = list(self._queue)[:self._n_requested]
            self._n_requested = sum(1 for t in requested if t.path.exists())
            self._queue = deque(t for t in self._queue if t.path.exists())
            self._pending = deque(t for t in self._pending if t.path.exists())
            if self._opening is not None and not self._opening[2].path.exists():
                self._opening = None
        gone = self._source_gone(kinds)
        if gone:                                 # what was on (or paused) has been deleted: back to the show
            log.info("streaming: %s is no longer in the library", gone)
            self.tune(None)
        if self._opening is None and not self.is_on_air:
            self._prepare_opening()
        log.info("library reloaded (%s): %d tracks, %d jingles, %d audiobooks", ", ".join(sorted(kinds)),
                 len(tracks), len(jingles), len(self.books.all()))
        return {"tracks": len(tracks), "jingles": self._jingle_count(), "books": len(self.books.all()),
                "ondemand": len(ondemand)}

    def _source_gone(self, kinds: set[str]) -> str | None:
        """After a rescan: the name of what's playing instead of the show (a book,
        an album, a folder, a track or playlist of tracks) if its files have all
        gone -- deleted, or moved away -- else None. It would otherwise stay as
        "what's on" until played, and then fail."""
        src = self._source
        if src is None:
            return None
        try:
            if src["kind"] == "book":
                kept = src["key"].startswith(KEPT)
                if ("ondemand" if kept else "audiobooks") in kinds and self._book(src["key"]) is None:
                    return src.get("title") or src["key"]
            elif src["kind"] == "album" and src.get("root", "music") in kinds:
                if self._album_by_folder(src["folder"], src.get("root", "music"), src.get("deep", False)) is None:
                    return src.get("title") or src["folder"]
            elif src["kind"] == "playlist" and kinds & {"music", "ondemand"}:
                refs = src.get("refs") or []
                if refs and not any(self._by_ref(*r) is not None for r in refs):
                    return src.get("name") or "the playlist"
        except (KeyError, TypeError, ValueError):
            pass
        return None

    def _jingle_count(self) -> int:
        """How many jingles there are: loaded only while jingles are on, so counted from
        the folder otherwise (the page said "0 jingles" with 35 in the folder)."""
        if self.jingles:
            return len(self.jingles)
        cached = getattr(self, "_jingle_n", None)
        if cached is not None and time.monotonic() - cached[0] < 60:     # (asked every few seconds by the pages)
            return cached[1]
        from .library import _audio_files
        try:
            folder = self.jingles_folder()
            n = len(_audio_files(folder)) if folder else 0       # (this station's)
        except OSError:
            n = 0
        self._jingle_n = (time.monotonic(), n)
        return n

    # --- artist radio ------------------------------------------------------------------

    def artists(self) -> list[dict]:
        """Every artist in the library with its track count, by name. Spellings
        that differ only in case or a leading "The" count as one artist."""
        groups: dict[str, Counter] = {}
        for t in self.tracks:
            if t.artist.strip():
                groups.setdefault(artist_key(t.artist), Counter())[t.artist.strip()] += 1
        out = [{"name": c.most_common(1)[0][0], "tracks": sum(c.values())} for c in groups.values()]
        return sorted(out, key=lambda a: artist_key(a["name"]))

    def _use_selection(self, profile: str | None = None) -> bool:
        """Switch the selector (and the DJ's station name) to a theme (None: the
        theme last played, else Default). False, and back to that, if the library
        has nothing to play for it (or there's no such theme). (Artist radio, one
        artist on its own, is retired: an artist is a one-artist theme now.)"""
        found, pool, name, artist = True, self.tracks, artist_station_name(None), None
        if not profile:
            profile = self._fallback_theme()  # (no default station: the theme last played, else the first)
        if profile:
            match = next((p for p in self.profiles if p["name"].lower() == profile.lower()), None)
            keys = {artist_key(a) for a in match["artists"]} if match else set()
            pool = self.tracks if match and match.get("all") else [t for t in self.tracks if artist_key(t.artist) in keys]
            profile = match["name"] if match else profile
            name = profiles_mod.station_name(profile)
        if profile and not pool:
            fb = self._fallback_theme()
            if fb and fb.lower() != (profile or "").lower() and any(
                    p["name"] == fb and (p.get("all") or p["artists"]) for p in self.profiles):
                log.warning("nothing to play for %r; playing %s", profile, fb)
                self._use_selection(profile=fb)
                return False
            log.warning("nothing to play for %r; playing everything", profile)
            found, artist, profile, pool, name = False, None, None, self.tracks, artist_station_name(None)
        self.artist, self.profile = (None, profile) if profile else (artist, None)
        if profile:
            self._last_theme = profile
        self.selector = BroadcastSelector(pool)
        self.builder.station = name
        if hasattr(self, "base"):             # (not while starting up: they come after)
            self._apply_settings()
        if hasattr(self, "_scan_pool"):       # (not while starting up: _load_jingles comes after)
            self._load_jingles()
        return found

    def _fallback_theme(self) -> str | None:
        """Going "back to the show" (after a station, or artist radio) is the theme last
        played, else Default (always there)."""
        names = [p["name"] for p in self.profiles]
        last = getattr(self, "_last_theme", None)
        if last and last.lower() in (n.lower() for n in names):
            return last
        return next((n for n in names if n.lower() == profiles_mod.DEFAULT.lower()), names[0] if names else None)

    def set_artist(self, artist: str | None) -> bool:
        """Play an artist: as a one-artist theme (theme_for_artist; artist radio is
        retired). None: the theme last played, else Default. False if the library
        has nothing by that artist (and nothing changes)."""
        if not artist:
            return self.set_profile(None)
        name = self.theme_for_artist(artist)
        return self.set_profile(name) if name else False

    def theme_for_artist(self, artist: str) -> str | None:
        """The theme that is just this artist, made if there isn't one ("The
        Beatles" -> a theme "Beatles", announced "Beatles Radio", as artist radio
        was). None if the library has nothing by them."""
        key = artist_key(artist)
        for p in self.profiles:
            if not p.get("all") and len(p["artists"]) == 1 and artist_key(p["artists"][0]) == key:
                return p["name"]
        tracks = [t for t in self.tracks if artist_key(t.artist) == key]
        if not tracks:
            return None
        named = Counter(t.artist for t in tracks).most_common(1)[0][0]
        station = artist_station_name(named)
        base = station[:-len(" Radio")] if station.endswith(" Radio") else named
        name, n = base, 2
        while any(p["name"].lower() == name.lower() for p in self.profiles):
            name, n = f"{base} {n}", n + 1
        self.set_profiles([*self.profiles, {"name": name, "artists": [named]}])
        log.info("a theme for %s: %s", named, name)
        return name

    def set_profile(self, profile: str | None) -> bool:
        """Play only the artists on this profile (None = everything: the all-my-music
        theme, if there is one)."""
        profile = profile or self._fallback_theme()
        if self._already(profile):
            return True
        return self._reselect(profile=profile)

    def _already(self, profile: str | None) -> bool:
        """Is that the choice already playing (or lined up)? Then nothing changes:
        a preset button for the show that's on mustn't throw away its welcome,
        which may be half made (slow on a Zero)."""
        return (profile or "").lower() == (self.profile or "").lower() \
            and (self._in_music or self._opening is not None)

    def rename_profile(self, old: str, new: str) -> str:
        """Rename a theme: its messages, and the theme playing, follow it (its jingles
        folder, buttons and programmes: the caller's). Returns the new name.
        ValueError if there's no such theme or the name can't be used."""
        cur = next((p for p in self.profiles if p["name"].lower() == old.lower()), None)
        if cur is None:
            raise ValueError(f"there's no theme called {old}")
        if profiles_mod.DEFAULT.lower() in (cur["name"].lower(), " ".join(str(new).split()).lower()):
            raise ValueError(f"{profiles_mod.DEFAULT} is always there, as it is: give another theme a different name")
        return self._rename(cur, new)

    def _rename(self, cur: dict, new: str) -> str:
        """(rename_profile, and Default's making) the renaming itself."""
        old = cur["name"]
        new = " ".join(str(new).split())
        lists = profiles_mod.validate([{**p, "name": new} if p is cur else p for p in self.profiles])
        cfg = self.messages.cfg
        self.messages.set({**cfg, "list": [{**m, "station": new} if (m.get("station") or "").lower() == old.lower() else m
                                           for m in cfg["list"]]})
        playing = (self.profile or "").lower() == cur["name"].lower()
        self.profiles = lists
        if playing:
            with self._lock:
                self.profile = new
                self.builder.station = profiles_mod.station_name(new)
        log.info("theme renamed: %s -> %s", cur["name"], new)
        return new

    def follow_artist(self, old: str, new: str) -> bool:
        """A song's artist tag was changed from old to new. Themes choose their
        songs by the artist's name, so every theme that plays old plays new too:
        the song stays in the themes it was in. True if a theme changed."""
        ko, kn = artist_key(old or ""), artist_key(new or "")
        if not ko or not kn or ko == kn:
            return False
        self._renamed_artists.add(ko)
        changed, profiles = False, []
        for p in self.profiles:
            keys = {artist_key(a) for a in p["artists"]}
            if ko in keys and kn not in keys:
                p, changed = {**p, "artists": [*p["artists"], new]}, True
            profiles.append(p)
        if changed:
            self.set_profiles(profiles)
        return changed

    def retire_artists(self) -> bool:
        """After the rescan: the old names of artists whose tags were changed, if
        no song has them any more, leave the themes. True if a theme changed."""
        olds, self._renamed_artists = self._renamed_artists, set()
        gone = olds - {artist_key(t.artist) for t in self.tracks}
        if not gone:
            return False
        profiles = [{**p, "artists": [a for a in p["artists"] if artist_key(a) not in gone]} for p in self.profiles]
        if [p["artists"] for p in profiles] == [p["artists"] for p in self.profiles]:
            return False
        self.set_profiles(profiles)
        return True

    def set_profiles(self, profiles: list[dict]) -> None:
        """Replace the profiles (validated; ValueError if wrong). If the one
        playing was changed it's re-applied; if it was removed, everything plays."""
        self.profiles = self.complete_settings(profiles_mod.validate(profiles))
        if self.profile:
            still = any(p["name"].lower() == self.profile.lower() for p in self.profiles)
            self._reselect(profile=self.profile if still else None)
        self._apply_settings()                # (the playing theme's own may have changed)
        if self.on_profiles is not None:      # (main: each new theme gets its jingles folder)
            self.on_profiles()

    def _reselect(self, profile: str | None = None) -> bool:
        with self._lock:
            # A theme is a station: what it plays next is its own. Mid-show, the song
            # lined up next is kept only if the DJ is already announcing it (the gap
            # has started) or it's on the new station anyway; otherwise another is
            # picked and the gap's talk re-worded for it. (It was always kept: White
            # Stripes on "Carisbrooke Radio", lined up in a moment on everything.)
            # Off air, or a station / album / book on instead, nothing from the old
            # choice is kept: not the lined-up song, not the prepared opening.
            # Requests always stay.
            in_show = self._in_music
            was_next = self._queue[0] if self._queue else None
            before = (self.profile or "").lower()
            found = self._use_selection(profile)
            # Another station, mid-show: it takes over now, like turning the dial -- the
            # song playing stops and the new one opens (its welcome if made, else a short
            # jingle of its own, else straight into its music). (It used to wait for the
            # song to end: a theme dropped on the radio seemed to do nothing.)
            retune = in_show and before != (self.profile or "").lower()
            if retune:
                in_show = False                   # (nothing lined up is kept, as off air)
                self._retune = True
            keep = self._n_requested
            if in_show and len(self._queue) > keep:
                lined = self._queue[keep]
                if self._in_gap or any(t.path == lined.path for t in self.selector.pool):
                    keep += 1
            while len(self._queue) > keep:
                self._queue.pop()
            if in_show and not self._in_gap:
                self._refill()
                if self._queue and self._queue[0] is not was_next and self._gap_decision is not None:
                    self._replan_gap(self._queue[0])
            if not in_show:
                if self._opening is not None:     # its words needn't be made now
                    for step in self._opening[1]:
                        if step.kind == "say":
                            step.speech.future.cancel()
                self._opening = None
        log.info("now playing from: %s (%s)%s", self.profile or self.artist or "everything", self.builder.station,
                 "; over to it now" if retune else "")
        if retune:
            self._switch.set()                    # (the show thread opens the new station)
        elif not in_show and self.tracks:
            self._prepare_opening()
        return found

    def _prepare_opening(self) -> None:
        """Pick the first track and synthesise the welcome while idle, so tuning in
        starts at once. Rebuilt if the greeting's time of day has changed."""
        if not self.tracks:
            return
        first = self._take_next()
        self._opening_requested = self._took_request
        greeting = self.builder.welcome_greeting(time_known=clock_trusted())
        steps: list[Step] = []
        startup = [j for j in self.jingles if 0 < j.duration_s < STARTUP_JINGLE_MAX_S]
        album = self._album_start(None, first)
        opener = self.builder.album_intro(album, first) if album else self.builder.welcome_first_track(first)
        if self._has_voice and self.dj_on:
            if startup:
                steps = [Step("say", self._say(greeting)),
                         Step("jingle", jingle=random.choice(startup)),
                         Step("say", self._say(opener))]
            else:
                steps = [Step("say", self._say(f"{greeting} {opener}"))]
        elif startup:
            steps = [Step("jingle", jingle=random.choice(startup))]
        self._opening = (greeting, steps, first)

    def _take_opening(self) -> tuple[list[Step], BroadcastTrack]:
        opening = self._opening
        # Remade only if its greeting is now wrong ("Good evening" after 10 pm). A
        # "Hello" (made before the clock was set, or at night) suits any time: remaking
        # it held up every start-up by a line's worth of synthesis on a Zero.
        if opening is None or (opening[0] != self.builder.welcome_greeting(time_known=clock_trusted())
                               and not opening[0].startswith("Hello,")):
            if opening is not None:
                self._queue.appendleft(opening[2])
            self._prepare_opening()
            opening = self._opening
        self._opening = None
        self._last_opening = opening
        return opening[1], opening[2]

    # --- status ----------------------------------------------------------------------

    def _source_status(self, on_air: OnAir | None) -> dict | None:
        src = self._source
        if src is None:
            return None
        if src["kind"] == "episode":
            key = pod_mod.episode_key(src["show"], src["guid"])
            now = self.book_now if self.book_now and self.book_now["key"] == key else \
                self._episode_state(src, self.book_positions.get(key))
            return {**src, **now, "playing": on_air is not None and on_air.kind == "episode"}
        if src["kind"] == "book":
            now = self.book_now if self.book_now and self.book_now["key"] == src["key"] else None
            if now is None:                      # not playing (paused): where it was left
                book = self._book(src["key"])
                now = self._book_state(book, self.book_positions.get(src["key"])) if book else {}
            return {**src, **now, "playing": on_air is not None and on_air.kind == "book"}
        if src["kind"] == "radio":
            return {**src, "title": self.radio_title,
                    "playing": self.radio_playing and on_air is not None and on_air.kind == "radio"}
        album = self._source_album(src)
        if src["kind"] == "playlist":
            src = {k: v for k, v in src.items() if k != "refs"}
        return {**src, "tracks": len(album["tracks"]) if album else 0,
                "playing": on_air is not None and on_air.kind == "track"}

    def status(self) -> dict:
        on_air = self.on_air
        nxt = self.next_track
        news = self._news_ready
        from sleepradiopi.config import brand
        return {
            "radio_name": brand.name,
            "on_air": self.is_on_air,
            "dj_on": self.dj_on,
            "listeners": self._listeners,
            # who is talking over the programme just now, in a room that's monitored: {"call", "room"} (playback/monitor.py)
            "over": self.monitor.status()["talking"] if self.monitor is not None else None,
            "now": None if on_air is None else {
                "kind": on_air.kind, "title": on_air.title, "artist": on_air.artist,
                "album": on_air.album, "elapsed_s": round(time.time() - on_air.started, 1),
                "duration_s": round(on_air.duration_s, 1),
            },
            "position": self.position(),         # (a song, book or podcast that can be moved through)
            "next": None if nxt is None or (self._source or {}).get("kind") in ("radio", "book", "episode")
            else {"title": nxt.title, "artist": nxt.artist},
            "gap_plan": self.gap_plan,
            "can_skip": self.is_on_air and on_air is not None and on_air.kind not in ("radio", "book", "episode"),
            "can_previous": self.is_on_air and on_air is not None
            and on_air.kind not in ("radio", "book", "episode", "wait")
            and (self._source is None or self._source.get("kind") in ("album", "playlist")),
            "news_ready": None if news is None else news.due.mark.strftime("%H:%M"),
            "history": list(self.history),
            "library": {"tracks": len(self.tracks), "jingles": self._jingle_count(), "books": len(self.books.all()),
                        "ondemand": len(self.ondemand_tracks)},
            "artist": self.artist,
            "profile": self.profile,
            "requests": self.requests(),
            "album": self.album_status(),
            "up_next_source": self.next_source_status(),
            "playlist": self.playlist_status(),
            "source": self._source_status(on_air),
            "source_error": self.source_error,
            "station_name": self.builder.station,
            "voices_ready": bool(self.tts and self.tts.ready),
            "voice": {"name": self.dj_voice, "rss_mb": self.tts.last_rss_mb, "restarts": self.tts.restarts}
            if self.tts else None,
        }
