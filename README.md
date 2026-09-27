# SleepRadioPi-OS

**A bedside radio station in a box.** Plug it in and, about 40 seconds
later, a friendly DJ says good evening and starts playing *your* music
through its own speakers — back-announcing the song that just finished,
introducing the next, dropping in 70s-style patter, station idents and
jingles, the time, the news on the hour, and happy-birthday wishes for your
family. No phone, no app, no subscription and no screen: one knob for the
volume, and a web page for everything else. The presenter's voice is made on
the device (offline neural text-to-speech, even in your own cloned voice), so
it works with no internet at all.

It runs on a **Raspberry Pi Zero 2 W** with a **HiFiBerry MiniAmp** and two
small speakers in a 3D-printed art deco cabinet (in [`hardware/case/`](hardware/case/)).
This repo builds everything: a small, fast-booting, read-only Linux image
(Buildroot) that you can pull the plug on at any moment, with the radio
software, and the case to print.

![The radio: white body, blue panels, sunburst grilles and knob](hardware/case/mockup_logo_sunburst.jpg)

## What it does

### On air

- **A real show, made from your music library.** A proper shuffle (no
  repeats within the last ~20 songs, the same artist spaced out), with the
  DJ talking between songs: *"That was Girl, by The Beatles. Coming up, Pink
  Moon, by Nick Drake."* How often it talks is up to you (before every song,
  or every 2, 3 or 5).
- **Time checks** worded for the exact moment they're spoken (twice as often
  in the morning), and a greeting for the time of day.
- **News** — BBC News bulletins at :00 (top stories) and :30 (softer
  stories), none in the night-time quiet hours.
