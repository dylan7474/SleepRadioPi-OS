"""The frequency display: third-octave levels of what the speaker is being fed.

The speaker (audio/speaker.py) keeps its last SCOPE_N samples -- after the EQ and the low cut, before the volume,
mixed to mono -- but only while someone is looking (GET /api/spectrum in the last WATCH_S), so it costs nothing
otherwise. bands() turns them into the 28 ISO third-octave bands from 31.5 Hz to 16 kHz, in dB against a full-scale
sine (0 dB = the loudest a sine can be; music sits well below). The page and the phone app draw them as bars; the phone
app draws what its microphone hears beside them, the same bands, for comparison.
"""

from __future__ import annotations

import numpy as np

CENTRES = (31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000, 1250, 1600, 2000, 2500,
           3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000)
SCOPE_N = 8192             # ~0.19 s at 44.1 kHz: 5.4 Hz apart, enough for the 31.5 Hz band (23 Hz wide)
WATCH_S = 5.0              # samples are kept this long after the last look
FLOOR_DB = -100.0          # silence


def bands(samples: np.ndarray, rate: int) -> list[float]:
    """Mono samples (-1..1) -> the level of each third-octave band in dB (0 dB = a full-scale sine)."""
    x = np.asarray(samples, dtype=np.float64)
    n = len(x)
    if n < 256:
        return [FLOOR_DB] * len(CENTRES)
    w = np.hanning(n)
    spec = np.fft.rfft(x * w)
    power = np.abs(spec) ** 2 * 2 / (n * np.sum(w ** 2))      # Parseval: a sine of amplitude A sums to A^2 / 2
    freqs = np.fft.rfftfreq(n, 1 / rate)
    out = []
    for fc in CENTRES:
        lo, hi = fc / 2 ** (1 / 6), fc * 2 ** (1 / 6)
        p = float(power[(freqs >= lo) & (freqs < hi)].sum())
        out.append(round(max(FLOOR_DB, 10 * np.log10(p / 0.5)) if p > 0 else FLOOR_DB, 1))
    return out
