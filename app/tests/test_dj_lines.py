"""DJ lines, fully managed: this radio's changes to the 70s lines that come with it (selector.py), and the shuffle."""
import pytest

from sleepradiopi.broadcast.selector import apply_hook_changes, validate_hook_changes
from sleepradiopi.config import backup


def test_changes_are_new_words_or_off_and_nothing_else() -> None:
    got = validate_hook_changes({"A": {"text": "  A   my  way "}, "B": {"off": True}, "C": {"text": "C"}})
    assert got == {"A": {"text": "A my way"}, "B": {"off": True}}      # (C unchanged: no change kept)
    for bad in ({"A": {"text": ""}}, {"A": "x"}, {"": {"off": True}}, {"A": {"text": "x" * 241}}, ["A"]):
        with pytest.raises(ValueError):
            validate_hook_changes(bad)
    assert validate_hook_changes(None) == {}


def test_the_lines_said_are_changed_or_left_out_and_new_shipped_ones_still_come() -> None:
    shipped = ["One", "Two", "Three", "Four (new in this release)"]
    changes = {"One": {"text": "One, my way"}, "Two": {"off": True}, "Gone (no longer shipped)": {"off": True}}
    assert apply_hook_changes(shipped, changes) == ["One, my way", "Three", "Four (new in this release)"]


def test_a_backup_keeps_the_changes_and_checks_them() -> None:
    assert backup._check("dj_hooks_changes", {"A": {"off": True}}) == {"A": {"off": True}}
    with pytest.raises(backup.BadSettings):
        backup._check("dj_hooks_changes", {"A": {"text": ""}})
