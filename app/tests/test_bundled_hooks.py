import re

from sleepradiopi.broadcast.selector import parse_hooks
from sleepradiopi.main import BUNDLED_HOOKS


def test_bundled_hooks_parse_and_follow_their_rules() -> None:
    hooks = parse_hooks(BUNDLED_HOOKS.read_text())
    assert len(hooks) >= 50
    # The file's own rules: no digits, and nothing shouted on a sleep station.
    assert not [h for h in hooks if re.search(r"\d", h)]
    assert not [h for h in hooks if "!" in h]
