#!/bin/bash
# Switch a running Pi's speaker between mono (one speaker, or both playing
# the same) and stereo, then restart the station so it takes effect.
#
#   scripts/speaker-mode.sh               show the current mode
#   scripts/speaker-mode.sh mono|stereo [ssh-host]   (default: sleepradiopi-os)
#
# It sets speaker_mono in /data/radio/.config/sleepradiopi/config.json. For a
# new card, provision-media.sh --mono / --stereo does the same.
set -euo pipefail

MODE=${1:-} HOST=${2:-sleepradiopi-os}
case "$MODE" in
	mono) MONO=True ;;
	stereo) MONO=False ;;
	"") MONO= ;;
	*) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
pi() { ssh -o ConnectTimeout=5 -o BatchMode=yes "$HOST" "$@"; }
CONF=/data/radio/.config/sleepradiopi/config.json

if [ -z "$MONO" ]; then
	pi "python3 -c 'import json; print(\"mono\" if json.load(open(\"$CONF\")).get(\"speaker_mono\") else \"stereo\")'"
	exit
fi

pi "python3 - <<'PY'
import json, os
conf = json.load(open('$CONF'))
conf['speaker_mono'] = $MONO
with open('$CONF.tmp', 'w') as f:
    json.dump(conf, f, indent=2)
    f.write('\n')
os.replace('$CONF.tmp', '$CONF')
PY
chown radio:radio $CONF && sync
# init restarts the station 5 s after it exits. The pattern is bracketed so
# this ssh shell's own command line doesn't match it.
kill \$(ps -o pid,args | awk '/python3 -u -m sleepradiopi.mai[n]/{print \$1}') 2>/dev/null || true"
echo "speaker set to $MODE; the station is restarting (back in about 20 s)"
