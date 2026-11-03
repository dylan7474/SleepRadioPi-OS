import os
import stat
import sys
import textwrap

import numpy as np
import pytest

from sleepradiopi.tts import flite

# A stand-in for the flite program: it writes a 16 kHz WAV (a tone, 0.01 s a character, longer for a bigger
# duration_stretch) to the file named by -o, and records the arguments it was given.
FAKE = textwrap.dedent('''\
    #!{py}
    import sys, wave, math, json, os
    a = sys.argv[1:]
    if os.environ.get("FAKE_FLITE_FAIL"):
        sys.stderr.write("boom"); sys.exit(3)
    text = a[a.index("-t") + 1]
    stretch = float(a[a.index("--setf") + 1].split("=")[1])
    out = a[a.index("-o") + 1]
    open(os.environ["FAKE_FLITE_LOG"], "a").write(json.dumps(a) + "\\n")
    n = int(len(text) * 0.01 * 16000 * stretch)
    with open(out, "wb") as f:
        w = wave.open(f, "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"".join(int(8000 * math.sin(i / 8)).to_bytes(2, "little", signed=True) for i in range(n))); w.close()
''')


@pytest.fixture
def fake(tmp_path, monkeypatch):
    exe = tmp_path / "flite"
    exe.write_text(FAKE.format(py=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "log"
    monkeypatch.setenv("FAKE_FLITE_LOG", str(log))
    monkeypatch.setenv(flite.ENV, str(exe))
    return exe, log


def test_stretch_brings_every_voice_to_the_same_words_a_minute():
    for pace, wpm in flite.PACES.items():
        for v, (_, _, natural) in flite.VOICES.items():
            assert flite.stretch(v, pace) == pytest.approx(natural / wpm, abs=0.002)
    # a slower pace is a bigger stretch; a faster voice by nature gets a bigger one for the same pace
    assert flite.stretch("flite-awb", "slow") > flite.stretch("flite-awb", "medium") > flite.stretch("flite-awb", "fast")
    assert flite.stretch("flite-slt", "medium") > flite.stretch("flite-rms", "medium")
    assert flite.stretch("flite-awb", "no-such-pace") == flite.stretch("flite-awb", flite.DEFAULT_PACE)


def test_stretch_is_kept_in_flites_range():
    assert flite.STRETCH_MIN <= flite.stretch("flite-rms", "fast") <= flite.STRETCH_MAX


def test_is_flite_and_label():
    assert flite.is_flite("flite-awb") and not flite.is_flite("stock") and not flite.is_flite("awb")
    assert "Scottish" in flite.label("flite-awb")
    assert flite.label("stock") == "stock"


def test_available_follows_the_program(monkeypatch, tmp_path):
    monkeypatch.setenv(flite.ENV, str(tmp_path / "nothing"))
    assert not flite.available()
    exe = tmp_path / "f"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv(flite.ENV, str(exe))
    assert flite.available() and flite.program() == str(exe)
    exe.chmod(0o644)                                    # (not executable: as good as not there)
    assert not flite.available()


def test_synth_returns_float_samples_and_the_rate(fake):
    x, rate = flite.FliteTts().synth("flite-awb", "Hello there.", 1.3)
    assert rate == 16000 and x.dtype == np.float32
    assert len(x) == int(len("Hello there.") * 0.01 * 16000 * 1.3)
    assert -1.0 <= x.min() < 0 < x.max() <= 1.0


def test_synth_passes_the_voice_stretch_and_text_without_a_shell(fake):
    exe, log = fake
    text = "- not an option; $(touch /tmp/x) `id` \"quoted\" 'q' é"
    flite.FliteTts().synth("flite-rms", text, 1.25)
    args = __import__("json").loads(log.read_text().splitlines()[0])
    assert args[args.index("-voice") + 1] == "rms"
    assert args[args.index("--setf") + 1] == "duration_stretch=1.250"
    assert args[args.index("-t") + 1] == text           # (one argument, whole, untouched)
    assert args[args.index("-o") + 1] == "/dev/stdout"


def test_synth_keeps_the_stretch_in_range(fake):
    exe, log = fake
    flite.FliteTts().synth("flite-slt", "a", 99)
    flite.FliteTts().synth("flite-slt", "a", 0.01)
    got = [__import__("json").loads(l) for l in log.read_text().splitlines()]
    assert [a[a.index("--setf") + 1] for a in got] == ["duration_stretch=2.000", "duration_stretch=0.600"]


def test_empty_text_makes_no_process(fake):
    exe, log = fake
    x, rate = flite.FliteTts().synth("flite-awb", "   \n", 1.0)
    assert len(x) == 0 and not log.exists()


def test_a_failing_program_raises_for_the_reader_to_count(fake, monkeypatch):
    monkeypatch.setenv("FAKE_FLITE_FAIL", "1")
    with pytest.raises(RuntimeError, match="flite failed"):
        flite.FliteTts().synth("flite-awb", "Hello.", 1.0)


def test_unknown_voice_and_missing_program(fake, monkeypatch):
    with pytest.raises(ValueError):
        flite.FliteTts().synth("stock", "Hello.", 1.0)
    monkeypatch.setenv(flite.ENV, "/nonexistent/flite")
    with pytest.raises(RuntimeError, match="isn't installed"):
        flite.FliteTts().synth("flite-awb", "Hello.", 1.0)


@pytest.mark.skipif(not flite.available() or "FAKE_FLITE_LOG" in os.environ, reason="flite isn't installed here")
def test_the_real_flite_speaks_every_voice_and_a_slower_pace_is_longer(monkeypatch):
    monkeypatch.delenv(flite.ENV, raising=False)
    t = flite.FliteTts()
    for v in flite.VOICES:
        try:
            x, rate = t.synth(v, "It was the best of times, it was the worst of times.", flite.stretch(v, "medium"))
        except RuntimeError as e:
            pytest.skip(f"this flite build lacks {v}: {e}")
        assert rate in (8000, 16000, 22050) and 1.0 < len(x) / rate < 8.0 and float(np.abs(x).max()) > 0.05
    slow = t.synth("flite-slt", "The quick brown fox jumps over the lazy dog.", flite.stretch("flite-slt", "slow"))[0]
    fast = t.synth("flite-slt", "The quick brown fox jumps over the lazy dog.", flite.stretch("flite-slt", "fast"))[0]
    assert len(slow) > len(fast) * 1.3
