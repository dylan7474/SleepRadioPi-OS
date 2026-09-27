# Pi Zero 2 W: dev/deploy setup

## Lessons from the first real setup (2026-09-22)

The Zero 2 W's 512 MB is the constraint. What it took to fit the station:

- **Use Raspberry Pi OS *Lite* (64-bit).** The desktop edition was flashed
  by mistake and ran a desktop nobody sees; fixed with
  `sudo systemctl set-default multi-user.target`.
- **Free the GPU's memory reservation.** The KMS display driver reserved
  256 MB of CMA. For a headless Pi, comment out `dtoverlay=vc4-kms-v3d` in
  `/boot/firmware/config.txt` and add `gpu_mem=16` (usable RAM 416 → 464 MB).
- **Turn off what a radio doesn't need:** `cups`, `cups-browsed`,
  `ModemManager`, `bluetooth`, `triggerhappy`, `rtkit-daemon` (system) and
  the user's `pipewire`/`wireplumber`/`filter-chain` (masked). Audio goes
  to ALSA directly.
- **zram doesn't help here.** The swapped-out data is the voice model's
  weights, which barely compress, so zram used as much RAM as it saved.
- **One voice.** Measured: a voice loads at ~120 MB and grows to 230–320 MB
  after long text; two voices plus the stream don't fit.
- **Raspberry Pi Imager:** version 1.9.6 (pinned) can't reliably customise
  the newer Trixie images, so use the Legacy (Bookworm) Lite image with it.
- On first boot, wait for the clock to sync and `packagekitd` to finish
  before `apt`.

The workflow: **edit and run Claude Code on your desktop**, the Pi is the
test device you deploy to over SSH — the same shape as the Android
`adb install` workflow used for the SleepRadio phone app, just `rsync`/`ssh`
instead of `adb`. See `README.md` for why (512 MB RAM makes the Pi Zero 2 W
a poor place to *develop* interactively, even though it's fine to *run* the
finished app).

## 1. Generate an SSH key (once, on the desktop)

```bash
ssh-keygen -t ed25519 -C "sleepradiopi" -f ~/.ssh/sleepradiopi
```

Leave the passphrase empty if you want fully non-interactive deploys from
scripts/Claude; set one if you'd rather type it each session.

## 2. Flash the SD card with Raspberry Pi Imager

Use **Raspberry Pi OS Lite (64-bit)** — no desktop needed, and 64-bit
matters: it's what gets you prebuilt `numpy`/`sherpa-onnx` wheels from PyPI
instead of a slow from-source build on a 512 MB board.

In Imager's gear-icon / "Edit Settings" (OS Customisation) before writing:

- Hostname: `sleepradiopi` (gives you `sleepradiopi.local` via mDNS —
  no need to hunt for a DHCP-assigned IP)
- Enable SSH → **"Allow public-key authentication only"**, paste the
  contents of `~/.ssh/sleepradiopi.pub`
- Set your Wi-Fi SSID/password (Zero 2 W has no Ethernet port)

This gets you a headless Pi that's SSH-reachable on first boot, no monitor
or keyboard needed.

## 3. First connection

```bash
ssh -i ~/.ssh/sleepradiopi pi@sleepradiopi.local
```

(If mDNS doesn't resolve from your network, find the IP from your router's
DHCP client list instead.) Worth adding a `~/.ssh/config` entry so you don't
need `-i` every time:

```
Host sleepradiopi
    HostName sleepradiopi.local
    User pi
    IdentityFile ~/.ssh/sleepradiopi
```

## 4. Enable the HiFiBerry overlay

Edit `/boot/firmware/config.txt` on the Pi (exact overlay name depends on
which HiFiBerry board you get — check its docs):

```ini
dtparam=audio=off          # disable the Pi's own onboard audio
dtoverlay=hifiberry-dac    # or hifiberry-amp / hifiberry-dacplus / etc.
```

Reboot, then confirm it enumerates as an ALSA device:

```bash
aplay -l
```

For the volume knob (rotary encoder on GPIO17/27, push switch on GPIO22;
wiring in the README), add:

```ini
gpio=17,27=ip,pu
dtoverlay=rotary-encoder,pin_a=17,pin_b=27,relative_axis=1
dtoverlay=gpio-key,gpio=22,active_low=1,gpio_pull=up,keycode=164,label="PLAYPAUSE"
```

and put the station's user in the `input` group (`sudo usermod -aG input pi`).
Set `"speaker_enabled": true` in `~/.config/sleepradiopi/config.json` to
play through the amp from start-up.

## 5. Python environment

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip vlc libportaudio2
git clone https://github.com/dylan7474/SleepRadioPi.git
cd SleepRadioPi
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

`gpiozero` ships preinstalled on Raspberry Pi OS; the `luma.lcd` extra
dependencies depend on which panel you end up with (SPI vs. I2C) — revisit
once that's chosen.

## 6. Deploy loop

`scripts/deploy.sh` (in this repo) rsyncs the working tree to the Pi and
runs the test suite there — the "adb install" equivalent for this project.
Usage:

```bash
./scripts/deploy.sh          # sync + run tests
./scripts/deploy.sh --run    # sync + run sleepradiopi/main.py
```

## Status

Written ahead of actually having the hardware — treat these steps as the
plan, not as verified-on-real-Pi instructions yet. First real task once the
board arrives: confirm `aplay -l` shows the HiFiBerry device (step 4) and
that a test tone plays through it — that's roadmap milestone 1.
