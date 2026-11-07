"""Radio stations, receivers and the ham readers: stations, the radio, own receivers, rooms, what was heard.

Methods of the web handler and its routes, moved out of server.py as they were: build(ctx) unpacks what they use of
what make_handler holds and returns them (and the GET / POST branches of this area) for make_handler to attach.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from sleepradiopi.config.settings import load as load_settings
from sleepradiopi.config.settings import save_setting
from sleepradiopi.playback import radio as radio_mod
from urllib.parse import parse_qs
from urllib.parse import urlencode
from urllib.parse import urlparse
from sleepradiopi.web.common import OWN_RX, OWN_RX_ASKS, _quietly


def build(ctx):
    config_file, directory, speaker, station = ctx.config_file, ctx.directory, ctx.speaker, ctx.station

    def _qrz(self) -> dict:
        """The QRZ logbook window's figures (never the key itself)."""
        from sleepradiopi.playback import qrzlog as qrz_mod
        had = qrz_mod.logbook.status() or {}
        return {"set": bool(qrz_mod.KEY), "had": bool(had), "refused": qrz_mod.logbook.refused, "t": had.get("t"),
                **{k: had.get(k) for k in ("total", "confirmed", "dxcc")}, "status": had.get("status") or {}, "recent": had.get("recent") or [],
                "fetched": had.get("fetched") or {}}

    def _one_reader(self, on: str | None, keep: bool = False) -> dict:
        """The readers for receivers, one at a time: each mode has its own place on a band, so with one switched
        on ("cw", "ft8", "keyboard" or "pictures") the others go off. -> {"cw", "ft8", "keyboard", "pictures"}:
        which is on, and how. keep: one was switched: which is on is kept, for after a restart (audio/readers.py)."""
        from sleepradiopi.audio import ft8 as ft8_mod, keyboard as keyboard_mod, morse as morse_mod, pictures as pictures_mod
        from sleepradiopi.audio import readers as readers_mod
        if on is not None:
            if on != "cw":
                morse_mod.reader.switch(False)
            if on != "ft8":
                ft8_mod.reader.switch(False)
            if on != "keyboard":
                keyboard_mod.reader.switch(False)
            if on != "pictures":
                pictures_mod.reader.switch(False)
        if keep and config_file is not None:
            save_setting(config_file, "rx_reader", readers_mod.state())
        return {"cw": morse_mod.reader.on, "ft8": ft8_mod.reader.mode if ft8_mod.reader.on else None,
                "keyboard": keyboard_mod.reader.mode if keyboard_mod.reader.on else None,
                "pictures": pictures_mod.reader.mode if pictures_mod.reader.on else None}

    def _heard_list(self) -> dict:
        from sleepradiopi.audio import ft8 as ft8_mod, heard as heard_mod
        return {"home": ft8_mod.HOME, "call": ft8_mod.own_call(), "where": heard_mod.store.where,
                "heard": [ft8_mod._far(h) for h in heard_mod.store.list()]}

    def _receiver_names(self) -> dict:
        """The names given to receivers on the desktop: {its address as the desktop has it: name}."""
        names = load_settings(config_file).receiver_names if config_file is not None else {}
        return {str(k): str(v) for k, v in names.items()} if isinstance(names, dict) else {}

    def _own_receiver(self, path: str, body: bytes | None = None) -> None:
        """/api/rx/own/WHAT?base=http://the-receiver:8074 (and whatever else WHAT takes): asked of that receiver
        of your own by the radio, its answer passed back as it came. The page asks only the radio, so a rig
        works wherever the radio's page is reached from -- away from home, or over https, where a browser can't
        ask a receiver at home itself. Only the receivers the saved stations play from, and only what the
        page asks of one: the radio fetches nothing else for anybody."""
        what = path[len("/api/rx/own/"):]
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        base = query.pop("base", [""])[0].rstrip("/")
        own = {m.group(1) for m in (OWN_RX.match(str(s.get("url", ""))) for s in self._radio_stations()) if m}
        if what not in OWN_RX_ASKS["GET" if body is None else "POST"]:
            self._error("that isn't asked of a receiver")
            return
        if base not in own:
            self._error("that isn't a receiver of your own (a saved station makes it one)")
            return
        req = urllib.request.Request(f"{base}/{what}" + (f"?{urlencode(query, doseq=True)}" if query else ""), data=body,
                                     headers={"Content-Type": "application/json"} if body is not None else {})
        try:
            try:
                with urllib.request.urlopen(req, timeout=8 if body is None else 15) as r:
                    code, said = r.status, r.read()
            except urllib.error.HTTPError as e:        # (its own no, and its reason: passed back as they are)
                code, said = e.code, e.read()
        except (OSError, ValueError) as e:
            code, said = 502, json.dumps({"error": f"the receiver isn't answering ({str(getattr(e, 'reason', e))[:60]})"}).encode()
        if what == "tune" and code == 200 and station is not None:
            # (tuned from its window: if the radio is playing it, it keeps where -- a restart comes back there)
            try:
                now = json.loads(said).get("receiving") or {}
                station.retuned(base, now.get("freq"), now.get("mode"))
            except (ValueError, AttributeError):
                pass
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(said)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(said)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser went away mid-reply; nothing to do

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
                "source_error": st.get("source_error"), "receiver_names": self._receiver_names(),
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

    def get(self, path) -> bool:
        if path == "/api/radio":
            self._send(json.dumps(self._radio_state()).encode(), "application/json")
            return True
        if path == "/api/radio/search":
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0][:200]
            try:
                found, where = directory.search(q) if directory is not None else (radio_mod.search(q), "online")
            except radio_mod.StreamError as e:
                self._error(str(e))
                return True
            self._send(json.dumps({"results": found, "from": where,
                                   "directory": directory.status() if directory else None}).encode(),
                       "application/json")
            return True
        if path in ("/api/rooms", "/api/rooms/search"):
            # Rooms (radio amateurs' digital voice: playback/room.py). /api/rooms: the radio's callsign and
            # whether the voice decoder is on it; /search?q=: the public register of rooms, by words.
            from sleepradiopi.playback import monitor as monitor_mod, room as room_mod, rooms_dir
            if path == "/api/rooms":
                monitor = getattr(station, "monitor", None)
                self._send(json.dumps({"callsign": room_mod.CALLSIGN or "", "decoder": room_mod.DECODER.is_file(),
                                       "decoder_path": str(room_mod.DECODER), "level": room_mod.LEVEL,
                                       "music": round(monitor_mod.DUCK * 100),
                                       "monitor": monitor.status() if monitor is not None else None}).encode(), "application/json")
            else:
                book = rooms_dir.shared()
                found = book.search(parse_qs(urlparse(self.path).query).get("q", [""])[0][:200])
                self._send(json.dumps({"results": found, "directory": book.status()}).encode(), "application/json")
            return True
        if path == "/api/rx/morse":
            # The Morse reader (audio/morse.py): whether it's on, the tape, and the overs read since ?since=N.
            from sleepradiopi.audio import morse as morse_mod
            try:
                since = int(parse_qs(urlparse(self.path).query).get("since", ["0"])[0])
            except ValueError:
                since = 0
            self._send(json.dumps(morse_mod.reader.status(since)).encode(), "application/json")
            return True
        if path == "/api/rx/ft8":
            # The FT8 / FT4 reader (audio/ft8.py): whether it's on, and the messages read since ?since=N.
            from sleepradiopi.audio import ft8 as ft8_mod
            try:
                since = int(parse_qs(urlparse(self.path).query).get("since", ["0"])[0])
            except ValueError:
                since = 0
            self._send(json.dumps(ft8_mod.reader.status(since)).encode(), "application/json")
            return True
        if path == "/api/rx/pictures":
            # The SSTV / weather fax reader (audio/pictures.py): whether it's on, what it's doing, the pictures it has.
            from sleepradiopi.audio import pictures as pictures_mod
            self._send(json.dumps(pictures_mod.reader.status()).encode(), "application/json")
            return True
        if path.startswith("/api/rx/pictures/") and path.endswith(".png"):
            # One of those pictures, as far as it has been read, as a PNG file. ?clean=1: with the speckle taken out.
            from sleepradiopi.audio import pictures as pictures_mod
            try:
                picture = pictures_mod.reader.picture(int(path[len("/api/rx/pictures/"):-4]))
            except ValueError:
                picture = None
            if picture is None:
                self._error("no such picture")
                return True
            if parse_qs(urlparse(self.path).query).get("clean", [""])[0] == "1":
                picture = pictures_mod.clean(picture)
            self._send(pictures_mod.png(picture), "image/png")
            return True
        if path == "/api/rx/name":
            # The names given to receivers on the desktop.
            self._send(json.dumps({"receiver_names": self._receiver_names()}).encode(), "application/json")
            return True
        if path == "/api/rx/calibrate":
            # A receiver of your own being put on frequency (playback/calibrate.py): how far it has got, what it found.
            # "itself": the receivers the radio calibrates by itself, and what that found lately.
            from sleepradiopi.playback import calibrate as calibrate_mod
            self._send(json.dumps({**calibrate_mod.job.status(), "itself": calibrate_mod.keeper.status()}).encode(), "application/json")
            return True
        if path == "/api/rx/keyboard":
            # The PSK31 / RTTY reader (audio/keyboard.py): whether it's on, what it's reading now, and the
            # pieces of text read since ?since=N.
            from sleepradiopi.audio import keyboard as keyboard_mod
            try:
                since = int(parse_qs(urlparse(self.path).query).get("since", ["0"])[0])
            except ValueError:
                since = 0
            self._send(json.dumps(keyboard_mod.reader.status(since)).encode(), "application/json")
            return True
        if path == "/api/rx/identify":
            # What's this? (audio/identify.py): how the listening is going, and what it was when it's done.
            from sleepradiopi.audio import identify as identify_mod
            self._send(json.dumps(identify_mod.listener.status()).encode(), "application/json")
            return True
        if path == "/api/rx/survey":
            # The survey run by the radio (playback/survey.py): how far a round has got; with ?base=, what's been
            # found on that receiver; "itself": the receivers surveyed on the hour.
            from sleepradiopi.playback import survey as survey_mod
            base = parse_qs(urlparse(self.path).query).get("base", [""])[0].rstrip("/")
            self._send(json.dumps({**survey_mod.job.status(), "found": survey_mod.job.found(base) if base else {},
                                   "itself": survey_mod.rounds.status()}).encode(), "application/json")
            return True
        if path == "/api/rx/report":
            # Reporting what's heard to PSK Reporter and WSPRnet (audio/report.py): whether it's on, and how it's doing.
            from sleepradiopi.audio import ft8 as ft8_mod, report as report_mod
            self._send(json.dumps({**report_mod.reporter.status(), "now": ft8_mod.reader.reports()}).encode(), "application/json")
            return True
        if path == "/api/rx/ft8/heard":
            # Who the FT8 / FT4 reader has heard, a band at a time, the latest first (since the radio started).
            from sleepradiopi.audio import ft8 as ft8_mod
            self._send(json.dumps(ft8_mod.reader.heard()).encode(), "application/json")
            return True
        if path == "/api/rx/heard":
            # Who every reader has heard (audio/heard.py: FT8, FT4, WSPR, Morse, PSK31, RTTY), the latest first:
            # {"home" (the radio's locator), "call" (the owner's), "where" (as the page said), "heard": [{"band", "call",
            # "family" ("wsjt" | "cw" | "psk" | "rtty"), "mode", "first", "last", "count", "cq", "snr"?, "grid"?, "km"?, "deg"?, "hz"?}]}
            self._send(json.dumps(self._heard_list()).encode(), "application/json")
            return True
        if path.startswith("/api/rx/own/"):
            self._own_receiver(path)
            return True
        if path == "/api/rx":
            # The rig on the desktop: the receiver being listened to (a KiwiSDR), where it's tuned and
            # its waterfall since row ?since=N. {"on": false} when the radio is playing something else.
            stream = getattr(station, "radio_stream", None)
            query = parse_qs(urlparse(self.path).query)
            try:
                since = int(query.get("since", ["0"])[0])
            except ValueError:
                since = 0
            state = {"on": True, **stream.rig(since)} if hasattr(stream, "rig") else {"on": False}
            want, monitor = query.get("base", [""])[0], getattr(station, "monitor", None)
            if want and state.get("base") != want and monitor is not None:      # (a room being monitored, not the station)
                watched = monitor.state(want)
                if watched is not None:
                    state = {"on": True, **watched}
            self._send(json.dumps(state).encode(), "application/json")
            return True
        if path in ("/api/receivers/search", "/api/receivers/info"):
            # Find a receiver: the public directory of internet software radios (OpenWebRX, KiwiSDR).
            # search?q=&kind=owrx|kiwi&near=LAT,LON -> the nearest first; info?url= -> one receiver's own word.
            from sleepradiopi.playback import receivers_dir
            q = parse_qs(urlparse(self.path).query)
            one = lambda k: q.get(k, [""])[0][:300]
            book = receivers_dir.shared()
            try:
                if path.endswith("/info"):
                    self._send(json.dumps(book.info(one("url"))).encode(), "application/json")
                    return True
                near = None
                if one("near"):
                    lat, lon = (float(v) for v in one("near").split(",")[:2])
                    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                        raise ValueError("near is LATITUDE,LONGITUDE")
                    near = (lat, lon)
                found = book.search(one("q"), one("kind") if one("kind") in ("owrx", "kiwi") else "", near)
            except ValueError as e:
                self._error(str(e))
                return True
            self._send(json.dumps({"results": found, "directory": book.status()}).encode(), "application/json")
            return True
        return False

    def post(self, path) -> bool:
        if path == "/api/radio/directory" and directory is not None:
            self._body()
            if not directory.refreshing:          # fetch a fresh copy now, in the background
                threading.Thread(target=lambda: _quietly(directory.refresh), name="station-directory",
                                 daemon=True).start()
            self._send(json.dumps({**directory.status(), "refreshing": True}).encode(), "application/json")
            return True
        if path == "/api/rx/nr":
            # {"level": "off" | "light" | "strong" | "rig"}: receivers' noise reduction (at once). Kept.
            from sleepradiopi.audio import denoise as denoise_mod
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                level = denoise_mod.clean_level(body["level"])
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) and "noise" in str(e) else "noise reduction is off, light, strong or rig")
                return True
            denoise_mod.LEVEL = level
            if config_file is not None:
                save_setting(config_file, "noise_reduction", level)
            self._send(json.dumps({"nr": level}).encode(), "application/json")
            return True
        if path == "/api/rx/morse":
            # {"on": true | false}: the Morse reader for receivers (audio/morse.py). Kept over a restart.
            from sleepradiopi.audio import morse as morse_mod
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if not isinstance(body["on"], bool):
                    raise ValueError
            except (ValueError, TypeError, KeyError, AttributeError):
                self._error('send {"on": true | false}')
                return True
            morse_mod.reader.switch(body["on"])
            self._send(json.dumps(self._one_reader("cw" if body["on"] else None, keep=True)).encode(), "application/json")
            return True
        if path == "/api/rx/ft8":
            # {"on": true | false, "mode"?: "ft8" | "ft4", "band"?: "40 m", "dial"?: Hz, "own"?: true}: the FT8 / FT4
            # reader for receivers (audio/ft8.py); band is what the page says the receiver is on, for the list of
            # who's been heard; dial is where it's tuned and own that it's a receiver of the owner's own (the one
            # the radio is playing): what's read from one of those can be reported (audio/report.py).
            # Kept over a restart (audio/readers.py).
            from sleepradiopi.audio import ft8 as ft8_mod
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if not isinstance(body["on"], bool) or body.get("mode", "ft8") not in ft8_mod.MODES or not isinstance(body.get("band", ""), str):
                    raise ValueError
                dial = body.get("dial")
                if dial is not None and (isinstance(dial, bool) or not isinstance(dial, (int, float)) or not 100_000 <= dial <= 500_000_000):
                    raise ValueError
            except (ValueError, TypeError, KeyError, AttributeError):
                self._error('send {"on": true | false, "mode": "ft8" | "ft4" | "wspr"}')
                return True
            if body["on"] and not ft8_mod.can(body.get("mode") or ft8_mod.reader.mode):
                self._error("this radio has no WSPR decoder" if (body.get("mode") or ft8_mod.reader.mode) == "wspr" else "this radio has no FT8 decoder")
                return True
            own = None
            if body.get("own") is True and station is not None:      # (its address is the radio's word, not the page's)
                at = urlparse((station.source or {}).get("url") or "")
                own = f"{at.scheme}://{at.netloc}" if at.scheme in ("http", "https") and at.netloc else None
                ft8_mod.reader.playing = lambda: (station.source or {}).get("url")
            ft8_mod.reader.switch(body["on"], body.get("mode"), body.get("band"), None if dial is None else int(dial), own)
            self._send(json.dumps(self._one_reader("ft8" if body["on"] else None, keep=True)).encode(), "application/json")
            return True
        if path == "/api/rx/survey":
            # {"base", "places": [{"key", "mode", "band", "hz"}]}: take that receiver of your own round those places
            # now and write down what's on each (playback/survey.py). {"stop": true}: the round stopped.
            # {"base", "itself": 0 | 1 | 2 | 3 | 6, "places"?}: surveyed by the radio itself, that many hours apart
            # (0: not), round those places. Kept. {"base", "forget": true}: what was found there forgotten.
            from sleepradiopi.playback import survey as survey_mod
            try:
                body = self._body()
                if body.get("stop") is True:
                    survey_mod.job.stop()
                else:
                    base = survey_mod.clean_base(body["base"])
                    if body.get("forget") is True:
                        survey_mod.job.forget(base)
                    elif "itself" in body:
                        plan = {b: h for b, h in survey_mod.rounds.plan.items() if b != base}
                        if body["itself"] != 0:
                            plan[base] = {"every_h": body["itself"], "places": body.get("places")}
                        plan = survey_mod.clean_plan(plan)
                        survey_mod.rounds.set_plan(plan)
                        if config_file is not None:
                            save_setting(config_file, "survey_itself", plan)
                    else:
                        survey_mod.job.start(base, survey_mod.clean_places(body.get("places")))
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else 'send {"base": "http://the receiver", "places": [...]}')
                return True
            base = str(body.get("base") or "").rstrip("/")
            self._send(json.dumps({**survey_mod.job.status(), "found": survey_mod.job.found(base) if base else {},
                                   "itself": survey_mod.rounds.status()}).encode(), "application/json")
            return True
        if path == "/api/rx/report":
            # {"on": true | false}: what's heard on a receiver of your own reported to PSK Reporter (FT8, FT4) and
            # WSPRnet (WSPR), as heard by your callsign at your locator. Both must be set. Kept.
            from sleepradiopi.audio import ft8 as ft8_mod, report as report_mod
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if not isinstance(body["on"], bool):
                    raise KeyError
                report_mod.reporter.switch(body["on"])
            except ValueError as e:
                self._error(str(e) if "callsign" in str(e) else 'send {"on": true | false}')
                return True
            except (TypeError, KeyError, AttributeError):
                self._error('send {"on": true | false}')
                return True
            if config_file is not None:
                save_setting(config_file, "report_heard", body["on"])
            self._send(json.dumps({**report_mod.reporter.status(), "now": ft8_mod.reader.reports()}).encode(), "application/json")
            return True
        if path == "/api/rx/ft8/heard":
            # {"clear": true}: the list of who's been heard emptied.
            from sleepradiopi.audio import ft8 as ft8_mod
            self._body()
            ft8_mod.reader.forget()
            self._send(json.dumps(ft8_mod.reader.heard()).encode(), "application/json")
            return True
        if path == "/api/rx/heard":
            # {"clear": true, "families"?: ["cw", ...]}: who's been heard forgotten -- those families' (a panel's), or everyone's.
            from sleepradiopi.audio import heard as heard_mod
            try:
                body = self._body()
                fams = body.get("families")
                if body.get("clear") is not True or not (fams is None or (isinstance(fams, list) and fams and set(fams) <= set(heard_mod.FAMILIES.values()))):
                    raise ValueError
            except (ValueError, AttributeError):
                self._error('send {"clear": true}, with "families": ["wsjt", "cw", "psk", "rtty"] for only some')
                return True
            heard_mod.store.forget(set(fams) if fams else None)
            self._send(json.dumps(self._heard_list()).encode(), "application/json")
            return True
        if path == "/api/rx/where":
            # {"band": "40 m" | null, "dial": Hz | null, "sideband": "usb" | "lsb" | "cw" | ..., "centre"?: Hz, "own"?: true}:
            # where the receiver the radio is playing is tuned, as the page has it: the one message every reader is
            # told by. Each station heard is kept by band, with where it was on the air (audio/heard.py), and FT8's
            # reader is told the band, the dial and -- with own, a receiver of the owner's own (the one the radio is
            # playing: its address is the radio's word, not the page's) -- what may be reported (audio/report.py).
            from sleepradiopi.audio import ft8 as ft8_mod, heard as heard_mod
            try:
                body = self._body()
                band, dial, side, centre = body.get("band"), body.get("dial"), body.get("sideband"), body.get("centre", 0)
                for v, lo, hi in ((dial, 100_000, 500_000_000), (centre, 0, 5_000)):
                    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi):
                        raise ValueError
                if not (band is None or (isinstance(band, str) and len(band) <= 8)) or not (side is None or (isinstance(side, str) and side.isalpha() and len(side) <= 4)):
                    raise ValueError
            except (ValueError, AttributeError):
                self._error('send {"band": "40 m", "dial": 7020000, "sideband": "usb"}')
                return True
            heard_mod.store.where = {"band": band or None, "dial": int(dial) if dial else None, "sideband": side, "centre": int(centre or 0)}
            own = None
            if body.get("own") is True and station is not None:
                at = urlparse((station.source or {}).get("url") or "")
                own = f"{at.scheme}://{at.netloc}" if at.scheme in ("http", "https") and at.netloc else None
                ft8_mod.reader.playing = lambda: (station.source or {}).get("url")
            ft8_mod.reader.place(band or None, int(dial) if dial else None, own)
            self._send(json.dumps({"where": heard_mod.store.where}).encode(), "application/json")
            return True
        if path == "/api/rx/keyboard":
            # {"on": true | false, "mode"?: "psk31" | "rtty"}: the PSK31 / RTTY reader for receivers
            # (audio/keyboard.py). Kept over a restart.
            from sleepradiopi.audio import keyboard as keyboard_mod
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if not isinstance(body["on"], bool) or body.get("mode", "psk31") not in keyboard_mod.MODES:
                    raise ValueError
            except (ValueError, TypeError, KeyError, AttributeError):
                self._error('send {"on": true | false, "mode": "psk31" | "rtty"}')
                return True
            keyboard_mod.reader.switch(body["on"], body.get("mode"))
            self._send(json.dumps(self._one_reader("keyboard" if body["on"] else None, keep=True)).encode(), "application/json")
            return True
        if path == "/api/rx/calibrate":
            # {"base": "http://192.168.50.154:8074"}: put that receiver of your own on frequency: by FT8 heard on it and
            # on a KiwiSDR among the stations at the same moment, or failing that from broadcast stations
            # (it's taken round the bands for a few minutes: playback/calibrate.py).
            # With "ppm" and "trim": set it to those instead (to undo one).
            from sleepradiopi.playback import calibrate as calibrate_mod
            try:
                body = self._body()
                base = str(body["base"]).rstrip("/")
                if not base.startswith(("http://", "https://")) or any(c.isspace() for c in base):
                    raise ValueError("base is the receiver's address: http://192.168.50.154:8074")
                if "itself" in body:
                    # {"base", "itself": "never" | "night" | "listening"}: calibrated by the radio itself, and when. Kept.
                    plan = {b: w for b, w in calibrate_mod.keeper.status()["plan"].items() if b != base}
                    if body["itself"] != "never":
                        plan[base] = body["itself"]
                    plan = calibrate_mod.clean_plan(plan)
                    calibrate_mod.keeper.set_plan(plan)
                    if config_file is not None:
                        save_setting(config_file, "calibrate_itself", plan)
                    self._send(json.dumps({**calibrate_mod.job.status(), "itself": calibrate_mod.keeper.status()}).encode(), "application/json")
                    return True
                if "ppm" in body or "trim" in body:
                    if isinstance(body.get("ppm"), bool) or isinstance(body.get("trim"), bool) or not isinstance(body.get("ppm"), int) or not isinstance(body.get("trim"), int):
                        raise ValueError("ppm and trim are whole numbers")
                    self._send(json.dumps(calibrate_mod.job.set(base, body["ppm"], body["trim"])).encode(), "application/json")
                    return True
                # (a KiwiSDR among the stations, if there is one: FT8 heard on both at once is the better yardstick)
                from sleepradiopi.playback import receiver as receiver_mod
                kiwis = [spec["base"] for spec in (receiver_mod.parse(str(st.get("url", ""))) for st in self._radio_stations()) if spec and spec["kind"] == "kiwi"]
                calibrate_mod.job.start(base, kiwis[0] if kiwis else None)
            except (ValueError, TypeError, KeyError, AttributeError, OSError) as e:
                self._error(str(e) if isinstance(e, ValueError) else 'send {"base": "http://the receiver"}' if not isinstance(e, OSError) else f"the receiver isn't answering ({e})")
                return True
            self._send(json.dumps(calibrate_mod.job.status()).encode(), "application/json")
            return True
        if path == "/api/rx/pictures":
            # {"on": true | false, "mode"?: "sstv" | "fax"}: the reader for SSTV pictures and weather charts
            # (audio/pictures.py); kept over a restart. Or {"begin": true}: read a chart from now, without
            # waiting for its start tone; {"end": true}: the picture being read is finished here.
            # {"slide": 0.37, "n": 5}: chart 5's lines begin that far across it: it's turned round to match.
            from sleepradiopi.audio import pictures as pictures_mod
            try:
                body = self._body()
                if "slide" in body:
                    if isinstance(body.get("n"), bool) or not isinstance(body.get("n"), int):
                        raise ValueError("n is the chart's number")
                    pictures_mod.reader.slide(body["n"], body["slide"])
                    self._send(json.dumps(pictures_mod.reader.status()).encode(), "application/json")
                    return True
                if body.get("begin") is True or body.get("end") is True:
                    pictures_mod.reader.order("begin" if body.get("begin") is True else "end")
                    self._send(json.dumps(pictures_mod.reader.status()).encode(), "application/json")
                    return True
                if not isinstance(body["on"], bool) or body.get("mode", "sstv") not in pictures_mod.MODES:
                    raise ValueError
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) and str(e) else 'send {"on": true | false, "mode": "sstv" | "fax"}, {"begin": true} or {"end": true}')
                return True
            pictures_mod.reader.switch(body["on"], body.get("mode"))
            self._send(json.dumps(self._one_reader("pictures" if body["on"] else None, keep=True)).encode(), "application/json")
            return True
        if path == "/api/rx/name":
            # {"base": "http://192.168.50.4:8075", "name": "Short wave"} ("" for its own again): what a receiver is
            # called on the desktop -- two on one computer are otherwise only told apart by their ports. Kept.
            try:
                body = self._body()
                base, name = str(body["base"]).strip(), " ".join(str(body.get("name") or "").split())[:30]
                if not base or len(base) > 300 or any(c.isspace() for c in base):
                    raise ValueError("base is the receiver's address, as the desktop has it")
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) and "base" in str(e) else 'send {"base": "http://the receiver", "name": "Short wave"}')
                return True
            names = self._receiver_names()
            if name:
                names[base] = name
            else:
                names.pop(base, None)
            if config_file is not None:
                save_setting(config_file, "receiver_names", names)
            self._send(json.dumps({"receiver_names": names}).encode(), "application/json")
            return True
        if path == "/api/rx/locator":
            # {"locator": "IO94"} ("" clears it): where the radio is, for how far an FT8 station is. Kept.
            from sleepradiopi.audio import ft8 as ft8_mod
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                home = ft8_mod.clean_locator(body.get("locator"))
            except (ValueError, TypeError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) and "locator" in str(e) else 'send {"locator": "IO94"}')
                return True
            ft8_mod.HOME = home or None
            if config_file is not None:
                save_setting(config_file, "locator", home or None)
            self._send(json.dumps({"home": home or None}).encode(), "application/json")
            return True
        if path == "/api/rooms/level":
            # {"level": 25-150}: how loud rooms are, % (at once: also for one that's playing). Kept.
            from sleepradiopi.playback import room as room_mod
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                level = int(body["level"])
                if isinstance(body["level"], bool) or not room_mod.MIN_LEVEL <= level <= room_mod.MAX_LEVEL:
                    raise ValueError
            except (ValueError, TypeError, KeyError, AttributeError):
                self._error(f"the room level is {room_mod.MIN_LEVEL} to {room_mod.MAX_LEVEL} (%)")
                return True
            room_mod.LEVEL = level
            if config_file is not None:
                save_setting(config_file, "room_level", level)
            self._send(json.dumps({"level": level}).encode(), "application/json")
            return True
        if path == "/api/rooms/music":
            # {"level": 0-100}: how loud the programme stays under a monitored room, % of itself (at once). Kept.
            from sleepradiopi.playback import monitor as monitor_mod
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                level = int(body["level"])
                if isinstance(body["level"], bool) or not 0 <= level <= 100:
                    raise ValueError
            except (ValueError, TypeError, KeyError, AttributeError):
                self._error("the music under a room is 0 to 100 (%)")
                return True
            monitor_mod.DUCK = level / 100.0
            if config_file is not None:
                save_setting(config_file, "monitor_music", level)
            self._send(json.dumps({"music": level}).encode(), "application/json")
            return True
        if path == "/api/rooms/monitor":
            # {"url", "name", "on": true | false}: hear that room over whatever's playing (or stop). Kept.
            from sleepradiopi.playback import monitor as monitor_mod
            monitor = getattr(station, "monitor", None)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if monitor is None:
                    raise ValueError("this radio can't monitor rooms")
                rooms = [r for r in monitor.rooms if r["url"] != body.get("url")]
                if body.get("on", True):
                    rooms.append({"name": body.get("name"), "url": body.get("url")})
                rooms = monitor.set_rooms(monitor_mod.clean_rooms(rooms))
            except (ValueError, TypeError, AttributeError) as e:
                self._error(str(e))
                return True
            if config_file is not None:
                save_setting(config_file, "monitor_rooms", rooms)
            self._send(json.dumps({"monitor": monitor.status()}).encode(), "application/json")
            return True
        if path == "/api/rooms/callsign":
            # {"callsign": "M0ABC"} ("" clears it): what rooms are joined under. Kept.
            from sleepradiopi.playback import room as room_mod
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                call = room_mod.clean_callsign(body.get("callsign"))
            except (ValueError, TypeError, AttributeError) as e:
                self._error(str(e))
                return True
            room_mod.CALLSIGN = call or None
            if getattr(station, "monitor", None) is not None:
                station.monitor.restart()                      # (rooms being monitored are joined again under it)
            if config_file is not None:
                save_setting(config_file, "callsign", call or None)
            self._send(json.dumps({"callsign": call, "decoder": room_mod.DECODER.is_file()}).encode(), "application/json")
            return True
        if path.startswith("/api/rx/own/"):
            self._own_receiver(path, self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)))
            return True
        if path == "/api/rx/identify":
            # {"hz": where the receiver being played is tuned, "mode": "usb" | "lsb" | "am" | ...}: What's this? The
            # radio listens to what it's playing for twelve seconds and names it (audio/identify.py): then GET it.
            from sleepradiopi.audio import identify as identify_mod
            try:
                body = self._body()
                if not isinstance(body, dict):
                    raise ValueError('send {"hz": Hz, "mode": "usb"}')
                said = identify_mod.listener.start(body.get("hz"), body.get("mode") or "usb")
            except (ValueError, TypeError) as e:
                self._error(str(e))
                return True
            self._send(json.dumps(said).encode(), "application/json")
            return True
        if path == "/api/rx/tune":
            # {"base", "freq" (Hz), "mode", "zoom", "centre" (Hz), "profile"}: retune the receiver being listened to, where
            # it is (any of them; "base" must be the one that's on, so a stale window can't move another).
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                stream = getattr(station, "radio_stream", None)
                if not hasattr(stream, "tune") or (stream.spec or {}).get("base") != body.get("base"):
                    raise ValueError("the radio isn't listening to that receiver")
                num = lambda k: None if body.get(k) is None else int(float(body[k]))
                stream.tune(num("freq"), None if body.get("mode") is None else str(body["mode"]), num("zoom"), num("centre"),
                            None if body.get("profile") is None else str(body["profile"]))
            except (ValueError, TypeError, AttributeError) as e:
                self._error(str(e))
                return True
            except Exception as e:                 # (the connection went as it was told)
                self._error(f"the receiver didn't take it ({str(e)[:60]})")
                return True
            self._send(json.dumps({"on": True, **stream.rig(1 << 60)}).encode(), "application/json")
            return True
        if path in ("/api/radio/play", "/api/radio/stop", "/api/radio/stations"):
            self._radio(path.rsplit("/", 1)[1])
            return True
        return False

    return {"methods": {"_qrz": _qrz, "_one_reader": _one_reader, "_heard_list": _heard_list, "_receiver_names": _receiver_names, "_own_receiver": _own_receiver, "_radio_stations": _radio_stations, "_radio_state": _radio_state, "_radio": _radio},
            "get": get, "post": post}
