#!/bin/bash
# Build the voice decoder that rooms need, and put it on a radio.
#
#   scripts/build-voice-decoder.sh [ssh-host]       (no host: just build it)
#
# A room (radio amateurs' digital voice: see app/sleepradiopi/playback/room.py)
# carries speech in the AMBE+2 codec. Sleep Radio does not contain a decoder
# for it, and none is in the releases: the codec is patented (by Digital Voice
# Systems, Inc.), and whether its patents still apply where you are, to what
# you're doing, is a question this project can't answer for you. So the
# decoder is a separate thing you add yourself, if you decide to.
#
# This script fetches mbelib -- an open implementation, ISC licence, which
# carries its own patent notice: read it in the fetched README -- from its
# own home, builds it for the radio with the toolchain the image was built
# with (so run `make` once first), and, given a host, copies the result to
# the radio's data partition:
#
#     /data/radio/decoders/libmbe.so
#
# That's outside the system image, so it survives updates and is never part
# of one. The radio loads it when a room is next tuned in; Find a room on the
# desktop says whether it's there. To take it away again: delete that file.
set -euo pipefail
cd "$(dirname "$0")/.."

REPO=https://github.com/szechyjs/mbelib.git
COMMIT=9a04ed5c78176a9965f3d43f7aa1b1f5330e771f      # (2019-05-29: the last change to it)
CC=output/host/bin/aarch64-linux-gcc
OUT=output/voice-decoder

[ -x "$CC" ] || { echo "no toolchain at $CC: build the image first (make)"; exit 1; }
mkdir -p "$OUT"
if [ ! -d "$OUT/mbelib/.git" ]; then
	git clone -q "$REPO" "$OUT/mbelib"
fi
git -C "$OUT/mbelib" checkout -q "$COMMIT"
echo "mbelib $(git -C "$OUT/mbelib" rev-parse --short HEAD), from $REPO"
echo "its patent notice:"
sed -n '/PATENT NOTICE/,/^$/p' "$OUT/mbelib/README.md" | sed 's/^/    /'
( cd "$OUT/mbelib" && "$OLDPWD/$CC" -O2 -shared -fPIC -I. ./*.c -o ../libmbe.so -lm )
echo "built $OUT/libmbe.so ($(stat -c %s "$OUT/libmbe.so") bytes)"

HOST=${1:-}
if [ -z "$HOST" ]; then
	echo "to put it on a radio: scripts/build-voice-decoder.sh sleepradiopi-os"
	exit 0
fi
ssh "$HOST" 'mkdir -p /data/radio/decoders'
scp -q "$OUT/libmbe.so" "$HOST:/data/radio/decoders/libmbe.so"
ssh "$HOST" 'chown -R radio:radio /data/radio/decoders && chmod 644 /data/radio/decoders/libmbe.so && ls -la /data/radio/decoders/libmbe.so'
echo "on $HOST: rooms can be heard from the next one tuned in."
