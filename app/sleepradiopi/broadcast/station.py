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
from typing import Protocol

import numpy as np

from sleepradiopi.audio import pcm
from sleepradiopi.config.clock import clock_trusted
from sleepradiopi.tts.worker import TtsWorker

from .library import scan_jingles, scan_music
from .models import BroadcastConfig, BroadcastTrack, Chattiness, JingleClip, LinkKind
from .news import DueNews, NewsRepository, NewsSchedule, QuietHours, build_bulletin_body, bulletin_time_line
from . import profiles as profiles_mod
from .birthdays import BirthdayWishes, wish_text
from .script_builder import DjScriptBuilder, ShowClock, artist_station_name
from .selector import BroadcastSelector, HookPool, parse_hooks

log = logging.getLogger(__name__)

LOOKAHEAD = 3             # tracks picked (and loudness-scanned) ahead
PREFETCH_S = 45.0         # word the time check / news time line this long before a track ends
                          # (room for a TTS worker recycle, ~15 s, to finish first)
STARTUP_JINGLE_MAX_S = 20.0   # the opening ident: longer ones (most jingles) wait for the first gap,
                              # so the first song isn't held back after a slow start-up
SPEECH_PAD_S = 0.25       # breath of silence either side of the DJ
SPEECH_WAIT_S = 45.0      # give up on a line that still isn't synthesised after this
PER_LINE_ESTIMATE_S = 4.0  # rough length of a spoken line, for wording a clock after one


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

    def describe(self) -> str:
        if self.kind == "say":
            return f'say("{self.speech.text}")'
        return self.kind


@dataclass
class OnAir:
    kind: str       # "track" | "dj" | "jingle" | "news"
    title: str
    artist: str = ""
    album: str = ""
    started: float = field(default_factory=time.time)
    duration_s: float = 0.0


def _natural(name: str) -> list:
    """'10 - x' after '9 - x': digits compare as numbers."""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", name)]


