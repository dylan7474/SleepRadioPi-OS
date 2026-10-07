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

- **The DJ is never waited for, like a real station.** At power-on and back
  to Sleep Radio from a station, album, audiobook or podcast, a welcome that's
  ready is said as usual; if not (a Zero takes ~20 s to reload the voice,
  then 30–60 s to say it, and waiting sounded broken), one of the **short
  jingles** (40 s or less) plays at once and then the music, and the DJ joins
  at the next gap (`Station._opening_steps`). Between songs, a line not made
  by the end of the song (2 s grace) gets a jingle while it's made, once a
  gap (the station's own short ones, else Power-on's), and is said if it's
  ready by then, else skipped (`Station._run_steps(gap=True)`) — the news
  too: its time line gets the jingle, and if it's still not made the
  bulletin goes straight to the stories. A **message** that wasn't ready
  comes round again at the next gap (`Messages.unplayed`). No jingles to be
  had: a short gap.
  The fill-in jingle follows the station's jingles on/off setting (jingles
  off: none at all, not even Default's: straight to the music); it counts as
  the station's jingle (so "every 4 songs" stays every 4), never comes two
  gaps running, and a line that *failed* (rather than being slow) gets no
  jingle: there's nothing to wait for.
- **A stuck sound card restarts the radio.** If aplay keeps failing (once, the
  card refused to open after a boot until a restart), the speaker retries ever
  more slowly (0.1 s up to 5 s, logged at the 1st, 2nd, 4th... failure), and
  after 2 minutes the radio restarts itself -- at most once an hour
  (`SpeakerOutput._failed`, `main._speaker_stuck`).
