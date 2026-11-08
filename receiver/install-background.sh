#!/bin/sh
# Put the background jobs (radiosondes, a 433 MHz scan) on the computer that has the dongle, over ssh. Run it
# again to update. The receiver (install.sh) goes first.
#
#   receiver/install-background.sh USER@HOST
#
# USER owns radiosonde_auto_rx (see receiver/README.md: it is built by hand under /opt/radiosonde) and runs this
# service. The settings are in /etc/sleepradio-background/options on that computer (made the first time, never
# overwritten), e.g.
#   OPTIONS="--device SRVHF001 --ppm 55 --gain 29 --sonde-window 22:45-01:30 --sonde-window 10:45-13:30"
# The receiver on the same dongle must be told about this service so it can ask for the dongle:
# add --background http://127.0.0.1:8076 to OPTIONS in /etc/sleepradio-receiver/options. The service is
# installed and ENABLED here, and started: it takes the dongle whenever nobody is listening.
set -eu
HOST=${1:?usage: receiver/install-background.sh USER@HOST}
HERE=$(cd "$(dirname "$0")" && pwd)
RUNAS=${HOST%@*}

scp -q "$HERE/sleepradio_background.py" "$HERE/sleepradio-background.service" "$HOST:/tmp/"
ssh "$HOST" RUNAS="$RUNAS" sh -s <<'REMOTE'
set -eu
command -v rtl_433 >/dev/null || { sudo apt-get update -qq; sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq rtl-433; }
sudo install -d /opt/sleepradio-receiver /etc/sleepradio-background
sudo install -m 644 /tmp/sleepradio_background.py /opt/sleepradio-receiver/sleepradio_background.py
[ -e /etc/sleepradio-background/options ] || echo 'OPTIONS="--sonde-window 22:45-01:30 --sonde-window 10:45-13:30"' | sudo tee /etc/sleepradio-background/options >/dev/null
sed "s/@USER@/$RUNAS/" /tmp/sleepradio-background.service | sudo tee /etc/systemd/system/sleepradio-background.service >/dev/null
rm -f /tmp/sleepradio_background.py /tmp/sleepradio-background.service
sudo systemctl daemon-reload
sudo systemctl enable -q sleepradio-background
sudo systemctl restart sleepradio-background
sleep 2
systemctl is-active sleepradio-background
REMOTE
echo "installed: http://${HOST#*@}:8076/status"
