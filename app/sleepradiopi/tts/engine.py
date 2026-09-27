"""Loads a voice pack and synthesises speech via sherpa-onnx.

Voice pack layout matches SleepRadio's VoicePack.kt exactly, so a pack
exported from the Android app (or built by its tools/build-stock-voice.sh)
can be copied into voices/<id>/ here unmodified:

    voices/<id>/model.onnx
    voices/<id>/model.onnx.json   (optional -- sample rate)
    voices/<id>/tokens.txt
    voices/<id>/espeak-ng-data/   (optional -- a model-only pack can borrow
                                    another installed pack's copy)
"""

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class TtsAudio:
    samples: np.ndarray  # mono float32 PCM in [-1, 1]
    sample_rate: int


@dataclass
class VoicePackFiles:
    model: Path
    tokens: Path
    espeak_data: Path


def resolve_pack(voice_dir: Path) -> VoicePackFiles | None:
    """Locate a pack's files, or None if it isn't usable.

    A model-only pack (no espeak-ng-data/ of its own) borrows the first
    sibling pack's copy, like VoicePackResolver.kt.
    """
    model = voice_dir / "model.onnx"
    tokens = voice_dir / "tokens.txt"
    if not (model.is_file() and tokens.is_file()):
        return None
    espeak = voice_dir / "espeak-ng-data"
    if not espeak.is_dir():
        siblings = sorted(p / "espeak-ng-data" for p in voice_dir.parent.iterdir())
        espeak = next((p for p in siblings if p.is_dir()), None)
        if espeak is None:
            return None
    return VoicePackFiles(model, tokens, espeak)


class OfflineTtsEngine:
    """Thin wrapper around sherpa_onnx.OfflineTts for one loaded voice pack.

    Mirrors SleepRadio's OfflineTtsEngine.kt: load() and synth() are
    synchronous/blocking, call off the main thread/loop.

    Only one voice is held in memory at a time -- a Pi Zero 2 W has ~416 MB
    usable, and loading takes 17-29 s there, so callers should switch
    voices rarely and synthesise ahead of time (the personal voice runs at
    ~0.7x realtime on a Zero).
    """

    def __init__(self, num_threads: int | None = None) -> None:
        self._loaded_id: str | None = None
        self._tts = None
        self._num_threads = num_threads or min(4, os.cpu_count() or 1)

    @property
    def loaded_id(self) -> str | None:
        return self._loaded_id

    def ensure_loaded(self, voice_dir: Path, voice_id: str) -> bool:
        if self._loaded_id == voice_id:
            return True
        files = resolve_pack(voice_dir)
        if files is None:
            return False
        import sherpa_onnx

        # Drop the old model before loading the new one, so both are never
        # resident at once.
        self._tts = None
        self._loaded_id = None
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(files.model),
                    tokens=str(files.tokens),
                    data_dir=str(files.espeak_data),
                ),
                num_threads=self._num_threads,
            )
        )
        self._tts = sherpa_onnx.OfflineTts(config)
        self._loaded_id = voice_id
        return True

    def synth(self, text: str, speed: float = 1.0) -> TtsAudio:
        if self._tts is None:
            raise RuntimeError("no voice loaded -- call ensure_loaded() first")
        audio = self._tts.generate(text, sid=0, speed=speed)
        return TtsAudio(np.asarray(audio.samples, dtype=np.float32), audio.sample_rate)
