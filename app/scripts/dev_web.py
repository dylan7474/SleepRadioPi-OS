#!/usr/bin/env python3
"""Quick-and-dirty test page: hear the Pi's audio in a desktop browser.

Before the amp is wired up (or any time you're away from the speakers),
this renders audio on the Pi -- TTS in any installed voice, the jingles,
local music albums, a test tone -- and plays it back through the browser instead of ALSA. It
exercises the whole software chain except the final hop to the HiFiBerry.

Usage (on the Pi, inside the venv):

    python3 scripts/dev_web.py              # then open http://sleepradiopi.local:8080

Dev tool only: no auth, so only run it on a trusted home network.
"""

from __future__ import annotations

import argparse
import html
import io
import json
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from smoke_test_audio import generate_tone  # noqa: E402
from sleepradiopi.tts.engine import OfflineTtsEngine  # noqa: E402

AUDIO_TYPES = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".wav": "audio/wav"}
MAX_TEXT = 500
MAX_TONE_SECONDS = 10.0


def track_label(path: Path) -> str:
    """'Artist - Title (m:ss)' from the file's tags, falling back to the file name."""
    import mutagen

    try:
        tags = mutagen.File(path, easy=True)
    except Exception:
        tags = None
    if tags is None:
        return path.name
    title = (tags.get("title") or [path.stem])[0]
    artist = (tags.get("artist") or [""])[0]
    secs = int(getattr(tags.info, "length", 0) or 0)
    return f"{artist + ' - ' if artist else ''}{title} ({secs // 60}:{secs % 60:02d})"


