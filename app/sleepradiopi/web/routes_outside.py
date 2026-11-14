"""The world outside and band conditions: conditions, lightning, quakes, warnings, QRZ, the health light.

Methods of the web handler and its routes, moved out of server.py as they were: build(ctx) unpacks what they use of
what make_handler holds and returns them (and the GET / POST branches of this area) for make_handler to attach.
"""

from __future__ import annotations

import json
import time
from sleepradiopi import wifi as wifi_mod
from sleepradiopi.config.settings import save_setting
from urllib.parse import parse_qs
from urllib.parse import urlparse


def build(ctx):
    config_file, speaker = ctx.config_file, ctx.speaker

    def air_state() -> dict:
        from sleepradiopi.playback import aircraft as air_mod
        had = air_mod.aircraft.status(False)
        return {"address": air_mod.ADDRESS, "watch": air_mod.WATCH, "up": bool(had), "t": had and had["t"], "n": had and had["n"], "placed": had and had["placed"],
                "alerts": [{"what": w, "short": sh, "says": words} for w, _, words, sh in air_mod.alerts(had) if w.split(":")[-1] not in air_mod.EMERGENCY],
                "flagged": air_mod.flagged(had), "off": sorted(air_mod.OFF), "kinds": {k: list(v) for k, v in air_mod.KINDS.items()},
                "planes": (had or {}).get("planes") or [], "emergency": (had or {}).get("emergency") or [], "far_nm": had and had["far_nm"],
                "aerial": bool(had and had["aerial"]), "at": had and had.get("at"), "range": air_mod.RANGE, "ranges": list(air_mod.SCOPE_RANGES), "from": had and had["from"], "says": had and had["says"], "low_ft": air_mod.LOW_FT}

    def r433_state() -> dict:
        from sleepradiopi.playback import radio433 as r_mod
        had = r_mod.radio433.status(False)
        return {"address": r_mod.ADDRESS, "up": bool(had), "t": had and had["t"], "now": had and r_mod.state_word(had), "paused": bool(had and had.get("job") != "scan433"),
                "windows": (had or {}).get("windows") or [], "messages": had and had["messages"], "devices": (had or {}).get("devices") or [],
                "activity": (had or {}).get("activity") or [], "series": (had or {}).get("series") or {}, "says": had and had["says"]}

    def sondes_state() -> dict:
        from sleepradiopi.playback import radio433 as r_mod, sondes as s_mod
        had = s_mod.sondes.status(False)
        t = time.gmtime()
        nxt = s_mod.next_window((had or {}).get("windows") or [], t.tm_hour * 60 + t.tm_min)
        return {"address": r_mod.ADDRESS, "up": bool(had), "t": had and had["t"], "now": had and s_mod.sonde_word(had), "job": had and had.get("job"), "windows": (had or {}).get("windows") or [],
                "window": nxt and {"start": nxt[0], "end": nxt[1], "on": nxt[2]}, "running": bool(had and had.get("running")), "flights": (had or {}).get("flights") or s_mod.sondes.kept(),
                "pins": s_mod.MAX_PINS, "aerial": (had or {}).get("aerial") or (list(a) if (a := s_mod.aerial()) else None), "says": had and had["says"], "words": s_mod.STATE_WORDS}

    def get(self, path) -> bool:
        if path == "/api/rx/conditions":
            # How the bands are (playback/conditions.py): from that receiver's survey (?base=) and who the FT8
            # reader has heard in the last hour, a band at a time and in a sentence; the sun's figures from NOAA,
            # N0NBH's forecast and AuroraWatch UK's alert. "alerts": whether the DJ says those when they begin.
            from sleepradiopi.audio import ft8 as ft8_mod
            from sleepradiopi.playback import conditions as conditions_mod, survey as survey_mod
            # With no receiver named: the one surveyed latest. "say_every_h": how often the DJ says it (0: never).
            base = parse_qs(urlparse(self.path).query).get("base", [""])[0].rstrip("/")
            found = survey_mod.job.found(base, week=True) if base else {}
            if not base:
                base, found = conditions_mod.freshest(survey_mod.job)
            out = conditions_mod.summary(found, ft8_mod.reader.heard()["heard"], time.time())
            sun_now, fc, aurora_now = conditions_mod.sun.status(), conditions_mod.forecast.status(), conditions_mod.aurora.status()
            day = conditions_mod.daytime(time.time(), ft8_mod.HOME)
            self._send(json.dumps({**out, "sun": sun_now, "forecast": fc, "aurora": aurora_now, "day": day, "home": ft8_mod.HOME,
                                   "base": base or None, "say_every_h": conditions_mod.EVERY_H, "alerts": conditions_mod.ALERTS, "aurora_means": conditions_mod.AURORA_MEANS,
                                   "spoken": conditions_mod.spoken(out, sun_now, fc, aurora_now, day)}).encode(), "application/json")
            return True
        if path == "/api/outside":
            # The desktop's "The world outside" (playback/conditions.py outside()): a tile for each thing known
            # from outside -- the bands, the sun, the aurora, VHF openings -- and the alerts there are now. It only
            # reads what's kept: conditions.keeper (a thread, started in main.py) keeps the feeds fresh; figures
            # past their cutoff but under a day old come back marked old, and show as an old tile (never as an alert).
            from sleepradiopi.audio import ft8 as ft8_mod
            from sleepradiopi.playback import conditions as conditions_mod, survey as survey_mod
            out = conditions_mod.summary(conditions_mod.freshest(survey_mod.job)[1], ft8_mod.reader.heard()["heard"], time.time())
            from sleepradiopi.playback import aircraft as air_mod, lightning as lightning_mod, qrzlog as qrz_mod, quakes as quakes_mod, radio433 as r433_mod, sondes as sondes_mod, spots as spots_mod, ukwarnings as warn_mod
            self._send(json.dumps(conditions_mod.with_placeholders(conditions_mod.outside(out, conditions_mod.sun.status(False, True), conditions_mod.forecast.status(False, True), conditions_mod.aurora.status(False, True),
                                                         conditions_mod.daytime(time.time(), ft8_mod.HOME), quakes_mod.quakes.status(False, True),
                                                         warn_mod.warnings.status(False, True), spots_mod.spots.status(False, True), qrz_mod.logbook.status(False, True),
                                                         lightning_mod.watch.status(), air_mod.aircraft.status(False),
                                                         r433_mod.radio433.status(False), sondes_mod.sondes.status(False)))).encode(), "application/json")
            return True
        if path == "/api/health":
            # The menu-bar light (health.py): is something wrong? Quiet things that go wrong on a small radio, in a sentence each.
            from sleepradiopi import health as health_mod
            from sleepradiopi.config.clock import clock_trusted
            from sleepradiopi.playback import conditions as conditions_mod
            try:
                offline = bool(wifi_mod.load()["offline"])
            except Exception:
                offline = False
            self._send(json.dumps(health_mod.health.issues(conditions_mod.outside_feeds(), offline, clock_trusted())).encode(), "application/json")
            return True
        if path == "/api/notify":
            # Notifications (playback/notify.py): every kind of alert, with its mode (off | active | always), and whether
            # Flite is here to say them.
            from sleepradiopi.playback import lightning as lightning_mod, notify as notify_mod
            from sleepradiopi.tts import flite as flite_mod
            kinds = [{"key": k, "label": label, "mode": lightning_mod.WHEN if k == "lightning" else notify_mod.mode(k)} for k, label in notify_mod.KINDS.items()]
            self._send(json.dumps({"kinds": kinds, "modes": list(notify_mod.MODES), "flite": flite_mod.available()}).encode(), "application/json")
            return True
        if path == "/api/lightning":
            # The Lightning window (playback/lightning.py): strikes within "area_km" of the locator in the last three
            # hours (the latest first), whether it's listening now, when it last did, and how much of the world it heard.
            from sleepradiopi.playback import conditions as conditions_mod, lightning as lightning_mod
            self._send(json.dumps({**lightning_mod.watch.status(), "alerts": conditions_mod.ALERTS}).encode(), "application/json")
            return True
        if path == "/api/qrz":
            # The QRZ logbook window (playback/qrzlog.py): whether a key is set (never the key), the book's figures,
            # everything its STATUS said, the latest QSOs, and QRZ's reason if it said no.
            self._send(json.dumps(self._qrz()).encode(), "application/json")
            return True
        if path == "/api/sondes":
            # The Radiosondes window (playback/sondes.py): the flights heard, kept in memory (the address is the 433 MHz window's: one service).
            self._send(json.dumps(sondes_state()).encode(), "application/json")
            return True
        if path == "/api/sondes/now":
            # The flights being heard now (the page opens the live console by itself for a new one): from memory, cheap to ask often.
            from sleepradiopi.playback import sondes as s_mod
            self._send(json.dumps({"live": s_mod.sondes.live_now() if s_mod.sondes.wanted() else []}).encode(), "application/json")
            return True
        if path == "/api/sondes/live":
            # ?id=Y1722550&since=1794612726.3: the live console's few-seconds ask (playback/sondes.py Sondes.live: the receiver's new rows and its decoder).
            from sleepradiopi.playback import sondes as s_mod
            q = parse_qs(urlparse(self.path).query)
            try:
                since = float((q.get("since") or ["0"])[0])
            except ValueError:
                since = 0.0
            self._send(json.dumps(s_mod.sondes.live((q.get("id") or [""])[0], since)).encode(), "application/json")
            return True
        if path == "/api/sondes/profile":
            # ?id=H4713951: that flight's whole story for the 3D view: its track and its readings over time (columns + rows), from memory.
            from sleepradiopi.playback import sondes as s_mod
            try:
                doc = s_mod.sondes.profile((parse_qs(urlparse(self.path).query).get("id") or [""])[0])
            except s_mod.BadFlight as e:
                self._error(str(e))
                return True
            self._send(json.dumps(doc).encode(), "application/json")
            return True
        if path == "/api/sondes/export":
            # ?id=H4713951: that flight as a file to keep (JSON); without it, all the flights kept.
            from sleepradiopi.playback import sondes as s_mod
            q = parse_qs(urlparse(self.path).query)
            try:
                doc = s_mod.sondes.export((q.get("id") or [None])[0])
            except s_mod.BadFlight as e:
                self._error(str(e))
                return True
            name = (q.get("id") or ["radiosondes"])[0] if q.get("id") else "radiosondes-" + time.strftime("%Y%m%d-%H%M")
            self._send(json.dumps(doc, indent=1).encode(), "application/json", [("Content-Disposition", f'attachment; filename="{"".join(c for c in name if c.isalnum() or c in "-_.")}.json"')])
            return True
        if path == "/api/radio433":
            # The 433 MHz window (playback/radio433.py): the background service's address, whether it answers, what the scan is doing, the devices heard.
            self._send(json.dumps(r433_state()).encode(), "application/json")
            return True
        if path == "/api/mapdata":
            # The Aircraft scope's built-in map (data/map.json, made by tools/build_mapdata.py from public-domain data): read from the
            # image each time (not kept in memory) and told to be kept by the browser, so it is asked for once.
            from sleepradiopi.playback import aircraft as air_mod
            try:
                body = air_mod.MAP_FILE.read_bytes()
            except OSError:
                self._error("no map in this build")
                return True
            self._send(body, "application/json", [("Cache-Control", "public, max-age=604800")])
            return True
        if path == "/api/aircraft":
            # The Aircraft window (playback/aircraft.py): the receiver's address, whether it answers, and the aircraft near.
            from sleepradiopi.playback import aircraft as air_mod
            self._send(json.dumps(air_state()).encode(), "application/json")
            return True
        if path == "/api/warnings":
            # The Warnings window (playback/ukwarnings.py): the Met Office's for the region ("region": which one, chosen
            # or nearest the locator; "chosen": "" for nearest), the Environment Agency's floods near here.
            from sleepradiopi.audio import ft8 as ft8_mod
            from sleepradiopi.playback import conditions as conditions_mod, ukwarnings as warn_mod
            had = warn_mod.warnings.status() or {}
            self._send(json.dumps({"met": had.get("met") or [], "floods": had.get("floods") or [], "t": had.get("t"), "had": bool(had),
                                   "region": warn_mod.region_for(), "chosen": warn_mod.REGION, "regions": {k: v[0] for k, v in warn_mod.REGIONS.items()},
                                   "flood_km": warn_mod.FLOOD_KM, "home": ft8_mod.HOME, "alerts": conditions_mod.ALERTS}).encode(), "application/json")
            return True
        if path == "/api/spots":
            # The My signal window (playback/spots.py): who heard the owner's callsign in the last day (PSK Reporter).
            from sleepradiopi.audio import ft8 as ft8_mod
            from sleepradiopi.playback import spots as spots_mod
            had = spots_mod.spots.status() or {}
            call = ft8_mod.own_call()
            self._send(json.dumps({"call": call, "spots": had.get("spots") or [], "t": had.get("t"), "had": bool(had),
                                   "map": spots_mod.MAP.format(call) if call else None}).encode(), "application/json")
            return True
        if path == "/api/quakes":
            # The Earthquakes window (playback/quakes.py): the world's in the last 12 hours (USGS, 4.5 and over, the
            # biggest first) and the UK's (BGS, the latest first), each with "km" from the radio's locator;
            # "alerts": whether the DJ says a big one (Band conditions' Alerts), and how big that is.
            from sleepradiopi.playback import conditions as conditions_mod, quakes as quakes_mod
            had = quakes_mod.quakes.status() or {}
            far = lambda q: {**q, "km": quakes_mod.km_from_home(q["lat"], q["lon"])}
            self._send(json.dumps({"world": [far(q) for q in quakes_mod.lately(had, "world")], "uk": [far(q) for q in quakes_mod.lately(had, "uk")],
                                   "hours": round(quakes_mod.LATELY_S / 3600), "t": had.get("t"), "had": bool(had), "alerts": conditions_mod.ALERTS,
                                   "alert_at": {"world": quakes_mod.WORLD_ALERT, "uk": quakes_mod.UK_ALERT, "near": quakes_mod.UK_NEAR_ALERT, "near_km": quakes_mod.UK_NEAR_KM}}).encode(), "application/json")
            return True
        return False

    def post(self, path) -> bool:
        if path == "/api/rx/conditions":
            # {"say_every_h": 0 | 1 | 2 | 3 | 6}: the DJ says how the short-wave bands are, that many hours apart
            # (0: not at all), between songs, like the news. {"alerts": true | false}: and says it when an aurora
            # alert, a sporadic E opening or a strong storm begins. Either or both; kept.
            from sleepradiopi.playback import conditions as conditions_mod
            try:
                body = self._body()
                every, alerts = body.get("say_every_h"), body.get("alerts")
                if (every is None and alerts is None) or (every is not None and (isinstance(every, bool) or every not in conditions_mod.SAY_HOURS)) \
                        or (alerts is not None and not isinstance(alerts, bool)):
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                self._error('send {"say_every_h": 0, 1, 2, 3 or 6} and/or {"alerts": true or false}')
                return True
            if every is not None:
                conditions_mod.EVERY_H = every
                if config_file is not None:
                    save_setting(config_file, "conditions_every_h", every)
            if alerts is not None:
                conditions_mod.ALERTS = alerts
                from sleepradiopi.playback import notify as notify_mod
                notify_mod.set_default(alerts)               # (the old switch: what a kind not set in Notifications follows)
                if config_file is not None:
                    save_setting(config_file, "conditions_alerts", alerts)
            self._send(json.dumps({"say_every_h": conditions_mod.EVERY_H, "alerts": conditions_mod.ALERTS}).encode(), "application/json")
            return True
        if path == "/api/lightning":
            # {"on": true | false}: listening for lightning near here now and then (the setting lightning_watch);
            # {"when": "off" | "active" | "always"}: the bell when it comes near -- muted, only while the radio plays,
            # or playing or paused (lightning_when; never in the quiet hours); {"ring": true}: the bell now, to
            # hear it (quiet hours or not). Kept.
            from sleepradiopi.playback import lightning as lightning_mod
            try:
                body = self._body()
                on, when, ring = body.get("on"), body.get("when"), body.get("ring")
                if (on is None and when is None and ring is None) or any(v is not None and not isinstance(v, bool) for v in (on, ring)) \
                        or (when is not None and when not in lightning_mod.WHENS):
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                self._error('send {"on": true or false}, {"when": "off", "active" or "always"} or {"ring": true}')
                return True
            if on is not None:
                lightning_mod.ON = on
                if config_file is not None:
                    save_setting(config_file, "lightning_watch", on)
            if when is not None:
                lightning_mod.WHEN = when
                lightning_mod.BELL = when != "off"
                if config_file is not None:
                    save_setting(config_file, "lightning_when", when)
            if ring:
                if speaker is None:
                    self._error("this radio has no speaker")
                    return True
                from sleepradiopi.io.announce import Clip as BellClip
                speaker.play_clip(BellClip(lightning_mod.bell(), "bell", "Lightning"))
            self._send(json.dumps({"on": lightning_mod.ON, "bell": lightning_mod.BELL, "when": lightning_mod.WHEN}).encode(), "application/json")
            return True
        if path == "/api/notify":
            # {"modes": {"aurora": "off" | "active" | "always", ...}}: when each kind of alert may be said (kept; lightning's is
            # lightning_when); {"test": "aurora"}: say a sample of it now (lightning: ring the bell), quiet hours or not.
            from sleepradiopi.playback import lightning as lightning_mod, notify as notify_mod
            try:
                body = self._body()
                given, test = body.get("modes"), body.get("test")
                if (given is None and test is None) or (given is not None and (not isinstance(given, dict) or not given
                        or any(k not in notify_mod.KINDS or v not in notify_mod.MODES for k, v in given.items()))) \
                        or (test is not None and test not in notify_mod.KINDS):
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                self._error('send {"modes": {"aurora": "off", "quake": "active", "met": "always"}} or {"test": "aurora"}')
                return True
            if given:
                for k, v in given.items():
                    if k == "lightning":
                        lightning_mod.WHEN = v
                        lightning_mod.BELL = v != "off"
                        if config_file is not None:
                            save_setting(config_file, "lightning_when", v)
                    else:
                        notify_mod.modes[k] = v
                if config_file is not None and any(k != "lightning" for k in given):
                    save_setting(config_file, "notify", dict(notify_mod.modes))
            if test:
                if test == "lightning":
                    if speaker is None:
                        self._error("this radio has no speaker")
                        return True
                    from sleepradiopi.io.announce import Clip as BellClip
                    speaker.play_clip(BellClip(lightning_mod.bell(), "bell", "Lightning"))
                elif notify_mod.notifier is None:
                    self._error("this radio can't say alerts")
                    return True
                else:
                    notify_mod.notifier.say(f"This is a test of the {notify_mod.KINDS[test]} alert.", notify_mod.KINDS[test])
            self._send(json.dumps({"ok": True}).encode(), "application/json")
            return True
        if path == "/api/qrz":
            # {"key": "..."}: the QRZ logbook's access key, kept (never sent back); "" forgets it and what was had.
            # QRZ is asked at once with a new one.
            from sleepradiopi.playback import qrzlog as qrz_mod
            try:
                key = self._body().get("key")
                if not isinstance(key, str) or len(key) > 64 or any(c.isspace() for c in key.strip()):
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                self._error('send {"key": "your QRZ logbook access key"} (or "" to forget it)')
                return True
            qrz_mod.KEY = key.strip()
            if config_file is not None:
                save_setting(config_file, "qrz_key", qrz_mod.KEY)
            qrz_mod.logbook.forget()
            if qrz_mod.KEY:
                qrz_mod.logbook.fetch()
            self._send(json.dumps(self._qrz()).encode(), "application/json")
            return True
        if path == "/api/radio433":
            # {"address": "192.168.50.4:8076"}: the receiver's background service on the local network ("" forgets it). Kept; asked at once.
            from sleepradiopi.playback import radio433 as r_mod
            try:
                address = self._body().get("address")
                if not isinstance(address, str) or (address.strip() and r_mod.base(address) is None):
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                self._error('send {"address": "host:port" of the receiver\'s background service (the one that serves /433), or ""}')
                return True
            r_mod.ADDRESS = address.strip()
            if config_file is not None:
                save_setting(config_file, "background_url", r_mod.ADDRESS)
            r_mod.radio433.forget()
            from sleepradiopi.playback import sondes as s_mod
            s_mod.sondes.forget()
            if r_mod.ADDRESS:
                r_mod.radio433.fetch()
                s_mod.sondes.refresh()                          # (in a thread of its own: a dead address must not make this wait twice)
            self._send(json.dumps(r433_state()).encode(), "application/json")
            return True
        if path == "/api/sondes/delete":
            # {"id": "H4713951"}: forget that flight now (pinned or not; a pinned one leaves the card's pins file too).
            from sleepradiopi.playback import sondes as s_mod
            try:
                body = self._body()
                if not isinstance(body.get("id"), str):
                    raise s_mod.BadFlight('send {"id": "the flight"}')
                s_mod.sondes.delete(body["id"])
            except (s_mod.BadFlight, ValueError, TypeError, AttributeError) as e:
                self._error(str(e) or "that isn't a flight")
                return True
            self._send(json.dumps(sondes_state()).encode(), "application/json")
            return True
        if path in ("/api/sondes/import", "/api/sondes/pin"):
            # import: a radiosonde file (from /api/sondes/export) into the radio's memory. pin: {"id": "H4713951", "pinned": true | false}:
            # keep that flight over a restart (a small file on the card, written once), or stop.
            from sleepradiopi.playback import sondes as s_mod
            try:
                body = self._body()
                if path.endswith("import"):
                    ids = s_mod.sondes.load(body)
                    self._send(json.dumps({"imported": ids, **sondes_state()}).encode(), "application/json")
                    return True
                if not isinstance(body.get("id"), str) or not isinstance(body.get("pinned"), bool):
                    raise s_mod.BadFlight('send {"id": "the flight", "pinned": true or false}')
                s_mod.sondes.pin(body["id"], body["pinned"])
            except (s_mod.BadFlight, ValueError, TypeError, AttributeError) as e:
                self._error(str(e) or "that isn't a radiosonde file")
                return True
            self._send(json.dumps(sondes_state()).encode(), "application/json")
            return True
        if path == "/api/aircraft":
            # {"address": "192.168.1.5:8080"}: the dump1090 / readsb receiver on the local network ("" forgets it); and/or
            # {"watch": "RRR, ASCOT, #43C0A1, mil"}: aircraft to watch for ("" for none). Kept; asked at once.
            from sleepradiopi.playback import aircraft as air_mod
            try:
                body = self._body()
                address, watch, off, rng = body.get("address"), body.get("watch"), body.get("off"), body.get("range")
                if rng is not None and (isinstance(rng, bool) or rng not in (0, *air_mod.SCOPE_RANGES)):
                    raise ValueError
                if off is not None and (not isinstance(off, list) or any(k not in air_mod.KINDS for k in off)):
                    raise ValueError
                if (address is None and watch is None and off is None and rng is None) or (address is not None and (not isinstance(address, str) or (address.strip() and air_mod.base(address) is None))):
                    raise ValueError
                if watch is not None:
                    if not isinstance(watch, str) or len(watch) > 200:
                        raise ValueError
                    air_mod.parse_watch(watch)
            except (ValueError, TypeError, AttributeError):
                self._error('send {"address": "host:port" of the receiver (the one that serves /data/aircraft.json), or ""} and/or '
                            '{"watch": "RRR, ASCOT, #43C0A1, #43C000-43CFFF, mil" (callsign starts, # addresses, mil), or ""} and/or '
                            '{"off": [kinds of alert switched off: ' + ", ".join(air_mod.KINDS) + ']} and/or '
                            '{"range": the scope\'s range in nm: ' + ", ".join(map(str, air_mod.SCOPE_RANGES)) + ', or 0 for automatic}')
                return True
            if address is not None:
                air_mod.ADDRESS = address.strip()
                if config_file is not None:
                    save_setting(config_file, "aircraft_url", air_mod.ADDRESS)
            if rng is not None:
                air_mod.RANGE = int(rng)
                if config_file is not None:
                    save_setting(config_file, "aircraft_range", air_mod.RANGE)
            if off is not None:
                air_mod.OFF = set(off)
                if config_file is not None:
                    save_setting(config_file, "aircraft_off", sorted(air_mod.OFF))
            if watch is not None:
                air_mod.WATCH = watch.strip()
                if config_file is not None:
                    save_setting(config_file, "aircraft_watch", air_mod.WATCH)
            if address is not None or watch is not None:
                air_mod.aircraft.forget()
            if air_mod.ADDRESS and (address is not None or watch is not None):
                air_mod.aircraft.fetch()
            self._send(json.dumps(air_state()).encode(), "application/json")
            return True
        if path == "/api/warnings":
            # {"region": "ne" | ...}: the Met Office region for weather warnings, or "" for the one nearest the
            # locator. Kept; what was had is fetched again at once.
            from sleepradiopi.playback import ukwarnings as warn_mod
            try:
                region = self._body().get("region")
                if not isinstance(region, str) or (region and region not in warn_mod.REGIONS):
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                self._error('send {"region": one of ' + ", ".join(warn_mod.REGIONS) + ', or ""}')
                return True
            warn_mod.REGION = region
            if config_file is not None:
                save_setting(config_file, "warnings_region", region)
            warn_mod.warnings.fetch()
            self._send(json.dumps({"region": warn_mod.region_for(), "chosen": region}).encode(), "application/json")
            return True
        return False

    return {"methods": {},
            "get": get, "post": post}
