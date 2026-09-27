# Roadmap

**Direction (2026-09-22): Broadcast Radio is the product.** The other
SleepRadio sources (local albums, audiobooks, internet radio, ambient noise
and binaural beats) are deferred and may never be built; their placeholder
modules stay in the tree until that's decided.

## Done

1. **Audio-out smoke test script** — `scripts/smoke_test_audio.py` (tone to
   the HiFiBerry). The MiniAmp's overlay is set up and it shows as card 0;
   the tone through real speakers waits on the header being soldered.
2. **Voice (TTS)** — the Android app's voice packs load unchanged via
   sherpa-onnx; `tts/worker.py` runs one voice in a recycled subprocess.
3. **Broadcast station** — ported from SleepRadio: show clock (links,
   idents, time checks), DJ scripts and 70s hooks, track selection, jingles
   (a short one opens the show; one every N tracks), loudness levelling,
   edge-silence trim, voice EQ, news bulletins (BBC RSS, :00 top stories,
   :30 softer stories, quiet hours). Time checks and bulletin time lines
   are worded just before they're spoken, from the real clock.
4. **Listen in a browser** — MP3 stream + page (tune in, volume, sleep
   timer, now playing, recent history) on port 80; systemd service.
5. **Speaker output** — `audio/speaker.py`: the show plays through aplay
   (the MiniAmp) from start-up, alongside the MP3 stream; software volume
   (the MiniAmp has none), remembered across restarts.
6. **The knob** — a rotary encoder for volume, its push switch for pause
   (`io/knob.py`, kernel input events). The only physical control: the
   display and extra buttons are dropped.
7. **Offline mode** — no time checks, time-of-day greetings or news until
   the clock has been set from the internet since power-up.
8. **Faster start** — track tags are cached; the station answers seconds
   after start-up instead of a minute.
9. **Appliance image** — a separate Buildroot image: read-only root, data
   and music partitions, pull-the-plug tested, A/B updates over Wi-Fi.

10. **The hardware** — MiniAmp fitted, two 40 mm speakers in a 3D-printed
    cabinet (stereo, or mono for a one-speaker build); software volume from
    the knob or the page.
11. **Real-time clock (software)** — a DS3231 sets the clock at boot when
    it's sensible, and is set from NTP; time checks and news work offline
    once it's fitted (the module is being wired up).
12. **The web page as the control panel** — stereo/mono, a 3-band EQ + low
    cut (the MiniAmp has none), a sleep timer that fades the speakers,
    save/load of all the settings as a file, skip, shut down, test sounds
    (sweeps, pink noise, left/right and phase checks), and a virtual knob.
13. **Knob long press** — hold 3 s: the radio reads out its network address
    (or says it isn't connected), for finding the page away from home.
14. **Artist radio and lists** — play one artist ("Beatles Radio") or your
    own named lists of artists ("Friday List on Sleep Radio").
15. **Birthdays** — the DJ wishes people a happy birthday on the day.
16. **DJ settings on the page** — voice, how often the DJ talks, 70s hooks,
    jingles and news.
17. **Tidier web page and a password** — everyday controls up front, the
    rest under Settings; an optional password, set from the page and
    resettable over ssh.
18. **Updates from GitHub** — releases made by scripts/release.sh, installed
    from the page into the spare slot; the DJ says how it's going; a new
    version that doesn't come on air undoes itself.
19. **Voices** — download the standard voice (automatically on a radio with
    none), and upload your own voice pack from the page.
20. **A wiring diagram** — `hardware/wiring/wiring.svg`: the header, MiniAmp,
    knob, RTC, speakers and power in one picture.
21. **A remote control, not a player** — listening in a browser is off unless
    switched on in Settings, so no MP3 encoder runs.
22. **A start-up sound** — a chime about 10 s after power-on, then the DJ
    saying it's warming up; the first song comes ~25 s sooner too.
23. **Album mode** — pick an album and it plays start to finish, introduced
    and back-announced by the DJ.
24. **Play next** — search the library from the page and queue songs; the
    DJ introduces them. Jingles only on the main mix.

## Next

25. **Wire the knob and the RTC** — check the knob's direction and step and
    the long press on the real switch; the RTC with no network.
26. **Wi-Fi manager** — several saved networks tried in turn at start-up,
    added from the page; a hotspot with a fixed name and password when none
    is in range.
27. **Library tools** — copying/syncing music from the desktop library.
28. **A needle VU meter** — a physical meter driven from a PWM pin, with the
    Android app's ballistics (see the appliance image's roadmap).