def to_wav(samples, sample_rate: int) -> bytes:
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SleepRadioPi test</title>
<style>
  body {{ font: 16px system-ui, sans-serif; max-width: 46rem; margin: 1.5rem auto;
         padding: 0 1rem; background: #14161a; color: #e6e1d6; }}
  h1 {{ font-size: 1.3rem; color: #f0b44c; }}
  h2 {{ font-size: 1.05rem; margin-top: 2rem; color: #f0b44c; }}
  textarea, select, input, button {{ font: inherit; }}
  textarea {{ width: 100%; box-sizing: border-box; }}
  button {{ background: #f0b44c; color: #14161a; border: 0; padding: .4rem .9rem;
           border-radius: .3rem; cursor: pointer; }}
  button:disabled {{ opacity: .5; cursor: wait; }}
  .row {{ display: flex; gap: .8rem; align-items: center; flex-wrap: wrap; margin: .5rem 0; }}
  .status {{ color: #a8a296; font-size: .9rem; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ display: flex; justify-content: space-between; align-items: center;
       gap: .5rem; padding: .25rem 0; border-bottom: 1px solid #2a2d33; flex-wrap: wrap; }}
  audio {{ height: 2rem; }}
  h3 {{ font-size: .95rem; margin: 1.2rem 0 .2rem; color: #d8cfbd; }}
</style></head><body>
<h1>SleepRadioPi &mdash; audio test page</h1>
<p class="status">Audio is made on the Pi and played here in the browser.
The first line in a voice is slow, because it loads the model (about 17&nbsp;s for stock, 29&nbsp;s for personal).</p>

<h2>Voice</h2>
<textarea id="text" rows="3">That was Pink Floyd. It's twenty past eleven, and this is Sleep Radio.</textarea>
<div class="row">
  <label>Voice <select id="voice">{voice_options}</select></label>
  <label>Speed <input id="speed" type="number" min="0.5" max="1.5" step="0.05" value="0.85"></label>
  <button id="speak">Speak</button>
</div>
<div class="row"><audio id="ttsAudio" controls></audio><span id="ttsStatus" class="status"></span></div>

<h2>Test tone</h2>
<div class="row">
  <label>Hz <input id="freq" type="number" value="440" min="50" max="5000"></label>
  <label>Seconds <input id="secs" type="number" value="3" min="0.5" max="10" step="0.5"></label>
  <button id="tone">Play tone</button>
</div>
<div class="row"><audio id="toneAudio" controls></audio></div>

<h2>Music ({album_count} albums)</h2>
{music_items}

<h2>Jingles ({jingle_count})</h2>
<ul>{jingle_items}</ul>

<script>
const $ = id => document.getElementById(id);
$("speak").onclick = async () => {{
  const btn = $("speak"); btn.disabled = true;
  $("ttsStatus").textContent = "generating on the Pi…";
  const q = new URLSearchParams({{voice: $("voice").value, speed: $("speed").value, text: $("text").value}});
  const t0 = performance.now();
  try {{
    const r = await fetch("/tts?" + q);
    if (!r.ok) throw new Error(await r.text());
    const info = JSON.parse(r.headers.get("X-Tts-Info"));
    $("ttsAudio").src = URL.createObjectURL(await r.blob());
    $("ttsAudio").play();
    $("ttsStatus").textContent =
      `${{info.speech_s}} s of speech: load ${{info.load_s}} s, synth ${{info.synth_s}} s ` +
      `(${{info.realtime_x}}× realtime), round trip ${{((performance.now()-t0)/1000).toFixed(1)}} s`;
  }} catch (e) {{ $("ttsStatus").textContent = "error: " + e.message; }}
  btn.disabled = false;
}};
$("tone").onclick = () => {{
  $("toneAudio").src = `/tone?freq=${{$("freq").value}}&seconds=${{$("secs").value}}&t=${{Date.now()}}`;
  $("toneAudio").play();
}};
</script>
</body></html>"""


class App:
    def __init__(self, voices_dir: Path, jingles_dir: Path, music_dir: Path) -> None:
        self.voices_dir = voices_dir
        self.jingles_dir = jingles_dir
        self.music_dir = music_dir
        self.engine = OfflineTtsEngine()
        self.tts_lock = threading.Lock()

    def voices(self) -> list[str]:
        if not self.voices_dir.is_dir():
            return []
        return sorted(p.name for p in self.voices_dir.iterdir() if (p / "model.onnx").is_file())

    def jingles(self) -> list[str]:
        if not self.jingles_dir.is_dir():
            return []
        return sorted(p.name for p in self.jingles_dir.iterdir() if p.suffix.lower() == ".mp3")

    def albums(self) -> dict[str, list[str]]:
        """SleepRadio's rule: any folder holding audio files is an album.

        Maps album folder -> sorted track paths, all relative to music_dir.
        """
        albums: dict[str, list[str]] = {}
        if self.music_dir.is_dir():
            for f in sorted(self.music_dir.rglob("*")):
                if f.is_file() and f.suffix.lower() in AUDIO_TYPES:
                    rel = f.relative_to(self.music_dir)
                    albums.setdefault(str(rel.parent), []).append(str(rel))
        return albums

    def page(self) -> str:
        voices = self.voices()
        jingles = self.jingles()
        albums = self.albums()
        return PAGE.format(
            voice_options="".join(
                f'<option{" selected" if v == "personal" else ""}>{html.escape(v)}</option>'
                for v in voices
            ),
            album_count=len(albums),
            music_items="".join(
                f"<h3>{html.escape(album)}</h3><ul>"
                + "".join(
                    f"<li><span>{html.escape(track_label(self.music_dir / t))}</span>"
                    f'<audio controls preload="none" src="/music/{quote(t)}"></audio></li>'
                    for t in tracks
                )
                + "</ul>"
                for album, tracks in albums.items()
            ),
            jingle_count=len(jingles),
            jingle_items="".join(
                f"<li><span>{html.escape(j)}</span>"
                f'<audio controls preload="none" src="/jingles/{quote(j)}"></audio></li>'
                for j in jingles
            ),
        )


class Handler(BaseHTTPRequestHandler):
    app: App

    def send_bytes(self, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/":
                self.send_bytes(self.app.page().encode(), "text/html; charset=utf-8")
            elif url.path == "/tone":
                freq = float(q.get("freq", 440))
                secs = min(float(q.get("seconds", 3)), MAX_TONE_SECONDS)
                self.send_bytes(to_wav(generate_tone(freq, secs, 44100), 44100), "audio/wav")
            elif url.path.startswith("/jingles/"):
                name = unquote(url.path.removeprefix("/jingles/"))
                if name not in self.app.jingles():  # also blocks path traversal
                    self.send_error(404)
                    return
                self.send_bytes((self.app.jingles_dir / name).read_bytes(), "audio/mpeg")
            elif url.path.startswith("/music/"):
                rel = unquote(url.path.removeprefix("/music/"))
                if not any(rel in tracks for tracks in self.app.albums().values()):  # blocks traversal
                    self.send_error(404)
                    return
                path = self.app.music_dir / rel
                self.send_bytes(path.read_bytes(), AUDIO_TYPES[path.suffix.lower()])
            elif url.path == "/tts":
                self.tts(q)
            else:
                self.send_error(404)
        except (ValueError, TypeError) as e:
            self.send_error(400, str(e))

    def tts(self, q: dict) -> None:
        voice = q.get("voice", "")
        text = q.get("text", "").strip()[:MAX_TEXT]
        speed = float(q.get("speed", 1.0))
        if voice not in self.app.voices() or not text or not 0.3 <= speed <= 2.0:
            self.send_error(400, "need an installed voice, some text, and a sane speed")
            return
        with self.app.tts_lock:  # one synthesis at a time; the Zero has no spare RAM/CPU
            t0 = time.monotonic()
            if not self.app.engine.ensure_loaded(self.app.voices_dir / voice, voice):
                self.send_error(500, f"voice pack {voice!r} is incomplete")
                return
            load_s = time.monotonic() - t0
            t0 = time.monotonic()
            audio = self.app.engine.synth(text, speed=speed)
            synth_s = time.monotonic() - t0
        speech_s = len(audio.samples) / audio.sample_rate
        info = {
            "load_s": round(load_s, 1),
            "synth_s": round(synth_s, 1),
            "speech_s": round(speech_s, 1),
            "realtime_x": round(speech_s / synth_s, 2) if synth_s else None,
        }
        self.log_message("tts %s %s", voice, info)
        self.send_bytes(
            to_wav(audio.samples, audio.sample_rate),
            "audio/wav",
            {"X-Tts-Info": json.dumps(info)},
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--voices", type=Path, default=REPO / "voices")
    parser.add_argument("--jingles", type=Path, default=Path.home() / "media" / "jingles")
    parser.add_argument("--music", type=Path, default=Path.home() / "media" / "music")
    args = parser.parse_args()

    Handler.app = App(args.voices, args.jingles, args.music)
    print(
        f"voices: {Handler.app.voices()}  jingles: {len(Handler.app.jingles())}"
        f"  albums: {len(Handler.app.albums())}"
    )
    print(f"serving on http://{args.host}:{args.port}/  (Ctrl+C to stop)")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
