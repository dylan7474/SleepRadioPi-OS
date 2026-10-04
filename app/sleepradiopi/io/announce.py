"""Say the radio's network address out loud (a long press of the knob).

Away from home there's no screen to show where the web page is, so a long
press makes the radio speak its IP address (and its .local name), or say
that it isn't connected. It beeps at once, so you know the press counted.

Speech takes a Pi Zero 2 W longer to make than to say, so the announcement is
made in the background as soon as the voice is ready and remade whenever the
address changes; a press then speaks straight away. Until the voice has
loaded after power-on the press waits for it.

Setting the Wi-Fi up is the other time the radio has to say where it is, and
there the wait matters most: someone is standing by a silent box. So each
step has a short tune of its own (CUES), played at once, and the lines that go
with them (how to join the radio's own network, "Joining", "I'm on ...",
"I couldn't join ...") are said in the plain voice (tts/plain.py: eSpeak),
which is in the image and instant -- so a new radio, with no DJ's voice yet,
talks you through it. The long press keeps the DJ's voice (the plain one
only if there's no other).
"""

from __future__ import annotations

import fcntl
import logging
import socket
import struct
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np

from sleepradiopi.audio import pcm

log = logging.getLogger(__name__)

SIOCGIFADDR = 0x8915
CHECK_S = 30.0               # how often to look for a new address
WAIT_VOICE_S = 2.0           # ...and for the voice, until it has loaded
DIGITS = "zero one two three four five six seven eight nine".split()


def addresses(net: Path = Path("/sys/class/net")) -> list[tuple[str, str]]:
    """(interface, IPv4 address) for each interface that's up, not loopback
    and not a self-assigned 169.254 address. Real hardware (Wi-Fi, Ethernet:
    they have a device/ link) comes before virtual ones like docker0."""
    found = []
    try:
        names = sorted((not (p / "device").exists(), p.name) for p in net.iterdir())
    except OSError:
        return found
    names = [name for _, name in names]
    for name in names:
        if name == "lo":
            continue
        try:
            if (net / name / "operstate").read_text().strip() not in ("up", "unknown"):
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                req = struct.pack("256s", name.encode()[:15])
                ip = socket.inet_ntoa(fcntl.ioctl(s.fileno(), SIOCGIFADDR, req)[20:24])
        except OSError:
            continue
        if not ip.startswith(("127.", "169.254.")):
            found.append((name, ip))
    return found


def spoken_ip(ip: str) -> str:
    """192.168.1.42 -> "one nine two, dot, one six eight, dot, one, dot, four two"."""
    return ", dot, ".join(" ".join(DIGITS[int(d)] for d in part) for part in ip.split("."))


def spoken_host(host: str) -> str:
    name = "sleep radio pi" if host.lower() == "sleepradiopi" else host.replace("-", " ")
    return f"{name} dot local"


def announcement(addrs: list[tuple[str, str]], host: str, hotspot: dict | None = None) -> str:
    """What the radio says. hotspot: {"ssid", "password", "ip"} when it has
    made its own network (no saved one in range)."""
    if hotspot:
        pw = hotspot["password"]
        return (f"I couldn't find a Wi-Fi network I know, so I've made my own. "
                f"On your phone, join {hotspot['ssid']}. The password is {pw}, spelled {', '.join(pw)}. "
                f"Then open: {spoken_ip(hotspot['ip'])}, and add your Wi-Fi.")
    if not addrs:                       # (no name: the radio may be playing any station)
        return "I'm not connected to a network."
    ip = spoken_ip(addrs[0][1])
    return (f"My address is: {ip}. Once more: {ip}. "
            f"Or type: {spoken_host(host)}.")


JOINING = "Joining your Wi-Fi now."

# The Wi-Fi set-up's tunes: (notes in Hz, 0 = a rest; seconds each).
CUES = {
    "setup": ((660.0, 880.0, 0, 660.0, 880.0), 0.14),      # "over here": the radio has made its own network
    "joining": ((880.0, 0, 880.0), 0.1),                   # two pips: leaving it to join yours
    "joined": ((660.0, 880.0, 1320.0), 0.14),              # rising: it's on your Wi-Fi
    "failed": ((440.0, 330.0), 0.3),                       # falling: it couldn't join
}


def joined_text(ssid: str | None) -> str:
    return f"I'm on {ssid}." if ssid else "I'm on your Wi-Fi."


def failed_text(hotspot: dict, ssid: str | None = None) -> str:
    """Said when a join asked for from the hotspot didn't work."""
    return (f"I couldn't join {ssid or 'that Wi-Fi network'}, so I've made my own again. "
            f"On your phone, join {hotspot['ssid']}, and check the Wi-Fi name and password.")


def cue(name: str) -> np.ndarray:
    freqs, each_s = CUES[name]
    return beep(freqs, each_s)


def beep(freqs=(880.0, 1320.0), each_s: float = 0.12) -> np.ndarray:
    """A short two-note acknowledgement, int16 stereo."""
    parts = []
    for f in freqs:
        t = np.arange(int(each_s * pcm.SAMPLE_RATE)) / pcm.SAMPLE_RATE
        env = np.minimum(1, np.minimum(t, each_s - t) / 0.01)
        parts.append(0.25 * np.sin(2 * np.pi * f * t) * env)
    x = (np.concatenate(parts) * 32767).astype(np.int16)
    return np.repeat(x[:, None], pcm.CHANNELS, axis=1)


