# Scope

> **Update 2026-09-22 — Broadcast Radio is the product.** The station and
> its listen-in-the-browser stream are the main feature; the network
> non-goal below no longer applies to that stream (LAN only, no auth). The
> other sources in the feature list (local albums, audiobooks, internet
> radio, ambient channels) are deferred. See `ROADMAP.md`.

SleepRadioPi ports the *functionality* of the Android app
[SleepRadio](https://github.com/dylan7474/SleepRadio) to a standalone
Raspberry Pi appliance. It deliberately drops SleepRadio's phone UI (Compose
screens, skins, VU meters, VOL/BAL knobs) in favour of physical buttons and a
small LCD — this is an embedded-device rebuild, not a UI port.

## What carries over unchanged

- **The cloned voice itself.** The exact same voice pack files used by
  SleepRadio's Broadcast mode (`model.onnx`, `tokens.txt`,
  `espeak-ng-data/`) work as-is: `sherpa-onnx` ships a Python binding that
  runs natively on Raspberry Pi ARM, no conversion needed.
- **The core algorithms**, as a design reference to translate mechanically
  into Python. Confirmed by inspecting the Android source directly — these
  have zero Android-API dependency in the original Kotlin:
  - Broadcast track selection (recency/artist-spaced shuffle) —
    `BroadcastSelector.kt`
  - DJ script/greeting text generation — `DjScriptBuilder.kt`
  - Broadcast state machine / segue models — `BroadcastModels.kt`
  - Noise-colouring and binaural-beat waveform synthesis (the *math*, not the
    Android `AudioTrack` output plumbing) — `NoiseGenerator.kt`,
    `BinauralGenerator.kt`

## What gets rebuilt (different platform, not a defect)

| SleepRadio (Android) | SleepRadioPi (Pi/Python) |
|---|---|
| Media3 `ExoPlayer` | `python-vlc` (libVLC) or `mpv` — handles local files, HTTP radio, ICY metadata, HLS |
| Storage Access Framework folder pickers | Plain filesystem paths in config — actually *simpler* on Linux |
| `MediaMetadataRetriever` | `mutagen` for tags/duration |
| DataStore + Room | Flat JSON or SQLite settings file |
| Android `MediaSession` / notification | Not needed — no phone lock screen, no Bluetooth media-key contract to satisfy |
| Compose UI, skins, VU meters | Physical buttons (`gpiozero`) + small LCD (`luma.lcd`/`RPLCD`) |
| `AudioTrack` output | Standard ALSA device (the HiFiBerry board just *is* an ALSA sound card) |

## Non-goals (v1)

- No phone app, no remote control, no network API (may revisit later)
- No podcasts (defer — lower priority than the core four sources)
- No Android Auto (not applicable — this *is* the dedicated device)
- No attempt to recreate the Compose skins/VU meters — physical buttons + LCD
  status text is the whole control surface

## Feature parity target (v1)

1. **Channel A (one main source at a time)**
   - Local music folder (any subfolder with audio = an "album")
   - Audiobooks folder (per-book resume position)
   - Internet radio (HTTP/HLS stream, ICY now-playing metadata)
   - **Broadcast Radio**: auto-DJ over the local music folder, with a
     scheduled spoken link between tracks (naming what just played / what's
     next), optional jingles between tracks, all via the on-device cloned
     voice
2. **Two ambient channels**, always layered under Channel A: procedural
   coloured noise, binaural beats
3. **Sleep timer** that fades and stops Channel A on schedule, leaving the
   ambient channels running
4. **Physical control surface**: a small number of preset buttons (source
   select, mirroring SleepRadio's 4 preset slots), transport controls
   (play/pause/skip), volume, and a sleep-timer control; LCD shows
   now-playing text and basic status

## Hardware assumptions

- Raspberry Pi (I2S-capable — required for HiFiBerry)
- HiFiBerry amp board + passive speakers
- A small LCD (exact model TBD — kept behind a `Display` interface in
  `sleepradiopi/io/` so swapping panel types doesn't touch application logic)
- A handful of GPIO buttons (exact pin mapping lives in config, not code)

## Stack

Python 3, `sherpa-onnx` (TTS), `python-vlc` or `mpv` (playback), `numpy` +
`sounddevice` (ambient mixer), `mutagen` (tags), `gpiozero` (buttons),
`luma.lcd`/`RPLCD` (display) — see `docs/ROADMAP.md` for build order.
