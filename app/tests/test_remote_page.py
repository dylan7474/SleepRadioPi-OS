"""The phone remote's position bar: our own scrubber, not the browser's slider (which a phone cancels when a drag drifts
up or down). Checked against the page source; the behaviour was driven with real touch events on the radio."""
from pathlib import Path

PAGE = (Path(__file__).parent.parent / "sleepradiopi" / "web" / "remote.html").read_text()


def test_the_position_bar_is_not_a_native_slider_and_takes_no_page_gestures() -> None:
    assert 'type="range"' not in PAGE
    assert 'id="pos" role="slider"' in PAGE
    css = PAGE[PAGE.index(".scrub {"):PAGE.index(".scrub .track")]
    assert "touch-action: none" in css


def test_it_holds_the_finger_sends_one_seek_on_letting_go_and_never_stays_stuck() -> None:
    js = PAGE[PAGE.index("const scrub = (() => {"):PAGE.index('$("back1").onclick')]
    assert "setPointerCapture" in js and '"pointercancel"' in js
    assert js.count('act("/api/seek", { to_ms: to })') == 1          # (once, on pointerup)
    assert "finally { seeking = false; }" in js                       # the radio's answers may move the bar again, always
    assert "Number.isFinite(to)" in js


def test_there_are_one_minute_keys_and_the_bar_waits_for_the_radio_to_catch_up() -> None:
    assert 'delta_ms: -60000' in PAGE and 'delta_ms: 60000' in PAGE
    assert "hold = { to, until:" in PAGE and "hold.until" in PAGE