def pip(sec: float = 0.1) -> np.ndarray:
    """One of the pips: a 1 kHz tone, int16 stereo."""
    t = np.arange(int(pcm.SAMPLE_RATE * sec)) / pcm.SAMPLE_RATE
    return np.repeat((np.sin(2 * np.pi * 1000 * t) * 9000).astype(np.int16)[:, None], pcm.CHANNELS, axis=1)


def pips() -> np.ndarray:
    """The pips: five short 1 kHz tones a second apart, then a long one (6 s), int16 stereo."""
    parts = []
    for _ in range(5):
        parts += [pip(), pcm.silence(0.9)]
    return np.concatenate(parts + [pip(0.5)])


class Clip:
    """Ready-made audio played on the speaker like a test sound."""

    __test__ = False

    def __init__(self, audio: np.ndarray, kind: str, label: str) -> None:
        self.audio = audio
        self.kind, self.label = kind, label
        self.total = len(audio)
        self.duration_s = round(self.total / pcm.SAMPLE_RATE, 1)
        self.pos = 0

    @property
    def done(self) -> bool:
        return self.pos >= self.total

    @property
    def elapsed_s(self) -> float:
        return min(self.pos, self.total) / pcm.SAMPLE_RATE

    def hz(self):
        return None

    def note(self):
        return None

    def next(self, n: int) -> np.ndarray:
        out = np.zeros((n, self.audio.shape[1]), dtype=np.float32)
        part = self.audio[self.pos:self.pos + n]
        out[:len(part)] = part
        self.pos += n
        return out


class Announcer:
    """speak() = the long press. render(text) makes int16 stereo speech in the
    DJ's voice (blocking, slow); plain(text) in the plain one (instant; None if
    it can't)."""

    def __init__(self, render: Callable[[str], np.ndarray] | None, play: Callable[[Clip], None],
                 voice_ready: Callable[[], bool] = lambda: True,
                 get_addresses: Callable[[], list] = addresses,
                 host: str | None = None, hotspot: Callable[[], dict | None] = lambda: None,
                 plain: Callable[[str], np.ndarray | None] | None = None) -> None:
        self.hotspot = hotspot
        self.render, self.play = render, play
        self.voice_ready, self.get_addresses = voice_ready, get_addresses
        self.host = host or socket.gethostname()
        self.plain = plain
        self._lock = threading.Lock()
        self._cache: tuple[str, np.ndarray] | None = None
        self._busy = threading.Lock()   # one announcement at a time

    def text(self) -> str:
        return announcement(self.get_addresses(), self.host, self.hotspot())

    def _speech(self, text: str) -> np.ndarray:
        with self._lock:
            if self._cache and self._cache[0] == text:
                return self._cache[1]
        audio = self.render(text)
        with self._lock:
            self._cache = (text, audio)
        return audio

    def start(self) -> None:
        """Keep the announcement ready in the background."""
        def run():
            while True:
                wait = CHECK_S
                try:
                    if self.render is not None and not self.voice_ready():
                        wait = WAIT_VOICE_S
                    elif self.render is not None:
                        text = self.text()
                        if not (self._cache and self._cache[0] == text):
                            self._speech(text)
                            log.info("announcement ready: %s", text)
                except Exception:
                    log.exception("announcement: couldn't prepare it")
                threading.Event().wait(wait)
        threading.Thread(target=run, name="announcer", daemon=True).start()

    def speak(self) -> bool:
        """Beep, then say the address (in the background). False if one is already going."""
        return self.say([self.text], beep(), "Address")

    def say(self, lines: list, tune: np.ndarray, label: str = "Wi-Fi", wait: float = 0,
            plain: bool = False) -> bool:
        """Play a tune at once, then say each line as soon as it's made (in the
        background). lines: texts, or functions giving one when its turn comes.
        plain: in the plain voice (the DJ's if there's no plain one), else the
        DJ's (the plain one if there's no DJ's). False if an announcement is
        already going (after waiting `wait` s for it)."""
        if not self._busy.acquire(timeout=wait) if wait else not self._busy.acquire(blocking=False):
            return False
        ack = Clip(tune, "beep", label)
        self.play(ack)

        def run():
            try:
                last = ack
                for line in lines:
                    text = line() if callable(line) else line
                    log.info("saying: %s", text)
                    speech = self.plain(text) if self.plain and (plain or self.render is None) else None
                    if speech is None:
                        if self.render is None:
                            if label == "Address":   # (the long press: a second beep says "no voice")
                                self.play(Clip(beep((440.0, 330.0)), "beep", "No voice"))
                            return
                        speech = self._speech(text)
                    for _ in range(int(last.duration_s / 0.02) + 100):       # let what's playing finish
                        if last.done:
                            break
                        threading.Event().wait(0.02)
                    gap = np.zeros((int(0.3 * pcm.SAMPLE_RATE), pcm.CHANNELS), dtype=np.int16)
                    last = Clip(np.concatenate([gap, speech]), "announce",
                                "Saying the address" if label == "Address" else label)
                    self.play(last)
            except Exception:
                log.exception("announcement failed")
            finally:
                self._busy.release()
        threading.Thread(target=run, name="announce", daemon=True).start()
        return True
