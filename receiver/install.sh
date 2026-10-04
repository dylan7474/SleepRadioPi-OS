#!/bin/sh
# Put the Sleep Radio receiver on the computer that has the dongle (Debian /
# Raspberry Pi OS), over ssh, and start it. Run it again to update.
#
#   receiver/install.sh USER@HOST [--disable-openwebrx]
#
# The dongle's own settings go in /etc/sleepradio-receiver/options on that
# computer (made the first time, never overwritten), e.g.
#   OPTIONS="--ppm 55 --gain 29"
# --ppm is its frequency error (OpenWebRX's "ppm" setting, or measure it with
# kal or rtl_test -p): get it wrong and every narrow FM channel is off tune.
# Then: sudo systemctl restart sleepradio-receiver
#
# USER needs sudo. The first time, rtl_airband is built there from source with
# narrow FM (about five minutes on a Pi 2); after that only the service is
# replaced. One program can use a dongle at a time, so anything else that
# holds it must be stopped: --disable-openwebrx stops OpenWebRX and keeps it
# from starting at boot (it isn't removed: `sudo systemctl enable --now
# openwebrx` brings it back, after `sudo systemctl disable --now
# sleepradio-receiver`).
set -eu
HOST=${1:?usage: receiver/install.sh USER@HOST [--disable-openwebrx]}
OWRX=${2:-}
HERE=$(cd "$(dirname "$0")" && pwd)

scp -q "$HERE/sleepradio_receiver.py" "$HERE/sleepradio-receiver.service" "$HOST:/tmp/"
ssh "$HOST" OWRX="$OWRX" sh -s <<'REMOTE'
set -eu
if [ ! -x /usr/local/bin/rtl_airband ]; then
	echo "building rtl_airband (a few minutes) ..."
	sudo apt-get update -qq
	sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential cmake pkg-config git rtl-sdr \
		libmp3lame-dev libshout3-dev libconfig++-dev libfftw3-dev librtlsdr-dev
	B=$HOME/rtl_airband-build
	[ -d "$B" ] || git clone -q --depth 1 --branch v5.4.2 https://github.com/rtl-airband/RTLSDR-Airband.git "$B"
	mkdir -p "$B/build" && cd "$B/build"
	cmake -DPLATFORM=native -DNFM=ON -DRTLSDR=ON -DSOAPYSDR=OFF -DMIRISDR=OFF -DPULSEAUDIO=OFF \
		-DCMAKE_BUILD_TYPE=Release .. >/dev/null
	make -j3 >/dev/null
	sudo install -m 755 src/rtl_airband /usr/local/bin/rtl_airband
fi
if [ "$OWRX" = "--disable-openwebrx" ] && systemctl list-unit-files openwebrx.service >/dev/null 2>&1; then
	sudo systemctl disable --now openwebrx 2>/dev/null || true
	echo "OpenWebRX stopped and disabled (not removed)"
fi
sudo install -d /opt/sleepradio-receiver /etc/sleepradio-receiver
sudo install -m 644 /tmp/sleepradio_receiver.py /opt/sleepradio-receiver/sleepradio_receiver.py
[ -e /etc/sleepradio-receiver/options ] || echo 'OPTIONS=""' | sudo tee /etc/sleepradio-receiver/options >/dev/null
sudo install -m 644 /tmp/sleepradio-receiver.service /etc/systemd/system/sleepradio-receiver.service
rm -f /tmp/sleepradio_receiver.py /tmp/sleepradio-receiver.service
sudo systemctl daemon-reload
sudo systemctl enable -q sleepradio-receiver
sudo systemctl restart sleepradio-receiver
sleep 2
systemctl is-active sleepradio-receiver
REMOTE
echo "installed: http://${HOST#*@}:8074/"
