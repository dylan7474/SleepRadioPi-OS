"""HTTP front end: the radio page, the MP3 stream, and a JSON status feed.

Plain stdlib http.server -- a handful of listeners on a home network needs
nothing more. No auth: meant for a trusted LAN only.
"""

from __future__ import annotations

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

from sleepradiopi.broadcast import birthdays, profiles
from sleepradiopi.broadcast.station import Station
from sleepradiopi.config.auth import COOKIE, SESSION_S, Auth
from sleepradiopi.config.clock import clock_trusted
from sleepradiopi.io.announce import Clip
from sleepradiopi import voices as voices_mod
from sleepradiopi.config import backup
from sleepradiopi.config.settings import save_setting
from sleepradiopi.config.power import can_power_off, request_power_off

from .stream import Mp3Output

log = logging.getLogger(__name__)

PAGE = (Path(__file__).parent / "page.html").read_bytes()
IDLE_CLOSE_S = 30  # close a stream connection that has had no audio for this long
MAX_SETTINGS_BYTES = 64 * 1024
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


def make_handler(station: Station, output: Mp3Output, speaker=None,
                 config_file: Path | None = None, announcer=None, voice_jobs=None, updates=None):
    auth = Auth(config_file)
    open_paths = {"/", "/index.html", "/api/auth", "/api/login"}

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
            if not self._gate(path):
                return
            if path == "/api/auth":
                self._send(json.dumps(self._auth_state()).encode(), "application/json")
            elif path in ("/", "/index.html"):
                self._send(PAGE, "text/html; charset=utf-8")
            elif path == "/api/status":
                status = station.status()
                if speaker is not None:
                    status["speaker"] = speaker.status()
                status["can_power_off"] = can_power_off()
                status["stream"] = output is not None and output.enabled
                self._send(json.dumps(status).encode(), "application/json")
            elif path == "/api/settings" and config_file is not None:
                self._save_settings()
            elif path == "/api/birthdays":
                self._send(json.dumps(self._birthdays_state()).encode(), "application/json")
            elif path == "/api/voices":
                self._send(json.dumps(self._voices_state()).encode(), "application/json")
            elif path == "/api/update" and updates is not None:
                self._send(json.dumps(updates.status()).encode(), "application/json")
            elif path == "/api/dj":
                self._send(json.dumps(self._dj_state()).encode(), "application/json")
            elif path == "/api/search":
                q = parse_qs(urlparse(self.path).query).get("q", [""])[0][:200]
                self._send(json.dumps({"results": station.search(q), "albums": station.search_albums(q)}).encode(),
                           "application/json")
            elif path == "/api/artists":
                self._send(json.dumps({**self._selection(), "artists": station.artists(),
                                       "profiles": station.profiles}).encode(), "application/json")
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
            elif path == "/api/skip":
                self.rfile.read(int(self.headers.get("Content-Length", 0)))  # no body needed
                self._send(json.dumps({"skipped": station.skip()}).encode(), "application/json")
            elif path == "/api/speaker" and speaker is not None:
                self._speaker()
            elif path == "/api/settings" and config_file is not None:
                self._load_settings()
            elif path == "/api/knob" and speaker is not None:
                self._knob()
            elif path == "/api/station":
                self._station()
            elif path == "/api/birthdays":
                self._set_birthdays()
            elif path == "/api/profiles":
                self._set_profiles()
            elif path == "/api/request":
                self._request()
            elif path == "/api/album":
                self._album()
            elif path == "/api/album/stop":
                self._body()
                self._send(json.dumps({"stopped": station.stop_album(), "requests": station.requests()}).encode(),
                           "application/json")
            elif path == "/api/dj":
                self._set_dj()
            elif path == "/api/stream":
                self._set_stream()
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
            """/api/power: shut the radio down. Saves the volume and silences
            the speaker first, so it goes quiet at once."""
            self.rfile.read(int(self.headers.get("Content-Length", 0)))  # no body needed
            if not can_power_off():
                self.send_error(404)
                return
            log.info("shutdown requested from %s", self.address_string())
            if speaker is not None:
                speaker.save_now()
                speaker.pause()
            ok = request_power_off()
            self._send(json.dumps({"shutting_down": ok}).encode(), "application/json")

        def _save_settings(self) -> None:
            """GET /api/settings: the settings as a file to download."""
            data = backup.export(config_file, speaker.volume if speaker is not None else None)
            body = (json.dumps(data, indent=2) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Disposition",
                             f'attachment; filename="sleepradio-settings-{time.strftime("%Y-%m-%d")}.json"')
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
                    raise backup.BadSettings("that file is far too big to be a settings file")
                try:
                    data = json.loads(self.rfile.read(length))
                except ValueError:
                    raise backup.BadSettings("this isn't a Sleep Radio settings file") from None
                settings, volume = backup.parse(data)
            except backup.BadSettings as e:
                body = json.dumps({"error": str(e)}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            changed = backup.apply(config_file, settings)     # before the live ones save theirs
            dj_keys = {"broadcast_chattiness", "broadcast_dj_hooks", "broadcast_jingle_enabled",
                       "broadcast_jingle_every", "news_enabled"}
            if changed & dj_keys:
                every = settings.get("broadcast_jingle_every", station.config.jingle_every or 4)
                station.set_dj(chattiness=settings.get("broadcast_chattiness"),
                               dj_hooks=settings.get("broadcast_dj_hooks"),
                               jingle_every=every if settings.get("broadcast_jingle_enabled", True) else 0,
                               news_enabled=settings.get("news_enabled"))
            if "profiles" in changed:
                station.set_profiles(settings["profiles"])
            if changed & {"broadcast_artist", "broadcast_profile", "profiles"}:
                if settings.get("broadcast_profile"):
                    station.set_profile(settings["broadcast_profile"])
                else:
                    station.set_artist(settings.get("broadcast_artist"))
            if "birthdays" in changed:
                station.set_birthdays(settings["birthdays"])
            if speaker is not None:
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
                save_setting(config_file, "broadcast_artist", station.artist)
                save_setting(config_file, "broadcast_profile", station.profile)

        def _station(self) -> None:
            """/api/station {"artist": "The Beatles"} (artist radio: only that
            artist, and the DJ says "Beatles Radio"), {"profile": "Friday List"}
            (only the artists on that list), or {"artist": null} (everything). Saved."""
            try:
                body = self._body()
                if "profile" in body:
                    kind, value = "profile", body["profile"]
                else:
                    kind, value = "artist", body["artist"]
                if value is not None and (not isinstance(value, str) or len(value) > 200):
                    raise ValueError("a name or null")
            except (ValueError, TypeError, KeyError, AttributeError):
                self.send_error(400)
                return
            value = value.strip() if value else None
            found = station.set_profile(value) if kind == "profile" else station.set_artist(value)
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
            "jingle_every" (0 = off), "news_enabled"}. All saved; all live except
            the voice, which restarts the station (only one voice fits in RAM)."""
            try:
                body = self._body()
                if not isinstance(body, dict):
                    raise ValueError("send a JSON object")
                for key in ("dj_hooks", "news_enabled", "startup_sound"):
                    if key in body and not isinstance(body[key], bool):
                        raise ValueError(f"{key} must be true or false")
                every = body.get("jingle_every")
                if every is not None and (isinstance(every, bool) or not isinstance(every, int)):
                    raise ValueError("jingle_every must be a number")
                voice = body.get("voice")
                if voice is not None and voice not in station.voices():
                    raise ValueError(f"no voice called {voice!r}")
                station.set_dj(chattiness=body.get("chattiness"), dj_hooks=body.get("dj_hooks"),
                               jingle_every=every, news_enabled=body.get("news_enabled"))
            except (ValueError, TypeError, AttributeError) as e:
                self._error(str(e))
                return
            restarting = False
            if config_file is not None:
                if "chattiness" in body:
                    save_setting(config_file, "broadcast_chattiness", station.chattiness)
                if "dj_hooks" in body:
                    save_setting(config_file, "broadcast_dj_hooks", body["dj_hooks"])
                if every is not None:
                    save_setting(config_file, "broadcast_jingle_enabled", every > 0)
                    if every > 0:
                        save_setting(config_file, "broadcast_jingle_every", every)
                if "news_enabled" in body:
                    save_setting(config_file, "news_enabled", body["news_enabled"])
                if "startup_sound" in body:
                    save_setting(config_file, "startup_sound", body["startup_sound"])
                if voice is not None and voice != station.dj_voice:
                    save_setting(config_file, "broadcast_voice", voice)
                    restarting = bool(os.environ.get(RESTART_ENV))
                    log.info("voice changed to %s from %s", voice, self.address_string())
            self._send(json.dumps({**self._dj_state(), "saved_voice": voice or station.dj_voice,
                                   "restarting": restarting}).encode(), "application/json")
            if restarting:
                _restart_soon(speaker)

        def _album(self) -> None:
            """POST /api/album {"id": n} (from /api/search's albums): play it next,
            start to finish."""
            try:
                album_id = self._body()["id"]
                if isinstance(album_id, bool) or not isinstance(album_id, int):
                    raise ValueError("id must be a number from /api/search")
                reply = station.request_album(album_id)
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"id\": n}")
                return
            self._send(json.dumps({**reply, "album": station.album_status(),
                                   "requests": station.requests()}).encode(), "application/json")

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
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"profiles\": [...]}")
                return
            station.set_profiles(entries)
            if config_file is not None:
                save_setting(config_file, "profiles", station.profiles)
            self._save_selection()
            self._send(json.dumps({**self._selection(), "profiles": station.profiles}).encode(),
                       "application/json")

        def _knob(self) -> None:
            """/api/knob {"press": "short" | "long"}: the knob's switch, from the
            page (to try it without the hardware). Short = pause/play, long =
            say the radio's address."""
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                press = body.get("press")
                if press not in ("short", "long"):
                    raise ValueError("press must be short or long")
            except (ValueError, TypeError, AttributeError):
                self.send_error(400)
                return
            if press == "short":
                speaker.toggle()
                done = True
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
            {"test": "bass" | "sweep" | "pink" | "left" | "right" | "phase" | "stop"}
            (a test sound on the speaker; see audio/testsignal.py).
            Replies with the speaker's status."""
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if "volume" in body:
                    speaker.set_volume(int(body["volume"]))
                if "step" in body:
                    speaker.step(int(body["step"]))
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
          config_file: Path | None = None, announcer=None, voice_jobs=None, updates=None) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", port),
                                 make_handler(station, output, speaker, config_file, announcer, voice_jobs,
                                              updates))
    server.daemon_threads = True
    log.info("Sleep Radio on http://0.0.0.0:%d/", port)
    server.serve_forever()
