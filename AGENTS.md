# Working on SleepRadioPi-OS

A Buildroot image + Python app for a Pi radio (see README.md). `app/` is the
radio software, `scripts/` the build/release/update tools, `hardware/` the
cases. This repo is PUBLIC: no IP addresses, keys, Wi-Fi details or personal
information in anything committed (`local/` is git-ignored for those).

## Commands

- Tests: `cd app && python -m pytest -q` (about 4 min). Run them, and
  start the program for real, before any release.
- Build the image: `make` (after changing a package, `make sleepradiopi-dirclean` first).
- Try an UNRELEASED change on a radio: `scripts/update.sh <ssh-host>`.
- Release: commit everything, then `scripts/release.sh X.Y.Z --sdcard --notes "..."`.
  It builds from the working tree, so do not edit the repo while it runs, and
  stash any uncommitted `hardware/case` work first (pop it afterwards).
- After a release, radios install the PUBLISHED release through their own
  update API (`POST /api/update/check`, then `/api/update/install`, then poll
  `/api/status` for the version). Do not build a separate image per radio.
  A radio comes back PLAYING: pause it again if it was paused.

## Rules

- Never chain commit/build/deploy after the tests without checking they passed.
- Ask before anything that makes sound on a radio.
- A radio must work with no Wi-Fi: nothing online may make it fail.
- Keep the READMEs (top level and `app/`) and ROADMAP current in the same
  commit as each feature. The project is "Sleep Radio" in all docs.
- Radio variants (themed radios, a family member's radio) are not committed
  or pushed unless asked.
- A question is not a go-ahead: answer and propose, then wait.
