# SleepRadioPi-OS

A small, fast-booting, power-loss-safe Buildroot image for the
[SleepRadioPi](https://github.com/dylan7474/SleepRadioPi) bedside radio on a
Raspberry Pi Zero 2 W with a HiFiBerry MiniAmp.

The app lives in its own repo and stays plain Python, so it can still be
developed on Raspberry Pi OS. This repo only builds the appliance image.

## Goals

- **Pull the plug at any time.** Boot, root (squashfs) and the media library
  are read-only; logs and runtime state live in RAM. The only thing written
  is a small data partition, and only by atomic replace.
- **Fast boot, more free RAM.** No desktop, no systemd, no services you don't
  need: BusyBox init, eudev, Wi-Fi, mDNS, ssh, and the station.

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

### Updating over Wi-Fi

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
- **The web page** (http://sleepradiopi.local/) is the control panel:
  volume, stereo/mono, a 3-band EQ and low cut, a sleep timer that fades the
  speakers, **play next** (search the library and queue songs; the DJ
  introduces them), artist radio ("Beatles Radio") and your own lists of
  artists ("Friday List"; the "Sleep Radio" jingles only play on the main mix), birthdays the DJ wishes on the day, test sounds (sweeps,
  pink noise, left/right and phase checks), a virtual knob, skip, save/load
  settings and shut down. All of it is described in the
  [SleepRadioPi README](https://github.com/dylan7474/SleepRadioPi#the-web-page),
  with the JSON API behind it.
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

A Pi Zero 2 W with a **HiFiBerry MiniAmp** (pHAT on a full 2×20 header), a
**rotary encoder** with push switch, and optionally a **DS3231 RTC**. The pin
table, parts list and cautions (3.3 V only for the encoder module and RTC;
the ZS-042 RTC board's charging circuit) are in the
[SleepRadioPi README](https://github.com/dylan7474/SleepRadioPi#wiring).
In short:

| Use | GPIO | Header pin |
|---|---|---|
| MiniAmp I2S | 18, 19, 21 | 12, 35, 40 (+ 5 V on 2/4, GND 6) |
| Encoder A / B / switch | 17 / 27 / 22 | 11 / 13 / 15 (GND 9 or 14) |
| RTC SDA / SCL | 2 / 3 | 3 / 5 (3.3 V on 1, GND 9) |

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

![SleepRadioPi case mock-up](hardware/case/mockup_logo_sunburst.jpg)

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
| `buildroot/` | Buildroot 2026.02.x LTS, pinned as a submodule |
| `configs/sleepradiopi_zero2w_defconfig` | The image config |
| `board/sleepradiopi/` | Boot config, cmdline, genimage layout, build scripts |
| `board/sleepradiopi/rootfs-overlay/` | Files copied into the root filesystem |
| `board/sleepradiopi/linux.fragment` | Kernel options on top of `bcm2711_defconfig` (squashfs built in; unused drivers trimmed) |
| `package/` | `python-sherpa-onnx` (PyPI wheels) and `sleepradiopi` (the app) |
| `scripts/` | `flash.sh`, `provision-media.sh`, `update.sh`, `speaker-mode.sh` (run on the PC) |
| `hardware/case/` | The 3D-printed case (OpenSCAD source, STLs, assembly notes) |

## Roadmap

1. **Minimal image**: boots, Wi-Fi, `sleepradiopi.local`, ssh, MiniAmp
   visible to ALSA; measure boot time.
2. **App runtime**: Python, ffmpeg, numpy, mutagen, and a sherpa-onnx
   package; the SleepRadioPi test suite passes on the Pi.
3. **Storage layout**: read-only root (squashfs), read-only music partition,
   small data partition written only with atomic replace; pull-the-plug tests.
4. **Appliance**: the station as a boot service, the ready LED, saved clock
   time (no RTC), A/B updates over Wi-Fi (`update.sh`).

All four are done, plus the speaker output and knob, offline mode and a
tag cache (on air ~20 s after power-up), mono or stereo speakers, and the
web page as a control panel (EQ, sleep timer, artist radio and lists,
birthdays, test sounds, settings backup, shut down, the knob's spoken
address). The MiniAmp plays real sound in the printed cabinet; RTC support
is in. Next: wire the knob and the RTC module and test them; then see the
[SleepRadioPi roadmap](https://github.com/dylan7474/SleepRadioPi/blob/main/docs/ROADMAP.md),
and a physical needle VU meter for this image.

## Licence

Build scripts, configs and the case design: GPL-2.0-or-later, the same as Buildroot. The image
contains many packages under their own licences; `make legal-info` lists them.
