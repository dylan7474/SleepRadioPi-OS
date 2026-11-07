"""HTTP front end: the radio page, the MP3 stream, and a JSON status feed.

Plain stdlib http.server -- a handful of listeners on a home network needs
nothing more. No auth: meant for a trusted LAN only.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
from http.cookies import SimpleCookie
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from sleepradiopi.broadcast.station import Station
from sleepradiopi.config.auth import COOKIE, SESSION_S, Auth
from sleepradiopi import updater as updater_mod
from sleepradiopi import wifi as wifi_mod
from sleepradiopi.config import backup
from sleepradiopi.playback import bookdl as bookdl_mod
from sleepradiopi.playback import ebookdl as ebookdl_mod
from sleepradiopi.config.power import can_power_off

from .common import MAX_SETTINGS_BYTES, RESTART_ENV
from .stream import Mp3Output

log = logging.getLogger(__name__)

REMOTE = (Path(__file__).parent / "remote.html").read_bytes()   # the phone remote (/), and the Wi-Fi set-up on the hotspot
DESKTOP = (Path(__file__).parent / "desktop.html").read_bytes()   # the new desktop page (for computers)
DESKTOP_TAG = hashlib.sha1(DESKTOP).hexdigest()[:12]   # an open desktop page that sees this change reloads itself
REMOTE_TAG = hashlib.sha1(REMOTE).hexdigest()[:12]
# The little icon in a browser's tab (a dial in the desktop's amber), for /favicon.ico: the pages link to it; without one a
# browser asks for it anyway and the radio answered 404, on every page load, in its log
FAVICON = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="#d99a35"/>'
           b'<circle cx="16" cy="18" r="9" fill="none" stroke="#3a1d00" stroke-width="2.4"/>'
           b'<path d="M16 18l5-7" stroke="#3a1d00" stroke-width="2.4" stroke-linecap="round"/>'
           b'<path d="M8 7.5h16" stroke="#3a1d00" stroke-width="2.2" stroke-linecap="round" opacity=".55"/></svg>')
_GZIPPED: dict[str, bytes] = {}                          # a page, compressed once (the first time a browser that takes it asks)


def _gzipped(tag: str, body: bytes) -> bytes:
    if tag not in _GZIPPED:
        _GZIPPED[tag] = gzip.compress(body, 9, mtime=0)
    return _GZIPPED[tag]
# The speaker analyser (also on GitHub Pages): served here too, for tuning with no internet.
ANALYSER = (Path(__file__).parent / "analyser" / "index.html").read_bytes()
# The world's coastlines, for the map of who's been heard (Natural Earth's 110 m land, public domain, to half a degree)
LAND = (Path(__file__).parent / "land.json").read_bytes()
KEY_HELD_MAX_S = 30  # a page's button held longer than this is let go (a lost "up")
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
                 config_file: Path | None = None, announcer=None, voice_jobs=None, updates=None,
                 presets=None, directory=None, media=None, lamps=None):
    auth = Auth(config_file)
    book_dl = bookdl_mod.BookDownloader(station.podcasts, media, station.book_positions) \
        if media is not None and hasattr(station, "podcasts") else None      # (a LibriVox book kept on the radio)
    ebook_dl = ebookdl_mod.EbookDownloader(media) if media is not None else None      # (a Project Gutenberg book)
    from sleepradiopi.web.hear import HearHere
    hear = HearHere(output, speaker) if output is not None else None      # the menu bar's Listen here
    if hear is not None and speaker is not None:
        speaker.elsewhere = hear          # (play and pause are about the listening there, while it's on)
    backups = backup.Store(config_file.parent / "backups") if config_file is not None else None
    open_paths = {"/", "/index.html", "/desktop", "/classic", "/api/auth", "/api/login", "/analyser", "/analyser/", "/analyser/index.html", "/favicon.ico"}



    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # --- the optional password (config/auth.py) --------------------------------------

        def _local(self) -> bool:
            # (A tunnel to this page -- Remote access -- arrives from 127.0.0.1 too, but Cloudflare's edge adds these
            # headers to everything it passes on, so that is NOT the radio itself and the password still applies.)
            if any(self.headers.get(h) for h in ("Cf-Connecting-Ip", "Cf-Ray", "X-Forwarded-For")):
                return False
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

        def _icon(self) -> None:
            """/favicon.ico: the tab icon (an SVG, which is what every current browser takes at that address), kept a day."""
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(FAVICON)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            try:
                self.wfile.write(FAVICON)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _page(self, body: bytes, tag: str) -> None:
            """A page of the radio's own (the desktop, the phone remote): kept by the browser until it changes (an ETag
            -- "not modified" is a few bytes, not 590 KB over Wi-Fi), and compressed for a browser that takes it."""
            etag = f'"{tag}"'
            if etag in [t.strip().removeprefix("W/") for t in self.headers.get("If-None-Match", "").split(",")]:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return
            gz = "gzip" in self.headers.get("Accept-Encoding", "")
            out = _gzipped(tag, body) if gz else body
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(out)))
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")     # (ask each time, but only take it again if it changed)
            self.send_header("Vary", "Accept-Encoding")
            if gz:
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            try:
                self.wfile.write(out)
            except (BrokenPipeError, ConnectionResetError):
                pass

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
            if (not path.startswith("/api/") and path not in ("/", "/index.html", "/desktop", "/classic", "/stream", "/favicon.ico")
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
                self._page(REMOTE, REMOTE_TAG)
            elif path == "/classic":          # (the classic page is retired: everything is on the desktop)
                self.send_response(302)
                self.send_header("Location", "/desktop")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif path == "/desktop":
                self._page(DESKTOP, DESKTOP_TAG)
            elif path == "/desktop/land.json":
                self._send(LAND, "application/json")
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
                status["hear"] = bool(hear is not None and hear.on)        # a browser has the radio (Listen here)
                status["hear_held"] = bool(hear is not None and hear.on and hear.held)   # ...and it's paused there
                from sleepradiopi.audio import denoise as denoise_mod
                status["nr"] = denoise_mod.LEVEL          # receivers' noise reduction: off, light, strong or rig
                from sleepradiopi.audio import morse as morse_mod
                status["cw"] = morse_mod.reader.on        # ...and their Morse reader
                from sleepradiopi.audio import ft8 as ft8_mod
                status["ft8"] = ft8_mod.reader.mode if ft8_mod.reader.on else None    # ...and their FT8 / FT4 one
                from sleepradiopi.audio import keyboard as keyboard_mod
                status["keyboard"] = keyboard_mod.reader.mode if keyboard_mod.reader.on else None    # ...and their PSK31 / RTTY one
                from sleepradiopi.audio import pictures as pictures_mod
                status["pictures"] = pictures_mod.reader.mode if pictures_mod.reader.on else None    # ...and the one for SSTV and weather fax
                status["version"] = updater_mod.this_version()
                status["desktop"] = DESKTOP_TAG
                self._send(json.dumps(status).encode(), "application/json")
            elif path == "/favicon.ico":
                self._icon()
            elif path == "/stream":
                if output is None or not output.enabled:
                    self.send_error(404, "listening in a browser is switched off")
                    return
                self._stream()
            else:
                for route in self._get_routes:          # (the areas': web/routes_*.py)
                    if route(self, path):
                        return
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
            else:
                for route in self._post_routes:          # (the areas': web/routes_*.py)
                    if route(self, path):
                        return
                self.send_error(404)






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













        def _body(self):
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

        def _error(self, message: str) -> None:
            body = json.dumps({"error": message}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)












































    from types import SimpleNamespace
    from . import routes_system, routes_controls, routes_messages, routes_library, routes_receivers, routes_outside
    ctx = SimpleNamespace(station=station, output=output, speaker=speaker, config_file=config_file, announcer=announcer,
                          voice_jobs=voice_jobs, updates=updates, presets=presets, directory=directory, media=media, lamps=lamps,
                          auth=auth, book_dl=book_dl, ebook_dl=ebook_dl, hear=hear, backups=backups, open_paths=open_paths)
    gets, posts = [], []
    for area in (routes_system, routes_controls, routes_messages, routes_library, routes_receivers, routes_outside):          # (the handler's helpers and routes, by area: web/routes_*.py)
        built = area.build(ctx)
        for name, method in built["methods"].items():
            setattr(Handler, name, method)
        if built["get"]:
            gets.append(built["get"])
        if built["post"]:
            posts.append(built["post"])
    Handler._get_routes, Handler._post_routes = tuple(gets), tuple(posts)
    return Handler


class RadioServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        """A browser or phone that drops its connection mid-request (a tab closed, a phone asleep) is one short line,
        not the traceback socketserver prints for every error; anything else is reported as it always was."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            log.info("%s: the client dropped its connection (%s)", client_address[0], type(exc).__name__)
            return
        super().handle_error(request, client_address)


def serve(station: Station, output: Mp3Output, port: int, speaker=None,
          config_file: Path | None = None, announcer=None, voice_jobs=None, updates=None,
          presets=None, directory=None, media=None, lamps=None) -> None:
    from sleepradiopi.config import brand
    server = RadioServer(("0.0.0.0", port),
                                 make_handler(station, output, speaker, config_file, announcer, voice_jobs,
                                              updates, presets, directory, media, lamps))
    server.daemon_threads = True
    log.info("%s on http://0.0.0.0:%d/", brand.name, port)
    server.serve_forever()
