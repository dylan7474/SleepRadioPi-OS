"""Ambient mixer: procedural coloured noise + binaural beats.

Ports the waveform synthesis from SleepRadio's NoiseGenerator.kt /
BinauralGenerator.kt (pure math there, no Android dependency in the
synthesis itself -- only the final AudioTrack write). Output here targets a
sounddevice stream onto the HiFiBerry ALSA device instead.
"""
