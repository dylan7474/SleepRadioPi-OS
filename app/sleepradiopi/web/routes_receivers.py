"""Radio stations, receivers and the ham readers: stations, the radio, own receivers, QRZ, what was heard.

Methods of the web handler, moved out of server.py as they were: build(ctx) unpacks what they use of what
make_handler holds and returns them for make_handler to attach to Handler.
"""

from __future__ import annotations

import json
import urllib.request
from sleepradiopi.config.settings import load as load_settings
from sleepradiopi.config.settings import save_setting
from sleepradiopi.playback import radio as radio_mod
from urllib.parse import parse_qs
from urllib.parse import urlencode
from urllib.parse import urlparse
from sleepradiopi.web.common import OWN_RX, OWN_RX_ASKS


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

    return {"_qrz": _qrz, "_one_reader": _one_reader, "_heard_list": _heard_list, "_receiver_names": _receiver_names, "_own_receiver": _own_receiver, "_radio_stations": _radio_stations, "_radio_state": _radio_state, "_radio": _radio}
