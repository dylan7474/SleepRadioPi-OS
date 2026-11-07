"""System: settings and backups, identity, lamps, power, Wi-Fi, voices, updates and the Mp3 stream.

Methods of the web handler and its routes, moved out of server.py as they were: build(ctx) unpacks what they use of
what make_handler holds and returns them (and the GET / POST branches of this area) for make_handler to attach.
"""

from __future__ import annotations

import logging
import json
import os
import queue
import time
from sleepradiopi import remote
from sleepradiopi import voices as voices_mod
from sleepradiopi import wifi as wifi_mod
from sleepradiopi.config import backup
from sleepradiopi.config.power import can_power_off
from sleepradiopi.config.power import request_power_off
from sleepradiopi.config.power import request_restart
from sleepradiopi.config.settings import save_setting
from sleepradiopi.io import presets as presets_mod
from urllib.parse import parse_qs
from urllib.parse import urlparse
from sleepradiopi.web.common import IDLE_CLOSE_S, MAX_SETTINGS_BYTES, RESTART_ENV
from sleepradiopi.web import server as server_mod          # (read at call time: tests patch these there)

log = logging.getLogger("sleepradiopi.web.server")


def build(ctx):
    backups, config_file, lamps, output, presets, speaker, station, updates, voice_jobs = ctx.backups, ctx.config_file, ctx.lamps, ctx.output, ctx.presets, ctx.speaker, ctx.station, ctx.updates, ctx.voice_jobs

    def _remote(self) -> None:
        """/api/remote: {"token": "..."} keeps a tunnel token (never sent back), {"enabled": true|false} switches
        Remote access on or off, {"forget": true} takes the token away. (Switching it off over the tunnel cuts that
        connection off; the page asks first.)"""
        if not remote.available():
            self.send_error(404)
            return
        try:
            body = self._body()
            if not isinstance(body, dict):
                raise ValueError
            if body.get("forget") is True:
                remote.forget()
            if "token" in body:
                remote.set_token(body["token"])
            if "enabled" in body:
                if not isinstance(body["enabled"], bool):
                    raise ValueError("enabled is true or false")
                remote.set_enabled(body["enabled"])
        except ValueError as e:
            self._error(str(e) or 'send {"token": "..."}, {"enabled": true} or {"forget": true}')
            return
        log.info("remote access: %s", {k: (v if k != "token" else "...") for k, v in body.items()})
        self._send(json.dumps(remote.status()).encode(), "application/json")

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
                   "broadcast_jingle_every", "news_enabled", "broadcast_announcer_speed", "news_speed", "ebook_speed", "ebook_voice", "ebook_pace", "ebook_pause"}
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
            station.set_dj(dj_speed=settings.get("broadcast_announcer_speed"), news_speed=settings.get("news_speed"),
                               ebook_speed=settings.get("ebook_speed"), ebook_voice=settings.get("ebook_voice"),
                               ebook_pause=settings.get("ebook_pause"), ebook_pace=settings.get("ebook_pace"))
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
            if "eq_slots" in settings:
                speaker.set_eq_slots(settings["eq_slots"])
            if changed & {"eq_by_type", "eq_auto"}:
                speaker.set_eq_auto(settings.get("eq_auto", speaker.eq_auto), settings.get("eq_by_type"))
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
            server_mod._restart_soon(speaker)

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
                "host": brand.hostname_for(saved), "default_host": brand.DEFAULT_HOST,
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
            server_mod._restart_soon(speaker)

    def _wifi_state(self) -> dict:
        st = wifi_mod.status()
        saved = wifi_mod.load()
        return {**st, "saved": [n["ssid"] for n in saved["networks"]],
                "on_card": wifi_mod.card_networks(), "hotspot_settings": {"ssid": saved["hotspot"]["ssid"]},
                "offline": saved["offline"]}

    def _wifi(self, action: str) -> None:
        """POST /api/wifi/add {"ssid", "password"}, /remove {"ssid"}, /scan,
        /hotspot {"ssid", "password"} (the radio's own network's details),
        /try (leave the hotspot and try the saved networks now), /hotspot-now
        (switch to the radio's own network now: the only way it does, besides a reset),
        /offline {"on": bool} (work without Wi-Fi: off from now on, the service menu's 2
        lets you back in), /trial (off for ten minutes, then back, to try it)."""
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
            elif action == "offline":
                if not isinstance(body.get("on"), bool):
                    raise ValueError('send {"on": true|false}')
                wifi_mod.set_offline(body["on"])
                wifi_mod.ask("offline")
            elif action == "trial":
                wifi_mod.ask("trial")
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

    def get(self, path) -> bool:
        if path == "/api/log":
            # The Log window (logview.py): the newest lines of the radio's log, which is in memory on the appliance (nothing
            # is written to the card); ?level=problems | all, ?q=text, ?lines=n; and how long the radio and its program have run.
            from sleepradiopi import health as health_mod
            from sleepradiopi import logview
            qs = parse_qs(urlparse(self.path).query)
            try:
                want = int(qs.get("lines", [logview.DEFAULT_LINES])[0])
            except ValueError:
                want = logview.DEFAULT_LINES
            out = logview.read_log("problems" if qs.get("level", [""])[0] == "problems" else "all", qs.get("q", [""])[0], want)
            out["system_up_s"] = health_mod.system_uptime()
            out["program_up_s"] = time.time() - health_mod.health.started if health_mod.health.appliance else None      # (a desktop has been up for days: nothing to say of the program)
            self._send(json.dumps(out).encode(), "application/json")
            return True
        if path == "/api/settings" and config_file is not None:
            self._save_settings()
            return True
        if path == "/api/backups" and config_file is not None:
            self._send(json.dumps({"backups": backups.list(), "max": backup.MAX_KEPT}).encode(), "application/json")
            return True
        if path == "/api/backups/file" and config_file is not None:
            try:
                name = backups.name(parse_qs(urlparse(self.path).query).get("name", [""])[0])
                self._send_settings(backups.read(name), f"{name}.json")
            except backup.BadSettings as e:
                self._error(str(e))
            return True
        if path == "/api/identity":
            self._send(json.dumps(self._identity()).encode(), "application/json")
            return True
        if path == "/api/lamps":
            self._send(json.dumps(lamps.settings() if lamps is not None else {"needle": False, "glow": False}).encode(),
                       "application/json")
            return True
        if path == "/api/voices":
            self._send(json.dumps(self._voices_state()).encode(), "application/json")
            return True
        if path == "/api/wifi":
            self._send(json.dumps(self._wifi_state()).encode(), "application/json")
            return True
        if path == "/api/update" and updates is not None:
            self._send(json.dumps(updates.status()).encode(), "application/json")
            return True
        if path == "/api/remote":
            self._send(json.dumps(remote.status()).encode(), "application/json")
            return True
        return False

    def post(self, path) -> bool:
        if path == "/api/power":
            self._power()
            return True
        if path == "/api/remote":
            self._remote()
            return True
        if path == "/api/settings" and config_file is not None:
            self._load_settings()
            return True
        if path.startswith("/api/backups/") and config_file is not None:
            self._backups(path.rsplit("/", 1)[1])
            return True
        if path == "/api/identity":
            self._set_identity()
            return True
        if path in ("/api/lamps", "/api/lamps/sweep") and lamps is not None:
            self._set_lamps(path.endswith("sweep"))
            return True
        if path == "/api/stream":
            self._set_stream()
            return True
        if path == "/api/voices/standard" and voice_jobs is not None:
            self._body()
            try:
                voice_jobs.download_standard()
            except ValueError as e:
                self._error(str(e))
                return True
            self._send(json.dumps(self._voices_state()).encode(), "application/json")
            return True
        if path == "/api/voices/upload" and voice_jobs is not None:
            self._upload_voice()
            return True
        if path.startswith("/api/wifi/"):
            self._wifi(path.rsplit("/", 1)[1])
            return True
        if path == "/api/update/file" and updates is not None:
            # the release's .update file as the body: for a radio with no internet
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                reply = updates.take_file(length, self.rfile.read)
            except (ValueError, OSError) as e:
                if length and not self.rfile.closed:
                    self.close_connection = True       # don't read a big body we've refused
                self._error(str(e) or e.__class__.__name__)
                return True
            log.info("update file from %s", self.address_string())
            self._send(json.dumps(reply).encode(), "application/json")
            return True
        if path in ("/api/update/check", "/api/update/install") and updates is not None:
            self._body()
            try:
                reply = updates.check() if path.endswith("check") else updates.install()
            except (ValueError, OSError) as e:          # incl. no network / GitHub errors
                self._error(str(e) or e.__class__.__name__)
                return True
            self._send(json.dumps(reply).encode(), "application/json")
            return True
        return False

    return {"methods": {"_remote": _remote, "_power": _power, "_save_settings": _save_settings, "_send_settings": _send_settings, "_load_settings": _load_settings, "_backups": _backups, "_apply_settings": _apply_settings, "_set_lamps": _set_lamps, "_identity": _identity, "_set_identity": _set_identity, "_wifi_state": _wifi_state, "_wifi": _wifi, "_voices_state": _voices_state, "_upload_voice": _upload_voice, "_set_stream": _set_stream, "_stream": _stream},
            "get": get, "post": post}
