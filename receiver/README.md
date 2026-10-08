# Sleep Radio receiver

An RTL-SDR dongle as a radio station on your network. It runs on a small
computer next to the aerial (a Raspberry Pi 2 is plenty) and a Sleep Radio --
or any internet radio player -- tunes in to it like any other station:

| Address | What you hear |
|---|---|
| `http://RECEIVER:8074/audio?band=2m` | every FM channel of the 2 m amateur band at once |
| `http://RECEIVER:8074/audio?band=marine` | every marine VHF channel (the ship side) at once |
| `http://RECEIVER:8074/audio?freq=145.5` | one frequency, narrow FM (`&mode=am` for AM, `&squelch=off` to hear the noise too) |
| `http://RECEIVER:8074/audio?freq=95.0&mode=wfm` | broadcast FM |
| `http://RECEIVER:8074/audio?band=fm` | the broadcast FM band, on the station it was last on (`&hold=96.6` for that one) |
| `http://RECEIVER:8074/audio?freq=28.5&mode=usb` | one frequency tuned like a rig: `usb`, `lsb` (sideband speech) or `cw` (Morse) |
| `http://RECEIVER:8074/audio?band=10m` | a band of that kind (12 m, CB, 10 m, 6 m, the sideband end of 2 m), where and how it was last |
| `http://RECEIVER:8074/audio?freq=9.41&mode=am` | a short-wave broadcast station, tuned the same way ([with an upconverter](#short-wave-an-upconverter)) |

The broadcast band (87.5 to 108 MHz, every 100 kHz) is a band of another
kind: it's twenty megahertz wide and each station wants the dongle to
itself, so it's one station at a time, not a scanner. `POST /hold
{"freq": 96.6}` moves it to another station while whoever is listening
stays tuned in (on the radio's desktop: the band's key in the receiver's
window, then ◀ ▶).

**Sideband and Morse** are a third kind: one frequency, tuned like a rig.
Neither rtl_airband nor rtl_fm does them, so this part is the receiver's own
(`sleepradio_tuner.py`, which needs numpy): a quarter of a megahertz of the
dongle's raw signal comes from `rtl_tcp`, a spectrum of it is taken every
8 ms (the waterfall, and the first filter), and the passband -- 2.4 kHz for
speech, 400 Hz for Morse -- is cut out and put where the ear wants it, with a
gain that follows the signal. `POST /tune {"freq": 28.495, "mode": "usb"}`
(either or both) moves it while whoever is listening stays tuned in; within
100 kHz of where the dongle sits that's instant, further and the dongle
moves too. A frequency is the carrier's in sideband (what a rig's dial says)
and the signal's own in Morse, which is heard as a 700 Hz tone. On the
radio's desktop: the band's key in the receiver's window, then click the
scope, ◀ ▶ and the wheel, as on an internet receiver; the CW key there reads
the Morse. An RTL-SDR starts at 24 MHz, so of short wave that's 12 m, CB and
10 m -- with an aerial for them; the rest wants an upconverter, which is next.
While it's being tuned the dongle isn't scanning: one dongle does one thing.

Add one to the radio as a station (Stations, *Add a station*, or
`POST /api/radio/stations`), and it goes on a button or into a programme like
any other.

**A band is a scanner that never misses the start of a call.** The dongle
hears 2.4 MHz at a time, and [rtl_airband](https://github.com/rtl-airband/RTLSDR-Airband)
takes all of it apart into its channels, each with its own squelch. Whichever
channel opens is what you hear; it keeps the air until two seconds after it
closes (for the reply); a priority channel (145.500, marine 16) takes it from
the others. The stream's "now playing" title is the channel's name, so the
radio's page shows who has the air, and its history is a log of what was
heard. With nobody transmitting the stream is silence, so the radio stays
tuned in.

A channel that stays open for half a minute -- a repeater's carrier, a
gateway that's keyed all day -- gives way to another that opens, and one
that's always open can be left out of its band: `POST /skip {"freq":
145237500}` (`"on": false` to take it back), or *Skip it* in the radio's
Receiver folder. That's remembered. Narrow FM is high-passed at 300 Hz, so
CTCSS tones aren't heard as a hum.

One dongle does one thing at a time. The latest request wins and listeners
to what it was doing before are disconnected, so 2 m *or* marine, not both
(they're 10 MHz apart; that needs a second dongle). With no listener for a
minute the dongle rests.

## Install

On your own computer, from this repository:

```
receiver/install.sh USER@HOST [--disable-openwebrx]
```

`USER` needs sudo on the receiver (Debian or Raspberry Pi OS). The first time
it builds rtl_airband there (about five minutes on a Pi 2); run it again to
update the service. One program can hold a dongle, so stop anything else that
does: `--disable-openwebrx` stops OpenWebRX and keeps it from starting at boot
(it isn't removed).

**Set the dongle's frequency error**, or narrow FM is off tune: a cheap
dongle can be 50 parts per million out, which at 145 MHz is 7 kHz -- most of
a channel. Put it, and the gain, in `/etc/sleepradio-receiver/options` on the
receiver:

```
OPTIONS="--ppm 55 --gain 29"
```

then `sudo systemctl restart sleepradio-receiver`. (`kal` or `rtl_test -p`
measures the error; OpenWebRX keeps it as the dongle's "ppm".)

### Short wave: an upconverter

An RTL-SDR starts at 24 MHz. An upconverter between the aerial and the dongle
(a NooElec Ham It Up, say, switched to *upconvert* and given its 5 V) adds its
oscillator's frequency to everything, so 7.1 MHz reaches the dongle at
132.1 MHz. Say so, and the receiver is a short-wave one:

```
OPTIONS="--upconverter 125 --ppm 62 --trim 75 --gain 19.7"
```

- `--upconverter` is the oscillator in MHz (a Ham It Up's is 125). Every
  frequency asked for and shown is still the real one, 0.1 to 60 MHz, and the
  built-in bands become the amateur ones from 160 m to 10 m (with CB) and the
  broadcast ones from medium wave to 16 m. A band of your own, watched all at
  once, works there too (CB's FM channels, say).
- `--ppm` matters more than ever: the dongle is working at 125 to 155 MHz,
  where a cheap one's 60 parts per million is 8 to 10 kHz. Get it roughly
  right first. Broadcast stations can't tell you by themselves: they're every
  5 kHz, so one that's 10 kHz out looks exactly on a channel (the first
  set-up of this was 10 kHz out for an evening that way). Use something whose
  frequency is known outright: FT8 is always there on 14.074 MHz by day and
  7.074 by night, a busy 3 kHz of warbling that starts and stops every
  15 seconds; if it's found at 14.065 instead, everything is 9 kHz low, and
  at 139 MHz that's 65 ppm.
- `--trim` is the last few hundred hertz, what the oscillator and the dongle
  are still out by between them. To measure it, listen in upper sideband
  1 kHz below a broadcast station's frequency (`?freq=9.459&mode=usb` for one
  on 9.460): its carrier is a steady tone, and however far that is above
  1000 Hz is what to add to the trim. Sideband wants it within a hundred
  hertz or so; AM doesn't care. A dongle without a temperature-compensated
  crystal moves by a few hundred hertz as it warms up, so measure it after
  half an hour's listening.
- `--gain`: a long aerial brings megawatt broadcast stations, and the dongle
  has eight bits. If the strongest are clipping, turn it down.

**AM below 30 MHz is tuned like a rig** (a broadcast station with the
waterfall round it, 9 kHz wide, no squelch), on any receiver; above it AM is
still a channel with a squelch, as airband wants. `/status` says what can be
tuned (`"tunable": [lowest, highest]` in Hz) and, with one, the
`"upconverter"`.

One dongle per receiver: for VHF and short wave at once that's two dongles,
on two computers for now.

## What else it answers

- `GET /status`: what it's receiving, who has the air (`on_air`), which
  channels are open now (`active`) and which were heard lately (`heard`).
On a Sleep Radio's desktop the receiver opens as a rig -- frequency, meter,
scope and waterfall, click to tune -- which is what `/spectrum`, `/hold`,
`/skip` and `/bands` below are for.

- `GET /bands`: the bands and their channels.
- `POST /skip {"freq": HZ, "on": true}`: leave a channel out of its band (kept in
  `/var/lib/sleepradio-receiver/skip.json`); `/status` lists them as `skipping`.
- `POST /bands {"name": "PMR 446", "from": 446.00625, "to": 446.19375, "step": 12.5}`:
  a band of your own -- a stretch of spectrum (MHz) with a channel every step
  (kHz); `"mode": "am"` for airband, `"priority": MHZ` for the channel that
  takes over from the others, or `"channels": [{"freq", "name"}]` to name them
  one by one. Up to 64 channels within the 1.9 MHz the dongle hears at once.
  Replies with its `id`; with an `"id"` it changes that band, and
  `{"id": ..., "remove": true}` removes it. Kept in
  `/var/lib/sleepradio-receiver/bands.json`. On a Sleep Radio's desktop this
  is *New band…* in the Receiver folder.
- `GET /spectrum?since=N`: for a waterfall. The spectrum of everything the
  dongle hears (2.4 MHz round `centre`), ten rows a second, the last five
  seconds kept: `rows` is `[[number, base64], ...]` after number `since`, each
  row one byte per 4.7 kHz from the lowest frequency up (0 = `zero_db`,
  `per_db` steps per dB). It comes from the FFT rtl_airband already does to
  find its channels -- `rtl_airband_spectrum.py` adds a few lines to it before
  it's built, which `install.sh` does -- so it costs next to nothing (measured
  on a Pi 2 with 48 channels: 94% of one core without, 97% with). Only while a
  band or a narrow FM / AM frequency is being received.
- `/audio?band=2m&hold=145.5`: the band, staying on that one of its channels
  -- what a single channel is as a station (a Sleep Radio's button, say). The
  whole band is still received, so moving to another channel, or letting go
  (`/audio?band=2m`, or `POST /hold`), is instant. A channel that's being
  skipped isn't in the band, so it's received on its own instead.
- `POST /hold {"freq": HZ}`: listen to that one channel of the band, whoever
  else speaks (instant: every channel is already being received);
  `{"freq": null}` lets go. `/status` says which as `hold`.
- `POST /select {"band": "2m"}` (or `{"freq": 145.5, "mode": "nfm"}`): change
  what it receives without listening.
- Your own bands: `/etc/sleepradio-receiver/bands.json`,
  `{"pmr": {"name": "PMR446", "channels": [{"freq": 446006250, "name": "Channel 1"}]}}`
  (add `"priority": true` to a channel). Its channels must fit in 1.9 MHz.

The audio is a WAV stream (16-bit mono; 16 kHz, 32 kHz for broadcast FM,
12 kHz for sideband and Morse) with ICY titles.

## Notes

- A Pi 2 keeps up with 48 channels at `fft_size = 512` (one core at about
  90%); 1024 and 2048 drop samples on it. Sideband and Morse take about 40%
  of one core.
- The built-in 2 m band names the repeaters near Guisborough (the first
  radio's home); elsewhere, list your own in `bands.json` under `"2m"`.
- Receive only. What you may listen to depends on where you are: in the UK,
  amateur and broadcast are for anyone; marine and airband are not general
  listening.
- Tests: `python -m pytest receiver/tests` (no dongle needed).
