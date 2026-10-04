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

## What else it answers

- `GET /status`: what it's receiving, who has the air (`on_air`), which
  channels are open now (`active`) and which were heard lately (`heard`).
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
- `POST /select {"band": "2m"}` (or `{"freq": 145.5, "mode": "nfm"}`): change
  what it receives without listening.
- Your own bands: `/etc/sleepradio-receiver/bands.json`,
  `{"pmr": {"name": "PMR446", "channels": [{"freq": 446006250, "name": "Channel 1"}]}}`
  (add `"priority": true` to a channel). Its channels must fit in 1.9 MHz.

The audio is a WAV stream (16-bit mono; 16 kHz, or 32 kHz for broadcast FM)
with ICY titles.

## Notes

- A Pi 2 keeps up with 48 channels at `fft_size = 512` (one core at about
  90%); 1024 and 2048 drop samples on it.
- The built-in 2 m band names the repeaters near Guisborough (the first
  radio's home); elsewhere, list your own in `bands.json` under `"2m"`.
- Receive only. What you may listen to depends on where you are: in the UK,
  amateur and broadcast are for anyone; marine and airband are not general
  listening.
- Tests: `python -m pytest receiver/tests` (no dongle needed).
