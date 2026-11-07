"""The DJ and what it says: messages, birthdays, the DJ's settings, and the on-demand 'hear it' lines.

Methods of the web handler and its routes, moved out of server.py as they were: build(ctx) unpacks what they use of
what make_handler holds and returns them (and the GET / POST branches of this area) for make_handler to attach.
"""

from __future__ import annotations

import logging
import json
import os
import threading
from datetime import date
from datetime import datetime
from sleepradiopi.broadcast import away as away_mod
from sleepradiopi.broadcast import birthdays
from sleepradiopi.broadcast import messages
from sleepradiopi.config.clock import clock_trusted
from sleepradiopi.config.settings import save_setting
from sleepradiopi.io.announce import Clip
from sleepradiopi.web.common import RESTART_ENV
from sleepradiopi.web import server as server_mod          # (read at call time: tests patch these there)

log = logging.getLogger("sleepradiopi.web.server")


def build(ctx):
    config_file, speaker, station = ctx.config_file, ctx.speaker, ctx.station

    def _birthdays_state(self) -> dict:
        trusted = clock_trusted()
        return {"birthdays": station.birthdays.entries,
                "today": station.birthdays.today(datetime.now()) if trusted else [],
                "clock_trusted": trusted,
                "can_hear": speaker is not None and station._has_voice}

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

    def _messages_state(self) -> dict:
        trusted = clock_trusted()
        nxt = station.messages.next_slot(datetime.now(), station.profile) if trusted else None
        return {**station.messages.cfg, "next": nxt.strftime("%H:%M") if nxt else None,
                "next_day": nxt.date() != date.today() if nxt else False,
                "due_now": bool(nxt and nxt <= datetime.now()),
                "clock_trusted": trusted, "can_hear": speaker is not None and station._has_voice,
                "away": away_mod.remote.status()}

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
        "jingle_every" (0 = off), "news_enabled", "dj_speed", "news_speed", "ebook_speed", "ebook_voice", "ebook_pause"
        (0.5-1.5, 1 = the voice's own pace), "ebook_pace" ("slow", "medium", "normal", "fast": for the Flite voices), "quiet_on", "quiet_start_min",
        "quiet_end_min" (the quiet hours: the radio's do-not-disturb)}. All saved; all live except
        the voice, which restarts the station (only one voice fits in RAM)."""
        try:
            body = self._body()
            if not isinstance(body, dict):
                raise ValueError("send a JSON object")
            for key in ("dj_hooks", "news_enabled", "startup_sound", "dj_on", "time_checks", "quiet_on"):
                if key in body and not isinstance(body[key], bool):
                    raise ValueError(f"{key} must be true or false")
            every = body.get("jingle_every")
            if every is not None and (isinstance(every, bool) or not isinstance(every, int)):
                raise ValueError("jingle_every must be a number")
            voice = body.get("voice")
            if voice is not None and voice not in station.voices():
                raise ValueError(f"no voice called {voice!r}")
            if any(k in body for k in ("quiet_on", "quiet_start_min", "quiet_end_min")):
                station.set_quiet(body.get("quiet_on"), body.get("quiet_start_min"), body.get("quiet_end_min"))
            station.set_dj(chattiness=body.get("chattiness"), dj_hooks=body.get("dj_hooks"),
                           jingle_every=every, news_enabled=body.get("news_enabled"),
                           dj_speed=body.get("dj_speed"), news_speed=body.get("news_speed"), ebook_speed=body.get("ebook_speed"),
                           ebook_voice=body.get("ebook_voice"), ebook_pause=body.get("ebook_pause"),
                           ebook_pace=body.get("ebook_pace"), dj_on=body.get("dj_on"), time_checks=body.get("time_checks"))
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
            for key, setting in (("quiet_on", "news_quiet_hours"), ("quiet_start_min", "news_quiet_start_min"), ("quiet_end_min", "news_quiet_end_min")):
                if key in body:
                    save_setting(config_file, setting, getattr(station.config, setting))
            if "dj_speed" in body:
                save_setting(config_file, "broadcast_announcer_speed", station.config.announcer_speed)
            if "news_speed" in body:
                save_setting(config_file, "news_speed", station.config.news_speed)
            for key in ("ebook_speed", "ebook_voice", "ebook_pace", "ebook_pause"):
                if key in body:
                    save_setting(config_file, key, getattr(station.config, key))
            if voice is not None and voice != station.dj_voice:
                save_setting(config_file, "broadcast_voice", voice)
                restarting = bool(os.environ.get(RESTART_ENV))
                log.info("voice changed to %s from %s", voice, self.address_string())
        self._send(json.dumps({**self._dj_state(), "saved_voice": voice or station.dj_voice,
                               "restarting": restarting}).encode(), "application/json")
        if restarting:
            server_mod._restart_soon(speaker)

    def get(self, path) -> bool:
        if path == "/api/birthdays":
            self._send(json.dumps(self._birthdays_state()).encode(), "application/json")
            return True
        if path == "/api/messages":
            self._send(json.dumps(self._messages_state()).encode(), "application/json")
            return True
        if path == "/api/dj":
            self._send(json.dumps(self._dj_state()).encode(), "application/json")
            return True
        return False

    def post(self, path) -> bool:
        if path == "/api/birthdays":
            self._set_birthdays()
            return True
        if path == "/api/messages":
            self._set_messages()
            return True
        if path in ("/api/messages/away", "/api/messages/away/look"):
            # {"url", "every_min"}: the document the messages from away are kept in (broadcast/away.py), and how
            # often it's looked at; saved. /look: fetch it now. Either way the answer waits for the fetch.
            try:
                if path.endswith("/look"):
                    self._body()
                else:
                    cfg = away_mod.validate(self._body())
                    away_mod.remote.set(cfg)
                    if config_file is not None:
                        save_setting(config_file, "messages_away", cfg)
                    log.info("messages from away: %s, every %d min", "a link" if cfg["url"] else "no link", cfg["every_min"])
            except (ValueError, TypeError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else 'send {"url", "every_min"}')
                return True
            away_mod.remote.look()
            self._send(json.dumps(self._messages_state()).encode(), "application/json")
            return True
        if path == "/api/messages/hear":
            self._hear_message()
            return True
        if path == "/api/dj":
            self._set_dj()
            return True
        if path == "/api/birthdays/hear":
            self._hear_birthday()
            return True
        return False

    return {"methods": {"_birthdays_state": _birthdays_state, "_set_birthdays": _set_birthdays, "_hear_birthday": _hear_birthday, "_messages_state": _messages_state, "_set_messages": _set_messages, "_hear_message": _hear_message, "_dj_state": _dj_state, "_set_dj": _set_dj},
            "get": get, "post": post}
