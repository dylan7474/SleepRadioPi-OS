"""HTTP front end: the radio page, the MP3 stream, and a JSON status feed.

Plain stdlib http.server -- a handful of listeners on a home network needs
nothing more. No auth: meant for a trusted LAN only.
"""

from __future__ import annotations

import hashlib
import json
import logging
from http.cookies import SimpleCookie
import os
import queue
import threading
import time
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from sleepradiopi.broadcast import birthdays, messages, profiles
from sleepradiopi.broadcast import playlists as playlists_mod
from sleepradiopi.broadcast.station import Station
from sleepradiopi.config.auth import COOKIE, SESSION_S, Auth
from sleepradiopi.config.clock import clock_trusted
from sleepradiopi.io.announce import Clip
from sleepradiopi import voices as voices_mod
from sleepradiopi import updater as updater_mod
from sleepradiopi import wifi as wifi_mod
from sleepradiopi.config import backup
from sleepradiopi.config.settings import load as load_settings, save_setting
from sleepradiopi.playback import radio as radio_mod
from sleepradiopi.io import presets as presets_mod
from sleepradiopi import media as media_mod
from sleepradiopi.playback import podcasts as pod_mod
from sleepradiopi.config.power import can_power_off, request_power_off, request_restart

from .stream import Mp3Output

log = logging.getLogger(__name__)

REMOTE = (Path(__file__).parent / "remote.html").read_bytes()   # the phone remote (/), and the Wi-Fi set-up on the hotspot
DESKTOP = (Path(__file__).parent / "desktop.html").read_bytes()   # the new desktop page (for computers)
DESKTOP_TAG = hashlib.sha1(DESKTOP).hexdigest()[:12]   # an open desktop page that sees this change reloads itself
# The speaker analyser (also on GitHub Pages): served here too, for tuning with no internet.
ANALYSER = (Path(__file__).parent / "analyser" / "index.html").read_bytes()
KEY_HELD_MAX_S = 30  # a page's button held longer than this is let go (a lost "up")
IDLE_CLOSE_S = 30  # close a stream connection that has had no audio for this long
MAX_SETTINGS_BYTES = 512 * 1024   # room for a long list of internet radio stations
# Set where something restarts the station when it exits (init's respawn on the
# appliance image, systemd's Restart= on Raspberry Pi OS). Without it, e.g. on
# a desktop, loaded settings that need a restart wait for the next start.
RESTART_ENV = "SLEEPRADIOPI_SUPERVISED"
RESTART_EXIT = 75  # EX_TEMPFAIL: "on-failure" restarts it too


def _restart_soon(speaker) -> None:
    """Exit shortly (after the reply has gone) so the supervisor restarts us."""
    def go():
        time.sleep(1)
        log.info("restarting to apply the loaded settings")
        if speaker is not None:
            speaker.save_now()
        os._exit(RESTART_EXIT)
    threading.Thread(target=go, daemon=True).start()


def _quietly(job) -> None:
    try:
        job()
    except Exception:            # (logged where it failed)
        pass