def artist_key(artist: str) -> str:
    """"The Beatles", "Beatles", "beatles " -> "beatles"."""
    key = " ".join(artist.lower().split())
    return key[4:] if key.startswith("the ") and len(key) > 4 else key


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

        self.tracks = scan_music(self.music_dir, cfg.get("tag_cache"))
        self.selector = BroadcastSelector(self.tracks)
        self.artist: str | None = None      # artist radio: only this artist's tracks
        self.profile: str | None = None     # ...or only the artists on this list
        try:
            self.profiles: list[dict] = profiles_mod.validate(cfg.get("profiles") or [])
        except ValueError as e:              # a hand-edited config: don't stop the station
            log.warning("profiles ignored: %s", e)
            self.profiles = []
        self.jingles = scan_jingles(self.jingles_dir) if self.config.jingle_every else []
        self._jingle_paths = {j.path for j in self.jingles}
        self._jingle_bag: deque[JingleClip] = deque()
        self._last_jingle: JingleClip | None = None

        self._tts_pool = ThreadPoolExecutor(1, thread_name_prefix="speech")
        self._scan_pool = ThreadPoolExecutor(1, thread_name_prefix="scan")
        self._scan_futures: dict[Path, Future] = {}
        for j in self.jingles:  # a handful of short files: scan them all once, up front
            self._scan(j.path)

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
        self._plan: list[Step] = []
        self._gap_decision = None
        self._in_gap = False
        self._n_requested = 0                # requests at the front of the queue
        self._pending: deque[BroadcastTrack] = deque()   # requests made during a gap
        self._albums: list[dict] = []        # album runs being played start to finish
        self._album_index: list[dict] | None = None
        self._gap_is_album = False           # this gap is between two tracks of an album
        self.current_track: BroadcastTrack | None = None
        self._took_request = False
        self._opening_requested = False      # the prepared opening's first song was asked for
        self._skip_for: OnAir | None = None

        self.on_air: OnAir | None = None
        self.next_track: BroadcastTrack | None = None
        self.gap_plan: list[str] = []
        self.history: deque[dict] = deque(maxlen=12)
        self.shows_started = 0
        if cfg.get("broadcast_profile"):
            self._use_selection(profile=cfg["broadcast_profile"])
        elif cfg.get("broadcast_artist"):
            self._use_selection(artist=cfg["broadcast_artist"])
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
        if not self.is_on_air or on_air is None:
            return False
        self._skip_for = on_air
        log.info("skip: %s %s", on_air.kind, on_air.title[:70])
        return True

    def _skipped(self, on_air: OnAir) -> bool:
        return self._skip_for is on_air

    def _run_show(self) -> None:
        self.shows_started += 1
        self._show_clock.reset()
        self._tracks_since_jingle = 0
        self.output.start()
        try:
            if not self.tracks:
                log.error("no music in %s: nothing to broadcast", self.music_dir)
                while not self._stop.is_set():
                    self._write(pcm.silence(0.5))
                return
            steps, first = self._take_opening()
            self._run_steps(steps)
            track = first
            while not self._stop.is_set():
                self._play_track(track)
                if self._stop.is_set():
                    break
                with self._lock:
                    self._in_gap = True       # a request now plays after the announced next song
                self._run_gap()
                with self._lock:
                    track = self._take_next()
                    self._in_gap = False
                    self._place_pending()
        except Exception:
            log.exception("show crashed")
        finally:
            self.output.stop()
            self.on_air = None
            self.gap_plan = []
            log.info("show ended")
            self._prepare_opening()

    # --- output -----------------------------------------------------------------------

    def _write(self, block: np.ndarray) -> None:
        self.output.write(block)

    def _await(self, fut: Future, limit_s: float = SPEECH_WAIT_S):
        """Wait for background work while keeping the stream fed with silence, so a slow
        synthesis is a pause on air rather than a dropped connection."""
        deadline = time.monotonic() + limit_s
        while not fut.done():
            if self._stop.is_set() or time.monotonic() > deadline:
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
        audio = self._await(speech.future)
        if audio is None:
            log.warning("dropped line (not ready): %s", speech.text)
            return True
        on_air = self.on_air = OnAir(kind, speech.text, duration_s=len(audio) / pcm.SAMPLE_RATE)
        self.history.appendleft({"kind": kind, "text": speech.text, "at": time.time()})
        self._write(pcm.silence(SPEECH_PAD_S))
        for block in pcm.blocks(audio):
            if self._stop.is_set():
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
                out.append({"id": i, "title": t.title, "artist": t.artist, "album": t.album})
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

    # --- albums: played start to finish -----------------------------------------------

    def albums(self) -> list[dict]:
        """The library's albums: one per folder (Artist/Album/NN - Title), its
        tracks in file order. Cached (the library doesn't change while running)."""
        if self._album_index is None:
            folders: dict[Path, list[BroadcastTrack]] = {}
            for t in self.tracks:
                folders.setdefault(t.path.parent, []).append(t)
            index = []
            for folder, tracks in folders.items():
                tracks.sort(key=lambda t: _natural(t.path.name))
                titles = Counter(t.album.strip() for t in tracks if t.album.strip())
                artists = Counter(t.artist.strip() for t in tracks if t.artist.strip())
                artist, n = artists.most_common(1)[0] if artists else ("", 0)
                index.append({"title": titles.most_common(1)[0][0] if titles else folder.name,
                              "artist": artist if n >= 0.6 * len(tracks) else "Various artists",
                              "tracks": tracks})
            index.sort(key=lambda a: (artist_key(a["artist"]), a["title"].lower()))
            for i, a in enumerate(index):
                a["id"] = i
            self._album_index = index
        return self._album_index

    def search_albums(self, query: str, limit: int = 20) -> list[dict]:
        words = query.lower().split()
        if not words:
            return []
        hits = [a for a in self.albums() if all(w in f"{a['title']} {a['artist']}".lower() for w in words)]
        return [{"id": a["id"], "title": a["title"], "artist": a["artist"], "tracks": len(a["tracks"])}
                for a in hits[:limit]]

    def request_album(self, album_id: int) -> dict:
        """Queue a whole album to play next, in order, straight through: the DJ
        introduces it and back-announces it, with nothing in between."""
        albums = self.albums()
        if not 0 <= album_id < len(albums):
            raise ValueError("no such album")
        album = albums[album_id]
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
            if not self._albums:
                return False
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

    def _play_file(self, path: Path, on_air: OnAir, near_end=None) -> None:
        """Play a file to its end (or until skipped). near_end(end_at, again) is called
        once PREFETCH_S before the end -- and again, with again=True, if a skip then
        makes that projected end wrong."""
        scan = self._await(self._scan(path), 60) or pcm.NO_SCAN
        if path not in self._jingle_paths:  # jingles recur; tracks' futures can go
            self._scan_futures.pop(path, None)
        playable_s = scan.playable_ms / 1000
        on_air.duration_s = playable_s
        on_air.started = time.time()
        self.on_air = on_air
        played = 0
        fired = near_end is None
        for block in pcm.decode(path, scan.start_ms, scan.end_ms):
            if self._stop.is_set():
                return
            if self._skipped(on_air):
                if fired and near_end is not None:
                    near_end(datetime.now(), True)
                break
            self._write(pcm.apply_gain(block, scan.gain))
            played += len(block)
            if not fired and playable_s and playable_s - played / pcm.SAMPLE_RATE <= PREFETCH_S:
                fired = True
                near_end(datetime.now() + timedelta(seconds=playable_s - played / pcm.SAMPLE_RATE), False)
        if not fired:
            near_end(datetime.now(), False)

    def _track_started(self, track: BroadcastTrack) -> None:
        self.current_track = track
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
        return {"voice": self.dj_voice, "voices": self.voices(), "chattiness": self.chattiness,
                "chattiness_options": [c.ident for c in Chattiness],
                "dj_hooks": self.builder.hooks is not None, "hooks_available": self._hook_pool is not None,
                "jingle_every": self.config.jingle_every, "jingles_available": self._jingles_available(),
                "news_enabled": self.config.news_enabled}

    def _jingles_available(self) -> bool:
        if self.jingles:
            return True
        folder = Path(self.jingles_dir)
        return folder.is_dir() and any(folder.iterdir())

    def set_dj(self, chattiness: str | None = None, dj_hooks: bool | None = None,
               jingle_every: int | None = None, news_enabled: bool | None = None) -> None:
        """Change the DJ live (from the next gap on). ValueError if a value is wrong."""
        if chattiness is not None:
            c = next((c for c in Chattiness if c.ident == chattiness), None)
            if c is None:
                raise ValueError(f"chattiness must be one of {[c.ident for c in Chattiness]}")
            self.chattiness = c.ident
            self.config.tracks_per_link = c.tracks_per_link
            self.config.announce_every_track = c == Chattiness.MAXIMUM
        if dj_hooks is not None:
            self.builder.hooks = self._hook_pool if dj_hooks else None
            self.config.dj_hooks_enabled = bool(dj_hooks and self._hook_pool)
        if jingle_every is not None:
            if not 0 <= jingle_every <= 50:
                raise ValueError("jingles: 0 (off) to every 50 tracks")
            if jingle_every and not self.jingles:     # off at start-up: find them now
                self.jingles = scan_jingles(self.jingles_dir)
                self._jingle_paths = {j.path for j in self.jingles}
                for j in self.jingles:
                    self._scan(j.path)
            self.config.jingle_every = jingle_every
            self._tracks_since_jingle = 0
        if news_enabled is not None:
            self.config.news_enabled = bool(news_enabled)
        log.info("DJ: chattiness %s, hooks %s, jingles every %s, news %s", self.chattiness,
                 self.builder.hooks is not None, self.config.jingle_every or "off", self.config.news_enabled)

    @property
    def main_mix(self) -> bool:
        """Everything, as opposed to artist radio or a list."""
        return not (self.artist or self.profile)

    def _plan_gap(self, prev: BroadcastTrack, nxt: BroadcastTrack | None) -> list[Step]:
        """onBroadcastTrackStarted: what fills the gap after [prev]. The decisions
        (link or time check, jingle, birthday) are made once here and kept, so a
        request for the next song can re-word the gap without making them again."""
        self._gap_is_album = nxt is not None and self._album_inside(prev, nxt)
        if self._gap_is_album:               # like a record: straight on to the next track
            self._gap_decision = None
            return []
        kind = self._show_clock.on_track_started(datetime.now().time())
        if kind == LinkKind.TIME_CHECK and not clock_trusted():
            kind = LinkKind.LINK   # offline, the clock may be hours out: say no times
        # The jingles say "Sleep Radio": none on artist radio or a list.
        jingle_due = self._jingle_due() and self.main_mix
        people = []
        if self._has_voice:
            now = datetime.now()
            people = self.birthdays.due(now, clock_trusted())
            if people:
                self.birthdays.wished(now)
                log.info("birthday wish planned for %s", ", ".join(p["name"] for p in people))
        self._gap_decision = (kind, jingle_due, people, prev)
        steps = self._build_gap(kind, jingle_due, people, prev, nxt)
        finished = self._album_end(prev)
        if finished is not None:              # its back-announcement is planned: done with it
            self._albums.remove(finished)
        return steps

    def _build_gap(self, kind: LinkKind, jingle_due: bool, people: list, prev: BroadcastTrack,
                   nxt: BroadcastTrack | None) -> list[Step]:
        b, voice = self.builder, self._has_voice
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
        if voice and people:                  # first thing in the gap
            steps.insert(0, Step("say", self._say(wish_text(people, b.station))))
        return steps

    def _replan_gap(self, nxt: BroadcastTrack) -> None:
        """The next song changed (a request) while a track plays: re-word the
        gap's lines for it. Clock, jingle and news steps are kept as they are
        (a time check may already be worded); only the talk is redone."""
        kind, jingle_due, people, prev = self._gap_decision
        old = self._plan
        new = self._build_gap(kind, jingle_due, people, prev, nxt)
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

    def _prefetch_gap(self, plan: list[Step], end_at: datetime, again: bool = False) -> None:
        """PREFETCH_S before the track ends: word anything time-dependent from the real
        end time, so it's synthesised by the time the gap arrives. [again]: the track was
        skipped after that, so throw away what was worded and word it from now."""
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
        offset = 0.0
        for step in plan:
            if step.kind == "say":
                offset += PER_LINE_ESTIMATE_S
            elif step.kind == "clock" and step.clock.speech is None:
                step.clock.speech = self._say(self.builder.time_line((end_at + timedelta(seconds=offset)).time()))
            elif step.kind == "jingle":
                break

    def _run_gap(self) -> None:
        plan = self._plan
        news = self._news_ready
        if news is not None and self.config.news_enabled and not self._gap_is_album:
            due = self.news_schedule.due_at(datetime.now())
            if due is not None and due.key == news.due.key:
                self.news_schedule.mark_read(due)
                self._news_ready = None
                had_jingle = any(s.kind == "jingle" for s in plan)
                plan = [Step("news", news=news)]
                if had_jingle:
                    plan.append(Step("jingle"))
                if self._has_voice:
                    plan.append(Step("say", self._say(self.builder.intro_line(self.next_track))))
                log.info("gap (news): %s", [s.describe() for s in plan])
        self._run_steps(plan)

    def _run_steps(self, steps: list[Step]) -> None:
        for step in steps:
            if self._stop.is_set():
                return
            if step.kind == "say":
                self._speak(step.speech)
            elif step.kind == "clock":
                if step.clock.speech is None:  # no prefetch (unknown track length): word it now
                    step.clock.speech = self._say(self.builder.time_line())
                self._speak(step.clock.speech)
            elif step.kind == "jingle":
                clip = step.jingle or self._next_jingle()
                if clip is not None:
                    name = clip.path.stem.replace("_", " ")
                    self.history.appendleft({"kind": "jingle", "text": name, "at": time.time()})
                    self._play_file(clip.path, OnAir("jingle", name))
            elif step.kind == "news":
                self._read_news(step.news)

    # --- news --------------------------------------------------------------------------

    def _maybe_prepare_news(self) -> None:
        # News is scheduled by the clock (and needs the internet anyway).
        if not (self.config.news_enabled and self.tts is not None and clock_trusted()):
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

    def _read_news(self, news: NewsItem) -> None:
        if news.time_line is None:
            news.time_line = self._say(bulletin_time_line(news.due.mark, datetime.now()),
                                       self.news_voice, self.config.news_speed)
        if self._speak(news.time_line, "news"):  # a skip in the time line skips the whole bulletin
            self._speak(news.body, "news")
        self.news_repo.mark_read(news.headlines)

    # --- opening ----------------------------------------------------------------------

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

    def _use_selection(self, artist: str | None = None, profile: str | None = None) -> bool:
        """Switch the selector (and the DJ's station name) to one artist, one
        profile, or everything. False, and back to everything, if the library
        has nothing to play for it (or there's no such profile)."""
        found, pool, name = True, self.tracks, artist_station_name(None)
        if profile:
            match = next((p for p in self.profiles if p["name"].lower() == profile.lower()), None)
            keys = {artist_key(a) for a in match["artists"]} if match else set()
            pool = [t for t in self.tracks if artist_key(t.artist) in keys]
            profile = match["name"] if match else profile
            name = profiles_mod.station_name(profile)
        elif artist:
            key = artist_key(artist)
            pool = [t for t in self.tracks if artist_key(t.artist) == key]
            name = artist_station_name(artist)
        if (artist or profile) and not pool:
            log.warning("nothing to play for %r; playing everything", profile or artist)
            found, artist, profile, pool, name = False, None, None, self.tracks, artist_station_name(None)
        self.artist, self.profile = (None, profile) if profile else (artist, None)
        self.selector = BroadcastSelector(pool)
        self.builder.station = name
        return found

    def set_artist(self, artist: str | None) -> bool:
        """Play only this artist (None = everything). On air, the track already
        lined up next still plays (the DJ may have introduced it), then the
        new choice. False if the library has nothing by that artist."""
        return self._reselect(artist=artist)

    def set_profile(self, profile: str | None) -> bool:
        """Play only the artists on this profile (None = everything)."""
        return self._reselect(profile=profile)

    def set_profiles(self, profiles: list[dict]) -> None:
        """Replace the profiles (validated; ValueError if wrong). If the one
        playing was changed it's re-applied; if it was removed, everything plays."""
        self.profiles = profiles_mod.validate(profiles)
        if self.profile:
            still = any(p["name"].lower() == self.profile.lower() for p in self.profiles)
            self._reselect(profile=self.profile if still else None)

    def _reselect(self, artist: str | None = None, profile: str | None = None) -> bool:
        with self._lock:
            on_air = self._thread is not None and self._thread.is_alive()
            found = self._use_selection(artist, profile)
            keep = max(1, self._n_requested) if on_air else self._n_requested
            while len(self._queue) > keep:   # keep requests (and on air the announced song)
                self._queue.pop()
            if not on_air:
                self._opening = None
        log.info("now playing from: %s (%s)", self.profile or self.artist or "everything", self.builder.station)
        if not on_air and self.tracks:
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
        startup = [j for j in self.jingles if 0 < j.duration_s < STARTUP_JINGLE_MAX_S] if self.main_mix else []
        album = self._album_start(None, first)
        opener = self.builder.album_intro(album, first) if album else self.builder.welcome_first_track(first)
        if self._has_voice:
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
        if opening is None or opening[0] != self.builder.welcome_greeting():
            if opening is not None:
                self._queue.appendleft(opening[2])
            self._prepare_opening()
            opening = self._opening
        self._opening = None
        return opening[1], opening[2]

    # --- status ----------------------------------------------------------------------

    def status(self) -> dict:
        on_air = self.on_air
        nxt = self.next_track
        news = self._news_ready
        return {
            "on_air": self.is_on_air,
            "listeners": self._listeners,
            "now": None if on_air is None else {
                "kind": on_air.kind, "title": on_air.title, "artist": on_air.artist,
                "album": on_air.album, "elapsed_s": round(time.time() - on_air.started, 1),
                "duration_s": round(on_air.duration_s, 1),
            },
            "next": None if nxt is None else {"title": nxt.title, "artist": nxt.artist},
            "gap_plan": self.gap_plan,
            "can_skip": self.is_on_air and on_air is not None,
            "news_ready": None if news is None else news.due.mark.strftime("%H:%M"),
            "history": list(self.history),
            "library": {"tracks": len(self.tracks), "jingles": len(self.jingles)},
            "artist": self.artist,
            "profile": self.profile,
            "requests": self.requests(),
            "album": self.album_status(),
            "station_name": self.builder.station,
            "voices_ready": bool(self.tts and self.tts.ready),
            "voice": {"name": self.dj_voice, "rss_mb": self.tts.last_rss_mb, "restarts": self.tts.restarts}
            if self.tts else None,
        }
