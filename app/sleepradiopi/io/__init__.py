"""Physical control surface: GPIO buttons + LCD status display.

No equivalent in SleepRadio (that's the phone's Compose UI) -- this is new,
Pi-specific code. Kept isolated from playback/broadcast/audio logic so
swapping button pin-outs or LCD panel type never touches application logic.
"""