def make_handler(station: Station, output: Mp3Output, speaker=None,
                 config_file: Path | None = None, announcer=None, voice_jobs=None, updates=None,
                 presets=None, directory=None, media=None, lamps=None):
    auth = Auth(config_file)
    backups = backup.Store(config_file.parent / "backups") if config_file is not None else None
    open_paths = {"/", "/index.html", "/desktop", "/classic", "/api/auth", "/api/login", "/analyser", "/analyser/", "/analyser/index.html"}

    _held: dict = {}                  # the page's buttons held down: keycode -> auto let-go timer
    _held_lock = threading.Lock()

    def _let_go(code: int) -> None:
        with _held_lock:
            if _held.pop(code, None) is None:
                return
        log.info("button key %d: let go (the page never said)", code)
        presets.keys[code][1]()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # --- the optional password (config/auth.py) --------------------------------------

        def _local(self) -> bool:
            return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

        def _token(self) -> str | None:
            try:
                morsel = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE)
            except Exception:
                return None
            return morsel.value if morsel else None

        def _logged_in(self) -> bool:
            return not auth.protected or self._local() or auth.valid(self._token())

        def _gate(self, path: str) -> bool:
            """False (and a 401 sent) if this needs the password and hasn't got it."""
            if path in open_paths or self._logged_in():
                return True
            body = json.dumps({"error": "login"}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False

        def _session_cookie(self, token: str | None) -> tuple[str, str]:
            if token is None:
                return ("Set-Cookie", f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
            return ("Set-Cookie", f"{COOKIE}={token}; Path=/; Max-Age={SESSION_S}; HttpOnly; SameSite=Strict")

        def _auth_state(self) -> dict:
            return {"protected": auth.protected, "logged_in": self._logged_in(), "local": self._local()}

        def _login(self) -> None:
            try:
                password = self._body().get("password")
            except (ValueError, AttributeError):
                password = None
            if not auth.protected:
                self._send(json.dumps(self._auth_state()).encode(), "application/json")
            elif auth.check(password):
                log.info("login from %s", self.address_string())
                self._send(json.dumps({"protected": True, "logged_in": True}).encode(), "application/json",
                           [self._session_cookie(auth.token())])
            else:
                log.warning("wrong password from %s", self.address_string())
                time.sleep(1)                 # slow down guessing
                body = json.dumps({"error": "wrong password"}).encode()
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def _set_password(self) -> None:
            """POST /api/password {"password": "..." | null}: set, change or remove.
            Changing it logs every other browser out; this one gets a new cookie."""
            try:
                password = self._body()["password"]
                auth.set_password(password)
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"password\": ...}")
                return
            log.info("web password %s from %s", "removed" if password is None else "set", self.address_string())
            cookie = self._session_cookie(None if password is None else auth.token())
            self._send(json.dumps(self._auth_state()).encode(), "application/json", [cookie])

        def log_message(self, fmt, *args):  # keep stream polling out of the journal
            if not self.path.startswith("/api/"):
                log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, body: bytes, ctype: str, headers: list[tuple[str, str]] = ()) -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the browser went away mid-reply; nothing to do

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if (not path.startswith("/api/") and path not in ("/", "/index.html", "/desktop", "/classic", "/stream")
                    and not path.startswith("/analyser")
                    and wifi_mod.status().get("mode") == "hotspot"):
                # The radio's own network: a phone checking for internet
                # (e.g. /generate_204) is sent to the page, which it then offers to open.
                self.send_response(302)
                self.send_header("Location", f"http://{wifi_mod.HOTSPOT_IP}/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not self._gate(path):
                return
            if path == "/api/auth":
                self._send(json.dumps(self._auth_state()).encode(), "application/json")
            elif path in ("/", "/index.html"):
                self._send(REMOTE, "text/html; charset=utf-8")
            elif path == "/classic":          # (the classic page is retired: everything is on the desktop)
                self.send_response(302)
                self.send_header("Location", "/desktop")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif path == "/desktop":
                self._send(DESKTOP, "text/html; charset=utf-8")
            elif path == "/analyser":
                self.send_response(301)
                self.send_header("Location", "/analyser/")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif path in ("/analyser/", "/analyser/index.html"):
                self._send(ANALYSER, "text/html; charset=utf-8")
            elif path == "/api/status":
                status = station.status()
                if speaker is not None:
                    status["speaker"] = speaker.status()
                status["can_power_off"] = can_power_off()
                sched = getattr(station, "scheduler", None)
                status["programme"] = sched.status() if sched else None
                status["switches"] = sched.switches_status() if sched else []
                status["programme_mode"] = bool(sched and sched.quiet)
                status["buttons_bank"] = presets.bank if presets is not None else None
                status["stream"] = output is not None and output.enabled
                status["version"] = updater_mod.this_version()
                status["desktop"] = DESKTOP_TAG
                self._send(json.dumps(status).encode(), "application/json")
            elif path == "/api/settings" and config_file is not None:
                self._save_settings()
            elif path == "/api/speaker/sounds" and config_file is not None:
                self._send(json.dumps({"sounds": load_settings(config_file).speaker_sounds}).encode(), "application/json")
            elif path == "/api/backups" and config_file is not None:
                self._send(json.dumps({"backups": backups.list(), "max": backup.MAX_KEPT}).encode(), "application/json")
            elif path == "/api/backups/file" and config_file is not None:
                try:
                    name = backups.name(parse_qs(urlparse(self.path).query).get("name", [""])[0])
                    self._send_settings(backups.read(name), f"{name}.json")
                except backup.BadSettings as e:
                    self._error(str(e))
            elif path == "/api/birthdays":
                self._send(json.dumps(self._birthdays_state()).encode(), "application/json")
            elif path == "/api/messages":
                self._send(json.dumps(self._messages_state()).encode(), "application/json")
            elif path == "/api/identity":
                self._send(json.dumps(self._identity()).encode(), "application/json")
            elif path == "/api/lamps":
                self._send(json.dumps(lamps.settings() if lamps is not None else {"needle": False, "glow": False}).encode(),
                           "application/json")
            elif path == "/api/voices":
                self._send(json.dumps(self._voices_state()).encode(), "application/json")
            elif path == "/api/wifi":
                self._send(json.dumps(self._wifi_state()).encode(), "application/json")
            elif path == "/api/update" and updates is not None:
                self._send(json.dumps(updates.status()).encode(), "application/json")
            elif path == "/api/dj":
                self._send(json.dumps(self._dj_state()).encode(), "application/json")
            elif path == "/api/search":
                q = parse_qs(urlparse(self.path).query).get("q", [""])[0][:200]
                self._send(json.dumps({"results": station.search(q), "albums": station.search_albums(q)}).encode(),
                           "application/json")
            elif path == "/api/playlists":
                self._send(json.dumps(self._playlists()).encode(), "application/json")
            elif path == "/api/programmes" and getattr(station, "scheduler", None) is not None:
                self._send(json.dumps(self._programmes()).encode(), "application/json")
            elif path == "/api/albums":
                self._send(json.dumps({"albums": station.all_albums()}).encode(), "application/json")
            elif path == "/api/browse":
                qs = parse_qs(urlparse(self.path).query)
                try:
                    reply = station.browse(qs.get("root", ["music"])[0], qs.get("path", [""])[0][:1000])
                except ValueError as e:
                    self._error(str(e))
                    return
                self._send(json.dumps(reply).encode(), "application/json")
            elif path == "/api/artists":
                self._send(json.dumps({**self._selection(), "artists": station.artists(),
                                       "profiles": station.profiles}).encode(), "application/json")
            elif path == "/api/buttons" and presets is not None:
                self._send(json.dumps(presets.status()).encode(), "application/json")
            elif path == "/api/media" and media is not None:
                q = parse_qs(urlparse(self.path).query)
                try:
                    listing = media.list(q.get("kind", ["music"])[0], q.get("path", [""])[0])
                except media_mod.MediaError as e:
                    self._error(str(e))
                    return
                self._send(json.dumps(listing).encode(), "application/json")
            elif path == "/api/media/download" and media is not None:
                self._download()
            elif path == "/api/books":
                self._send(json.dumps({"books": station.book_list(), "scanning": station.books_scanning}).encode(),
                           "application/json")
            elif path == "/api/podcasts":
                self._send(json.dumps({"shows": station.podcasts.summary()}).encode(), "application/json")
            elif path == "/api/podcasts/search":
                q = parse_qs(urlparse(self.path).query).get("q", [""])[0][:200]
                try:
                    found = pod_mod.search(q)
                except pod_mod.PodcastError as e:
                    self._error(str(e))
                    return
                self._send(json.dumps({"results": found}).encode(), "application/json")
            elif path == "/api/podcasts/episodes":
                q = parse_qs(urlparse(self.path).query)
                sid = q.get("id", [""])[0]
                if station.podcasts.show(sid) is None:
                    self._error("that podcast isn't followed")
                    return
                note = ""
                if q.get("refresh", ["0"])[0] == "1":
                    try:
                        station.podcasts.refresh(sid)
                    except pod_mod.PodcastError as e:
                        note = str(e)
                self._send(json.dumps({"show": station.podcasts.show(sid), "episodes": station.podcasts.episodes(sid),
                                       "note": note}).encode(), "application/json")
            elif path == "/api/radio":
                self._send(json.dumps(self._radio_state()).encode(), "application/json")
            elif path == "/api/radio/search":
                q = parse_qs(urlparse(self.path).query).get("q", [""])[0][:200]
                try:
                    found, where = directory.search(q) if directory is not None else (radio_mod.search(q), "online")
                except radio_mod.StreamError as e:
                    self._error(str(e))
                    return
                self._send(json.dumps({"results": found, "from": where,
                                       "directory": directory.status() if directory else None}).encode(),
                           "application/json")
            elif path == "/stream":
                if output is None or not output.enabled:
                    self.send_error(404, "listening in a browser is switched off")
                    return
                self._stream()
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if not self._gate(path):
                self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
                return
            if path == "/api/login":
                self._login()
            elif path == "/api/logout":
                self._send(json.dumps({"logged_in": False}).encode(), "application/json",
                           [self._session_cookie(None)])
            elif path == "/api/password":
                self._set_password()
            elif path == "/api/power":
                self._power()
            elif path == "/api/seek":
                try:
                    body = self._body()
                    to_ms, delta_ms = body.get("to_ms"), body.get("delta_ms")
                    for v in (to_ms, delta_ms):
                        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                            raise ValueError("send {\"to_ms\": n} or {\"delta_ms\": n}")
                    if (to_ms is None) == (delta_ms is None):
                        raise ValueError("send {\"to_ms\": n} or {\"delta_ms\": n}")
                    moved = station.seek(to_ms=None if to_ms is None else int(to_ms),
                                         delta_ms=None if delta_ms is None else int(delta_ms))
                except (ValueError, TypeError, AttributeError) as e:
                    self._error(str(e) if isinstance(e, ValueError) else "send {\"to_ms\": n} or {\"delta_ms\": n}")
                    return
                self._send(json.dumps({"moved": moved is not None, "position": station.position()}).encode(),
                           "application/json")
            elif path == "/api/skip":
                self.rfile.read(int(self.headers.get("Content-Length", 0)))  # no body needed
                self._send(json.dumps({"skipped": station.skip()}).encode(), "application/json")
            elif path == "/api/previous":
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self._send(json.dumps({"previous": station.previous()}).encode(), "application/json")
            elif path == "/api/playlists":
                self._set_playlists()
            elif path == "/api/track/play":
                try:
                    body = self._body()
                    reply = station.play_track(body.get("root") or "music", body.get("path"),
                                               then="pause" if body.get("then") == "pause" else None)
                except (ValueError, TypeError, KeyError, AttributeError) as e:
                    self._error(str(e) if isinstance(e, ValueError) else "send {\"root\", \"path\"}")
                    return
                if speaker is not None:
                    speaker.play()
                self._send(json.dumps(reply).encode(), "application/json")
            elif path == "/api/programmes/mode" and getattr(station, "scheduler", None) is not None:
                try:
                    on = self._body()["on"]
                    if not isinstance(on, bool):
                        raise ValueError("on is true or false")
                except (ValueError, TypeError, KeyError, AttributeError) as e:
                    self._error(str(e) if isinstance(e, ValueError) else "send {\"on\": true | false}")
                    return
                station.scheduler.set_quiet(on)
                if config_file is not None:
                    save_setting(config_file, "programme_mode", on)
                self._send(json.dumps(self._programmes()).encode(), "application/json")
            elif path in ("/api/programmes", "/api/programmes/play", "/api/programmes/stop") \
                    and getattr(station, "scheduler", None) is not None:
                self._programmes_post(path)
            elif path == "/api/lengths":
                try:
                    items = self._body()["items"]
                    if not isinstance(items, list) or len(items) > 200:
                        raise ValueError("send up to 200 items")
                    reply = {"minutes": [station.minutes_of(it) for it in items]}
                except (ValueError, TypeError, KeyError, AttributeError) as e:
                    self._error(str(e) if isinstance(e, ValueError) else "send {\"items\": [...]}")
                    return
                self._send(json.dumps(reply).encode(), "application/json")
            elif path == "/api/playlists/rename":
                self._rename_playlist()
            elif path == "/api/playlists/add":
                self._playlist_add()
            elif path == "/api/playlists/play":
                self._playlist_play()
            elif path == "/api/playlists/stop":
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self._send(json.dumps({"stopped": station.stop_playlist(), **self._playlists()}).encode(),
                           "application/json")
            elif path == "/api/speaker" and speaker is not None:
                self._speaker()
            elif path == "/api/speaker/sounds" and config_file is not None:
                try:                             # {"sounds": [...]}: the saved speaker set-ups, the whole list
                    from sleepradiopi.audio.eq import validate_sounds
                    sounds = validate_sounds(self._body().get("sounds"))
                except (ValueError, TypeError, AttributeError) as e:
                    self._error(str(e))
                    return
                save_setting(config_file, "speaker_sounds", sounds)
                self._send(json.dumps({"sounds": sounds}).encode(), "application/json")
            elif path == "/api/settings" and config_file is not None:
                self._load_settings()
            elif path.startswith("/api/backups/") and config_file is not None:
                self._backups(path.rsplit("/", 1)[1])
            elif path == "/api/knob" and speaker is not None:
                self._knob()
            elif path == "/api/station":
                self._station()
            elif path == "/api/birthdays":
                self._set_birthdays()
            elif path == "/api/messages":
                self._set_messages()
            elif path == "/api/identity":
                self._set_identity()
            elif path in ("/api/lamps", "/api/lamps/sweep") and lamps is not None:
                self._set_lamps(path.endswith("sweep"))
            elif path == "/api/messages/hear":
                self._hear_message()
            elif path == "/api/profiles":
                self._set_profiles()
            elif path == "/api/profiles/rename":
                self._rename_profile()
            elif path == "/api/request":
                self._request()
            elif path == "/api/album":
                self._album()
            elif path.startswith("/api/media/") and media is not None:
                self._media(path.rsplit("/", 1)[1])
            elif path in ("/api/buttons", "/api/buttons/press") and presets is not None:
                self._buttons(path.endswith("press"))
            elif path == "/api/buttons/slot" and presets is not None:
                try:                             # {"slot": "knob_long" | "power_on", "preset": {...} | null}
                    body = self._body()
                    presets.set_slot(body["slot"], body["preset"])
                except (ValueError, TypeError, KeyError, AttributeError) as e:
                    self._error(str(e) if isinstance(e, ValueError) else "send {\"slot\", \"preset\"}")
                    return
                self._send(json.dumps(presets.status()).encode(), "application/json")
            elif path == "/api/buttons/key" and presets is not None and presets.keys:
                self._button_key()
            elif path == "/api/instant" and presets is not None:
                try:
                    done = presets.instant(self._body().get("preset"))
                except (ValueError, TypeError, AttributeError) as e:
                    self._error(str(e) if isinstance(e, ValueError) else "send {\"preset\": {...}}")
                    return
                self._send(json.dumps({"done": done}).encode(), "application/json")
            elif path in ("/api/buttons/bank", "/api/buttons/auto") and presets is not None:
                self._button_bank(path.endswith("auto"))
            elif path.startswith("/api/podcasts/") and path.rsplit("/", 1)[1] in ("follow", "unfollow", "play", "heard"):
                self._podcasts(path.rsplit("/", 1)[1])
            elif path in ("/api/books/play", "/api/books/seek"):
                self._books(path.rsplit("/", 1)[1])
            elif path == "/api/album/play":
                self._play_album()
            elif path == "/api/album/stop":
                self._body()
                self._send(json.dumps({"stopped": station.stop_album(), "requests": station.requests()}).encode(),
                           "application/json")
            elif path == "/api/dj":
                self._set_dj()
            elif path == "/api/stream":
                self._set_stream()
            elif path == "/api/radio/directory" and directory is not None:
                self._body()
                if not directory.refreshing:          # fetch a fresh copy now, in the background
                    threading.Thread(target=lambda: _quietly(directory.refresh), name="station-directory",
                                     daemon=True).start()
                self._send(json.dumps({**directory.status(), "refreshing": True}).encode(), "application/json")
            elif path in ("/api/radio/play", "/api/radio/stop", "/api/radio/stations"):
                self._radio(path.rsplit("/", 1)[1])
            elif path == "/api/voices/standard" and voice_jobs is not None:
                self._body()
                try:
                    voice_jobs.download_standard()
                except ValueError as e:
                    self._error(str(e))
                    return
                self._send(json.dumps(self._voices_state()).encode(), "application/json")
            elif path == "/api/voices/upload" and voice_jobs is not None:
                self._upload_voice()
            elif path.startswith("/api/wifi/"):
                self._wifi(path.rsplit("/", 1)[1])
            elif path in ("/api/update/check", "/api/update/install") and updates is not None:
                self._body()
                try:
                    reply = updates.check() if path.endswith("check") else updates.install()
                except (ValueError, OSError) as e:          # incl. no network / GitHub errors
                    self._error(str(e) or e.__class__.__name__)
                    return
                self._send(json.dumps(reply).encode(), "application/json")
            elif path == "/api/birthdays/hear":
                self._hear_birthday()
            else:
                self.send_error(404)

        def _power(self) -> None:
            """/api/power: shut the radio down, or {"restart": true}: restart it.
            Saves the volume and silences the speaker first, so it goes quiet at once."""
            try:
                restart = self._body().get("restart") is True
            except (ValueError, AttributeError):
                restart = False
            if not can_power_off():
                self.send_error(404)
                return
            log.info("%s requested from %s", "restart" if restart else "shutdown", self.address_string())
            if speaker is not None:
                speaker.save_now()
                speaker.pause()
            if restart:
                self._send(json.dumps({"restarting": request_restart()}).encode(), "application/json")
            else:
                self._send(json.dumps({"shutting_down": request_power_off()}).encode(), "application/json")

        def _save_settings(self) -> None:
            """GET /api/settings: the settings as a file to download."""
            data = backup.export(config_file, speaker.volume if speaker is not None else None)
            self._send_settings(data, f'sleepradio-settings-{time.strftime("%Y-%m-%d")}.json')

        def _send_settings(self, data: dict, filename: str) -> None:
            body = (json.dumps(data, indent=2) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _load_settings(self) -> None:
            """POST /api/settings with a saved settings file as the body.
            Volume, mono and EQ change at once; anything else restarts the
            station (if it's supervised). Replies {"ok", "restarting"} or
            {"error"} with status 400."""
            length = int(self.headers.get("Content-Length", 0))
            try:
                if length > MAX_SETTINGS_BYTES:
                    self._drain(length)
                    raise backup.BadSettings("that file is far too big to be a settings file")
                try:
                    data = json.loads(self.rfile.read(length))
                except ValueError:
                    raise backup.BadSettings("this isn't a Sleep Radio settings file") from None
            except backup.BadSettings as e:
                self._error(str(e))
                return
            self._apply_settings(data)

        def _drain(self, length: int, most: int = 16 * 1024 * 1024) -> None:
            """Read and throw away a body that's being refused. Answering while
            the page is still sending makes the send fail (a broken pipe), and
            the page then shows a lost connection instead of the reason. Past
            `most`, give up on that and just close afterwards."""
            left = min(length, most)
            try:
                while left > 0:
                    chunk = self.rfile.read(min(left, 65536))
                    if not chunk:
                        break
                    left -= len(chunk)
            except OSError:
                pass
            if length > most:
                self.close_connection = True

        def _backups(self, action: str) -> None:
            """The settings files the radio keeps (the desktop's Backups folder):
            POST /api/backups/save {"name"?} (the settings as they are now; no
            name: the date and time), /delete {"name"}, /load {"name"} (as
            POST /api/settings does; the settings as they were are kept as
            "Before the last load"), /upload?name=N with a settings file as the
            body (kept, not loaded). GET /api/backups lists them;
            /api/backups/file?name=N downloads one."""
            now = lambda: backup.export(config_file, speaker.volume if speaker is not None else None)
            try:
                if action == "upload":
                    length = int(self.headers.get("Content-Length", 0))
                    if length > MAX_SETTINGS_BYTES:
                        self._drain(length)
                        raise backup.BadSettings("that file is far too big to be a settings file")
                    try:
                        data = json.loads(self.rfile.read(length))
                    except ValueError:
                        raise backup.BadSettings("this isn't a Sleep Radio settings file") from None
                    name = backups.save(data, parse_qs(urlparse(self.path).query).get("name", [""])[0] or None)
                else:
                    body = self._body()
                    if not isinstance(body, dict):
                        raise backup.BadSettings("send a JSON object")
                    if action == "save":
                        name = backups.save(now(), body.get("name"))
                    elif action == "delete":
                        name = backups.name(body.get("name"))
                        backups.delete(name)
                    elif action == "load":
                        name = backups.name(body.get("name"))
                        data = backups.read(name)
                        backup.parse(data)
                        if name != backup.BEFORE:
                            backups.save(now(), backup.BEFORE, replace=True)
                        self._apply_settings(data)
                        return
                    else:
                        self.send_error(404)
                        return
            except (backup.BadSettings, ValueError, TypeError) as e:
                self._error(str(e))
                return
            self._send(json.dumps({"ok": True, "name": name, "backups": backups.list()}).encode(), "application/json")

        def _apply_settings(self, data: dict) -> None:
            """Take up a settings file's contents (already read): see _load_settings."""
            try:
                settings, volume = backup.parse(data)
            except backup.BadSettings as e:
                self._error(str(e))
                return
            try:
                changed = backup.apply(config_file, settings)     # before the live ones save theirs
            except ValueError as e:                               # (the settings file can't be read: nothing written)
                self._error(str(e))
                return
            dj_keys = {"broadcast_chattiness", "broadcast_dj_hooks", "broadcast_jingle_enabled",
                       "broadcast_jingle_every", "news_enabled", "broadcast_announcer_speed", "news_speed"}
            if changed & dj_keys:
                # (a file from before themes had their own settings: its DJ values are Default's,
                # and any theme's in it that has none)
                every = settings.get("broadcast_jingle_every", station.base["jingle_every"] or 4)
                for key, value in (("chattiness", settings.get("broadcast_chattiness")),
                                   ("dj_hooks", settings.get("broadcast_dj_hooks")),
                                   ("jingle_every", every if settings.get("broadcast_jingle_enabled", True) else 0),
                                   ("news", settings.get("news_enabled")), ("time_checks", settings.get("broadcast_time_checks"))):
                    if value is not None:
                        station.base[key] = value
                station.set_dj(dj_speed=settings.get("broadcast_announcer_speed"), news_speed=settings.get("news_speed"))
            if "profiles" in changed:
                station.set_profiles(settings["profiles"])
            if changed & {"broadcast_artist", "broadcast_profile", "profiles"}:
                if settings.get("broadcast_profile"):
                    station.set_profile(settings["broadcast_profile"])
                else:                         # (an old file's artist radio: its one-artist theme)
                    station.set_artist(settings.get("broadcast_artist"))
                    if config_file is not None:
                        save_setting(config_file, "profiles", station.profiles)
                self._save_selection()
            if "birthdays" in changed:
                station.set_birthdays(settings["birthdays"])
            if "playlists" in changed:
                station.set_playlists(settings["playlists"])
            if "programmes" in changed and getattr(station, "scheduler", None) is not None:
                station.scheduler.set_programmes(settings["programmes"])
            if "programme_mode" in changed and getattr(station, "scheduler", None) is not None:
                station.scheduler.set_quiet(settings["programme_mode"])
            if "knob_mode" in changed and speaker is not None:
                speaker.set_knob_mode(settings["knob_mode"])
            if "messages" in changed:
                station.set_messages(settings["messages"])
            if changed & set(presets_mod.SLOTS) and presets is not None:
                presets.load_slots({k: settings.get(k) for k in presets_mod.SLOTS if k in changed})
            if changed & {"buttons", "buttons_night", "buttons_bank", "buttons_auto"} and presets is not None:
                presets.load_all(settings.get("buttons"), settings.get("buttons_night"),
                                 settings.get("buttons_bank"), settings.get("buttons_auto"))
            if speaker is not None:
                if changed & {"noise_on", "noise_kind", "noise_mix"}:
                    speaker.set_noise(on=settings.get("noise_on"), kind=settings.get("noise_kind"),
                                      mix=settings.get("noise_mix"))
                if "speaker_mono" in settings:
                    speaker.set_mono(settings["speaker_mono"])
                if "speaker_eq" in settings:
                    speaker.set_eq(settings["speaker_eq"])
                if "speaker_highpass_hz" in settings:
                    speaker.set_highpass(settings["speaker_highpass_hz"])
                if volume is not None:
                    if volume != speaker.volume:
                        changed.add("volume")
                    speaker.set_volume(volume)
                    speaker.save_now()
            needs_restart = bool(changed - backup.LIVE - {"volume"})
            restarting = needs_restart and bool(os.environ.get(RESTART_ENV))
            log.info("settings loaded from %s; changed: %s", self.address_string(),
                     ", ".join(sorted(changed)) or "nothing")
            self._send(json.dumps({"ok": True, "changed": sorted(changed),
                                   "needs_restart": needs_restart,
                                   "restarting": restarting}).encode(), "application/json")
            if restarting:
                _restart_soon(speaker)

        def _birthdays_state(self) -> dict:
            trusted = clock_trusted()
            return {"birthdays": station.birthdays.entries,
                    "today": station.birthdays.today(datetime.now()) if trusted else [],
                    "clock_trusted": trusted,
                    "can_hear": speaker is not None and station._has_voice}

        def _set_lamps(self, sweep: bool) -> None:
            """POST /api/lamps {"glow_day", "glow_night" (0-100 %), "meter_trim_db" (-12..12)}:
            saved; /api/lamps/sweep: the needle up to full scale and back (to set its trimmer)."""
            try:
                if sweep:
                    lamps.sweep()
                    reply = lamps.settings()
                else:
                    body = self._body()
                    reply = lamps.set(body.get("glow_day"), body.get("glow_night"), body.get("meter_trim_db"))
                    if config_file is not None:
                        for k in ("glow_day", "glow_night", "meter_trim_db"):
                            if k in body:
                                save_setting(config_file, k, reply[k])
            except (ValueError, TypeError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"glow_day\", ...}")
                return
            self._send(json.dumps(reply).encode(), "application/json")

        def _identity(self) -> dict:
            from sleepradiopi.config import brand
            try:
                saved = json.loads(config_file.read_text()) if config_file is not None else {}
            except (OSError, ValueError):
                saved = {}
            return {"hardware": saved.get("hardware", "box"), "station_name": saved.get("station_name") or "",
                    "name": brand.name, "default_names": {"box": brand.DEFAULT, **brand.BY_HARDWARE},
                    "can_restart": bool(os.environ.get(RESTART_ENV))}

        def _set_identity(self) -> None:
            """POST /api/identity {"hardware": "box" | "cathedral", "station_name": "..." | ""}:
            which radio this is, and its name (empty: the hardware's own). Saved; the
            station restarts to take it up."""
            try:
                body = self._body()
                hw = body.get("hardware", "box")
                name = body.get("station_name") or None
                if hw not in ("box", "cathedral"):
                    raise ValueError("hardware is box or cathedral")
                if name is not None and (not isinstance(name, str) or len(name.strip()) > 40):
                    raise ValueError("a name is up to 40 characters")
            except (ValueError, TypeError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"hardware\", \"station_name\"}")
                return
            if config_file is not None:
                save_setting(config_file, "hardware", hw)
                save_setting(config_file, "station_name", name.strip() if name else None)
            restarting = bool(os.environ.get(RESTART_ENV))
            self._send(json.dumps({**self._identity(), "restarting": restarting}).encode(), "application/json")
            if restarting:
                _restart_soon(speaker)

        def _messages_state(self) -> dict:
            trusted = clock_trusted()
            nxt = station.messages.next_slot(datetime.now(), station.profile) if trusted else None
            return {**station.messages.cfg, "next": nxt.strftime("%H:%M") if nxt else None,
                    "next_day": nxt.date() != date.today() if nxt else False,
                    "due_now": bool(nxt and nxt <= datetime.now()),
                    "clock_trusted": trusted, "can_hear": speaker is not None and station._has_voice}

        def _set_messages(self) -> None:
            """POST /api/messages {"on", "every_min", "offset_min", "start_min", "end_min",
            "date_first", "list": [{"text", "until"?, "off"?}]}: replace them. Saved."""
            try:
                cfg = messages.validate(self._body())
            except (ValueError, TypeError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send the messages' settings")
                return
            station.set_messages(cfg)
            if config_file is not None:
                save_setting(config_file, "messages", cfg)
            self._send(json.dumps(self._messages_state()).encode(), "application/json")

        def _hear_message(self) -> None:
            """POST /api/messages/hear {"text"}: the DJ says it on the speaker now
            (with the day and date first, if that's on)."""
            try:
                text = messages.validate({"list": [{"text": self._body().get("text")}]})["list"][0]["text"]
            except (ValueError, TypeError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"text\"}")
                return
            if speaker is None or not station._has_voice:
                self._error("this radio has no speaker or no voice")
                return
            words = station.messages.words(text, date.today())

            def run():
                try:
                    speaker.play_clip(Clip(station.render_speech(words), "message", "Message"))
                except Exception:
                    log.exception("message preview failed")
            threading.Thread(target=run, name="message-preview", daemon=True).start()
            self._send(json.dumps({"ok": True, "text": words}).encode(), "application/json")

        def _body(self):
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

        def _error(self, message: str) -> None:
            body = json.dumps({"error": message}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _set_birthdays(self) -> None:
            """POST /api/birthdays {"birthdays": [...]}: replace the list. Saved."""
            try:
                entries = birthdays.validate(self._body()["birthdays"])
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"birthdays\": [...]}")
                return
            station.set_birthdays(entries)
            if config_file is not None:
                save_setting(config_file, "birthdays", entries)
            self._send(json.dumps(self._birthdays_state()).encode(), "application/json")

        def _hear_birthday(self) -> None:
            """POST /api/birthdays/hear {"name", "day", "month", "year"?}: say
            that person's wish on the speaker now, as if it were their day."""
            try:
                person = birthdays.validate([self._body()])[0]
            except (ValueError, TypeError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send a name, day and month")
                return
            if speaker is None or not station._has_voice:
                self._error("this radio has no speaker or no voice")
                return
            age = date.today().year - person["year"] if person.get("year") else None
            text = birthdays.wish_text([{"name": person["name"], "age": age if age and 1 <= age <= 120 else None}],
                                       station.builder.station)

            def run():
                try:
                    speaker.play_clip(Clip(station.render_speech(text), "birthday", "Birthday wish"))
                except Exception:
                    log.exception("birthday preview failed")
            threading.Thread(target=run, name="birthday-preview", daemon=True).start()
            self._send(json.dumps({"ok": True, "text": text}).encode(), "application/json")

        def _selection(self) -> dict:
            return {"artist": station.artist, "profile": station.profile,
                    "station_name": station.builder.station}

        def _save_selection(self) -> None:
            if config_file is not None:
                save_setting(config_file, "broadcast_artist", None)    # (artist radio is retired)
                save_setting(config_file, "broadcast_profile", station.profile)

        def _station(self) -> None:
            """/api/station {"profile": "Carisbrooke"}: play that theme; {"profile": null}
            or {"artist": null}: the theme last played, else Default. Saved. (Artist radio,
            {"artist": "The Beatles"}, is retired: make a theme with the artist.)"""
            try:
                body = self._body()
                value = body.get("profile")
                if body.get("artist"):
                    self._error("artist radio has gone: make a theme with the artist in it")
                    return
                if value is not None and (not isinstance(value, str) or len(value) > 200):
                    raise ValueError("a name or null")
            except (ValueError, TypeError, KeyError, AttributeError):
                self.send_error(400)
                return
            value = value.strip() if value else None
            found = station.set_profile(value)
            self._save_selection()
            self._send(json.dumps({"found": found, **self._selection()}).encode(), "application/json")

        def _dj_state(self) -> dict:
            startup = True
            if config_file is not None:
                try:
                    startup = bool(json.loads(config_file.read_text()).get("startup_sound", True))
                except (OSError, ValueError):
                    pass
            return {**station.dj_settings(), "startup_sound": startup,
                    "can_restart": bool(os.environ.get(RESTART_ENV))}

        def _set_dj(self) -> None:
            """POST /api/dj with any of {"voice", "chattiness", "dj_hooks",
            "jingle_every" (0 = off), "news_enabled", "dj_speed", "news_speed"
            (0.5-1.5, 1 = the voice's own pace)}. All saved; all live except
            the voice, which restarts the station (only one voice fits in RAM)."""
            try:
                body = self._body()
                if not isinstance(body, dict):
                    raise ValueError("send a JSON object")
                for key in ("dj_hooks", "news_enabled", "startup_sound", "dj_on", "time_checks"):
                    if key in body and not isinstance(body[key], bool):
                        raise ValueError(f"{key} must be true or false")
                every = body.get("jingle_every")
                if every is not None and (isinstance(every, bool) or not isinstance(every, int)):
                    raise ValueError("jingle_every must be a number")
                voice = body.get("voice")
                if voice is not None and voice not in station.voices():
                    raise ValueError(f"no voice called {voice!r}")
                station.set_dj(chattiness=body.get("chattiness"), dj_hooks=body.get("dj_hooks"),
                               jingle_every=every, news_enabled=body.get("news_enabled"),
                               dj_speed=body.get("dj_speed"), news_speed=body.get("news_speed"),
                               dj_on=body.get("dj_on"), time_checks=body.get("time_checks"))
            except (ValueError, TypeError, AttributeError) as e:
                self._error(str(e))
                return
            restarting = False
            if config_file is not None:
                if any(k in body for k in ("chattiness", "time_checks", "news_enabled", "jingle_every", "dj_hooks")):
                    save_setting(config_file, "profiles", station.profiles)   # (the theme playing's settings)
                if "dj_on" in body:
                    save_setting(config_file, "broadcast_dj", body["dj_on"])
                if "startup_sound" in body:
                    save_setting(config_file, "startup_sound", body["startup_sound"])
                if "dj_speed" in body:
                    save_setting(config_file, "broadcast_announcer_speed", station.config.announcer_speed)
                if "news_speed" in body:
                    save_setting(config_file, "news_speed", station.config.news_speed)
                if voice is not None and voice != station.dj_voice:
                    save_setting(config_file, "broadcast_voice", voice)
                    restarting = bool(os.environ.get(RESTART_ENV))
                    log.info("voice changed to %s from %s", voice, self.address_string())
            self._send(json.dumps({**self._dj_state(), "saved_voice": voice or station.dj_voice,
                                   "restarting": restarting}).encode(), "application/json")
            if restarting:
                _restart_soon(speaker)

        def _playlists(self) -> dict:
            return {"playlists": [station.playlist_view(p) for p in station.playlists],
                    "playing": station.playlist_status()}

        def _programmes(self) -> dict:
            sched = station.scheduler
            return {"programmes": sched.programmes, "playing": sched.status(), "programme_mode": sched.quiet}

        def _programmes_post(self, path: str) -> None:
            """POST /api/programmes {"programmes": [...]} (the whole list, saved),
            /api/programmes/play {"name"}, /api/programmes/stop {"name"?}."""
            sched = station.scheduler
            try:
                body = self._body()
                if path == "/api/programmes":
                    sched.set_programmes(body["programmes"])
                    if config_file is not None:
                        save_setting(config_file, "programmes", sched.programmes)
                elif path == "/api/programmes/play":
                    sched.play(body["name"])
                    if speaker is not None and sched.run is not None:   # (only moments and switches: over what's on)
                        speaker.play()
                else:
                    sched.stop(name=body.get("name") if isinstance(body.get("name"), str) else None)
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"name\"} or {\"programmes\": [...]}")
                return
            self._send(json.dumps(self._programmes()).encode(), "application/json")

        def _save_playlists(self) -> None:
            if config_file is not None:
                save_setting(config_file, "playlists", station.playlists)

        def _set_playlists(self) -> None:
            """POST /api/playlists {"playlists": [{"name", "tracks": [[root, path], ...]}]}:
            the whole list (new, renamed, reordered, removed), saved."""
            try:
                station.set_playlists(self._body()["playlists"])
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"playlists\": [...]}")
                return
            self._save_playlists()
            self._send(json.dumps(self._playlists()).encode(), "application/json")

        def _rename_playlist(self) -> None:
            """POST /api/playlists/rename {"old", "new"}: buttons and programmes that
            play it follow it. Saved."""
            try:
                body = self._body()
                old, new = body["old"], body["new"]
                if not isinstance(old, str) or not isinstance(new, str):
                    raise ValueError("send {\"old\", \"new\"}")
                was = playlists_mod.find(station.playlists, old)
                new = station.rename_playlist(old, new)
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"old\", \"new\"}")
                return
            old = was["name"]
            if presets is not None:
                presets.rename_playlist(old, new)
            sched = getattr(station, "scheduler", None)
            renamed = bool(sched and sched.rename_playlist(old, new))
            self._save_playlists()
            if renamed and config_file is not None:
                save_setting(config_file, "programmes", sched.programmes)
            self._send(json.dumps({"name": new, **self._playlists()}).encode(), "application/json")

        def _playlist_add(self) -> None:
            """POST /api/playlists/add {"name", and "root" + "path" (a track), "root" +
            "folder" (+ "deep") (a folder), or "now": true (the song playing)}."""
            try:
                body = self._body()
                refs = station.refs_for(root=body.get("root"), path=body.get("path"), folder=body.get("folder"),
                                        deep=body.get("deep") is True, now=body.get("now") is True)
                p = station.add_to_playlist(body["name"], refs)
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"name\", ...}")
                return
            self._save_playlists()
            self._send(json.dumps({"name": p["name"], "added": len(refs), "tracks": len(p["tracks"]),
                                   **self._playlists()}).encode(), "application/json")

        def _playlist_play(self) -> None:
            """POST /api/playlists/play {"name", "shuffle": bool}: now, then the show."""
            try:
                body = self._body()
                reply = station.play_playlist(body["name"], body.get("shuffle") is True,
                                              then="pause" if body.get("then") == "pause" else None)
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"name\"}")
                return
            if speaker is not None:
                speaker.play()
            self._send(json.dumps({**reply, **self._playlists()}).encode(), "application/json")

        def _album_args(self, body: dict | None = None) -> dict:
            """{"id": n} (from /api/search or /api/albums) or {"root", "folder", "deep"}
            (from /api/browse)."""
            body = self._body() if body is None else body
            if "id" in body:
                return {"album_id": body["id"]}
            if not isinstance(body.get("folder"), str):
                raise ValueError("send {\"id\": n} or {\"root\", \"folder\"}")
            out = {"root": body.get("root") or "music", "folder": body["folder"], "deep": body.get("deep") is True}
            if "track" in body:
                if isinstance(body["track"], bool) or not isinstance(body["track"], int):
                    raise ValueError("track is a number")
                out["track"] = body["track"]
            return out

        def _album(self) -> None:
            """POST /api/album: play it next. Music goes into the show, introduced by
            the DJ; On demand plays with no DJ when the song (or album) playing ends."""
            try:
                reply = station.play_next(**self._album_args())
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"id\": n}")
                return
            if reply.get("now") and speaker is not None:
                speaker.play()
            self._send(json.dumps({**reply, "album": station.album_status(),
                                   "up_next_source": station.next_source_status(),
                                   "requests": station.requests()}).encode(), "application/json")

        def _radio_stations(self) -> list[dict]:
            saved = load_settings(config_file).radio_stations if config_file is not None else None
            if saved is None:
                return [dict(s) for s in radio_mod.DEFAULT_STATIONS]
            try:
                return radio_mod.validate_stations(saved)
            except ValueError:
                return []

        def _radio_state(self) -> dict:
            st = station.status()
            return {"stations": self._radio_stations(), "source": st.get("source"),
                    "source_error": st.get("source_error"),
                    "directory": directory.status() if directory is not None else None}

        def _radio(self, action: str) -> None:
            """POST /api/radio/play {"name", "url"}: play that internet radio station
            instead of the show; /api/radio/stop: back to the show (from a station or
            an album); /api/radio/stations {"stations": [...]}: the saved list. All saved."""
            try:
                body = self._body()
                if action == "play":
                    tuned = radio_mod.validate_station(body)
                elif action == "stations":
                    stations = radio_mod.validate_stations(body["stations"])
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send a station or {\"stations\": [...]}")
                return
            if action == "stations":
                if config_file is not None:
                    save_setting(config_file, "radio_stations", stations)
            else:
                tuned = tuned if action == "play" else None
                station.tune(tuned)                  # (the station saves it: on_source)
                if tuned is not None and speaker is not None:
                    speaker.play()               # choosing a station means "play it"
            self._send(json.dumps(self._radio_state()).encode(), "application/json")

        def _media(self, action: str) -> None:
            """The music library manager: POST /api/media/upload?kind=&dir=&name= with
            the file as the body (name may include folders, for a folder upload);
            /api/media/mkdir {"kind", "path", "name"}; /api/media/delete {"kind",
            "path"}; /api/media/done {} (changes finished: read-only again, and
            the library is rescanned)."""
            if action == "upload":
                q = parse_qs(urlparse(self.path).query)
                length = int(self.headers.get("Content-Length", 0) or 0)
                try:
                    reply = media.receive(q.get("kind", ["music"])[0], q.get("dir", [""])[0],
                                          q.get("name", [""])[0], length, self.rfile.read)
                except media_mod.MediaError as e:
                    self.close_connection = True          # (the rest of the body wasn't read)
                    self._error(str(e))
                    return
                self._send(json.dumps(reply).encode(), "application/json")
                return
            try:
                body = self._body()
                kind = body.get("kind", "music")
                if action == "mkdir":
                    reply = {"path": media.mkdir(kind, body.get("path", ""), body.get("name", ""))}
                elif action == "delete":
                    media.delete(kind, body.get("path", ""))
                    reply = {"deleted": body.get("path")}
                elif action == "move":
                    old = body.get("path", "")
                    to_kind = body.get("to_kind", kind)
                    new = media.move(kind, old, to_kind, body.get("to", ""))
                    reply = {"path": new, "kind": to_kind}
                    old_root, new_root = kind, to_kind          # playlists, programmes and buttons follow it
                    if old_root in ("music", "ondemand") and new_root in ("music", "ondemand"):
                        old_rel = "/".join(p for p in old.split("/") if p)
                        if station.rename_refs(old_root, old_rel, new_root, new) and config_file is not None:
                            save_setting(config_file, "playlists", station.playlists)
                        sched = getattr(station, "scheduler", None)
                        if sched and sched.rename_refs(old_root, old_rel, new_root, new) and config_file is not None:
                            save_setting(config_file, "programmes", sched.programmes)
                        if presets is not None:
                            presets.rename_refs(old_root, old_rel, new_root, new)
                elif action == "done":
                    changed = media.done()
                    reply = {"changed": changed,
                             "library": {"tracks": len(station.tracks), "jingles": len(station.jingles),
                                         "books": len(station.books.all()) if hasattr(station, "books") else 0,
                                         "ondemand": len(getattr(station, "ondemand_tracks", []))}}
                else:
                    self.send_error(404)
                    return
            except (media_mod.MediaError, AttributeError, TypeError) as e:
                self._error(str(e) if isinstance(e, media_mod.MediaError) else "send {\"kind\", \"path\"}")
                return
            self._send(json.dumps(reply).encode(), "application/json")

        def _download(self) -> None:
            """GET /api/media/download?kind=&path=: a file as it is, or a folder as a
            zip (stored, not squeezed: audio doesn't shrink), streamed."""
            import zipfile
            q = parse_qs(urlparse(self.path).query)
            try:
                target = media.resolve(q.get("kind", ["music"])[0], q.get("path", [""])[0])
            except media_mod.MediaError as e:
                self._error(str(e))
                return
            if not target.exists() or target == media._root(q.get("kind", ["music"])[0]):
                self._error("that isn't there to download")
                return
            name = target.name.replace('"', "'")
            from urllib.parse import quote
            self.send_response(200)
            self.send_header("Cache-Control", "no-store")
            if target.is_file():
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(target.stat().st_size))
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(target.name)}")
                self.end_headers()
                with open(target, "rb") as f:
                    while chunk := f.read(1 << 16):
                        self.wfile.write(chunk)
                return
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(target.name + '.zip')}")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            class Out:                                   # zipfile writes to a stream it can't seek
                def __init__(self, w): self.w, self.n = w, 0
                def write(self, b): self.w.write(b); self.n += len(b); return len(b)
                def tell(self): return self.n
                def flush(self): self.w.flush()
            try:
                with zipfile.ZipFile(Out(self.wfile), "w", zipfile.ZIP_STORED) as z:
                    for f in sorted(target.rglob("*")):
                        if f.is_file() and not any(part.startswith(".") for part in f.relative_to(target).parts):
                            z.write(f, str(Path(name) / f.relative_to(target)))
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _buttons(self, press: bool) -> None:
            """POST /api/buttons {"button": 1-4, "preset": {...} | null} (null
            empties it), {"button", "steps": [{...}, ...]} (several, stepped through
            by pressing it again), {"button", "preset", "add": true} (one more step
            on the end) or {"button", "now": true, "add"?} (what's playing, on it);
            each takes "bank"? (day or night). /api/buttons/press {"button",
            "step"?: 1-6}: as if pressed on the case (step: that one, at once)."""
            try:
                body = self._body()
                n = body["button"]
                if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= presets.count:
                    raise ValueError(f"button is 1 to {presets.count}")
                if press:
                    step = body.get("step")
                    if step is not None and (isinstance(step, bool) or not isinstance(step, int)):
                        raise ValueError("step is a number")
                    if body.get("hold"):
                        presets.hold(n - 1)
                    else:
                        presets.press(n - 1, None if step is None else step - 1)
                elif "steps" in body:
                    if not isinstance(body["steps"], list):
                        raise ValueError("steps is a list of presets")
                    presets.set(n - 1, body["steps"], body.get("bank"))
                else:
                    preset = presets.current() if body.get("now") else body["preset"]
                    (presets.add if body.get("add") else presets.set)(n - 1, preset, body.get("bank"))
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else
                            "send {\"button\": 1-4, \"preset\": ... | \"steps\": [...] | \"now\": true, \"add\"?, \"bank\"?}")
                return
            self._send(json.dumps(presets.status()).encode(), "application/json")

        def _button_bank(self, auto: bool) -> None:
            """POST /api/buttons/bank {"bank": "day" | "night"} (or {} to swap): the
            set of buttons in use (announced on the radio); /api/buttons/auto
            {"on", "night_min", "day_min"}: the timetable that swaps them. Saved."""
            try:
                body = self._body()
                if auto:
                    presets.set_auto(body)
                elif body.get("bank") is None:
                    presets.toggle_bank()
                else:
                    presets.set_bank(body["bank"])
            except (ValueError, TypeError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"bank\": \"day\" | \"night\"}")
                return
            self._send(json.dumps(presets.status()).encode(), "application/json")

        def _button_key(self) -> None:
            """POST /api/buttons/key {"button": 1-4, "down": bool}: the page's button
            going down or up, through the same timers as the real ones -- a tap
            plays (or steps on), 1 and 4 held for 5 s open the service
            menu. A button the page never lets go of is let go after KEY_HELD_MAX_S."""
            try:
                body = self._body()
                n, down = body["button"], body["down"]
                if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= presets.count \
                        or not isinstance(down, bool):
                    raise ValueError(f"send {{\"button\": 1-{presets.count}, \"down\": true | false}}")
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"button\", \"down\"}")
                return
            code = next(c for c, i in presets_mod.KEYCODES.items() if i == n - 1)
            with _held_lock:
                was = _held.pop(code, None)
                if was is not None:
                    was.cancel()
                if down:
                    t = _held[code] = threading.Timer(KEY_HELD_MAX_S, lambda: _let_go(code))
                    t.daemon = True
                    t.start()
            if down and was is None:
                presets.keys[code][0]()
            elif not down and was is not None:
                presets.keys[code][1]()
            self._send(json.dumps(presets.status()).encode(), "application/json")

        def _podcasts(self, action: str) -> None:
            """POST /api/podcasts/follow {"feed_url"}, /unfollow {"id"}, /play {"id", "guid"?}
            (no guid: the one part-heard, else the newest unheard), /heard {"id", "guid", "heard": bool}."""
            try:
                body = self._body()
                pods = station.podcasts
                if action == "follow":
                    reply = {"show": pods.subscribe(str(body.get("feed_url") or ""))}
                elif action == "unfollow":
                    pods.unsubscribe(str(body.get("id") or ""))
                    reply = {"ok": True}
                elif action == "play":
                    station.play_episode(str(body.get("id") or ""), body.get("guid"))
                    if speaker is not None:
                        speaker.play()
                    reply = {"source": station.status()["source"]}
                else:
                    sid, guid = str(body.get("id") or ""), str(body.get("guid") or "")
                    key = pod_mod.episode_key(sid, guid)
                    station.book_positions.set(key, 0, done=bool(body.get("heard", True)))
                    reply = {"ok": True}
            except (ValueError, AttributeError, TypeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"id\"}")
                return
            self._send(json.dumps(reply).encode(), "application/json")

        def _books(self, action: str) -> None:
            """POST /api/books/play {"key"}: that audiobook, from where it was left;
            /api/books/seek {"delta_ms": -60000} or {"to_ms": n}: move in the book on."""
            try:
                body = self._body()
                if action == "play":
                    if not isinstance(body.get("key"), str):
                        raise ValueError("send {\"key\"} from /api/books")
                    station.play_book(body["key"])
                    if speaker is not None:
                        speaker.play()
                    reply = {"source": station.status()["source"]}
                else:
                    delta, to = body.get("delta_ms"), body.get("to_ms")
                    for v in (delta, to):
                        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                            raise ValueError("delta_ms / to_ms are milliseconds")
                    reply = station.book_seek(None if delta is None else int(delta), None if to is None else int(to))
            except (ValueError, AttributeError, TypeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"key\"} or {\"delta_ms\"}")
                return
            self._send(json.dumps(reply).encode(), "application/json")

        def _play_album(self) -> None:
            """POST /api/album/play {"id": n} or {"root", "folder", "deep"} (+ "shuffle",
            "then": "pause"): play it straight through instead of the show -- no DJ --
            then back to the show (or pause)."""
            try:
                body = self._body()
                station.play_album(**self._album_args(body), shuffle=body.get("shuffle") is True,
                                   then="pause" if body.get("then") == "pause" else None)
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"id\": n}")
                return
            if speaker is not None:
                speaker.play()
            self._send(json.dumps(self._radio_state()).encode(), "application/json")

        def _wifi_state(self) -> dict:
            st = wifi_mod.status()
            saved = wifi_mod.load()
            return {**st, "saved": [n["ssid"] for n in saved["networks"]],
                    "on_card": wifi_mod.card_networks(), "hotspot_settings": {"ssid": saved["hotspot"]["ssid"]}}

        def _wifi(self, action: str) -> None:
            """POST /api/wifi/add {"ssid", "password"}, /remove {"ssid"}, /scan,
            /hotspot {"ssid", "password"} (the radio's own network's details),
            /try (leave the hotspot and try the saved networks now), /hotspot-now
            (switch to the radio's own network now: the only way it does, besides a reset)."""
            try:
                body = self._body()
                if action == "add":
                    wifi_mod.add_network(body.get("ssid"), body.get("password", ""))
                    wifi_mod.ask("reload")
                elif action == "remove":
                    wifi_mod.remove_network(body.get("ssid", ""))
                    wifi_mod.ask("reload")
                elif action == "scan":
                    wifi_mod.ask("scan")
                elif action == "hotspot":
                    wifi_mod.set_hotspot(body.get("ssid"), body.get("password", ""))
                elif action == "try":
                    wifi_mod.ask("station")
                elif action == "hotspot-now":
                    wifi_mod.ask("hotspot")
                else:
                    self.send_error(404)
                    return
            except (ValueError, TypeError, AttributeError, OSError) as e:
                self._error(str(e))
                return
            log.info("wifi %s from %s", action, self.address_string())
            self._send(json.dumps(self._wifi_state()).encode(), "application/json")

        def _voices_state(self) -> dict:
            there = station.voices()
            return {"voices": there, "current": station.dj_voice,
                    "job": voice_jobs.status() if voice_jobs is not None else {"state": "idle"},
                    "standard_name": voices_mod.STANDARD_NAME,
                    "has_standard": voices_mod.STANDARD_NAME in there,
                    "credit": voices_mod.STANDARD_CREDIT}

        def _upload_voice(self) -> None:
            """POST /api/voices/upload?name=N&file=F with the archive as the body."""
            q = parse_qs(urlparse(self.path).query)
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                voice_jobs.receive(q.get("name", [""])[0], q.get("file", [""])[0], length, self.rfile.read)
            except ValueError as e:
                if length and not self.rfile.closed:
                    self.close_connection = True       # don't read a big body we've refused
                self._error(str(e))
                return
            self._send(json.dumps(self._voices_state()).encode(), "application/json")

        def _set_stream(self) -> None:
            """POST /api/stream {"enabled": bool}: listening in a browser. Saved."""
            try:
                enabled = self._body()["enabled"]
                if not isinstance(enabled, bool):
                    raise ValueError("enabled must be true or false")
                if not enabled and speaker is None:
                    raise ValueError("this radio has no speaker: the browser is the only way to listen")
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"enabled\": true|false}")
                return
            if output is not None:
                output.set_enabled(enabled)
            if config_file is not None:
                save_setting(config_file, "web_stream", enabled)
            log.info("listening in a browser %s", "on" if enabled else "off")
            self._send(json.dumps({"stream": output is not None and output.enabled}).encode(), "application/json")

        def _request(self) -> None:
            """POST /api/request {"id": n} (from /api/search): play that track next."""
            try:
                track_id = self._body()["id"]
                if isinstance(track_id, bool) or not isinstance(track_id, int):
                    raise ValueError("id must be a number from /api/search")
                reply = station.request(track_id)
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"id\": n}")
                return
            self._send(json.dumps({**reply, "requests": station.requests()}).encode(), "application/json")

        def _set_profiles(self) -> None:
            """POST /api/profiles {"profiles": [{"name", "artists": [...]}, ...]}:
            replace the lists. Saved; the one playing follows the edit."""
            try:
                entries = profiles.validate(self._body()["profiles"])
                if not any(p["name"].lower() == profiles.DEFAULT.lower() for p in entries):
                    raise ValueError(f"{profiles.DEFAULT} is always there: it can't be deleted or renamed")
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"profiles\": [...]}")
                return
            station.set_profiles(entries)
            if config_file is not None:
                save_setting(config_file, "profiles", station.profiles)
            self._save_selection()
            self._send(json.dumps({**self._selection(), "profiles": station.profiles}).encode(),
                       "application/json")

        def _rename_profile(self) -> None:
            """POST /api/profiles/rename {"old", "new"}: rename a theme. Everything of its
            own follows it: its jingles folder, its messages, buttons and programmes that
            play it, and (playing) the station. Saved."""
            try:
                body = self._body()
                old, new = body["old"], body["new"]
                if not isinstance(old, str) or not isinstance(new, str):
                    raise ValueError("send {\"old\", \"new\"}")
                new = station.rename_profile(old, new)
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"old\", \"new\"}")
                return
            if media is not None and (Path(station.jingles_dir) / old).is_dir() and old != new:
                try:
                    media.rename("jingles", old, new)
                except media_mod.MediaError as e:
                    log.warning("theme rename: jingles folder: %s", e)
                finally:
                    media.done()
            if presets is not None:
                presets.rename_theme(old, new)
            sched = getattr(station, "scheduler", None)
            renamed = bool(sched and sched.rename_theme(old, new))
            if config_file is not None:
                save_setting(config_file, "profiles", station.profiles)
                save_setting(config_file, "messages", station.messages.cfg)
                if renamed:
                    save_setting(config_file, "programmes", sched.programmes)
            self._save_selection()
            self._send(json.dumps({"name": new, "profiles": station.profiles}).encode(), "application/json")

        def _knob(self) -> None:
            """/api/knob {"press": "short" | "long" | "service"} or {"press": "turn", "clicks": n}
            (pressed and turned: back or on through a book, podcast or song): the knob's switch, from
            the page (to try it without the hardware). Short = pause/play, long =
            say the radio's address; "service" = as holding buttons 1 and 4 for 5 s
            (the service menu; then the page's buttons 1-4 answer it)."""
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                press = body.get("press")
                if press not in ("short", "long", "service", "back", "back_long", "turn"):
                    raise ValueError("press must be short, long, service, back, back_long or turn")
                clicks = body.get("clicks", 0)
                if press == "turn" and (isinstance(clicks, bool) or not isinstance(clicks, int) or not -50 <= clicks <= 50):
                    raise ValueError("clicks is -50 to 50")
            except (ValueError, TypeError, AttributeError):
                self.send_error(400)
                return
            if press in ("back", "back_long") and presets is not None:   # the cathedral's back button
                (presets.back_press if press == "back" else presets.back_hold)()
                done = True
            elif press == "service":
                menu = getattr(presets, "menu", None)
                if menu is not None:
                    menu.open()
                done = menu is not None
            elif press == "short":
                speaker.toggle()
                done = True
            elif press == "turn":                # pressed and turned: back or on through what's playing
                seeker = getattr(station, "seeker", None)
                if seeker is not None:
                    seeker.turn(clicks, speed_up=False)   # (one step a click)
                done = seeker is not None
            elif presets is not None:            # held: what's on the knob, else the address
                done = presets.knob_long()
            else:
                done = announcer.speak() if announcer is not None else False
            self._send(json.dumps({"press": press, "done": done, **speaker.status()}).encode(),
                       "application/json")

        def _speaker(self) -> None:
            """/api/speaker with a JSON body: {"volume": 0-100}, {"step": n},
            {"pause": true | false | "toggle"}, {"sleep": minutes (0 = off)},
            {"mono": true | false} or
            {"eq": {"bass": dB, "mid": dB, "treble": dB}} (any of the three),
            {"highpass": Hz} (the low cut; 0 = off) or
            {"test": "bass" | "sweep" | "pink" | "left" | "right" | "sides" | "phase" | "tune" | "stop"}
            (a test sound on the speaker; see audio/testsignal.py).
            Replies with the speaker's status."""
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if "volume" in body:
                    speaker.set_volume(int(body["volume"]))
                if "step" in body:
                    speaker.step(int(body["step"]))
                if "knob_mode" in body:
                    speaker.set_knob_mode(body["knob_mode"])
                if "mono" in body:
                    if not isinstance(body["mono"], bool):
                        raise ValueError("mono must be true or false")
                    speaker.set_mono(body["mono"])
                if "eq" in body:
                    eq = body["eq"]
                    if not isinstance(eq, dict) or not all(
                            k in ("bass", "mid", "treble") and isinstance(v, (int, float))
                            and not isinstance(v, bool) for k, v in eq.items()):
                        raise ValueError("eq must map bass/mid/treble to numbers")
                    speaker.set_eq(eq)
                if "highpass" in body:
                    hz = body["highpass"]
                    if isinstance(hz, bool) or not isinstance(hz, (int, float)) or not 0 <= hz <= 300:
                        raise ValueError("highpass must be 0-300 Hz")
                    speaker.set_highpass(hz)
                if "sleep" in body:
                    minutes = body["sleep"]
                    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) \
                            or not 0 <= minutes <= 600:
                        raise ValueError("sleep must be 0-600 minutes")
                    speaker.set_sleep(minutes)
                if "noise" in body or "noise_kind" in body or "noise_mix" in body:
                    on = body.get("noise")
                    if on == "toggle":
                        on = speaker.speaker.noise is None
                    kind, mix = body.get("noise_kind"), body.get("noise_mix")
                    if on is not None and not isinstance(on, bool):
                        raise ValueError("noise: true, false or \"toggle\"")
                    if mix is not None and (isinstance(mix, bool) or not isinstance(mix, (int, float)) or not 0 <= mix <= 100):
                        raise ValueError("noise_mix: 0-100")
                    speaker.set_noise(on=on, kind=kind, mix=None if mix is None else int(mix))
                if "test" in body:
                    if body["test"] == "stop":
                        speaker.stop_test()
                    elif isinstance(body["test"], str):
                        speaker.start_test(body["test"])   # ValueError if unknown
                    else:
                        raise ValueError("test must be a name")
                if body.get("pause") == "toggle":
                    speaker.toggle()
                elif body.get("pause") is True:
                    speaker.pause()
                elif body.get("pause") is False:
                    speaker.play()
            except (ValueError, TypeError, AttributeError, OverflowError):
                self.send_error(400)
                return
            self._send(json.dumps(speaker.status()).encode(), "application/json")

        def _stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            q = output.add_client()
            station.listener_joined()
            try:
                while True:
                    chunk = q.get(timeout=IDLE_CLOSE_S)
                    if chunk is None:
                        break
                    self.wfile.write(chunk)
            except (queue.Empty, BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            finally:
                output.remove_client(q)
                station.listener_left()

    return Handler


def serve(station: Station, output: Mp3Output, port: int, speaker=None,
          config_file: Path | None = None, announcer=None, voice_jobs=None, updates=None,
          presets=None, directory=None, media=None, lamps=None) -> None:
    from sleepradiopi.config import brand
    server = ThreadingHTTPServer(("0.0.0.0", port),
                                 make_handler(station, output, speaker, config_file, announcer, voice_jobs,
                                              updates, presets, directory, media, lamps))
    server.daemon_threads = True
    log.info("%s on http://0.0.0.0:%d/", brand.name, port)
    server.serve_forever()
