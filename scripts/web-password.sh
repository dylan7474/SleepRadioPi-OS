#!/bin/bash
# Set or clear the radio's web page password from the PC, over ssh:
#
#   scripts/web-password.sh clear|set|status [ssh-host]   (default: sleepradiopi-os)
#
# "clear" is the way back in if the password is forgotten.
set -euo pipefail
ACTION=${1:-status} HOST=${2:-sleepradiopi-os}
case "$ACTION" in clear|set|status) ;; *) sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;; esac
exec ssh -t -o ConnectTimeout=5 "$HOST" sleepradio-password "$ACTION"