- **Birthdays** — tell it whose birthday is when, and on the day the DJ
  wishes them a happy one between songs (*"happy forty-first birthday to
  Sarah"*), a few times through the day.
- **Artist radio and your own lists** — play only one artist (the DJ then
  says *"welcome to Beatles Radio"*) or a named list of artists (*"Friday
  List on Sleep Radio"*).
- **Play next** — search the library from your phone and queue songs; the
  DJ introduces them.
- **Album mode** — or pick a whole album: the DJ introduces it, then it plays
  start to finish like a record (no talk between tracks), and the DJ
  back-announces it at the end.
- **Jingles** from your own collection, shuffled, and a short one to open
  the show (on the main mix only, since they say "Sleep Radio").

### How it sounds

- **Every song at the same loudness.** Each track is measured once and
  turned up or down to a common level, with a soft limit so nothing clips.
- **No dead air.** Silence at the start and end of songs is trimmed, so the
  DJ comes in as the song ends.
- **A broadcast voice.** The presenter is EQ'd like a radio voice and
  levelled to sit with the music; every line is prepared while the previous
  song plays, so there's never a wait.
- **Tuned for small speakers.** A 3-band EQ with a low cut, stereo or mono,
  all done in software (the MiniAmp has no controls of its own); an optional
  bass-port back panel for the case.

### Using it

- **Power on and it plays.** About 10 seconds after you plug it in it chimes
  and the DJ says it's warming up, so you know it's alive; the show starts
  once the voice has loaded. (An LED flickers while it boots and pulses when
  it's on air.)
- **One knob.** Turn for volume; press to pause or play; **hold it for 3
  seconds** and the radio reads out its network address (handy away from
  home).
- **The web page** — http://sleepradiopi.local/ from any phone or computer
  on your network (optionally with a password) is its remote control.
  Everyday controls up front: now playing and skip, volume and a sleep timer
  that fades the speakers out, artist radio, play next (and, if you switch
  it on, listening in the browser too). Under ⚙ Settings: the
  speakers (stereo/mono, EQ, low cut), the DJ (voice, how often it talks,
  hooks, jingles, news), voices (download the standard one, upload your
  own), your artist lists, birthdays, test sounds for
  checking speakers and wiring, the password, a settings backup you can
  download and load back, and shut down.
- **Offline is normal.** Without the internet it keeps playing and talking;
  it only leaves out what needs the right time (time checks, news, birthday
  wishes) until the clock is known, from the internet or an optional
  real-time clock module.
- **Pull the plug any time.** The system and the music are read-only, logs
  live in RAM, and settings are written atomically, so a power cut can't
  corrupt anything.
- **Updates from the web page.** Settings → Updates checks for a new release
  on GitHub and installs it into a spare system slot while the music plays;
  the radio restarts, and the DJ says it's been updated. If a new version
  doesn't come on air within 5 minutes, the radio goes back to the old one
  by itself (and says so).

## The appliance image

- **Pull the plug at any time.** Boot, root (squashfs) and the media library
  are read-only; logs and runtime state live in RAM. The only thing written
  is a small data partition, and only by atomic replace.
- **Fast boot, more free RAM.** No desktop, no systemd, no services you don't
  need: BusyBox init, eudev, Wi-Fi, mDNS, ssh, and the station.
- **The radio software** is plain Python (sherpa-onnx for the voice, ffmpeg
  for audio, numpy for the EQ) in [`app/`](app/), with its own README (the
  settings, the web page's API, how it's put together) and tests.
  `package/sleepradiopi` installs it into the image from there.

## Building

Needs about 15 GB of disk space. The first build takes an hour or more; later
builds reuse `dl/` and ccache.

```sh
git clone --recurse-submodules <this repo>
cd SleepRadioPi-OS
cp board/sleepradiopi/wpa_supplicant.conf.example local/wpa_supplicant.conf
$EDITOR local/wpa_supplicant.conf     # your Wi-Fi network
make
```

The image is `output/images/sdcard.img`. With the card in the PC's reader:

```sh
sudo scripts/flash.sh /dev/mmcblk0          # new card: the whole image
sudo scripts/provision-media.sh /dev/mmcblk0 \
    --music ~/Music/SleepRadioMusic --voices ~/voices \
    --jingles ~/Music/SleepRadioJingles --mono    # or --stereo
```

`provision-media.sh` adds the media partition (filling the card) on its first
run and syncs the folders on later runs; it also writes a starter config to
`/data` that points at `/media`. `--voices` is a folder of voice packs, one
per subfolder (`stock/`, `personal/`: `model.onnx`, `tokens.txt`,
`espeak-ng-data/`). The 70s DJ hooks come with the app; `--hooks FILE` puts
your own list on `/media` instead. `--mono` (one speaker, or two playing the
same) or `--stereo` picks the model; a new config defaults to stereo.

After a rebuild, the same `flash.sh` command on a provisioned card rewrites
only boot and root, keeping `/data` and `/media`; `--full` wipes the card.

### Changing the radio software

The code is in `app/`. Run its tests on the PC (`cd app`, then as in
[`app/README.md`](app/README.md#running-it)), rebuild just the app into the
image, and update the Pi:

```sh
make sleepradiopi-dirclean && make && scripts/update.sh
```

### Releases, and updating from the web page

`scripts/release.sh 1.2.0 --notes "What's new."` builds the image with that
version in it and publishes a GitHub release (tag `v1.2.0`) with the system
image, `config.txt` and a `manifest.json` of sizes and SHA-256s (and the
kernel's). `--sdcard` also attaches the whole card image for new builds;
`--local` builds the files without tagging or publishing, to test an update
from a local web server (set `"update_source": "http://<PC>:8765/manifest.json"`
in the radio's config).

A radio updates itself from its page (Settings → Updates): the station asks
the root helper `update-watch` (`app/sleepradiopi/updater.py`), which
refuses a kernel change, streams the image into the spare root slot,
checks it against the SHA-256 (and reads it back from the card), updates
`config.txt` if needed, switches `cmdline.txt` and reboots. The station
confirms the update once music plays; if it hasn't within 5 minutes,
`S98update-watchdog` switches back to the old slot and reboots. Logs:
`grep update /var/log/messages`.

### Updating over Wi-Fi from the PC

```sh
make && scripts/update.sh
```

Writes the new root to the slot the Pi isn't running from (p2 or p3),
verifies it, updates `config.txt` if it changed, switches `cmdline.txt` to
it and reboots, then waits for the station (about 2 minutes). The previous root stays in the other slot. If the
Pi doesn't come back, put the card in the PC and set `root=` in
`cmdline.txt` on the boot partition back to the other slot. Kernel changes
can't be done this way (the kernel is on the shared boot partition): the
script refuses, and you use `flash.sh`.

`local/` is git-ignored. It holds your Wi-Fi credentials and the SSH host
keys, which are generated on the first build so the Pi keeps the same
identity across rebuilds.

**Building it yourself?** Put your own public key(s) in
`board/sleepradiopi/rootfs-overlay/root/.ssh/authorized_keys` first: the
ones there are the author's, and root login is by key only.

Other targets are passed through to Buildroot: `make menuconfig`,
`make savedefconfig`, `make linux-menuconfig`, `make <pkg>-rebuild`, ...

## Using the Pi

- `ssh -i ~/.ssh/sleepradiopi root@sleepradiopi.local` (key only; the
  authorised keys are in `board/sleepradiopi/rootfs-overlay/root/.ssh/authorized_keys`).
- Green ACT LED: flickers while booting; a **heartbeat** while the station
  starts; **3 slow pulses** once it's playing, then dark. Wi-Fi isn't shown:
  offline is normal. A heartbeat that never ends means the station isn't
  starting (see the logs); a steady light means the kernel didn't start or
  mount the root.
- Wi-Fi: `wpa_supplicant.conf` on the boot partition (FAT, readable on
  any PC).
- Serial console: GPIO14/15, 115200 baud (Bluetooth is disabled so the full
  UART is used).
- Logs: `/var/log/messages` (in RAM, lost at power-off).
- The station starts at boot as user `radio` and is restarted if it exits.
  Settings: `/data/radio/.config/sleepradiopi/config.json`; logs:
  `grep sleepradiopi /var/log/messages`; restart: `pkill -f sleepradiopi.main`;
  keep it off: `touch /data/radio/station.off`.
- It plays through the MiniAmp from power-up (`speaker_enabled`). The knob:
  turn for volume (remembered), press to pause; **hold it 3 s** and the radio
  beeps and reads out its IP address (or says it isn't connected), for
  finding the web page away from home.
- **The web page** (http://sleepradiopi.local/) is the control panel
  (everyday controls on the main page, the rest under ⚙ Settings):
  volume, stereo/mono, a 3-band EQ and low cut, a sleep timer that fades the
  speakers, the DJ (voice, how often it talks, hooks, jingles, news), **play next** (search the library and queue songs; the DJ
  introduces them), artist radio ("Beatles Radio") and your own lists of
  artists ("Friday List"; the "Sleep Radio" jingles only play on the main mix), birthdays the DJ wishes on the day, test sounds (sweeps,
  pink noise, left/right and phase checks), a virtual knob, skip, save/load
  settings and shut down (see *What it does*, above). Everything on it is a
  small JSON API too, e.g. `wget -qO- http://127.0.0.1/api/status` over
  ssh.
- **Web password** (optional, off by default): set it on the page under
  Settings → Password. Forgotten it? `sleepradio-password clear` on the Pi
  (or `scripts/web-password.sh clear` from the PC) opens the page again at
  once; `set` chooses a new one. The radio itself never needs it.
- **Shut down** from the web page (http://sleepradiopi.local/, bottom of
  the page): the station creates `/run/sleepradiopi/poweroff`, and
  `power-request-watch` (root, from inittab) runs `poweroff`, which saves the
  clock and unmounts `/data` as usual. Unplug after about 20 seconds; plug
  it back in to turn it on.
- Mono or stereo (`speaker_mono`; only the speaker, the web stream stays
  stereo): the **Speakers** buttons on the web page switch it at once and
  save it. `scripts/speaker-mode.sh mono|stereo` from the PC does the same
  but restarts the station; with no argument it shows the current mode.
- Speaker EQ (`speaker_eq`, bass / mid / treble, ±12 dB) and low cut
  (`speaker_highpass_hz`): the **Speaker EQ** card on the web page. The
  MiniAmp has no EQ, so the app does it in software; it's heard at once and
  saved on `/data`. With the case's bass port back panel, set the low cut
  to 140 Hz.
- **Save / Load settings** on the web page: a JSON copy of the settings and
  volume on your computer or phone, to load back after re-flashing the card
  or onto another radio (each radio keeps its own folders, port and pins).
  If a loaded setting needs it, the station restarts itself (init respawns
  it; `SLEEPRADIOPI_SUPERVISED=1` in `sleepradiopi-station` allows this).
- **Offline is normal.** The station only says the time and schedules news
  once the clock can be trusted (`/run/time-synced`): either NTP has set it
  since power-on, or an **RTC** answered at boot with a sensible time (its
  oscillator never stopped, and it's no earlier than the image build or the
  last saved time; `S12rtc`, logged as `rtc` in `/var/log/messages`). Until
  then it says no times, greets with "Hello" and skips news. Without an RTC
  the clock is still restored from `/data/clock` at boot, but that can be
  hours out. When Wi-Fi comes up, a udhcpc hook restarts ntpd so the clock
  is set within seconds, and every NTP update (on sync, then every 11
  minutes) is written to the RTC. To test offline:
  `touch /data/wifi-off-once; reboot` (Wi-Fi off for 5 minutes).
- **An update restarts the station, and it plays at start-up.** To update a
  paused radio without it starting to play: `POST /api/speaker
  {"volume": 0}`, wait 4 s (the volume is saved), run `update.sh`, then as
  soon as the page answers `{"pause": true}` and put the volume back.
- `media-rw` / `media-rw off`: make `/media` writable for a quick change over
  ssh (a card reader is much faster for anything big).

## Hardware and wiring

| Part | Notes |
|---|---|
| Raspberry Pi Zero 2 W | With a full 2×20 header soldered on. |
| HiFiBerry MiniAmp | 2 × 3 W class-D amp, a pHAT on the 40-pin header, powered from the Pi's 5 V. No volume control of its own: the radio does it in software. |
| Two speakers | 4–8 Ω; the case takes two 40 mm Gikfun EK1794. One speaker works too (set mono). |
| Rotary encoder with push switch | Volume, pause, and the long press. A bare encoder or a KY-040-style module. |
| DS3231 RTC module (optional) | Keeps the time with no network. |
| 5 V supply, 2.5 A or more | The amp draws from the Pi's 5 V. |

![Wiring diagram: the Pi's header, the MiniAmp, the knob, the optional RTC, speakers and power](hardware/wiring/wiring.svg)

(The drawing is made by `hardware/wiring/make_wiring.py`; keep it in step with
`board/sleepradiopi/config.txt` if a pin ever changes.)

The MiniAmp covers the pins the knob and RTC need, so either solder their
wires to the underside of the Pi's header, or put a stacking header / GPIO
extender between the Pi and the amp.

| Use | GPIO | Header pin |
|---|---|---|
| MiniAmp I2S | 18, 19, 21 | 12, 35, 40 (+ 5 V on 2/4, GND 6) |
| Encoder A / B / switch | 17 / 27 / 22 | 11 / 13 / 15 (GND 9 or 14) |
| RTC SDA / SCL | 2 / 3 | 3 / 5 (3.3 V on 1, GND 9) |

- The encoder pins have the Pi's internal pull-ups on, so a bare encoder
  works. A module with its own pull-ups (KY-040) has a `+` pin: put it on
  **3.3 V (pin 1), never 5 V**.
- Power the RTC from **3.3 V, never 5 V** (its I2C pull-ups go to its
  supply, and the Pi's pins are 3.3 V only). The common ZS-042 board charges
  its battery for a rechargeable LIR2032; with an ordinary CR2032, remove its
  charging resistor (often marked 201) or diode.
- Each speaker to its own channel's + and − on the MiniAmp; never join the
  two − terminals. A loose or reversed wire makes centred vocals vanish in
  stereo: the web page's test sounds (left/right and a phase check) find it.

`board/sleepradiopi/config.txt` sets the MiniAmp, encoder and switch
overlays (and the encoder pull-ups), and the RTC overlay
(`dtoverlay=i2c-rtc,ds3231`, which also turns I2C on; harmless with no RTC
fitted). A new RTC is set from NTP the first time the Pi is online. `update.sh` brings `config.txt` up to date on
the Pi, so none of this needs the card reader.

### Case

[`hardware/case/`](hardware/case/) is a 3D-printable stereo cabinet for this
build: two 40 mm speakers behind hex or art deco sunburst grilles (with
optional SLEEP RADIO lettering), the Pi, MiniAmp and RTC on the removable
back panel, and the encoder knob in the top. There's an optional **bass
port** back panel (~160 Hz) with a grommet for the cable slot; compare the
panels with the web page's test sounds. OpenSCAD source, ready STLs and
assembly notes are there (work in progress: the panels are printed and
fitted to a test ring; the full tube isn't printed yet).

## Card layout

| # | Partition | Size | Mounted | Holds |
|---|---|---|---|---|
| 1 | boot, FAT | 64 MB | `/boot` ro | firmware, kernel, `wpa_supplicant.conf` |
| 2 | rootA, squashfs | 256 MB | `/` ro | the system and the app |
| 3 | rootB | 256 MB | `/` ro when active | the other root slot: `update.sh` alternates between p2 and p3 |
| 5 | data, ext4 | 256 MB | `/data` rw | settings, caches, the random seed |
| 6 | media, ext4 | rest of the card | `/media` ro | music, voices, jingles |

Partitions 5 and 6 are in an extended partition (MBR: the Zero 2 W can't boot
from GPT). `S00data` checks `/data` with `e2fsck -p` and mounts it before
anything else starts.

## Layout

| Path | What |
|---|---|
| `app/` | The radio software (Python): the station, the DJ, the web page and API, and its tests |
| `buildroot/` | Buildroot 2026.02.x LTS, pinned as a submodule |
| `configs/sleepradiopi_zero2w_defconfig` | The image config |
| `board/sleepradiopi/` | Boot config, cmdline, genimage layout, build scripts |
| `board/sleepradiopi/rootfs-overlay/` | Files copied into the root filesystem |
| `board/sleepradiopi/linux.fragment` | Kernel options on top of `bcm2711_defconfig` (squashfs built in; unused drivers trimmed) |
| `package/` | `python-sherpa-onnx` (PyPI wheels) and `sleepradiopi` (installs `app/`) |
| `scripts/` | `flash.sh`, `provision-media.sh`, `update.sh`, `release.sh`, `speaker-mode.sh`, `web-password.sh` (run on the PC) |
| `hardware/case/` | The 3D-printed case (OpenSCAD source, STLs, assembly notes) |
| `hardware/wiring/` | The wiring diagram (`wiring.svg`) and the script that draws it |

## Roadmap

1. **Minimal image**: boots, Wi-Fi, `sleepradiopi.local`, ssh, MiniAmp
   visible to ALSA; measure boot time.
2. **App runtime**: Python, ffmpeg, numpy, mutagen, and a sherpa-onnx
   package; the radio's test suite passes on the Pi.
3. **Storage layout**: read-only root (squashfs), read-only music partition,
   small data partition written only with atomic replace; pull-the-plug tests.
4. **Appliance**: the station as a boot service, the ready LED, saved clock
   time (no RTC), A/B updates over Wi-Fi (`update.sh`).

All four are done, plus the speaker output and knob, offline mode and a
tag cache (on air ~20 s after power-up), mono or stereo speakers, and the
web page as a control panel (EQ, sleep timer, artist radio and lists,
birthdays, test sounds, settings backup, shut down, the knob's spoken
address). The MiniAmp plays real sound in the printed cabinet; RTC support
is in. Next: wire the knob and test it; a Wi-Fi manager (saved
networks, and a hotspot when none is in range); downloading
the default voice when a radio has none, and uploading your own; and a
physical needle VU meter.

## Licence

Build scripts, configs and the case design: GPL-2.0-or-later, the same as Buildroot. The radio
software in `app/` is GPL-3.0-or-later (`app/LICENSE`; its offline voice engine links eSpeak-NG,
which is GPL). The standard voice, downloaded by the radio when it has none, is Piper's
"southern_english_female" (low) via sherpa-onnx; its training data is OpenSLR 83, CC BY-SA 4.0. The image
contains many packages under their own licences; `make legal-info` lists them.