- **Power on and it plays.** About 10 seconds after power-up it chimes, and
  the DJ says "Just warming up." (no name: it may be about to play any
  station), so you know it's alive; then a
  soft tick every 3 s (like a clock) until the station opens the speaker,
  and the show starts: with the DJ's welcome if it's made ("Good evening, and
  welcome to Sleep Radio..."), otherwise a short jingle and the music (above).
  No app, no phone, no button press. The chime is a small
  separate program (`startup_sound.py`, standard library only) run before
  the station; the spoken line is made once in the DJ's voice after the show
  is under way and kept in the cache, so it plays from the next start-up.
  It follows the saved volume (0 = silent) and can be turned off.
- **One knob.** Turn for volume (0–100 in 0.5 dB steps; the level is
  remembered). Press (and let go) to pause or play. **Hold it for 3 seconds**
  and the radio beeps and reads out its network address — handy away from
  home, where there's no screen to show where the web page is.
  **Press and turn** to go back or on through what's playing: a book or
  podcast half a minute a click, a song from an album or playlist ten
  seconds a click (a quick spin goes two or four times as far). Playing, it
  jumps; paused, it moves the place and plays a snatch of it (0.3 s) every
  half second while it moves, like a CD player's scan, and the radio stays
  paused until you press. On Theme Radio or a station it does nothing; let
  go and the press does nothing either (`io/seek.py`,
  `Station.knob_seek`; `POST /api/knob {"press": "turn", "clicks": n}`
  tries it from the page). Paused inside such a song, it keeps its place.
- **Everything else is on its web page** (below): EQ, sleep timer, artist
  radio and your own lists of artists, birthdays, test sounds, settings
  backup, restart and shut down.
- **Offline is normal.** Until the clock is known — from the internet (NTP),
  or from an optional DS3231 real-time clock module — the DJ **doesn't say
  the time, greets with "Hello" rather than "Good evening", and there's no
  news** (or birthday wishes); hooks, idents, track intros and jingles carry
  on. When Wi-Fi comes back, time checks and news return within seconds.
  A radio that lives off Wi-Fi keeps its clock to the RTC (re-read once a
  day), and how far out the RTC was is noted each time the internet time
  comes back (`/data/clock.log` on the appliance image).
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
  short one opens the show. **Each station has its own folder** in the
  jingles folder (`Station.jingles_folder`): `Sleep Radio/` (the radio's name)
  for a radio with no themes yet, `<theme>/` for each theme -- made by
  themselves a minute after start-up and whenever the themes change
  (`main.jingle_folders`), which also moves loose jingles from before into
  the main show's. No shared jingles: an empty folder means none on that
  station. **At start-up** the station playing gets one of its own jingles
  (a short one if it has any, when jingles are on), else one from
  `Power-on/` (loose jingles at the top of the folder move there), then the
  welcome (if made) or the music (`Station._start_up_jingle`).
- **News** — BBC News bulletins at :00 (top stories) and :30 (softer
  stories), read with a time line for when they're read, and none in the
  night-time quiet hours. A bulletin still being made when its gap comes
  (a skip can bring the gap early) is read at the next gap instead of
  leaving dead air; any other wait for speech shows "Just a moment" on the
  page, and Skip ends it.
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
their wires to the underside of the Pi's header, or use a 1-to-2 GPIO
splitter (this build uses a GeeekPi 1-to-2 40-pin GPIO Edge Adapter Board) or
a stacking header between the Pi and the amp.

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
| Preset buttons 1 / 2 / 3 / 4 (each to GND) | GPIO5 / 6 / 16 / 8 | 29 / 31 / 36 / 24 |
| *Cathedral:* selector positions 1–4 | GPIO5 / 6 / 7 / 8 (3 not on GPIO16: the MiniAmp's mute) | 29 / 31 / 26 / 24 |
| *Cathedral:* selector positions 5 / 6 | GPIO23 / 24 | 16 / 18 |
| *Cathedral:* selector common, back button's other side | GND | 30, 34 or 39 |
| *Cathedral:* back button | GPIO25 | 22 |
| *Cathedral:* VU needle (~5.6 k + 2 k trimmer to the meter's +) | GPIO12 (PWM0) | 32 |
| *Cathedral:* grille light (a logic-level MOSFET's gate, ~100 Ω) | GPIO13 (PWM1) | 33 |

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
dtoverlay=gpio-key,gpio=5,active_low=1,gpio_pull=up,keycode=2,label="PRESET1"   # ... 6/3, 16/4, 8/5
dtoverlay=gpio-key,gpio=23,active_low=1,gpio_pull=up,keycode=6,label="PRESET5"  # cathedral: 24/7
dtoverlay=gpio-key,gpio=7,active_low=1,gpio_pull=up,keycode=4,label="PRESET3B"  # cathedral position 3
dtoverlay=gpio-key,gpio=25,active_low=1,gpio_pull=up,keycode=139,label="BACK"   # cathedral
dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4                                # cathedral: needle, light
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

### Two radios

The same software runs the box radio and the cathedral radio (the
[Phonosphere](https://github.com/dylan7474/Phonosphere), private for now);
`hardware` in the settings says which, and the "This radio" card on the page
sets it (Settings → The radio → This radio). The name the DJ says, the page shows and the set-up Wi-Fi uses comes
from one place (`config/brand.py`): `station_name`, or the hardware's own.
Recorded jingles are just files, so swap in ones that say the new name.

| | Box (`"box"`) | Cathedral (`"cathedral"`) |
|---|---|---|
| Presets | 4 buttons: press plays, hold saves | A 6-way rotary selector: a position plays once the knob settles there (0.6 s), no hold-to-save (save from the page) |
| Day / night sets | Hold 2 + 3 for 1 s | Press the back button |
| Service menu | Hold 1 + 4 for 5 s; the buttons choose | Hold the back button 5 s; turn the selector to choose (it says the option), press the back button to confirm |
| VU meter | — | A 500 uA needle on GPIO12 (hardware PWM), fed the music's level before the volume; ~0 VU for normal music, falls back at rest |
| Grille light | — | Warm-white LEDs behind the cloth via a MOSFET on GPIO13: its own brightness for each set, fades on pause and with the sleep timer |

The selector's positions and the back button are GPIO keys (keycodes 2–7 and
139, see config.txt); the lamps need `dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4`.
Without the PWM (a desktop, the box) they quietly do nothing.

### Settings you're likely to change

Most of these are set from the web page; the rest are in the config file.

| Key | Default | |
|---|---|---|
| `hardware` | `"box"` | Which radio this is: `"box"` (four buttons) or `"cathedral"` (the Phonosphere: six-way selector, back button, VU needle and grille light). See *Two radios*. |
| `station_name` | `null` | The radio's name as the DJ says it and the page shows it; `null` = the hardware's own ("Sleep Radio", "Phonosphere"). |
| `glow_day`, `glow_night`, `meter_trim_db` | `60`, `15`, `0` | The cathedral's grille light by day and night set (0–100 %), and where normal music sits on the meter (-12 to 12 dB). |
| `speaker_enabled` | `false` | Play through the sound card from start-up (the appliance turns it on). Off by default so a desktop test run doesn't play out loud. |
| `speaker_device` | `"default"` | ALSA device. |
| `speaker_volume` | `30` | Volume on the very first start; after that the knob's last setting. |
| `update_source` | `github:dylan7474/SleepRadioPi-OS` | Where updates come from, or a manifest.json URL (for testing). |
| `web_stream` | `false` | Listen in a browser too (always on without a speaker). |
| `speaker_mono` | `false` | Both speakers play left + right mixed (the web stream stays stereo). |
| `speaker_eq` | flat | `{"bass", "mid", "treble"}` in dB, -12 to 12. |
| `noise_on`, `noise_kind`, `noise_mix` | off, `pink`, `50` | The noise layer: on/off, its colour, and the balance with the programme (0 programme only, 50 both full, 100 noise only). |
| `speaker_highpass_hz` | `0` | Low cut for the speaker (~140 with the case's bass port); 0 = off. |
| `knob_step` | `2` | Volume steps per click (1 dB) in the knob's Normal mode. |
| `knob_mode` | `"auto"` | The volume knob: `auto` (a slow turn a step a click, a quick spin 2, 3 or 5 a click), `fine` (1), `normal` (`knob_step`) or `coarse` (5). Settings → Speakers → Knob. |
| `programmes` | `[]` | Running orders the radio plays by itself (see *Programmes*) |
| `playlists` | `[]` | `[{"name": "Sunday", "tracks": [["music" \| "ondemand", "path/in/it.mp3"], ...]}]` (see *Playlists*) |
| `broadcast_voice` | `"stock"` | `"stock"` or `"personal"` (a folder in the voices folder). |
| `buttons_night`, `buttons_bank` | `[]`, `"day"` | The night set of four (like `buttons`, which is the day set), and the set in use. Hold buttons 2 and 3 together for 1 s to swap: three notes, rising for day, falling for night. |
| `buttons_auto` | `{}` | `{"on", "night_min", "day_min"}` (defaults off, 21:00, 07:00): swap the sets by the clock, quietly; a swap by hand lasts until the next switch time. |
| `broadcast_dj` | `true` | `false`: the show is music only, no speech at all (see *The DJ*). |
| `buttons` | `[]` | The four preset buttons (the day set): `[preset or null, ...]`, each `{"kind": "show", "artist", "profile"}`, `{"kind": "radio", "name", "url"}`, `{"kind": "album", "folder", "title", "artist"}` or `{"kind": "action", "action": "time" \| "news" \| "sleep" \| "address"}`. |
| `radio_stations` | the starter set | Internet radio: your saved stations, `[{"name", "url", "info"?}]`. |
| `stream_source` | `null` | What's playing instead of the show — a station, or an album and the track it's on — kept over a restart; `null` = the show. Not in the settings backup. |
| `broadcast_profile` | `null` | The theme playing (one of your `profiles`); `null` = the theme last played, else Default. (`broadcast_artist`, artist radio, is retired: one saved from before becomes a one-artist theme at start-up.) |
| `profiles` | `[]` | `[{"name": "Friday List", "artists": [...]}]` |
| `birthdays` | `[]` | `[{"name", "day", "month", "year"?}]` |
| `messages` | `{}` | `{"on", "every_min", "offset_min", "start_min", "end_min", "date_first", "list": [{"text", "until"?, "off"?}]}` (defaults: on, 15, 7, 480, 1260, true) |
| `broadcast_chattiness` | `"maximum"` | `maximum` (a link before every song), `chatty`, `balanced`, `minimal` (every 5). |
| `broadcast_dj_hooks`, `broadcast_jingle_enabled` / `broadcast_jingle_every`, `news_enabled` | on, on / 4, on | The DJ card sets these. |
| `broadcast_announcer_speed`, `news_speed` | `0.85`, `0.70` | Speech speed, 0.5–1.5 (1 = the voice's own pace, higher = faster). The DJ card sets these. |
| `broadcast_announcer_volume`, `news_quiet_hours`, ... | | Config file only for now; see `sleepradiopi/config/settings.py`. |

The "is the clock right?" check reads `SLEEPRADIOPI_CLOCK_FLAG`: a file that
exists once the time is known (the appliance creates `/run/time-synced` when
NTP sets the clock). If the variable isn't set, the clock is trusted.

### The web page

**http://sleepradiopi.local/** (or the address the knob reads out). Designed
for a phone, with three tabs along the bottom (and plain links: `#find`,
`#settings`, `#set-<page>`; the old `#radio` and `#media` still work):

- **Radio** — a glowing **dial window** with what's on (its needle points at
  the preset button that's playing; *Skip* or *Back to Sleep Radio*; greyed
  and "Paused" when paused), **the radio** itself (the knob and the four
  preset buttons, drawn like the real one), the sleep timer (on the phone
  remote its key fills with the time left, counting down, while it runs) and what's been
  on air.
- **Find** — one search over **artists and your themes** (*Play* a theme),
  **albums** (*Play* start to finish, or *With the DJ*), **songs** (*Play
  next*) and **internet stations** (your saved ones and the whole directory:
  *Play*, *Save*), narrowed with Everything / Artists / Albums / Songs /
  Stations. With nothing typed: tiles for Sleep Radio (everything, your lists,
  the artist playing) and your saved stations, and what's coming up.
- **Settings** — a list grouped as Sound, The show, Music and The radio, each
  row showing what it's set to now; a row opens that setting's own page. Rows
  that need a speaker only appear when the station has one.

It works with no internet (system fonts, nothing loaded from elsewhere).

**Password (optional).** Off to start with; set, change or remove it under
Settings → Password. Other phones and computers then get a login screen, and
stay logged in for 30 days (changing or removing the password logs them all
out). It's kept as a salted PBKDF2 hash in the config (`web_password`), never
in the settings backup file. The radio itself (127.0.0.1) never needs it, so
it can always be reset over ssh: `python3 -m sleepradiopi.config.auth clear`
(`sleepradio-password clear` on the appliance image), or `set` to choose a
new one. The change is picked up at once.

The cards:

- **The radio** — the top of the radio drawn as it is: the blue **knob** at
  the back and the four silver **preset buttons** along the front edge, each
  labelled with what it plays (the one playing glows), over the front face's
  grilles and lettering. The knob works like the real one: drag it round (or
  − / +, the mouse wheel, arrow keys) for volume, over a 7-to-5 o'clock sweep;
  **tap it to pause or play** (it greys out when paused). With no speaker of
  its own, a tap starts or stops listening in the browser. The sleep timer
  sits under it.

- **Now playing** with a progress bar, what's next, **⏮ Back** and **Skip**.
  Back works like a CD player: more than 5 seconds into a song it starts it
  again, sooner it goes to the one before (and then the one cut short plays
  again), with no DJ in between; while the DJ talks it replays the song that
  just ended. In an album or On demand it steps back a track. Not on stations,
  books or podcasts (they have ⏪ 1 min). **＋** adds the song on air to a
  playlist.
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
  with it. Requests play on any theme. The search finds
  **albums and folders** too (one per folder, in file order, matched on the
  tags and the folder's name), each with the same two buttons as everywhere:
  **Play now** plays it start to finish instead of the show, with no DJ, and
  back to the show at the end; **Play next** queues it after the song
  playing — from the music, the DJ introduces it ("an album all the way
  through: Rubber Soul, by The Beatles…"), then it's straight on like a
  record, with no talk, jingles or news between tracks, and a
  back-announcement at the end. *Stop album* goes back to the usual mix
  after the song playing.
- **Playlists** (Find → Playlists) — your own lists of songs, from the music
  or On demand: make one, press *Add songs*, then **＋** on songs in Find, on
  albums and folders (search or Browse), or beside Skip for the song on air;
  in the playlist, move songs up or down, take them out, rename or delete it.
  **Play** or **Shuffle** starts it at once, **straight through like an album:
  no DJ, jingles or news** (those are Theme Radio's), then back to the show.
  Skip and Back step through it; **Stop playlist** lets the song playing
  finish. A song whose loudness hasn't been measured yet (a whole decode,
  tens of seconds for a long track on a Zero) doesn't hold it up: after
  1.5 s it plays as it is, and the measurement is kept for next time
  (`SOURCE_SCAN_WAIT_S`; albums too). A programme's music block plays the same way. A new playlist on the
  desktop opens with its name ready to type; the name box in its window
  renames it, and buttons and programmes that play it follow. A preset button
  can hold one, in order or shuffled. A song whose file has gone is skipped. Kept in the settings (`playlists`), so *Save settings* has them
  (`broadcast/playlists.py`, `Station.play_playlist`).
- **The phone remote** (`web/remote.html`, served at `/`), styled as the
  desktop's radio but big, for eyes that aren't what they were, all on one
  screen with no scrolling. The top fifth at most is the radio's amber dial
  with **one line of large scrolling text** -- what's on (song and artist, or
  station) -- and its needle following the song. The rest is the cream case
  with the radio's silver keys (Play/Pause is the blue knob; Sleep and Noise
  get the amber ring when on), CD-player style: a thick
  **position bar** (drag it), always there so nothing moves, greyed out when
  there's nothing to move through (a station, the DJ, a jingle); **⏮ Play/Pause ⏭** -- a tap on ⏭ is the next song, held it fast-
  forwards (faster the longer it's held), and ⏮ the same backwards (a tap:
  the start, or the one before; in a book or podcast a tap is half a minute);
  volume − / the number / +; **Sleep 30 min** (tap again: off; it shows the
  minutes left) and **Noise** on/off. *Desktop* in the menu bar has the rest.
  (The four buttons are on the radio itself.) It goes dark at night by the
  clock (the buttons' *Night from* / *Day from* times, 21:00-07:00 unless
  changed) and while the radio uses its Night buttons. A computer opening `/` goes to
  the desktop. On the radio's own network (first set-up, or after a Wi-Fi
  reset) `/` is a **Wi-Fi set-up** screen instead: networks nearby, tap one,
  its password, Join (or type a hidden network's name); phones checking for
  internet there are sent to it. It uses no web fonts, so it works with no
  internet.
- **The classic page is retired** (2026-10-01): everything it had is on the
  desktop (the speaker analyser hands its results to the desktop's Speakers
  window; Stations, Test sounds and Listen in a browser are native windows),
  and the phone has the remote. `/classic` goes to the desktop.
- **The desktop** (`http://<radio>/desktop`, `web/desktop.html`) — the
  interface for a computer: opening the radio's address on a wide screen with a
  mouse goes straight to it (this page stays for phones and tablets; the
  desktop's *Classic page* link keeps you here, and this page's *Desktop* link
  goes back). A menu bar;
  icons for Music, On demand, Audiobooks, Podcasts, Stations, Playlists,
  Programmes, Theme radio, Actions and Jingles (folders load from `/api/browse` as you open
  them); windows you move and resize; every folder window shows its things as
  **icons or a list** (the switch in its toolbar; the list has Name, Details,
  Size and Kind columns (`/api/browse` gives each folder's and track's
  `bytes`), and rows drag, drop and open like icons; click a column
  heading to sort by it, again to reverse, a third time for the folder's own
  order (the icons follow the same sort, and the window's footer says when
  one is on); view and sort are kept per folder
  in the browser, and a music or On demand folder you haven't chosen for
  looks like the one it's in); **windows remember** the size and place you
  give them, in this browser (each window its own, and each kind its size
  for ones not opened yet; `srd.geom`); **pick several things** as on a Mac
  (Ctrl/Cmd-click one more or one less, Shift-click a run, drag a box over
  empty space, Ctrl+A all in the front window) and drag them together: into
  a folder (moved), onto a playlist (added), to the Trash (files: one
  question for them all; Delete does it too), or onto the radio's dial, which
  makes them an **"On the fly" playlist** (songs, albums and folders, in the
  order picked; made again each time, kept in Playlists) and plays it;
  **the radio on the desktop** showing
  what's on air (polled every 2.5 s), with **Back** (like a CD player) and **Skip** keys under the dial (pause is the knob): drop anything on its dial to play it (a theme, or a button for one, takes over at once, like turning the dial: the song playing stops and the new station opens with its welcome if it's made, else one of its short jingles, else its music -- it used to wait for the song to end; music played from the desktop -- a song, an album, a folder, a playlist, an "On the fly" one -- plays **on its own, with no DJ or jingles, and then the radio pauses**; an artist's folder plays their songs shuffled, once each; the knob then plays the theme again. Buttons keep going back to the theme),
  on one of its buttons to keep it there (including a **message**, a **jingle** or a **birthday** — said or played over what's on — an **action** from the Actions folder: the time, the pips, the news, the address, noise on/off, DJ on/off; and a **sleep time** from the Sleep timer folder (in Actions; a line under the radio's knob shows the sleep timer: faint when it's off, lit with the minutes left while it counts down): 5, 10, 15, 30, 45, 60 or 90 minutes, each a thing -- double-click one, or drop it on the radio, to start it now (again: off), or drop it on a button, the knob's folder or a programme; or pick from each button's *Change…* list in the Buttons window), a **☀ Day / ☾ Night** switch above them shows either set, and dropping on a button programmes the set shown without swapping the radio's (*Use it now* swaps), scroll the knob for the volume,
  click it to pause. Programmes are built in their window: drop things on the
  timeline for new blocks or on a block to add to it, drag a block to move it
  or its right edge to change its length, and set its rule, order, start time,
  days, what fills the gaps and what happens after; every block has a start
  time you can type, and dragging a block along the timeline moves it in time
  (the order follows). The timeline covers the **whole day**: a day strip above
  it shows where the programme's blocks are (click or drag it to move along),
  *Show* zooms to the programme, 1, 3, 12 or 24 hours (or Ctrl+scroll), the
  wheel scrolls along the day, a block dragged to an edge scrolls on, and a
  second click on a chosen block opens the keys to set its start time (every time of day on the page is set the same way: click it for big up and down keys, hours and minutes in fives, Shift for single minutes, the mouse wheel too; a length has a minus and a plus round it); *＋ Moment* and the block's *Say it / Time check / The
  news / Sleep in 30 minutes* add moments (any sleep time can be dragged in from its folder); drag a sticky note or a jingle in too.
  They're saved on the radio. It works like a Mac's desktop throughout: **Escape**
  closes the front window, **Delete** puts the chosen thing in the Trash, the
  **arrow keys** move around a folder and Enter opens; a delete that can be taken
  back (a playlist, a programme, a message, a birthday, a theme, a station, a
  block) happens at once with **Undo** on its message, and only deleting files
  (or unfollowing a podcast) asks first, with Cancel as the default; double-click
  a **jingle** to hear it or an **action** to do it now (also POST /api/instant);
  what's playing wears a small level-bars badge wherever its icon is; windows
  that were open come back after a reload. The menu bar's **New** menu makes anything: a
  programme, playlist, message, birthday, artist list, folder, station or
  podcast. Dropping a podcast **episode** on a button keeps the podcast there,
  starting from that episode. Drop songs, albums
  or folders on a playlist; the Trash deletes playlists and programmes (asks
  first) or takes shortcuts off the desktop. Settings: click the part of the
  radio they affect (the speakers, the knob, the buttons, its name) or turn it
  round (**Settings on the back**): each has its own window reading and
  writing the radio — Speakers (knob, stereo/mono, EQ, low cut, test sounds),
  the DJ (on/off, voice, how often, hooks, jingles, speeds, the news, the
  power-on chime), Noise, Voices, Wi-Fi (join, forget, its own
  network), This radio (name, case, password, listening in a browser),
  Buttons (and the day and night sets), Updates, Save & load, Power.
  **There's always a theme called Default** (`profiles.DEFAULT`; it can't be
  renamed or deleted): all my music (`"all": true`) unless given artists,
  first in Themes. The DJ calls it "Default Radio", a station like any
  other (`profiles.station_name`). Its jingles are the start-up ones, and
  fill gaps, when the theme playing has none of its own. Going "back" (after a station, an album,
  artist radio, a programme ending) is the theme last played, else Default
  (`Station._fallback_theme`). `main.ensure_default_theme` makes it (the
  "Sleep Radio" theme of all my music made earlier became it, jingles folder,
  buttons and programmes too) and gives it any message with no theme.
  **Messages belong to a theme**, only read while it plays.
  **A theme is a folder of its own things** (the desktop's **Themes**
  folder holds them; the radio's back has Themes too): it opens like any
  folder — its name (renaming it takes everything of its own along), ▶ Play,
  and **Artists** (shortcuts: drag an artist's folder, an album or a song in
  from Music, onto the theme or its Artists; to the Trash: off the theme; or
  all my music), **Jingles** (its own; upload or drop files in), **Messages**
  (its own; New message there, or drag a note in from another theme's) and
  **Settings**:
  how much the DJ talks, time checks, the news, how often jingles play and
  70s hooks — **each theme its own full set**, no master settings
  (`profiles.THEME_SETTINGS`, kept in the theme as `"settings"`; a new theme
  starts with Default's, `Station.complete_settings`; the phone's DJ page changes the playing theme's). The DJ window
  on the radio's back keeps the whole radio's: DJ on/off (buttons and
  programmes switch it), the voice, speaking and news speeds, the chime.
  **Renaming a theme** (`POST /api/profiles/rename {"old", "new"}`) takes its
  things with it: its jingles folder, its messages, buttons and programme
  blocks that play it, and the station if it's playing.
  **Messages** are objects: the desktop's sticky note opens a folder of notes
  (all of them), each with its own window — the words, **which station**
  (every station, or one theme's only; a theme's Messages folder shows its
  own, and its New message makes one for it), and **when it's said**: with the
  others in turn (every N minutes in the day's hours), or **at set times**;
  **on chosen days**; **once a year on a date** (e.g. Happy Christmas on 25
  December); **until** a date; paused. Hear it now, delete (or drag it to the
  Trash), drag it into a programme, or drop it on the radio to hear it. *When
  they're read…* holds the shared settings. **Birthdays** are objects too: the
  calendar opens a folder of people, soonest first (today's glows), each with
  a window (name, day, month, year for their age, hear the wish); New
  birthday, the Trash, or drop one on the radio to hear the wish. **Themes**
  (sets of artists, once called artist lists) each open their own window (rename, add and take off artists, play,
  delete). **Voices** is a folder: double-click one to make it the DJ's (the
  radio restarts), drop a voice archive on it to install it. A programme's
  window has **▶ Play now** and **Arm**: armed, it starts by itself at the time
  beside it — **once** (today, or tomorrow if that time's gone; it disarms
  itself after), unless its repeat says **every day**, **weekdays**,
  **weekends** or **chosen days**. A line says what will happen ("Armed:
  starts today at 10:00 · in 20 min", "Playing since 15:06"), kept up to date;
  while it plays, the blocks show their real times. Several programmes can be
  armed (e.g. 10:00–11:00 and 21:00–23:00); arming one that **overlaps**
  another asks first, and if two do overlap the **later start takes over**.
  The **Schedule** window (menu bar) shows the next 7 days of armed programmes
  on 24-hour lines (overlaps outlined red, chained ones after them); the menu
  bar says what's next or playing, and programme icons show ⏰ armed once, ↻
  repeats, ● playing. A chained programme whose first block is *at* a later
  time waits (with its gaps' choice: silence, the show, something) until then.
  **Programme mode** (a switch on the radio and in the Schedule window;
  setting `programme_mode`) makes it an alarm clock: quiet unless a programme
  is on — silent between programmes and at power-on, armed programmes wake
  it, gaps are silent, and when a programme ends (unless it chains on or
  repeats) it goes quiet again. The knob or Play still plays as usual.
  Each programme can **wake the radio if it's paused** (🔔, the default: an
  alarm) or not (🔕: if you've paused the radio when it's due, the pause wins
  and that start is skipped). Pausing during a programme keeps it paused for
  the rest of that programme; the next armed 🔔 programme wakes it. A programme can also repeat **every 15 minutes, 30
  minutes or hour**, from its time until a last time each day. The **pips**
  (five short and a long one on the minute) and **the time, spoken** are items
  too — made a minute ahead (the voice is slow on a Zero), then played on the
  dot — so a programme of just those, repeating every 15 minutes from 08:00 to
  21:00, is a talking clock. A programme of only moments plays over whatever's
  on (it never stops another programme, and can't clash with one); its
  moments all happen at each of its starts, and the timeline shows them there.
  Anything
  a button can do goes in a programme too. **Noise** and **the DJ** go in as
  **switches**: dropped on a programme, each is a bar in its own lane under the
  running order (dropped on a block, it covers that block). Drag the bar to
  move it, or drag either end to stretch it. It switches **on at the bar's
  start and off at its end**, whatever it was before, beside the blocks rather
  than taking a turn (the noise can run over two albums). A programme of only
  switches (noise 22:00-07:00) plays over whatever's on, and **starts when its
  first bar does** (move the bar and the programme's time follows; its window
  says "Armed: noise on 22:00 → 07:00 · today only", fades the *then* and
  *gaps* choices it doesn't use, and it doesn't end programme mode's quiet). In
  a programme with blocks a bar can't start before the programme does. A
  dashed amber line marks **midnight** on the timeline, with the next day's
  name. A bar that has
  started runs to its end even if something else is chosen; one not yet
  started is dropped. *■ Stop* switches them off at once. An open desktop
  page reloads itself when the radio has been updated with a new one. (Older programmes'
  *Noise on*/*DJ off* moments still work as before.) A sleep timer already
  counting down is left alone, and a programme dropped onto another plays
  next (its *then*). Stations and podcasts are found
  and followed from their windows (*Find a station*, *Follow a podcast*) and
  forgotten by dragging to the Trash. **Jingles** is a folder of its own
  (upload, download, delete) and **Artist lists** an editor (make a list with
  its first artist, add and take off artists, play it, delete it). Open windows ask
  the radio again every 20 s (and when you come back to the tab or close a
  setting), so changes made elsewhere show up. Your icon layout and light or
  dark are kept in the browser. **The media manager is built in**: in Music,
  On demand and Audiobooks windows, *Upload…* / *Upload a folder…* or drop
  files and whole folders from your computer (one at a time, with progress);
  *New folder*; *Download* (a file, or a folder as a zip); **drag a folder,
  album or song onto another folder to move it** (Music → On demand takes it
  out of the show) — playlists, programmes and buttons that pointed at it
  follow it; drag to the Trash to delete for good (asks first). Dropping music
  into a programme asks the radio for its **real running time** (read from the
  files, `POST /api/lengths`), so the timeline is right.
- **Programmes** — a running order the radio plays **by itself**, with no
  browser open (`broadcast/programmes.py`, a `Scheduler` ticking once a
  second). Each block holds things (albums, tracks, folders, playlists,
  stations, audiobooks, podcast episodes, the show or an artist list) and a
  rule: **for** so many minutes, **until** a clock time, **until it ends**, or
  **at** a clock time sharp (the block before plays on until then and one still
  playing is cut off: the news at 13:00; too early, the show fills in). In
  order or shuffled. Blocks can also be **moments**: a message the DJ says, a
  jingle, or an action (a time check, the news now, a sleep time), done at
  their time over what's playing, then on. **In the gaps** (before an "at"
  block, or an empty block): the show, silence, or something you choose.
  After the last block: back to the show, stop, fade out and pause, start
  again, leave what's playing, or **play another programme** (chained). Or a programme can be one that **starts when you start it** (the window's *starts at a time / starts when I start it*): a routine for a button, with no time of day -- its times are counted from the moment it's started (+0:30 is half an hour in; a block "at" or "until" a time means that long after the start), and it never starts by itself. A programme can **start by itself** at its start time
  on chosen days (and plays even if the radio was paused). Blocks use what the
  radio already does, so the DJ follows its setting: music goes through the
  show's queue (the DJ as set); a station, book, episode or a single On demand
  folder plays alone, no DJ. Choosing something else stops the programme. A
  preset button can hold one; *Coming up* shows it with **Stop programme**.
  Made and edited on the new desktop page (coming: see the roadmap), or
  through `POST /api/programmes`.
- **Browse** (Find → *Albums and folders*, or *On demand*) — everything
  playable without typing: **Albums A–Z** (named from the tags) or
  **Folders**, the libraries exactly as they are on the radio, with a
  breadcrumb to step back out — for anything untagged or laid out its own
  way. A folder with tracks has *Play now* / *Play next*; a folder of
  folders (a box set's CDs, a series' episodes) has **Play all** / *Play all
  next*, everything under it in path order. **Tap an album or folder's name**
  (in Browse, A–Z or the search) to see its tracks, each with **Play** (just
  that track now: from the music, it cuts into the show and the show carries
  on, the DJ as set; from On demand, on its own with no DJ, then the show),
  **From here** (the album from that track to the end, no DJ) and **＋** (into
  a playlist) (`Station.browse`, `Station.play_track`).
- **On demand** (`/media/ondemand`) — a second library for anything to pick
  and play yourself but **never in the show**: storms, old radio shows, long
  classical pieces, your own recordings. Any layout: each folder is shown by
  its own name (the tags are used only where they give an album name). It's
  not in the show's mix, artist radio, lists or the song search; only in
  Browse and the album search. **Anything in it can remember its place, like
  an audiobook** (the desktop's *Remember its place*, on a file or a folder in
  its window; off as it comes, so a piece starts from the beginning each time):
  it's then played exactly as a book is -- from where it was left, a minute
  back after the sleep timer, the knob seeking by the minute, the radio
  pausing at its end -- a folder as one book, its files the chapters in order
  (`KeptLibrary` in `playback/audiobooks.py`; the list is the `ondemand_keep`
  setting, the places are kept with the books'). A button holding it carries
  on from its place too.
- **Tags** — the names inside the files can be changed from the desktop page
  (*Info…* in a Music or On demand window): a track's title, artist, album and
  number, or the artist and album of every track in a folder at once, and the
  folder's own name. Useful where they're long, wrong, or in letters the DJ
  can't say. The file is copied, the tags written to the copy and the copy
  swapped in, so a power cut can't leave half a song; mp3, m4a, m4b, flac, ogg
  and opus (a wav has none). What a file's tags were before the first change is
  kept on the radio (`original-tags.json`), and *Put it back as it came*
  restores them. Buttons, playlists and programmes point at files, so they're
  untouched; their labels show the new names. **Themes choose songs by the
  artist's name**, so when a song in the music gets a new artist, every theme
  that played the old name plays the new one too, and the old name leaves the
  themes once no song has it (`Station.follow_artist`, `retire_artists`).
  On demand **always plays with no DJ**: *Play now*
  starts it, and *Play next* waits for the song (or album) playing to end,
  then plays it straight through (`Station.play_next`); with a station, book
  or podcast on, or the radio off, it's simply chosen. A preset button can
  hold one (`root`, `deep` in the preset). Fill it under Settings → Media
  library → *On demand*.
- **Media library** (Settings → Media library) — browse the music, jingles,
  audiobooks and On demand folders on the radio, **add files or a whole folder**
  (e.g. an album, keeping its Disc 1 / Disc 2 folders and cover pictures),
  make folders, and **delete** files or folders (with a confirmation; no
  undo). Uploads go one file at a time with a progress bar, to a hidden
  `.part` file then renamed into place, so a dropped connection never leaves
  half a song; a file that's already there is skipped, and a different one of
  the same name is kept as "… (2)". Up to 1 GB a file, keeping 200 MB free.
  `/media` stays read-only (power-cut safe) except while you're changing
  things: the station asks the root helper `media-rw-watch` for it (a lease
  in `/run/sleepradiopi/media-rw`) and it goes read-only again ~2 minutes
  after the last change, or at once when you leave the page. The library is
  then rescanned while it plays (only new files' tags are read); songs lined
  up whose files were deleted are dropped (`media.py`,
  `Station.reload_library`).
- **Buttons** (the Radio tab; *Change what the buttons play*, or Settings →
  The radio's buttons, opens a panel to change them) — the four preset
  buttons on the case (`io/presets.py`), like a car radio's. A press plays
  what the button holds: the show (all artists, an artist or a list), an
  internet station, an album straight through, or **one track on its own**
  (a long piece from On demand, say: drag the track onto the button; when it
  ends the radio pauses). Or an action: *Say the
  time*, *The news now* (the latest top stories, made on the spot — about a
  minute on a Zero), a *sleep time* (5 to 90 minutes, each its own thing: again to cancel,
  another time's button switches to that time, and once the timer has run out
  and paused the radio a press plays it again and starts the countdown afresh:
  dozed off, woke up, another go) or *Say the
  address*. A button only ever plays: **pausing is the knob's job**, and a
  long press is just a press (buttons are set from the page, so one held too
  long can't wipe what's on it). Wired as GPIO keys (KEY_1–KEY_4,
  config.txt), read alongside the knob.
  **Steps**: a button can hold up to six things (`MAX_STEPS`), stepped
  through by pressing it again, round and round — stations on one button,
  podcasts on the next, audiobooks on a third. A button with steps waits
  `STEP_SETTLE_S` (0.8 s) after its last press before playing, so stepping
  past a book or a podcast doesn't tune it or move its place; then it says
  the step's name, or gives one pip for step one, two for step two...
  **The names are made ahead of time** (`io/button_names.py`): when the
  buttons change, the names of steps on buttons with more than one are made
  in the background, one at a time once the show is on air, and kept as
  small raw files in `~/.cache/sleepradiopi/buttons` (named by a hash of
  the voice, its speed and volume, and the words) — so a press only plays a
  file, with no speech to make and no wait. A name not made yet (or made
  for another voice: they're made again) gives the pips that once; with the
  DJ off it's always pips. Files for names no longer on a button are
  deleted. `GET /api/buttons` has `named` on each such step.
  The next press means the step after the one that's playing; coming back
  to a button from something else, the step it was last on (remembered until
  a restart). An action in the steps is done and stepped past: within 30 s
  (`STEP_RESET_S`) the next press goes on to the one after it. A second
  press within 0.15 s is a switch bounce and ignored (`PRESS_GUARD_S`).
  On the desktop there's a **Buttons folder** with **a folder for each
  button**, four for the day set and four for the night set (blue): dropping
  something on a button — on the radio's front, on its folder, or into its
  open window — adds it as the next step (dropped on a row: at that place).
  Open one (double-click its folder, or click the name under its key on the
  radio; *What the buttons play…* opens the Buttons folder) to see what's on it: drag a row up or down to change the order, to
  the Trash to take it off (with Undo), or onto another button to put it
  there too; *▶ Press it*, a ▶ on each row to play that step now, and *＋
  What's playing*. Dragged below the last row, a step goes to the end; **a button's folder
  dragged onto another button swaps what they hold**. A button's folder dragged to the Trash empties it.
  **Noise** is a folder too: each colour is a thing (`{"kind": "noise",
  "colour"}`, an instant like the actions) — double-click it to switch that
  colour on over what's playing (again: off), or drag it onto a button; on a
  button, a press switches that colour on, and off if it's the one that's on.
  Two more folders hold one thing each (`SLOTS` in `io/presets.py`, saved
  as the settings `knob_long` and `power_on`): **Hold the knob** — what a
  3-second hold of the knob does (empty: it says the radio's address, which
  it always does while the radio is its own hotspot, so it can still be
  found) — and **When switched on** — what the radio plays when it's
  powered (something that plays, not an action; empty: what was on last;
  not used in programme mode).
  **Backups** is a folder of saved settings kept on the radio itself
  (`config/backup.py` `Store`: `backups/<name>.json` beside `config.json`,
  up to 30): *Save the settings now* (with a name, or the date and time),
  *Upload…* a settings file from the PC, *Download* one; drag one onto the
  radio (or double-click it) to load it — the settings as they were are
  kept first as *Before the last load*, so a load can be undone — or to the
  Trash to delete it. **Voices** is on the desktop too: a voice dragged
  onto **The DJ** becomes his voice (the radio restarts: only one voice
  fits in a Zero's memory, which is also why the news can't have a voice
  of its own there).
  **Speaker sounds** keeps speaker set-ups as things (the setting
  `speaker_sounds`: `[{"name", "eq", "highpass", "mono"}]`, up to 20): *Save
  the sound as it is now* under a name; drag one onto the radio's speaker
  grilles (or double-click it) and it's heard at once; to the Trash to
  delete it (with Undo). The sliders stay in the Speakers window.
  **Wi-Fi networks** shows the networks the radio knows (saved ones, and
  the ones put on the card): drag a saved one to the Trash to forget it;
  joining a new one still needs its password, in the Wi-Fi window.
  **Night buttons in a programme**: the switches' lane takes a third kind,
  `"night"` (drag *Day / night buttons* from Actions onto a programme): the
  night set of buttons is in use for the bar's stretch, then the day set,
  swapped quietly. The *Day / night buttons* action can go on a button too
  (it swaps the sets, with the three notes). The buttons' own timetable
  (*Swap by the clock*) is still there.
  The ones that set the radio up live in one **Radio** folder on the desktop
  (Buttons, Hold the knob, When switched on, Speaker sounds, Voices, Wi-Fi
  networks, Backups); what can go on a button is in **Actions**, with the
  **Sleep timer** and **Noise** folders (the times, and Noise on/off with the
  colours) inside it. So the desktop itself has the media folders, Actions and
  Radio; any of them dragged onto the desktop leaves a shortcut there. (The Buttons window
  on the radio's back keeps only the day and night sets and their
  timetable. The phone remote has none of this: it's a remote control.) The key on the radio's front shows a small
  counter (3, or 2/3 while step two plays). With one thing on a button
  nothing changes: no wait, no pips, saved as before. The cathedral's
  selector can't be pressed again, so it plays a position's first step.
- **Noise** (the Radio tab's Noise toggle, Settings → Noise, or a preset button
  set to *Noise on/off*) — coloured noise to sleep to, as in the SleepRadio app:
  White, Pink, Brown, Deep brown, Blue, Violet, or Ambient (pink with a slow
  swell), each at the music's loudness (`audio/noise.py`: FFT-shaped blocks,
  overlapped so it never repeats, fading in when switched on). It plays **on top
  of whatever's on** -- the show, a station, an album or a book -- set against it
  by a **balance** (the middle = both at full level; towards one side turns the
  other down). The **sleep timer fades and pauses the programme, not the
  noise**, and a knob pause only pauses the programme: the noise stays on until
  it's switched off (a small pump thread feeds the speaker while the programme
  is paused). Only on the radio's own speakers (not the browser stream).
  Remembered over a restart (`noise_on`, `noise_kind`, `noise_mix`).
- **Audiobooks** (Find, and the Radio tab while one plays) — books go in their
  own folder, next to the music (`/media/audiobooks` on the radio; upload them
  in the desktop's Audiobooks window: *Upload a book…* for a file, *Upload a folder of
  chapters…* for a book in chapters, or drop either on the window): a folder of mp3s or m4as is one book
  (its chapters in file order, CD1/CD2 sub-folders included), and a single
  `.m4b`, `.m4a` or `.mp3` is a book too, an m4b's or m4a's chapter markers becoming its chapters.
  A book plays **instead of the show, with nothing from the DJ** (no links,
  jingles or news), and **every book remembers its place**: saved every 30 s
  and on pause, the sleep timer, switching to something else, restarts and
  power cuts (`~/.local/state/sleepradiopi/book-positions.json`). While one
  plays, the dial window shows the book and chapter, a bar for the whole book
  (tap it to jump), the time gone and left, and **⏪ 1 min · ⏯ · 1 min ⏩**. A
  pause picks up a few seconds back; **after the sleep timer, a minute back**
  (you'd dozed off during the fade). At the end of a book the radio **pauses**
  rather than going back to the show, and next time the book starts again.
  Find shows your books (the most recently heard first, with how far through
  each is) and searches them; a preset button can hold one (one press
  carries on). Chapter lists are cached (`~/.cache/sleepradiopi/books.json`):
  reading a long m4b takes a Zero a while the first time
  (`playback/audiobooks.py`, `Station._run_book`).
- **Podcasts** (Find → Podcasts) — search the iTunes podcast directory (as the
  Android app does) or paste a feed's address, and **follow** a show (up to 50,
  kept in the settings as `podcasts`). Each show's episode list (the newest
  200) is fetched when you look and every 3 hours, and saved on the radio
  (`~/.cache/sleepradiopi/podcasts/`), so it's there offline. An episode plays
  **like an audiobook**: instead of the show, nothing from the DJ, its place
  remembered in the same file (a minute back after the sleep timer), with the
  same bar and **1 min** back / forward; within a minute of the end counts as
  heard, and **Heard / Unheard** marks one by hand. Episodes stream (https via
  Python, like the stations); starting part-way through an mp3 asks the server
  for just the rest (an HTTP Range), and a dropped connection picks up where it
  was. **A run on a button**: *On button…* beside any episode puts the show on
  a preset button **starting from that episode** — the button plays the first
  not yet heard from there on, oldest to newest, and when one finishes the
  radio goes straight on to the next newer episode, up to the latest, then
  pauses (as at the end of a book). A button holding just the show (Settings →
  Buttons) plays the episode part-heard, else the newest unheard; an
  episode dragged onto a button (or *＋ What's playing* while one plays)
  starts the run from that one; and
  playing any episode from the list carries on through the newer ones too
  (`playback/podcasts.py`, `Station._run_episode`).
- **Albums** (Find) — search the library's albums and play one start
  to finish **instead of the show**: no DJ, jingles or news; Skip goes to the
  next track; then back to Sleep Radio. The radio remembers the album and
  track over a restart. (Play next on the main page queues an album *in* the
  show, with the DJ's introduction.)
- **Internet radio** (Find, and Settings → Internet stations) — *My stations* (starting with the
  Android app's set: BBC Radio 4 and World Service, Radio Paradise, SomaFM)
  with ▶ Play, remove, and add one by its stream address; *Find stations*
  searches the [Radio Browser](https://www.radio-browser.info/) directory by
  name or tag (most listened-to first, as the Android app does) to play or
  save. Its servers often time out, so the radio **keeps its own copy of the
  directory** (~50,000 working stations, one per stream, ~6 MB in `~/.cache/sleepradiopi/`),
  made in the background two minutes after start-up and refreshed weekly
  (*Update the list* does it now), trying each mirror in turn; searches use
  the copy (instant, and they work when the servers don't), or go online
  until there is one.
  A station plays **instead of the show**, through the same speakers, EQ,
  volume, knob and sleep timer (and the browser stream); the main page then
  shows the station and what it's playing (Icecast/Shoutcast titles), with
  **Back to Sleep Radio**. No DJ over it — no links, time checks or news —
  and no Skip; songs picked in *Play next* wait for the show. Stations are
  levelled gently towards the show's loudness. It needs the radio online: if
  a station stays silent for 45 seconds (no Wi-Fi, or the station's gone)
  the show comes back on and the page says why; a dropped stream reconnects
  by itself. An HLS segment whose download drops is fetched again (the BBC's
  https servers drop about one connection in ten), and one that still won't
  come is skipped, so a blip never becomes a reconnect. The radio remembers the station over a restart. Plays plain
  Icecast/Shoutcast streams (MP3, AAC, Ogg), .pls/.m3u playlists and HLS
  (.m3u8, e.g. the BBC); Python fetches them (http or https) and pipes them
  to ffmpeg, which decodes them (`sleepradiopi/playback/radio.py`).
- **Artist radio is retired** (2026-10-01): themes do the same job. An
  artist is a **one-artist theme** ("The Beatles" -> a theme "Beatles",
  announced "welcome to Beatles Radio"); anything saved as artist radio
  before -- the radio left on it, a button, a programme block -- becomes one
  at start-up (`main.retire_artist_radio`, `Station.theme_for_artist`), and
  `POST /api/station {"artist": ...}` is refused. Choosing another theme
  takes over at once, like turning the dial. From the desktop, an artist's
  folder dropped on the radio plays their songs shuffled, once each, no DJ,
  then the radio pauses.
- **The DJ** — **on or off** (off: the show is music only, with no speech at
  all — no welcome, links, track intros, time checks, news, Messages or
  birthday wishes, including lines already made before it was switched off;
  the buttons' confirmations become beeps -- holding one to save what's playing
  gives four quick high notes -- while "Say the time", the service menu and the
  spoken address still talk; jingles keep their own switch; a preset button
  can toggle it too: "DJ on/off"),
  the voice (your voice packs; changing it restarts the radio,
  about a minute, because only one voice fits in a Zero 2 W's memory), how
  often it talks (before every song, or every 2, 3 or 5), 70s hooks on/off,
  jingles (off or every 2–8 songs), the **DJ speed** and **news speed**
  (sliders, 0.5–1.5×; a line or two may already be made at the old speed),
  news on/off, and the start-up sound (chime or silent). All but the voice
  are heard from the next gap.
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
- **Messages** — short notes the DJ reads between songs, each a theme's own
  (`"station": "<theme>"`, read only while it plays; none: Default's;
  `Messages.due(at, clock_ok, station)`), e.g. for someone with a poor memory:
  "You're at Willow Court, and you're safe here. Dylan lives five minutes
  away and is coming to see you on Sunday." **Each message has its own
  timing**: every 15, 20, 30 or 60 minutes or 2 hours, **7 past** (:07, :22,
  :37, :52 for 15 — well clear of the news at :00 and :30), between its own
  hours (08:00–21:00 to start with), or at set times instead; when two are
  due in one gap, one is read (a timed one first, else the one said longest
  ago) and the other waits for the next gap. A message is read first thing in
  the first gap between songs from its time on (worded 45 s before the song
  ends, for the gap's real time); if the news is due in that gap, it waits
  for the next one. With its **Day and date first** on, it opens "It's Tuesday,
  the twenty-ninth of September." Each message can have a **last day**, and
  can be **paused** without deleting it; *Hear it* plays one now. Only on the
  show (not while a station, album, audiobook or podcast plays), only once
  the clock is known, and on every theme. Kept in the settings
  as `messages` (`broadcast/messages.py`).
- **Volume** — the same 0–100 scale as the knob, and it follows the knob.
  **Speakers: Stereo / Mono** switches at once. **Sleep timer** (5 min to
  1½ h; on the desktop, the Sleep timer folder in Actions) runs on the radio: it fades the speakers (and the page's own stream)
  over the last minute, then pauses; every open page shows the same
  countdown, and a pause cancels it.
- **Speaker EQ** — bass / mid / treble (±12 dB; shelves at 200 Hz and 6 kHz,
  a peak at 1 kHz) and a **Low cut** (Off / 100–160 Hz, 24 dB/octave) that
  keeps the deepest bass out of small speakers (~140 Hz with the case's bass
  port). The MiniAmp has no EQ of its own, so it's done in software
  (`audio/eq.py`: one linear-phase FIR from the biquads, FFT per block, ~11%
  of one Zero 2 W core when not flat; changes are crossfaded, so no clicks).
  Boosts lower the overall level so they can't clip. The
  [spectrum analyser](https://dylan7474.github.io/SleepRadioPi-OS/) can suggest the settings (see *Tuning the
  speakers*, below).
- **Knob switch** — works like the real knob: tap to pause/play, hold 3 s to
  hear the address. If the radio was paused it speaks, then pauses again.
  **Open the service menu** does what holding buttons 1 and 4 for 5 s does;
  answer it with the buttons on the Radio tab. Those buttons behave like the
  real ones: each sends *down* and *up* to the radio (`/api/buttons/key`),
  through the same timers, so a tap plays (or steps on),
  and holding 1 and 4 together (two fingers) for 5 s opens the menu (the
  page counts down from 5 under the buttons, and the radio ticks once a
  second while they're held -- the same with the real buttons).
- **The service menu** (`io/service.py`; buttons 1 and 4 held together for
  5 s — the moment the second goes down, neither button's own press
  counts, so nothing plays; `io/knob.py` `Chord`).
  While it's open the show is **paused** and the speaker plays only the
  menu: its words, and a soft tick once a second while they're being made;
  leaving it (or cancelling, or the Wi-Fi reset) resumes what was playing;
  restarting, going back and the factory reset say a short line ("Rebooting.")
  and only then act, so no music comes back in between.
  Its fixed lines are made once per voice in the background after start-up
  and kept (`~/.cache/sleepradiopi/service-menu/`), so they play at once;
  only the status report is made fresh (while the buttons are held). A line
  the DJ hasn't made yet -- a new voice still making them, or a radio with no
  DJ's voice at all -- is said at once in the plain voice (eSpeak,
  `tts/plain.py`) instead, so the menu always talks.
  While it's open the four buttons answer it. **1**: restart (the Pi).
  **2**: reset the Wi-Fi — delete `/data/radio/wifi.json` (the networks
  added on the page, and a renamed hotspot) and ask the Wi-Fi manager for
  the hotspot, which then announces how to join it; the network on the card
  (`/boot/wpa_supplicant.conf`) stays, so with nobody on the hotspot the
  radio tries that again every 5 minutes. **3**: a status report (network,
  address, version, songs, free space), then **3** again goes back to the
  previous version: the root helper (`power-request-watch` →
  `updater.py rollback`) checks the other root slot holds a system
  (squashfs), points `cmdline.txt` at it and reboots; the old version then
  says it's back (or the DJ says there's nothing to go back to).
  **4**: a factory reset, confirmed with **2** then **3**
  (`config/reset.py`): the settings file keeps only what belongs to the
  radio (`backup.LOCAL`: its folders, speaker, pins, update source) plus the
  voice in use and the speaker tuning (mono, EQ, low cut); everything else
  goes, the web password included, with the page's Wi-Fi networks and the
  saved volume; music, audiobooks (and their places), voices and caches
  stay. It asks for the hotspot and restarts the station, which then says
  how to join it. Anything else, or 45 s of nothing after the DJ stops
  talking, closes it. The menu's words and the status report are made
  while the buttons are being held, so a press answers at once.
- **Test sound** — for comparing speaker cabinets and checking wiring, on the
  speakers instead of the show for a moment, with the EQ and low cut
  bypassed: a **bass sweep** (40–600 Hz) and a **full sweep** (40 Hz–16 kHz),
  24 s each with the frequency shown live, **pink noise** (for the
  spectrum analyser, or a phone app), noise on the **left** or **right** speaker only,
  **left, then right** (the DJ says "Left speaker" with noise on that side
  only, then the same on the right, twice; noise alone if no voice is
  ready), **tune** (quiet then hiss, three times, for *Tune speakers…*;
  see below), and a **phase check** (every 3 s: both speakers the same, then the right
  one inverted — if the inverted part sounds fuller, a speaker is wired the
  wrong way round, and vocals would vanish in stereo but not in mono).
- **Receivers** — a folder that appears on the desktop once a saved
  station's address is a receiver's. Each receiver in it is an object of
  its own -- one of your own (`receiver/` in the repository: an RTL-SDR
  dongle on a spare Pi, `http://HOST:8074/audio?band=…`) or someone's
  **KiwiSDR** or **OpenWebRX** on the internet (a station like
  `http://HOST:8073/?f=7150.00lsb`; *Find a receiver* has a *Rig* button on
  each one too) -- and each opens
  as a **rig**, laid out like the screen of a radio amateur's transceiver:
  the frequency, an S meter, and the band's spectrum over a waterfall. Band
  keys down the left, modes down the right; *click the scope* to tune; click
  the frequency to type one, or **drag the frequency onto a radio button** to
  keep where it's tuned there (onto the radio plays it; onto the desktop
  leaves a shortcut). It's dragged as a station: a channel of your own
  receiver's band is that band held on the channel.
  - *A receiver of your own* hears a whole band at once. Press a band and the
    radio tunes in (allow ten seconds). A click on the scope is instant and
    stays on that channel (*Hold*); *Scan* goes back to whoever speaks, ◀ ▶
    step a channel, *Skip* leaves the channel out of the band. Modes: FM, AM
    or broadcast WFM for a typed frequency. *Channels…* opens the band as a
    folder of stations to drag onto buttons (a channel as a station is its
    band held on that channel: the rig looks the same as after a click on
    the scope, and *Scan* lets go); *Bands…* opens its bands as
    folders (*◂ Rig* comes back).
  - *A KiwiSDR* is short wave, one frequency at a time. The radio holds the
    one connection to it -- the sound and the waterfall -- so it takes one
    listener's place, and the rig tunes it where it is: band keys for the
    amateur bands (160 m to 10 m) and the broadcast ones, LSB / USB / AM / CW /
    FM, a click on the scope, ◀ ▶ and the mouse wheel in steps of 10 Hz to
    9 kHz, *Zoom − / +*, and *Save* to keep the frequency as a station. The
    meter is the receiver's own reading in dBm. Some KiwiSDRs have fewer
    waterfalls than listeners: then the scope says so and the sound carries
    on. `GET /api/rx?since=N` and `POST /api/rx/tune {"base", "freq", "mode",
    "zoom", "centre", "profile"}` are what the page uses.
  - *An OpenWebRX* (a station like `http://HOST:8073/#freq=145500000,mod=nfm`)
    works the same way, through the radio's one connection. Its band keys are
    whatever bands its owner has set up; **changing band moves everyone who
    is listening to that receiver**, so the rig asks first. Within a band:
    FM / AM / USB / LSB / CW, a click on the scope, ◀ ▶ and the wheel, a typed
    frequency (MHz). Narrow FM gets an automatic squelch, worked out again
    after each retune (a couple of seconds of quiet); the other modes are
    left open. Its waterfall is the whole band -- often several MHz -- so
    *Zoom +* looks closer at the part round where you're tuned (on the page:
    nothing is asked of the receiver). The radio passes the receiver's
    waterfall lines on as they come (compressed); the page works them out.
  - *A room* (radio amateurs' digital voice: see *Find a room*) is in the
    Receivers folder too, once it's a saved station, and opens as the same
    rig with nothing to tune: the screen shows who is talking, the last
    heard with how long each spoke and how long ago, and how many are
    connected. The keys down the side are your saved rooms; *Listen* joins it
    on the radio; the room's name drags onto a button. **Monitor** hears
    the room over whatever is playing instead: the radio carries on with its
    music, station or book, and when someone speaks in the room the
    programme dips (to about a quarter) and they come through, then it comes
    back a second or so after the last word. Up to four rooms can be
    monitored at once (the first to speak has the air); they're kept, and
    joined again after a restart. A paused radio stays silent. While
    someone is talking over the programme, the radio's display (and the
    phone remote's) says who and in which room (`"over"` in `/api/status`).
    `POST /api/rooms/monitor {"url", "name", "on"}`; `playback/monitor.py`.
  A receiver of your own's *Bands…* folder holds its bands, each a folder: the whole band at once, then its channels one by one,
  every one a station -- double-click to listen, drag onto a button to keep
  it there. While its window is open the page asks the receiver itself (not
  the radio) every few seconds who has the air: "On air now", "Heard 3 min
  ago". *Tune…* listens to any frequency (narrow FM, AM or broadcast FM); *Skip it*
  leaves the channel you clicked out of its band (one that's always open).
  *New band…* makes a band of your own on the receiver: a name, where it
  starts and ends (MHz) and how far apart its channels are -- PMR 446, the
  local airband, 70 cm repeaters -- up to 64 channels within the 1.9 MHz a
  dongle hears at once; it's then a folder like the others, and *Remove this
  band* in its window takes it away again (the built-in two stay).
- **Find a room** — in the Stations and Receivers folders' toolbars: radio
  amateurs' digital voice rooms (Yaesu System Fusion reflectors: CQ-UK,
  America Link, ...), from the public register Pi-Star also uses (fetched
  once a day and kept). First *your callsign*: a room shows who is
  connected, so the radio joins under yours, and won't join without one.
  Then search by name, country or number, and *Listen* or *Save as a
  station*. Rooms on the FCS network are found too (it's where most of
  Yaesu's WIRES-X rooms are bridged: "CQ-UK-WiresX", "America-Link-WiresX").
  A room is a station with the address `ysf://HOST:PORT/NAME`, or
  `fcs://FCS00290/NAME` for an FCS one: it
  plays whoever speaks, with their callsign as what's playing, and silence
  in between (rooms are quiet most of the time), and it only ever listens.
  The window also says whether the *voice decoder* is on the radio: it
  isn't part of the software (the top README's "Rooms and the voice
  decoder" says why, and how to add it); without it a room shows who's
  talking but makes no sound. A key held with nobody speaking is left as
  silence, not hiss. *Room level* sets how loud rooms are beside everything
  else. A room isn't levelled like music and stations are (speech at the
  same average level as music sounds far louder, and levelling turned it up
  until it clipped), and it isn't played just as it's decoded either
  (talkers arrive at very different levels: one a little under the music,
  the next a seventh of it). Each talker is brought to the same place
  instead: the level of the loud parts of their voice is followed (up
  quickly, down slowly, the gaps between words left alone) and put where
  a voice sounds as loud as the music on the radio's own speaker (set by
  ear: a narrow radio voice on small speakers needs a good deal more level
  than the music does, and the loudest moments are squashed by the limiter
  rather than clipped). This setting is then on top of that, for every
  talker alike. A room's own window has *Voice −* and *Voice +* keys for
  it, five at a time.
  A talker who comes in over a DMR bridge arrives under their DMR ID (a
  seven-figure number) rather than a callsign: the radio looks the number up
  in radioid.net's register (Pi-Star's copy of it, fetched once a week in the
  background and kept as a small file: `playback/dmr_ids.py`) and shows the
  callsign; a number it doesn't have stays a number.
  *Noise reduction* for receivers (the **NR** key in any receiver's window:
  off, 1 light, 2 strong, 3 rig; `POST /api/rx/nr {"level"}`): the steady hiss under a
  voice is turned down -- 6 to 8 dB between words at light, 12 to 16 at
  strong, the voice itself all but untouched. It's done on what's about to
  be played, so it's the same for your own receiver, a KiwiSDR or an
  OpenWebRX, and for a receiver that's monitored. It's for speech in noise:
  a steady tone is taken for noise after a couple of seconds, and on a
  clean broadcast station there's nothing for it to take away but the
  programme's own quiet parts. *3, rig* is the chain a modern transceiver
  has, for a voice and nothing else: the passband narrowed to the voice with
  a little lift for clarity, an automatic notch for a whistle that stays
  put, and a noise reduction that first judges band by band whether speech
  is there at all -- as much hiss off as *strong*, with 1 to 2 dB more of
  the voice kept (measured with a clean voice in real band noise); about a
  sixth of one core on the Zero 2 W (`audio/denoise.py`).
  *Morse, read as text* (the **CW** key in a receiver's window;
  `POST /api/rx/morse {"on": true | false}`, off again at the next start):
  the radio listens to what a receiver is playing -- as it came, before the
  noise reduction -- for a keyed tone between 250 and 2800 Hz, and reads the
  strongest, an over at a time. Real traffic comes in overs: a three-second
  call, silence, then someone else at another speed and a slightly different
  pitch; so each over is read on its own, with its own pitch and speed, when
  it has been quiet for 1.3 s (a long one every six seconds or so, at a gap
  between words), and the text arrives an over at a time. The tone is moved
  to 0 Hz and low-passed to a width matched to the sender's speed (about
  30 Hz at 22 words a minute: a station ten times as strong 150 Hz away
  makes no difference, though its key clicks can add a stray dot while this
  one is silent); the envelope is then cut into dots, dashes and gaps by the
  sequence that fits it best under Morse's own timing (a Viterbi search over
  segments), the signal's level taken again from the marks just read so
  that fading is followed, and the speed (10 to 51 words a minute) is the
  one whose strict-timing reading beats "nothing was sent" by most. Each
  letter's confidence is how far its weakest mark stood above the noise. A
  reading that doesn't stand clear of the noise, whose timing isn't
  Morse's, or with too many runs of marks that are no letter (a
  teleprinter's tone looks keyed too) is left unsaid. Measured with known
  Morse mixed into real 30 m noise (strength against the noise in 2.5 kHz):
  clean down to -6 dB, a shaky fist about 3% of letters wrong, about 15%
  wrong at -9 dB, nothing said at -12 dB -- a good ear still goes a few dB
  lower. Weak spots: a weak signal that is also fading fast, two stations
  keying at once inside the one filter, and a shaky fist's short gap between
  words run together. `GET /api/rx/morse?since=N` gives whether it's on, the
  tone's pitch, the last eight seconds of envelope for the tape, and the
  overs read after number N (each with its letters, when each began and how
  sure it is, and its marks). The reading is done in a process of its own
  at low priority, on another core: tried as a thread of the radio's own
  process it took the processor from the sound, which stuttered on the
  Zero 2 W. There it takes about a quarter of one core in a pile-up, and
  next to nothing when all is quiet (`audio/morse.py`).
  A receiver of your own can be monitored too (the *Monitor* key in its
  window): what it's doing just then -- a band, a held channel or one
  frequency -- comes over the programme whenever a squelch opens, named by
  its channel, at the level rooms are. A transmission has to last 0.4 s to
  count (a click or a burst of noise doesn't dip the music), one receiver
  is monitored for one thing at a time, broadcast FM or a receiver with its
  squelch off can't be (they never go quiet), and while the radio is tuned
  to that receiver itself the monitor leaves it alone and comes back to it
  afterwards (`playback/monitor.py`, `ReceiverWatch`).
  *Music under a room* (0-100%, 10 to begin with) is how loud what's playing
  stays while a monitored room speaks: `POST /api/rooms/music {"level": 0-100}`,
  and the *Music −* / *Music +* keys in a room's window.
  `POST /api/rooms/level {"level": 25-150}`. `GET /api/rooms`, `/api/rooms/search?q=`,
  `POST /api/rooms/callsign {"callsign"}`; `playback/room.py`, `rooms_dir.py`.
- **Find a receiver** — in the Stations folder's toolbar (and the Receiver
  folder's): other people's receivers on the internet, from the public
  directory at receiverbook.de (fetched once a day and kept; OpenWebRX and
  KiwiSDR -- WebSDRs need a browser, so they're left out). Search by place,
  callsign or country. *Nearest to this* on any result makes that spot
  "here": from then on the nearest come first, with their distance (the
  page remembers it; the radio never needs to know where it is). *Tune…*
  asks the receiver about itself -- an OpenWebRX its bands, a KiwiSDR how
  many places are taken -- then a frequency and a mode, and *Listen* or
  *Save as a station*. `GET /api/receivers/search?q=&kind=owrx|kiwi&near=LAT,LON`
  and `/api/receivers/info?url=` (only for a receiver that's in the
  directory).
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
- **Restart** (beside Shut down, Settings → Restart or shut down) — for when something seems stuck: the radio goes quiet, restarts and is back in about a minute; the page waits and reloads itself when it answers again (the same root helper, `power-request-watch`, watching `/run/sleepradiopi/reboot`).
- **Shut down** — turns the radio off safely before unplugging (appliance
  image only: the station isn't root, so it creates the file named by
  `SLEEPRADIOPI_POWER_REQUEST`, which a root service watches).

### Tuning the speakers

The [spectrum analyser](https://dylan7474.github.io/SleepRadioPi-OS/) (source in
[`sleepradiopi/web/analyser/`](sleepradiopi/web/analyser/)) is a web page for a laptop or a
phone. It listens through the device's microphone and suggests the speaker EQ and low cut.

**The easy way:** on the radio's desktop (a phone can open it too), the
Speakers window → **Tune speakers…**:

**With no internet** (e.g. on the radio's own hotspot) the radio serves its
own copy of the analyser at `/analyser/`. Chrome only lets a page use the
microphone on a secure (https) address, so once per phone: open
`chrome://flags`, enable **Insecure origins treated as secure** and add the
radio's addresses (`http://sleepradiopi.local`, and `http://192.168.4.1` for the
hotspot), then relaunch Chrome. After that **Tune speakers…** opens the radio's
own copy (the flag makes the radio's page count as secure, which is how it
knows); without it, it opens the GitHub copy, which needs internet. The
radio's copy explains the flag, with its own address, if the mic is blocked.

1. It opens the analyser in a new tab and the radio plays its **tune** test
   sound: 5 s of quiet, then 10 s of hiss, three times.
2. Hold the phone about 50 cm in front of the radio and tap **Start**.
3. The analyser listens for a quiet stretch followed by the hiss starting,
   measures both, and shows its suggestion.
4. **Apply on the radio** opens the radio's desktop with it in the Speakers
   window, ready to **Apply** or **Undo** (the hiss stops).

Because it detects the hiss by ear, it doesn't matter when you tap Start.
If it starts listening in the middle of a burst, it waits for the next.

**By hand** (any test sound, any timing):

1. Put the laptop about 50 cm in front of the radio and press **Measure the
   quiet room**.
2. Press **Pink noise** on the radio's page (Settings → Test sounds and the knob; it
   bypasses the EQ and low cut, so it's the bare box), then **Measure the
   radio**.
3. It suggests bass / mid / treble and a low cut, and draws the curve it
   predicts with them.
4. **Send to radio** opens the radio's page (at the address you give it,
   `sleepradiopi.local` by default) with the suggestion in the address,
   `#tune=bass,mid,treble,lowcut`. The page shows it next to the current
   settings, and nothing changes until you press **Apply**. **Undo** puts
   the old ones back, so you can compare them by ear. Or **Download
   settings file** and load it with Settings → Save or load settings → Load settings…

How it decides:
- **Sound** picks what it aims for: *Warm, like a radio* (the default: up
  to +6 dB of bass below ~350 Hz and a little treble) or *Neutral*
  (flat). Flat sounds thin on small speakers.
- The low cut is where the box has fallen 10 dB below its mids. If the mic
  can't hear the radio that low over the room's noise, it can't tell, so
  it suggests the gentlest (100 Hz) and says so. Try 120 or 140 Hz by ear.
- The EQ is the whole-dB setting that gets closest to the target. It is
  worked out through the same filters the radio uses (`audio/eq.py`).
- Boosts cost more than cuts, so small speakers aren't pushed.
- Only frequencies at least 10 dB above the room's noise count, from
  150 Hz (or just above the low cut) to 12 kHz. The bass shelf is at 200 Hz
  (the box plays little below ~150 Hz), inside what the mic hears, so the
  suggestion covers bass too; the low cut is still worth checking by ear.

A laptop mic hears little below ~150 Hz. **Mic → Typical laptop mic** adds
back a rough guess at what it misses. A measurement mic's calibration file
(like a UMIK-1's) is exact.

The page is on GitHub Pages rather than the radio because browsers only let
an HTTPS page use the microphone, and the radio's page is plain HTTP. For
the same reason it can't send the settings to the radio itself. It opens
the radio's page with them instead, and you apply them there.

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
| `POST /api/speaker` | Any of `{"volume": 0-100}`, `{"step": n}`, `{"knob_mode": "auto" \| "fine" \| "normal" \| "coarse"}`, `{"pause": true \| false \| "toggle"}`, `{"sleep": minutes}` (0 = off), `{"mono": bool}`, `{"noise": true \| false \| "toggle", "noise_kind": "pink", "noise_mix": 0-100}`, `{"eq": {"bass": dB, ...}}`, `{"highpass": Hz}`, `{"test": "bass" \| "sweep" \| "pink" \| "left" \| "right" \| "sides" \| "phase" \| "tune" \| "stop"}` |
| `POST /api/stream` | `{"enabled": bool}` — listening in a browser on/off (saved as `web_stream`) |
| `POST /api/knob` | `{"press": "short" \| "long" \| "service"}` — the knob's switch; `service` opens the service menu (then `/api/buttons/press` answers it; `GET /api/buttons` has its `service` state); `{"press": "turn", "clicks": n}`: pressed and turned |
| `GET` / `POST /api/identity` | `{"hardware", "station_name"}` — which radio this is and its name; a POST saves and restarts the station |
| `GET` / `POST /api/lamps` | The cathedral's `{"glow_day", "glow_night", "meter_trim_db", "needle", "glow"}` (the last two: is the PWM there) / any of the first three, saved |
| `POST /api/lamps/sweep` | The needle up to full scale and back over 4 s, to set the meter's trimmer |
| `POST /api/skip` | Skip what's on air (track, link, jingle or bulletin) |
| `POST /api/seek` | `{"to_ms": n}` or `{"delta_ms": n}`: move through the song (Theme Radio's, an album's or playlist's, paused too), audiobook or podcast playing → `{"moved", "position"}`. The status's `position` (`pos_ms`, `total_ms`) is where it is, or null when there's nothing to move (a station, a jingle, the DJ) |
| `GET` / `POST /api/programmes` | `{"programmes": [...], "playing"}` / `{"programmes": [{"name", "start", "auto", "days": [0-6], "then": "show" \| "sleep" \| "repeat", "blocks": [{"name", "items": [{"kind": "album", "root", "folder"} \| {"kind": "track", "root", "path"} \| {"kind": "station", "name", "url"} \| {"kind": "playlist", "name"} \| {"kind": "book", "key"} \| {"kind": "podcast", "show"} \| {"kind": "episode", "show", "guid"} \| {"kind": "show", "artist"?} \| {"kind": "list", "name"}], "rule": "for" \| "until" \| "end" \| "at", "min", "at", "until", "order"}]}]}` — the whole list, saved. The status's `programme` has the one playing (`name`, `block`, `until`, `next`, `waiting`) |
| `POST /api/programmes/play` / `/stop` | `{"name"}` — start it now (and the speaker) / stop it (what's on carries on) |
| `POST /api/programmes/mode` | `{"on": bool}` — programme mode (quiet unless a programme is on); the status has `programme_mode` |
| `POST /api/previous` | Back, like a CD player (see *Now playing*); `{"previous": false}` if there's nothing to go back to. The status's `can_previous` says when it works |
| `GET` / `POST /api/playlists` | `{"playlists": [{"name", "tracks": [{"root", "path", "title", "artist", "missing"}]}], "playing"}` / `{"playlists": [{"name", "tracks": [[root, path], ...]}]}` — the whole list, saved |
| `POST /api/playlists/add` | `{"name"` + `"root", "path"` (a song; search results have `path`) or `"root", "folder", "deep"` (a folder) or `"now": true` (the song on air)`}` — added at the end; a new name makes the playlist |
| `POST /api/playlists/play` / `/stop` | `{"name", "shuffle": bool}` — now, straight through (no DJ), then the show / end it after the song playing |
| `POST /api/playlists/rename` | `{"old", "new"}` → `{"name", "playlists", "playing"}`; buttons and programmes that play it follow |
| `GET` / `POST /api/dj` | The DJ settings (and the voices there are) / any of `{"voice", "chattiness", "dj_hooks", "jingle_every", "news_enabled", "startup_sound", "dj_speed", "news_speed"}` |
| `GET /api/wifi` | Wi-Fi: mode (station / hotspot / connecting), network and address, saved networks, nearby ones (`signal` in dBm); `setup` while a join asked for from the hotspot is under way or has worked, `failed` (the network's name) when it didn't and the hotspot came back |
| `POST /api/wifi/add` / `remove` / `scan` / `hotspot` / `try` | `{"ssid", "password"}` / `{"ssid"}` / – / `{"ssid", "password"}` / – |
| `GET /api/voices` | The voices, the one in use, and any download/upload/install under way |
| `POST /api/voices/standard` | Download and install the standard voice |
| `POST /api/voices/upload?name=N&file=F` | The archive as the body (up to 400 MB); installed in the background |
| `GET /api/update` | The radio's version, any update found, and one under way |
| `POST /api/update/check` / `/api/update/install` | Look for a newer release / install it (the root helper does the work) |
| `GET /api/search?q=` | Up to 40 tracks and 20 albums matching every word, each with an `id` |
| `GET /api/albums` | Every album and folder with tracks: `id`, `root` (`music` \| `ondemand`), `folder`, `title`, `artist`, `tracks` |
| `GET /api/browse?root=&path=` | One folder: the `folders` in it holding audio (`name`, `tracks`, `folders`, `own`: tracks of its own) and its own `album`, if any, with its tracks in `list` (`n`, `title`, `artist`, `path`) |
| `POST /api/track/play` | `{"root", "path"}` — that one track now (music: in the show; On demand: on its own, no DJ), then the show |
| `POST /api/album` / `/api/album/stop` | `{"id": n}` or `{"root", "folder", "deep"}` — play it next (music: in the show with the DJ; On demand: no DJ, after the song playing; `up_next_source`) / drop the rest of it, or the queued one |
| `POST /api/request` | `{"id": n}` — play that track next; the status's `requests` lists what's queued |
| `GET /api/radio` | Streaming: the saved stations, what's playing instead of the show (`source`: a station with its `title`, or an album with its `track`), why the last one stopped, and the directory copy's state |
| `GET /api/radio/search?q=` | Stations by name or tag (`from`: `copy` or `online`; 400 with `{"error"}` if there's no copy and Radio Browser can't be reached) |
| `POST /api/radio/directory` | Fetch a fresh copy of the station directory now (in the background) |
| `POST /api/radio/play` / `/api/radio/stop` | `{"name", "url"}` — play that station instead of the show (starts the speaker if paused) / back to the show (from a station or an album) |
| `GET /api/books` | The audiobooks, each with `key`, `title`, `author`, `total_ms`, `pos_ms` (where it was left), `chapters` |
| `GET /api/tags?kind=&path=` | The tags (title, artist, album, track number) of a track, or of every track under a folder; `changed`: not the ones it came with |
| `POST /api/tags` / `/api/tags/restore` | `{"kind", "path", "tags": {"title", "artist", "album", "track"}}` — change one track's tags in the file (rewritten as a copy, swapped in) / `{"kind", "path"}` — as they were before they were first changed |
| `POST /api/media/rename` | `{"kind", "path", "name"}` — rename a file or folder where it is; playlists, programmes and buttons follow |
| `GET /api/ondemand/kept` / `POST /api/ondemand/keep` | The On demand files and folders that remember their place, each with where it was left / `{"path", "keep": true \| false}` — set one, or clear it |
| `POST /api/books/play` / `/api/books/seek` | `{"key"}` — that book from where it was left / `{"delta_ms": -60000}` or `{"to_ms": n}` in the book on |
| `GET /api/podcasts` | The shows followed, each with its number of episodes, how many recent ones are unheard, and the latest |
| `GET /api/podcasts/search?q=` | Shows from the podcast directory (`id`, `feed_url`, `title`, `author`) |
| `GET /api/podcasts/episodes?id=&refresh=1` | A show's episodes, newest first (`guid`, `gid`, `title`, `pub`, `duration_ms`, `pos_ms`, `done`); `refresh=1` fetches the feed first (`note` if it couldn't) |
| `POST /api/podcasts/follow` / `/unfollow` | `{"feed_url"}` / `{"id"}` |
| `POST /api/podcasts/play` / `/heard` | `{"id", "guid"?}` — that episode (none: the one part-heard, else the newest unheard), then on through the newer ones / `{"id", "guid", "heard": bool}`; seek with `/api/books/seek` |
| `POST /api/album/play` | `{"id": n}` or `{"root", "folder", "deep", "track"?}` (`deep`: everything under the folder; `track`: start at that one) — play it straight through instead of the show |
| `GET /api/media?kind=music\|jingles\|audiobooks\|ondemand&path=` | A folder of the library: its folders (with item counts) and files, and the space left |
| `POST /api/media/upload?kind=&dir=&name=` | The file as the body (`name` may include folders); `{"path", "status": "added" \| "same"}` |
| `POST /api/media/move` | `{"kind", "path", "to_kind", "to"}` — move a file or folder into the folder `to` (same library or the other: music ↔ ondemand); playlists, programmes and album buttons follow it. `{"path", "kind"}`: where it is now |
| `GET /api/media/download?kind=&path=` | The file, or the folder as a zip (stored), as a download |
| `POST /api/lengths` | `{"items": [{"kind": "track", "root", "path"} \| {"kind": "album", "root", "folder", "deep"?} \| {"kind": "playlist", "name"}]}` → `{"minutes": [n \| null]}`, read from the files' tags (cached) |
| `POST /api/media/mkdir` / `/api/media/delete` | `{"kind", "path", "name"}` / `{"kind", "path"}` (a file or a folder) |
| `POST /api/media/done` | Changes finished: /media read-only again, and the library rescanned |
| `POST /api/buttons/bank` / `/api/buttons/auto` | `{"bank": "day" \| "night"}` (or `{}` to swap), announced on the radio / `{"on", "night_min", "day_min"}`: the timetable. `GET /api/buttons` has `bank` and `auto` |
| `POST /api/buttons/key` | `{"button": 1-4, "down": bool}` — the page's button going down / up, through the real buttons' timers (tap, 1+4 for 5 s); one held over 30 s is let go |
| `GET /api/buttons` | The four preset buttons (`steps`: each `preset`, `label`, `playing`; `step`: the one playing; and, for the step playing or else the first, `preset`, `label`, `playing`), `max_steps`, what's playing now as a preset, and the actions |
| `POST /api/buttons` | `{"button": 1-4, "preset": {...} \| null, "bank"?: "day" \| "night"}` (bank: programme that set without swapping; `GET` has both in `sets`), `{"button", "steps": [{...}, ...]}` (up to six, stepped through by pressing it again), `{"button", "preset", "add": true}` (one more step on the end) or `{"button", "now": true, "add"?}` (what's playing, on it); a podcast preset is `{"kind": "podcast", "show", "title", "start": gid, "start_title"}` |
| `GET` / `POST /api/speaker/sounds` | The saved speaker set-ups: `{"sounds": [{"name", "eq": {"bass", "mid", "treble"}, "highpass", "mono"}]}` (POST: the whole list, up to 20; applying one is `POST /api/speaker`) |
| `GET /api/backups` | The settings files kept on the radio: `{"backups": [{"name", "saved", "bytes"}], "max"}`; `GET /api/backups/file?name=N` downloads one |
| `POST /api/backups/save` / `/load` / `/delete` / `/upload?name=N` | `{"name"?}` keeps the settings as they are now (no name: the date and time) / `{"name"}` loads one (as `POST /api/settings`; the settings as they were are kept as "Before the last load") / `{"name"}` deletes one / a settings file as the body is kept (not loaded) |
| `POST /api/buttons/slot` | `{"slot": "knob_long" \| "power_on", "preset": {...} \| null}` — what holding the knob does / what plays at switch-on (null: as it came); `GET /api/buttons` has them in `slots` |
| `POST /api/buttons/press` | `{"button": 1-4, "step"?: 1-6}` — as if pressed on the case (a button with steps moves on one, and plays after 0.8 s); `step`: that one, at once |
| `POST /api/radio/stations` | `{"stations": [...]}` — replace the saved list |
| `GET /api/artists` | Every artist with a track count, your lists, and what's playing |
| `POST /api/station` | `{"artist": name \| null}` or `{"profile": name}` |
| `POST /api/profiles` | `{"profiles": [{"name", "artists": [...]}]}` — replace the lists |
| `GET` / `POST /api/birthdays` | The list (and whose birthday it is today) / `{"birthdays": [...]}` to replace it |
| `POST /api/birthdays/hear` | `{"name", "day", "month", "year"?}` — say that wish now |
| `GET` / `POST /api/messages` | The messages and their times, with `next` (HH:MM) and `due_now` / the same object to replace them |
| `POST /api/messages/hear` | `{"text"}` — the DJ says it now (with the day and date first, if that's on) |
| `GET` / `POST /api/settings` | Download the settings file / load one (400 with `{"error"}` if it's wrong) |
| `POST /api/power` | Shut down, or `{"restart": true}` to restart (404 unless the appliance's watcher is there) |
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
               eq.py (3-band EQ + low cut), testsignal.py (sweeps, noise, left/right, phase check)
  tts/         sherpa-onnx voice, run in a recycled worker process
  web/         MP3 stream, the web page (page.html) and the JSON API (server.py)
  config/      Settings (JSON), atomic file writes, settings backup (backup.py), the web password
               (auth.py), "is the clock right?"
  io/          knob.py (rotary encoder + push switch, short/long press, two-button chord), announce.py
               (spoken address), presets.py (the four buttons), service.py (the service menu)
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
clause at a time (at most 12 words), and the TTS worker process is recycled
when it passes 250 MB after a line, or 280 MB partway through one (a fresh
worker says the rest) -- its buffers grow and never shrink. One long clause
can still take it past ~300 MB, and if the kernel's out-of-memory killer
ends it, the next line loads a fresh one (it used to stay dead until a
restart: no DJ, no spoken time). The appliance
image adds 256 MB of compressed swap in RAM (zram, lz4: `S01zram`), without
which the personal voice's ~300 MB peaks left the Zero thrashing. The CPU
is shared the same way: on the radio the station runs at nice -5 (the
speaker feed, aplay and the music decoder inherit it) and the voice worker
and library scan lower themselves to +5, with a 500 ms speaker buffer; and
the voice worker **sleeps** (its process exits, freeing 150-300 MB) after 30 s
with nothing to say or once a line leaves it over 250 MB, and the next line
wakes it (~17 s; lines are made well ahead, time checks 75 s before the song
ends, and "Say the time" wakes it before working out the time); and
once music is playing the station locks the memory it's using in RAM
(`mlockall(MCL_CURRENT | MCL_ONFAULT)`, ~100 MB), because the voice reloading
its ~60 MB model pushed the station's code out of memory and the stall while
it was read back in was a blip in the music. See `sleepradiopi/tts/worker.py`
and the "Lessons" section of `docs/PI_SETUP.md`.
