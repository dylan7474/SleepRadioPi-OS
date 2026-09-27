"""Settings persistence: folder paths, preset assignments, broadcast voice
and chattiness, jingle settings, GPIO pin mapping, LCD panel type.

Replaces SleepRadio's DataStore + Room with a flat JSON file -- there's no
Storage Access Framework ceremony to route around on Linux, so this is
simpler than the Android equivalent, not a cut-down version of it.
"""
