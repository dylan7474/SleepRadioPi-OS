"""Channel A: the one main source playing at a time -- local music,
audiobooks, internet radio, or Broadcast Radio (see sleepradiopi.broadcast).

Uses python-vlc (libVLC) or mpv for actual decoding/streaming, replacing
SleepRadio's Media3/ExoPlayer. Local file discovery uses plain filesystem
paths (no Storage Access Framework equivalent needed on Linux).
"""
