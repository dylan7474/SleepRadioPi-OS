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
    --jingles ~/Music/SleepRadioJingles [--hooks dj_hooks_70s.txt]
```

`provision-media.sh` adds the media partition (filling the card) on its first
run and syncs the folders on later runs; it also writes a starter config to
`/data` that points at `/media`. `--voices` is a folder of voice packs, one
per subfolder (`stock/`, `personal/`: `model.onnx`, `tokens.txt`,
`espeak-ng-data/`).

After a rebuild, the same `flash.sh` command on a provisioned card rewrites
only boot and root, keeping `/data` and `/media`; `--full` wipes the card.

### Updating over Wi-Fi

```sh
make && scripts/update.sh
```

Writes the new root to the slot the Pi isn't running from (p2 or p3),
verifies it, switches `cmdline.txt` to it and reboots, then waits for the
station (about 2 minutes). The previous root stays in the other slot. If the
Pi doesn't come back, put the card in the PC and set `root=` in
`cmdline.txt` on the boot partition back to the other slot. Kernel changes
can't be done this way (the kernel is on the shared boot partition): the
script refuses, and you use `flash.sh`.

`local/` is git-ignored. It holds your Wi-Fi credentials and the SSH host
keys, which are generated on the first build so the Pi keeps the same
identity across rebuilds.

Other targets are passed through to Buildroot: `make menuconfig`,
`make savedefconfig`, `make linux-menuconfig`, `make <pkg>-rebuild`, ...

## Using the Pi

- `ssh -i ~/.ssh/sleepradiopi root@sleepradiopi.local` (key only; the
  authorised keys are in `board/sleepradiopi/rootfs-overlay/root/.ssh/authorized_keys`).
- Green ACT LED: flickers while booting; a **fast blink** means no Wi-Fi
  address yet (check `wpa_supplicant.conf`); a **heartbeat** means it's on the
  network and the station is starting; **3 slow pulses** when the station is
  on http://sleepradiopi.local/, then dark. A heartbeat that never ends means
  the station isn't starting (see the logs). A steady light means the kernel
  didn't start or mount the root.
- Wi-Fi: `wpa_supplicant.conf` on the boot partition (FAT, readable on
  any PC).
- Serial console: GPIO14/15, 115200 baud (Bluetooth is disabled so the full
  UART is used).
- Logs: `/var/log/messages` (in RAM, lost at power-off).
- The station starts at boot as user `radio` and is restarted if it exits.
  Settings: `/data/radio/.config/sleepradiopi/config.json`; logs:
  `grep sleepradiopi /var/log/messages`; restart: `pkill -f sleepradiopi.main`;
  keep it off: `touch /data/radio/station.off`.
- The clock is saved to `/data/clock` (no RTC) and restored at boot, so the
  time is roughly right even before NTP answers.
- `media-rw` / `media-rw off`: make `/media` writable for a quick change over
  ssh (a card reader is much faster for anything big).

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
| `scripts/` | `flash.sh`, `provision-media.sh`, `update.sh` (run on the PC) |

## Roadmap

1. **Minimal image**: boots, Wi-Fi, `sleepradiopi.local`, ssh, MiniAmp
   visible to ALSA; measure boot time.
2. **App runtime**: Python, ffmpeg, numpy, mutagen, and a sherpa-onnx
   package; the SleepRadioPi test suite passes on the Pi.
3. **Storage layout**: read-only root (squashfs), read-only music partition,
   small data partition written only with atomic replace; pull-the-plug tests.
4. **Appliance**: the station as a boot service, the ready LED, saved clock
   time (no RTC), updates.

## Licence

Build scripts and configs: GPL-2.0-or-later, the same as Buildroot. The image
contains many packages under their own licences; `make legal-info` lists them.
