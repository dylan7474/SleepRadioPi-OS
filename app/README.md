# The radio software (app/)

This folder is the radio itself, the Python program that runs on the Pi; the
rest of the repo builds the image it runs on and the case (see the
[top-level README](../README.md) for the whole radio). It's a **bedside
radio station** on a Raspberry Pi: an auto-DJ that plays your
local music with an offline text-to-speech presenter between tracks —
back-announcements and intros, 70s-style DJ hooks, station idents, jingles,
and (when it's online) time checks and news bulletins. Plug it in and it's
on air through its own speaker; a single knob sets the volume and pauses.
It also streams to a web page on your home network.

This is a **from-scratch Python reimplementation** of the Broadcast mode of
[SleepRadio](https://github.com/dylan7474/SleepRadio), an Android app. Its
logic (track selection, DJ scripts, time checks, news, loudness levelling,
edge-silence trim, voice EQ) is ported closely, with the Android app's unit
test cases ported alongside. See [`docs/SCOPE.md`](docs/SCOPE.md).

**Status:** running every day on a Pi Zero 2 W with a HiFiBerry MiniAmp
and two 40 mm speakers in a 3D-printed cabinet (the image and the case are
in the rest of [this repo](../README.md)). It starts at power-up, has been tested with pulled plugs
and with no network, and is set up from its web page. See
[`docs/ROADMAP.md`](docs/ROADMAP.md).

## How it behaves

- **Power on and it plays.** About 10 seconds after power-up it chimes, and
  the DJ says "Sleep Radio is warming up…", so you know it's alive; the show
  itself starts once the voice has loaded ("Good evening, and welcome to
  Sleep Radio..."). No app, no phone, no button press. The chime is a small
  separate program (`startup_sound.py`, standard library only) run before
  the station; the spoken line is made once in the DJ's voice after the show
  is under way and kept in the cache, so it plays from the next start-up.
  It follows the saved volume (0 = silent) and can be turned off.
- **One knob.** Turn for volume (0–100 in 0.5 dB steps; the level is
  remembered). Press (and let go) to pause or play. **Hold it for 3 seconds**
  and the radio beeps and reads out its network address — handy away from
  home, where there's no screen to show where the web page is.
- **Everything else is on its web page** (below): EQ, sleep timer, artist
  radio and your own lists of artists, birthdays, test sounds, settings
  backup, shut down.
- **Offline is normal.** Until the clock is known — from the internet (NTP),
  or from an optional DS3231 real-time clock module — the DJ **doesn't say
  the time, greets with "Hello" rather than "Good evening", and there's no
  news** (or birthday wishes); hooks, idents, track intros and jingles carry
  on. When Wi-Fi comes back, time checks and news return within seconds.
- **Pull the plug any time.** On the appliance image the system and the
  music are read-only, and settings are saved atomically (temp file, fsync,
  rename), so a power cut can't leave a half-written file.

## How it sounds

The work that makes it sound like a real station, most of it ported from
SleepRadio:

- **Every track at the same loudness.** Each track is measured (the first
  two minutes' loudness, once, then cached) and turned up or down to a
  common level (~-19 dBFS), within 0.2×–4× and with a soft limit so a boost
  can't clip. No reaching for the knob between a quiet folk song and a
  loud rock one.
- **No dead air.** Silence at the start (0.3–5 s) and end (0.4–25 s) of
  tracks is trimmed, so the DJ comes in as the song ends.
- **A presenter, fully offline.** Neural text-to-speech (sherpa-onnx) in
  the stock voice or your own cloned one, with a broadcast voice EQ
  (high-pass at 120 Hz, less boom at 250 Hz, presence at 3.5 kHz, a little
  air) and levelled to sit with the music. It speaks a clause at a time with
  natural pauses, and every line is made while the previous song plays, so
  there's never a wait.
- **Real links.** Back-announcements and intros ("That was…", "Coming
  up…"), 70s-style DJ hooks, station idents, a greeting for the time of day,
  and time checks worded for the moment they'll actually be spoken (twice as
  often in the morning; a skip re-words them). How often the DJ talks is up
  to you.
- **A proper shuffle.** No repeat within the last ~20 tracks, and the same
  artist is spaced out.
- **Jingles** from a shuffled bag (never the same one twice in a row); a
  short one opens the show. Only on the main mix, since they say "Sleep
  Radio".
- **News** — BBC News bulletins at :00 (top stories) and :30 (softer
  stories), read with a time line for when they're read, and none in the
  night-time quiet hours.
- **It plays when someone's listening.** The show starts for the speaker (or
  the first browser) and stops 30 s after the last listener goes; pressing
  play again within that carries on live.


| Part | Notes |
|---|---|
| Raspberry Pi Zero 2 W | Any Pi with a 40-pin header works; the Zero 2 W is the target (and the tight one: see memory, below). |
| HiFiBerry MiniAmp | 2 × 3 W class-D amp, powered from the Pi's 5 V. No volume control of its own: the station does it in software. |
| Speaker(s) | 4–8 Ω. |
| Rotary encoder with push switch | Volume + pause. A bare encoder or a KY-040-style module. |
| DS3231 RTC module (optional) | Keeps the time with no network. |
| 5 V supply, **2.5 A or more** | The amp draws from the Pi's 5 V. |

### Wiring

A diagram of all of it: [`hardware/wiring/wiring.svg`](../hardware/wiring/wiring.svg).

Solder a **full 2×20 header** on the Pi: the MiniAmp is a pHAT that plugs
onto all 40 pins. It covers the pins the knob and RTC need, so either solder
their wires to the underside of the Pi's header, or put a stacking header /
GPIO extender between the Pi and the amp.

| Use | GPIO | Header pin |
|---|---|---|
| MiniAmp: I2S bit clock | GPIO18 | 12 |
| MiniAmp: I2S word clock | GPIO19 | 35 |
| MiniAmp: I2S data | GPIO21 | 40 |
| MiniAmp: power / ground | 5 V, GND | 2, 4 / 6 |
| Encoder A (CLK) | GPIO17 | 11 |
| Encoder B (DT) | GPIO27 | 13 |
| Encoder push switch (SW) | GPIO22 | 15 |
| Encoder ground | GND | any GND (e.g. 9, 14 or 25) |
| RTC VCC | 3.3 V | 1 |
| RTC SDA / SCL | GPIO2 / GPIO3 | 3 / 5 |
| RTC ground | GND | 9 |

- The Pi's internal pull-ups are enabled on the encoder pins, so a bare
  encoder works. A module with its own pull-ups (KY-040) has a `+` pin: put
  it on **3.3 V (pin 1 or 17), never 5 V**.
- Power the RTC from **3.3 V, never 5 V**: its I2C pull-ups go to its supply,
  and the Pi's I2C pins are 3.3 V only. The common ZS-042 DS3231 board has a
  charging circuit meant for a rechargeable LIR2032; with an ordinary CR2032
  remove its resistor (often marked 201) or diode.
- The MiniAmp doesn't use I2C, and none of the knob/RTC pins are its I2S
  pins. Check HiFiBerry's MiniAmp documentation in case your board uses
  GPIO16/26 for mute or shutdown; nothing else here uses them.

### config.txt

```ini
dtparam=audio=off
dtoverlay=hifiberry-dac                 # the MiniAmp
gpio=17,27=ip,pu                        # encoder pull-ups
dtoverlay=rotary-encoder,pin_a=17,pin_b=27,relative_axis=1
dtoverlay=gpio-key,gpio=22,active_low=1,gpio_pull=up,keycode=164,label="PLAYPAUSE"
# dtoverlay=i2c-rtc,ds3231              # once the RTC is fitted
```

The kernel turns the encoder and switch into input events, which the station
reads from `/dev/input` (`sleepradiopi/io/knob.py`): no GPIO library needed,
and any encoder or button the kernel knows works. The user running the
station must be in the `input` and `audio` groups.

## Running it

There are two ways:

- **The appliance image** (for the finished radio), built by the rest of
  this repo: it boots from a read-only root, keeps settings on a small data
  partition and the music on a read-only one, starts the station at
  power-up, and updates over Wi-Fi into a spare root slot. Change the code
  here, then `make && scripts/update.sh` at the top of the repo (see the
  [top-level README](../README.md)).
- **Raspberry Pi OS** (development): see [`docs/PI_SETUP.md`](docs/PI_SETUP.md).
- **On the desktop**, for the tests (from this folder):

  ```bash
  python3 -m venv .venv
  .venv/bin/pip install numpy mutagen pytest sherpa-onnx
  .venv/bin/python -m pytest
  ```

```bash
./scripts/deploy.sh                 # on the desktop: sync to the Pi, run the tests there
./scripts/install_service.sh        # on the Pi, once: start the station at boot
```

### Files

| What | Where (defaults; all can be set in the config) |
|---|---|
| Settings | `~/.config/sleepradiopi/config.json` (created on first run) |
| Music | `~/media/music/<Artist>/<Album>/NN - Title.ext` |
| Jingles | `~/media/jingles/` |
| Voice packs | `voices/<name>/` — `model.onnx`, `tokens.txt`, `espeak-ng-data/` (the same files SleepRadio uses; never committed) |
| DJ hooks | bundled: `sleepradiopi/data/dj_hooks_70s.txt` (a copy of SleepRadio's) |
| Caches | `~/.cache/sleepradiopi/` — `tags.json` (track tags, so start-up takes seconds, not a minute), `scans.json` (loudness) |
| Volume | `~/.local/state/sleepradiopi/speaker.json` |

### Settings you're likely to change

Most of these are set from the web page; the rest are in the config file.

| Key | Default | |
|---|---|---|
| `speaker_enabled` | `false` | Play through the sound card from start-up (the appliance turns it on). Off by default so a desktop test run doesn't play out loud. |
| `speaker_device` | `"default"` | ALSA device. |
| `speaker_volume` | `30` | Volume on the very first start; after that the knob's last setting. |
| `update_source` | `github:dylan7474/SleepRadioPi-OS` | Where updates come from, or a manifest.json URL (for testing). |
| `web_stream` | `false` | Listen in a browser too (always on without a speaker). |
| `speaker_mono` | `false` | Both speakers play left + right mixed (the web stream stays stereo). |
| `speaker_eq` | flat | `{"bass", "mid", "treble"}` in dB, -12 to 12. |
| `speaker_highpass_hz` | `0` | Low cut for the speaker (~140 with the case's bass port); 0 = off. |
| `knob_step` | `2` | Volume steps per click (1 dB). |
| `broadcast_voice` | `"stock"` | `"stock"` or `"personal"` (a folder in the voices folder). |
| `broadcast_artist` / `broadcast_profile` | `null` | Artist radio, or one of your `profiles` (lists of artists); `null` = everything. |
| `profiles` | `[]` | `[{"name": "Friday List", "artists": [...]}]` |
| `birthdays` | `[]` | `[{"name", "day", "month", "year"?}]` |
| `broadcast_chattiness` | `"maximum"` | `maximum` (a link before every song), `chatty`, `balanced`, `minimal` (every 5). |
| `broadcast_dj_hooks`, `broadcast_jingle_enabled` / `broadcast_jingle_every`, `news_enabled` | on, on / 4, on | The DJ card sets these. |
| `broadcast_announcer_speed`, `broadcast_announcer_volume`, `news_speed`, `news_quiet_hours`, ... | | Config file only for now; see `sleepradiopi/config/settings.py`. |

The "is the clock right?" check reads `SLEEPRADIOPI_CLOCK_FLAG`: a file that
exists once the time is known (the appliance creates `/run/time-synced` when
NTP sets the clock). If the variable isn't set, the clock is trusted.

### The web page

**http://sleepradiopi.local/** (or the address the knob reads out). The
main page is the radio's remote control — now playing, volume and sleep
timer, artist radio, play next, recently on air — and **⚙ Settings** has the
rest, grouped as Sound, Music and the DJ, and The radio. Cards that need a
speaker only appear when the station has one.

**Password (optional).** Off to start with; set, change or remove it under
Settings → Password. Other phones and computers then get a login screen, and
stay logged in for 30 days (changing or removing the password logs them all
out). It's kept as a salted PBKDF2 hash in the config (`web_password`), never
in the settings backup file. The radio itself (127.0.0.1) never needs it, so
it can always be reset over ssh: `python3 -m sleepradiopi.config.auth clear`
(`sleepradio-password clear` on the appliance image), or `set` to choose a
new one. The change is picked up at once.

The cards:

- **Now playing** with a progress bar, what's next, and **Skip**.
- **Listen here** (only when *Listen in a browser* is on, under Settings →
  Sound; off by default): an MP3 stream of the same show in the browser. Off,
  no encoder runs (it costs a Zero 2 W CPU all the time), `/stream` answers
  404, and the stream output only paces the show to real time. Without a
  speaker it's always on.
- **Play next** — search the library (every word must match the title,
  artist or album) and pick a song: it plays after the current one, and the
  DJ introduces it (the gap's talk is re-worded; a time check already worded
  is kept). Several requests play in order; one made during a gap plays
  after the song the DJ has just introduced; off air the next show opens
  with it. Requests play even on artist radio or a list. The search finds
  **albums** too (one per folder, in file order): *Play album* queues the
  whole album to play start to finish — the DJ introduces it ("an album all
  the way through: Rubber Soul, by The Beatles…"), then it's straight on
  like a record, with no talk, jingles or news between tracks, and a
  back-announcement at the end. *Stop album* goes back to the usual mix
  after the song playing.
- **Artist radio** — play one artist only: the DJ then calls the station
  after them ("welcome to Beatles Radio"; a leading "The" is dropped), and
  so do the page heading and tab. **Lists…** makes your own named lists of
  artists (e.g. a "Friday List"; find and tick artists), which appear in the
  same dropdown ("welcome to Friday List on Sleep Radio"). On air, the song
  already lined up next still plays first. If the library has nothing for
  the choice, it plays everything. The jingles say "Sleep Radio", so they
  only play on the main mix (all artists).
- **The DJ** — the voice (your voice packs; changing it restarts the radio,
  about a minute, because only one voice fits in a Zero 2 W's memory), how
  often it talks (before every song, or every 2, 3 or 5), 70s hooks on/off,
  jingles (off or every 2–8 songs), news on/off, and the start-up sound
  (chime or silent). All but the voice are heard from the next gap.
- **Voices** — the voice packs on the radio; *Download the standard voice*
  (if it isn't there) and *Upload* your own (an archive — .zip, .tar.gz,
  .tar.bz2 or .tar.xz — of a Piper / sherpa-onnx voice folder: a .onnx
  model, tokens.txt, espeak-ng-data; the model is renamed model.onnx). Then
  pick it on the DJ card. A radio with **no voice at all** downloads the
  standard one by itself once it's online, and switches to it. On the
  appliance image the voices are on the read-only media partition: the
  station saves the archive under `~/voice-inbox` and a root helper
  (`voice-install-watch`, via `SLEEPRADIOPI_VOICE_REQUEST`) installs it
  (`sleepradiopi/voices.py`).
- **Birthdays** — starts empty; add a name, day and month, and optionally
  the year born. On the day, once the clock is known and outside the news
  quiet hours, the DJ wishes them a happy birthday first thing in a gap
  between songs, at most every 90 minutes and up to 4 times ("happy
  forty-first birthday to Sarah" when the year is known; 29 February is
  celebrated on the 28th in other years). *Hear it* plays a wish now.
- **Volume** — the same 0–100 scale as the knob, and it follows the knob.
  **Speakers: Stereo / Mono** switches at once. **Sleep timer** (15 min to
  1½ h) runs on the radio: it fades the speakers (and the page's own stream)
  over the last minute, then pauses; every open page shows the same
  countdown, and a pause cancels it.
- **Speaker EQ** — bass / mid / treble (±12 dB; shelves at 120 Hz and 6 kHz,
  a peak at 1 kHz) and a **Low cut** (Off / 100–160 Hz, 24 dB/octave) that
  keeps the deepest bass out of small speakers (~140 Hz with the case's bass
  port). The MiniAmp has no EQ of its own, so it's done in software
  (`audio/eq.py`: one linear-phase FIR from the biquads, FFT per block, ~11%
  of one Zero 2 W core when not flat; changes are crossfaded, so no clicks).
  Boosts lower the overall level so they can't clip.
- **Knob switch** — works like the real knob: tap to pause/play, hold 3 s to
  hear the address. If the radio was paused it speaks, then pauses again.
- **Test sound** — for comparing speaker cabinets and checking wiring, on the
  speakers instead of the show for a moment, with the EQ and low cut
  bypassed: a **bass sweep** (40–600 Hz) and a **full sweep** (40 Hz–16 kHz),
  24 s each with the frequency shown live, **pink noise** (for a phone
  spectrum-analyser app), noise on the **left** or **right** speaker only,
  and a **phase check** (every 3 s: both speakers the same, then the right
  one inverted — if the inverted part sounds fuller, a speaker is wired the
  wrong way round, and vocals would vanish in stereo but not in mono).
- **Wi-Fi** — the connection; saved networks (the card's, and ones added
  here — kept as WPA keys, never passwords); *Find networks*; add or remove
  one; the hotspot's name and password (SleepRadio-Setup / sleepradio to
  start with); *Try saved networks now* while it's a hotspot. See
  `sleepradiopi/wifi.py` for how the manager decides.
- **Updates** — the radio's version; *Check for updates* looks at the latest
  GitHub release (`update_source`) and *Install* puts it on (see the
  top-level README): the music carries on while it downloads, the DJ says
  when it's restarting and afterwards that it's been updated -- or, if the
  new version didn't come on air, that it went back.
- **Backup** — *Save settings* downloads everything above plus the volume
  as one JSON file; *Load settings* puts it back (after re-flashing the
  card, or onto a second radio). The file is checked before anything is
  written, and a radio's own set-up (folders, port, sound device, pins) is
  never saved or loaded. Most settings apply at once; others restart the
  station (only where `SLEEPRADIOPI_SUPERVISED` is set, as the service and
  the appliance image do; otherwise at the next start).
- **Shut down** — turns the radio off safely before unplugging (appliance
  image only: the station isn't root, so it creates the file named by
  `SLEEPRADIOPI_POWER_REQUEST`, which a root service watches).

### API

All JSON. What the page uses, for scripts and testing. With a password set,
everything but the page itself and the login needs the session cookie
(except from the radio itself):

| Request | Does |
|---|---|
| `GET /api/auth` | `{"protected", "logged_in", "local"}` |
| `POST /api/login` / `/api/logout` | `{"password"}` → a session cookie (401 if wrong) / forget it |
| `POST /api/password` | `{"password": "..." \| null}` — set, change or remove (needs to be logged in) |
| `GET /api/status` | What's on air, next, history, the library, the voice, the speaker (volume, playing, mono, EQ, low cut, sleep timer, test sound), artist/list playing |
| `POST /api/speaker` | Any of `{"volume": 0-100}`, `{"step": n}`, `{"pause": true \| false \| "toggle"}`, `{"sleep": minutes}` (0 = off), `{"mono": bool}`, `{"eq": {"bass": dB, ...}}`, `{"highpass": Hz}`, `{"test": "bass" \| "sweep" \| "pink" \| "left" \| "right" \| "phase" \| "stop"}` |
| `POST /api/stream` | `{"enabled": bool}` — listening in a browser on/off (saved as `web_stream`) |
| `POST /api/knob` | `{"press": "short" \| "long"}` — the knob's switch |
| `POST /api/skip` | Skip what's on air (track, link, jingle or bulletin) |
| `GET` / `POST /api/dj` | The DJ settings (and the voices there are) / any of `{"voice", "chattiness", "dj_hooks", "jingle_every", "news_enabled", "startup_sound"}` |
| `GET /api/wifi` | Wi-Fi: mode (station / hotspot / connecting), network and address, saved networks, nearby ones |
| `POST /api/wifi/add` / `remove` / `scan` / `hotspot` / `try` | `{"ssid", "password"}` / `{"ssid"}` / – / `{"ssid", "password"}` / – |
| `GET /api/voices` | The voices, the one in use, and any download/upload/install under way |
| `POST /api/voices/standard` | Download and install the standard voice |
| `POST /api/voices/upload?name=N&file=F` | The archive as the body (up to 400 MB); installed in the background |
| `GET /api/update` | The radio's version, any update found, and one under way |
| `POST /api/update/check` / `/api/update/install` | Look for a newer release / install it (the root helper does the work) |
| `GET /api/search?q=` | Up to 40 tracks and 20 albums matching every word, each with an `id` |
| `POST /api/album` / `/api/album/stop` | `{"id": n}` from the search's `albums` — play it next, start to finish / drop the rest of it |
| `POST /api/request` | `{"id": n}` — play that track next; the status's `requests` lists what's queued |
| `GET /api/artists` | Every artist with a track count, your lists, and what's playing |
| `POST /api/station` | `{"artist": name \| null}` or `{"profile": name}` |
| `POST /api/profiles` | `{"profiles": [{"name", "artists": [...]}]}` — replace the lists |
| `GET` / `POST /api/birthdays` | The list (and whose birthday it is today) / `{"birthdays": [...]}` to replace it |
| `POST /api/birthdays/hear` | `{"name", "day", "month", "year"?}` — say that wish now |
| `GET` / `POST /api/settings` | Download the settings file / load one (400 with `{"error"}` if it's wrong) |
| `POST /api/power` | Shut down (404 unless the appliance's watcher is there) |
| `GET /stream` | The MP3 stream (404 while listening in a browser is off) |

## Why Python

The one piece of SleepRadio that has to carry over byte-for-byte — the
on-device cloned TTS voice via `sherpa-onnx` — has a first-class Python
binding that runs unchanged on Raspberry Pi ARM, and ffmpeg does the audio
work. See `docs/SCOPE.md` for the full stack rationale.

## License

The standard voice (downloaded, not in this repo) is Piper's
"southern_english_female" (low), packaged by sherpa-onnx; its training
data is [OpenSLR 83](http://www.openslr.org/83/), CC BY-SA 4.0.

GPL-3.0-or-later — see [`NOTICE`](NOTICE) for why (short version: the offline
TTS engine statically links eSpeak-NG, which is GPL).

## Layout

```
sleepradiopi/
  broadcast/   The station: show clock, DJ scripts, track selection, news, library scan (+ tag cache),
               birthdays.py (wishes on the day), profiles.py (lists of artists)
  audio/       PCM via ffmpeg (decode, loudness, voice EQ); speaker.py (aplay, volume, mono, sleep fade),
               eq.py (3-band EQ + low cut), testsignal.py (sweeps, noise, phase check)
  tts/         sherpa-onnx voice, run in a recycled worker process
  web/         MP3 stream, the web page (page.html) and the JSON API (server.py)
  config/      Settings (JSON), atomic file writes, settings backup (backup.py), the web password
               (auth.py), "is the clock right?"
  io/          knob.py (rotary encoder + push switch, short/long press), announce.py (spoken address)
  data/        dj_hooks_70s.txt
  playback/    Other sources (deferred: Broadcast is the focus)
scripts/       deploy.sh, install_service.sh, smoke_test_audio.py, dev_web.py
docs/          SCOPE.md, ROADMAP.md, PI_SETUP.md
voices/        (git-ignored) voice packs — never committed
tests/
```

## Pi Zero 2 W: memory is the limit

A neural voice is the heaviest part, and ~464 MB (after freeing the GPU's
reservation) only fits **one** voice alongside the stream. So the DJ and the
newsreader share a voice (`news_voice: "same"`), text is synthesised a
clause at a time, and the TTS worker process is recycled when it passes
200 MB (its buffers grow and never shrink). See `sleepradiopi/tts/worker.py`
and the "Lessons" section of `docs/PI_SETUP.md`.
