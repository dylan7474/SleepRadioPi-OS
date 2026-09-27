"""Broadcast Radio: auto-DJ over the local music folder, with a scheduled
spoken link between tracks (via sleepradiopi.tts) and optional jingles.

This is the most directly portable part of SleepRadio: BroadcastSelector.kt,
DjScriptBuilder.kt, and BroadcastModels.kt have zero Android dependency in
the original Kotlin -- they're pure selection/text-generation logic and
translate to Python close to line-for-line.
"""
