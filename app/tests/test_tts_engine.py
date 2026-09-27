from pathlib import Path

import pytest

from sleepradiopi.tts.engine import OfflineTtsEngine, resolve_pack


def make_pack(root: Path, name: str, espeak: bool = True) -> Path:
    pack = root / name
    pack.mkdir()
    (pack / "model.onnx").write_bytes(b"")
    (pack / "tokens.txt").write_text("")
    if espeak:
        (pack / "espeak-ng-data").mkdir()
    return pack


def test_complete_pack_uses_its_own_espeak_data(tmp_path: Path) -> None:
    pack = make_pack(tmp_path, "stock")
    files = resolve_pack(pack)
    assert files is not None
    assert files.espeak_data == pack / "espeak-ng-data"


def test_model_only_pack_borrows_sibling_espeak_data(tmp_path: Path) -> None:
    stock = make_pack(tmp_path, "stock")
    personal = make_pack(tmp_path, "personal", espeak=False)
    files = resolve_pack(personal)
    assert files is not None
    assert files.model == personal / "model.onnx"
    assert files.espeak_data == stock / "espeak-ng-data"


def test_pack_missing_tokens_is_unusable(tmp_path: Path) -> None:
    pack = make_pack(tmp_path, "stock")
    (pack / "tokens.txt").unlink()
    assert resolve_pack(pack) is None


def test_lone_model_only_pack_is_unusable(tmp_path: Path) -> None:
    assert resolve_pack(make_pack(tmp_path, "personal", espeak=False)) is None


def test_missing_pack_does_not_load(tmp_path: Path) -> None:
    engine = OfflineTtsEngine()
    assert engine.ensure_loaded(tmp_path / "nope", "nope") is False
    assert engine.loaded_id is None


def test_synth_before_load_raises() -> None:
    with pytest.raises(RuntimeError):
        OfflineTtsEngine().synth("hello")
