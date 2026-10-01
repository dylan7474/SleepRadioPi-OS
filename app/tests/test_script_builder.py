from pathlib import Path


def test_a_hook_names_the_station_playing() -> None:
    """A bundled hook said "Sleep Radio" on every theme: it names the theme now."""
    from importlib.resources import files
    from sleepradiopi.broadcast.models import BroadcastTrack
    from sleepradiopi.broadcast.script_builder import DjScriptBuilder, LinkKind
    from sleepradiopi.broadcast.selector import HookPool, parse_hooks
    b = DjScriptBuilder(hooks=HookPool(["{station}, turntable turning, tea brewing."]), station="Carisbrooke Radio")
    t = BroadcastTrack(Path("x.mp3"), "Paper Doll", "The Ink Spots")
    line = b.build(LinkKind.LINK, t, t)
    assert "Carisbrooke Radio, turntable turning" in line and "{station}" not in line
    hooks = parse_hooks((files("sleepradiopi") / "data" / "dj_hooks_70s.txt").read_text())
    assert not any("Sleep Radio" in h for h in hooks)
