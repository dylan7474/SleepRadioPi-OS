"""Controls: playback transport, the volume knob, the speaker (volume, EQ, noise, sleep), the preset buttons and keys.

Methods of the web handler and its routes, moved out of server.py as they were: build(ctx) unpacks what they use of
what make_handler holds and returns them (and the GET / POST branches of this area) for make_handler to attach.
"""

from __future__ import annotations

import logging
import json
import threading
from sleepradiopi.audio.eq import known as known_band
from sleepradiopi.config.settings import load as load_settings
from sleepradiopi.config.settings import save_setting
from sleepradiopi.io import presets as presets_mod
from sleepradiopi.web import server as server_mod          # (read at call time: tests patch these there)

log = logging.getLogger("sleepradiopi.web.server")


def build(ctx):
    announcer, config_file, hear, output, presets, speaker, station = ctx.announcer, ctx.config_file, ctx.hear, ctx.output, ctx.presets, ctx.speaker, ctx.station

    _held: dict = {}                  # the page's buttons held down: keycode -> auto let-go timer
    _held_lock = threading.Lock()

    def _let_go(code: int) -> None:
        with _held_lock:
            if _held.pop(code, None) is None:
                return
        log.info("button key %d: let go (the page never said)", code)
        presets.keys[code][1]()

    def _knob(self) -> None:
        """/api/knob {"press": "short" | "long" | "service"} or {"press": "turn", "clicks": n}
        (pressed and turned: back or on through a book, podcast or song): the knob's switch, from
        the page (to try it without the hardware). Short = pause/play, long =
        what's on Hold the knob (nothing on it: pause/play, as on the radio); "service" = as holding buttons 1 and 4 for 5 s
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
        elif presets is not None:            # held: what's on the knob, else a press
            done = presets.knob_long()
            if done is False:
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
        {"eq": {"g31": dB ... "g16k": dB}} (any of the ten bands; the old
        "bass"/"mid"/"treble" are still accepted and set the bands they covered),
        {"eq_auto": true | false, "eq_by_type": {"music"|"radio"|"audiobook"|"ebook"|"podcast": sound name|null}}
        (the sound each kind of audio gets; a sound picked by hand holds until the kind changes),
        {"eq_slots": [name|null x4]} (the sound each button picks in EQ mode: a ready-made
        one or one saved with /api/speaker/sounds),
        {"highpass": Hz} (the low cut; 0 = off), {"evenness": "off" | "gentle" | "strong"} (streams' volume evened out:
        audio/compressor.py) or
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
            if "evenness" in body:
                speaker.set_evenness(body["evenness"])                 # ValueError if unknown
            if "mono" in body:
                if not isinstance(body["mono"], bool):
                    raise ValueError("mono must be true or false")
                speaker.set_mono(body["mono"])
            if "eq" in body:
                eq = body["eq"]
                if not isinstance(eq, dict) or not all(
                        known_band(k) and isinstance(v, (int, float))
                        and not isinstance(v, bool) for k, v in eq.items()):
                    raise ValueError("eq must map band names (g31..g16k) to numbers")
                speaker.set_eq(eq)
            if "eq_slots" in body:         # the sound each button picks in EQ mode (hold button 1 for 3 s)
                speaker.set_eq_slots(body["eq_slots"])
                if presets is not None:
                    presets.bake()         # have the new names said ahead
            if "eq_auto" in body or "eq_by_type" in body:    # the sound by what's playing
                on = body.get("eq_auto", speaker.eq_auto)
                if not isinstance(on, bool):
                    raise ValueError("eq_auto must be true or false")
                speaker.set_eq_auto(on, body.get("eq_by_type"))
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
        menu. A button the page never lets go of is let go after server_mod.KEY_HELD_MAX_S."""
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
                t = _held[code] = threading.Timer(server_mod.KEY_HELD_MAX_S, lambda: _let_go(code))
                t.daemon = True
                t.start()
        if down and was is None:
            presets.keys[code][0]()
        elif not down and was is not None:
            presets.keys[code][1]()
        self._send(json.dumps(presets.status()).encode(), "application/json")

    def get(self, path) -> bool:
        if path == "/api/speaker/sounds" and config_file is not None:
            self._send(json.dumps({"sounds": load_settings(config_file).speaker_sounds}).encode(), "application/json")
            return True
        if path == "/api/buttons" and presets is not None:
            self._send(json.dumps(presets.status()).encode(), "application/json")
            return True
        return False

    def post(self, path) -> bool:
        if path == "/api/seek":
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
                return True
            self._send(json.dumps({"moved": moved is not None, "position": station.position()}).encode(),
                       "application/json")
            return True
        if path == "/api/skip":
            self.rfile.read(int(self.headers.get("Content-Length", 0)))  # no body needed
            self._send(json.dumps({"skipped": station.skip()}).encode(), "application/json")
            return True
        if path == "/api/previous":
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self._send(json.dumps({"previous": station.previous()}).encode(), "application/json")
            return True
        if path == "/api/track/play":
            try:
                body = self._body()
                reply = station.play_track(body.get("root") or "music", body.get("path"),
                                           then="pause" if body.get("then") == "pause" else None)
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"root\", \"path\"}")
                return True
            if speaker is not None:
                speaker.play()
            self._send(json.dumps(reply).encode(), "application/json")
            return True
        if path == "/api/lengths":
            try:
                items = self._body()["items"]
                if not isinstance(items, list) or len(items) > 200:
                    raise ValueError("send up to 200 items")
                reply = {"minutes": [station.minutes_of(it) for it in items]}
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"items\": [...]}")
                return True
            self._send(json.dumps(reply).encode(), "application/json")
            return True
        if path == "/api/speaker" and speaker is not None:
            self._speaker()
            return True
        if path == "/api/speaker/sounds" and config_file is not None:
            try:                             # {"sounds": [...]}: the saved speaker set-ups, the whole list
                from sleepradiopi.audio.eq import validate_sounds
                sounds = validate_sounds(self._body().get("sounds"))
            except (ValueError, TypeError, AttributeError) as e:
                self._error(str(e))
                return True
            save_setting(config_file, "speaker_sounds", sounds)
            self._send(json.dumps({"sounds": sounds}).encode(), "application/json")
            return True
        if path == "/api/knob" and speaker is not None:
            self._knob()
            return True
        if path in ("/api/buttons", "/api/buttons/press") and presets is not None:
            self._buttons(path.endswith("press"))
            return True
        if path == "/api/buttons/slot" and presets is not None:
            try:                             # {"slot": "knob_long" | "power_on", "preset": {...} | null}
                body = self._body()
                presets.set_slot(body["slot"], body["preset"])
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"slot\", \"preset\"}")
                return True
            self._send(json.dumps(presets.status()).encode(), "application/json")
            return True
        if path == "/api/buttons/key" and presets is not None and presets.keys:
            self._button_key()
            return True
        if path == "/api/instant" and presets is not None:
            try:
                done = presets.instant(self._body().get("preset"))
            except (ValueError, TypeError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) else "send {\"preset\": {...}}")
                return True
            self._send(json.dumps({"done": done}).encode(), "application/json")
            return True
        if path in ("/api/buttons/bank", "/api/buttons/auto") and presets is not None:
            self._button_bank(path.endswith("auto"))
            return True
        if path == "/api/hear":
            # {"here": true}: a browser is about to tune in (the stream is made for it); {"here": "quiet"}: it's
            # heard there, so the speaker goes quiet; {"here": false}: back to the speaker. See web/hear.py.
            try:
                here = self._body()["here"]
                if hear is None:
                    raise ValueError("this radio makes no stream to listen to")
                if here is True:
                    hear.start()
                elif here == "quiet":
                    hear.hush()
                elif here is False:
                    hear.stop()
                else:
                    raise ValueError
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                self._error(str(e) if isinstance(e, ValueError) and str(e) else 'send {"here": true | "quiet" | false}')
                return True
            self._send(json.dumps({"hear": hear.on, "held": hear.held, "stream": output.enabled}).encode(), "application/json")
            return True
        return False

    return {"methods": {"_knob": _knob, "_speaker": _speaker, "_buttons": _buttons, "_button_bank": _button_bank, "_button_key": _button_key},
            "get": get, "post": post}
