# SleepRadioPi-OS

A small, fast-booting, power-loss-safe Buildroot image for the
[SleepRadioPi](https://github.com/dylan7474/SleepRadioPi) bedside radio on a
Raspberry Pi Zero 2 W with a HiFiBerry MiniAmp.

The app lives in its own repo and stays plain Python, so it can still be
developed on Raspberry Pi OS. This repo only builds the appliance image.

## Goals

- **Pull the plug at any time.** The boot and root filesystems are mounted
  read-only; logs and runtime state live in RAM.
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

The image is `output/images/sdcard.img`. Flash it with `dd` or Raspberry Pi
Imager ("Use custom").

`local/` is git-ignored. It holds your Wi-Fi credentials and the SSH host
keys, which are generated on the first build so the Pi keeps the same
identity across rebuilds.

Other targets are passed through to Buildroot: `make menuconfig`,
`make savedefconfig`, `make linux-menuconfig`, `make <pkg>-rebuild`, ...

## Using the Pi

- `ssh -i ~/.ssh/sleepradiopi root@sleepradiopi.local` (key only; the
  authorised key is `board/sleepradiopi/rootfs-overlay/root/.ssh/authorized_keys`).
- Wi-Fi: `wpa_supplicant.conf` on the boot partition (FAT, readable on
  any PC).
- Serial console: GPIO14/15, 115200 baud (Bluetooth is disabled so the full
  UART is used).
- Logs: `/var/log/messages` (in RAM, lost at power-off).

## Layout

| Path | What |
|---|---|
| `buildroot/` | Buildroot 2026.02.x LTS, pinned as a submodule |
| `configs/sleepradiopi_zero2w_defconfig` | The image config |
| `board/sleepradiopi/` | Boot config, cmdline, genimage layout, build scripts |
| `board/sleepradiopi/rootfs-overlay/` | Files copied into the root filesystem |

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
