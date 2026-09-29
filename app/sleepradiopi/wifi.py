"""Wi-Fi: saved networks, and a hotspot when none of them is in range.

The network(s) in /boot/wpa_supplicant.conf (edited on a PC) always count;
networks added on the web page are kept in ~/wifi.json (the station owns it)
as the WPA key worked out from the password -- never the password itself --
and given to wpa_supplicant as an extra config file (-I). wpa_supplicant
tries every saved network it can see; the first on the list wins a tie.

A radio that can't reach its network -- at start-up, or after a drop (a
router restarting, a signal fading in the night) -- keeps quietly trying to
reconnect, for as long as it takes: a nudge (wpa_cli reassociate) every
RECONNECT_S, backing off to RECONNECT_MAX_S, and a full restart of the Wi-Fi
every RESTART_S in case it's stuck. It never switches to its own network by
itself (that could wake someone at night, and a router reboot shouldn't
change anything): the hotspot -- "SleepRadio-Setup" / "sleepradio" unless
changed, at http://192.168.4.1, whose DNS answers every name with the radio
so phones offer to open the page -- only comes on when asked for (the web
page, or a settings reset), or on a radio with no network set up at all.
While it's a hotspot with nobody on it, it tries the saved networks again
every RETRY_S.

The manager runs as root (started by S35wifi):

    python3 -m sleepradiopi.wifi manager

The station asks it for things by writing REQUEST ({"action": "reload" |
"scan" | "hotspot" | "station"}) and reads STATUS.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)

IFACE = "wlan0"
BASE_CONF = Path("/boot/wpa_supplicant.conf")
NETWORKS = Path("/data/radio/wifi.json")
RUN = Path("/run/sleepradiopi")
EXTRA_CONF = RUN / "wifi-extra.conf"
REQUEST, STATUS = RUN / "wifi-request", RUN / "wifi-status"
HOTSPOT_IP = "192.168.4.1"
HOTSPOT = {"ssid": "SleepRadio-Setup", "password": "sleepradio"}
JOIN_S, RETRY_S = 45, 300
RECONNECT_S, RECONNECT_MAX_S, RESTART_S = 30, 120, 600


# --- the saved networks (station side) --------------------------------------------------

def psk(ssid: str, password: str) -> str:
    """The WPA key wpa_supplicant uses: PBKDF2(password, ssid), as wpa_passphrase."""
    return hashlib.pbkdf2_hmac("sha1", password.encode(), ssid.encode(), 4096, 32).hex()


def check_network(ssid, password) -> None:
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode()) <= 32:
        raise ValueError("a network name is 1-32 characters")
    if not isinstance(password, str) or (password and not 8 <= len(password) <= 63):
        raise ValueError("a Wi-Fi password is 8-63 characters (or none, for an open network)")


def load(path: Path = NETWORKS) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    nets = [n for n in data.get("networks", []) if isinstance(n, dict) and isinstance(n.get("ssid"), str)]
    hotspot = data.get("hotspot") if isinstance(data.get("hotspot"), dict) else {}
    return {"networks": nets, "hotspot": {**default_hotspot(path), **hotspot}}


def default_hotspot(networks: Path = NETWORKS) -> dict:
    """The set-up network, named after the radio ("Phonosphere-Setup") unless changed."""
    from sleepradiopi.config import brand
    name = brand.name_from_config(networks.parent / ".config" / "sleepradiopi" / "config.json") \
        if networks == NETWORKS else brand.name
    return {**HOTSPOT, "ssid": brand.hotspot_ssid(name)}


def save(data: dict, path: Path = NETWORKS) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, 0o600)                      # the keys are as good as passwords
    os.replace(tmp, path)


def add_network(ssid: str, password: str, path: Path = NETWORKS) -> dict:
    """Add (or update) a network; it goes to the top of the list."""
    check_network(ssid, password)
    data = load(path)
    entry = {"ssid": ssid, "psk": psk(ssid, password)} if password else {"ssid": ssid, "open": True}
    data["networks"] = [entry] + [n for n in data["networks"] if n["ssid"] != ssid]
    save(data, path)
    return data


def remove_network(ssid: str, path: Path = NETWORKS) -> dict:
    data = load(path)
    data["networks"] = [n for n in data["networks"] if n["ssid"] != ssid]
    save(data, path)
    return data


def set_hotspot(ssid: str, password: str, path: Path = NETWORKS) -> dict:
    check_network(ssid, password)
    if len(password) < 8:
        raise ValueError("the hotspot needs a password of 8-63 characters")
    data = load(path)
    data["hotspot"] = {"ssid": ssid, "password": password}
    save(data, path)
    return data


def card_networks(conf: Path = BASE_CONF) -> list[str]:
    """The networks set up on the card (/boot/wpa_supplicant.conf), by name."""
    try:
        return re.findall(r'^\s*ssid="([^"]*)"', conf.read_text(), re.M)
    except OSError:
        return []


# --- config files (root side) --------------------------------------------------------------

def extra_conf(networks: list[dict]) -> str:
    """wpa_supplicant network blocks; SSIDs in hex so any name is safe."""
    out = []
    for i, n in enumerate(networks):
        lines = [f"\tssid={n['ssid'].encode().hex()}", f"\tpriority={100 - i}"]
        if n.get("open"):
            lines.append("\tkey_mgmt=NONE")
        elif re.fullmatch(r"[0-9a-f]{64}", str(n.get("psk", ""))):
            lines.append(f"\tpsk={n['psk']}")
        else:
            continue
        out.append("network={\n" + "\n".join(lines) + "\n}\n")
    return "\n".join(out)


def hostapd_conf(ssid: str, password: str, channel: int = 6, country: str = "GB") -> str:
    return (f"interface={IFACE}\ndriver=nl80211\nssid={ssid}\ncountry_code={country}\n"
            f"hw_mode=g\nchannel={channel}\nauth_algs=1\nwpa=2\nwpa_key_mgmt=WPA-PSK\n"
            f"rsn_pairwise=CCMP\nwpa_passphrase={password}\n")


def dnsmasq_conf() -> str:
    base = HOTSPOT_IP.rsplit(".", 1)[0]
    return (f"interface={IFACE}\nbind-interfaces\nno-resolv\nno-hosts\n"
            f"dhcp-range={base}.10,{base}.100,255.255.255.0,1h\n"
            f"dhcp-option=3,{HOTSPOT_IP}\ndhcp-option=6,{HOTSPOT_IP}\n"
            f"address=/#/{HOTSPOT_IP}\n")         # every name -> the radio: phones open its page


# --- the manager (root) ----------------------------------------------------------------------

class Manager:
    def __init__(self, sh=None, base_conf: Path = BASE_CONF, networks: Path = NETWORKS, run: Path = RUN,
                 clock=time.monotonic, sleep=time.sleep) -> None:
        self.sh = sh or self._sh
        self.base_conf, self.networks, self.rundir = base_conf, networks, run
        self.clock, self.sleep = clock, sleep
        self.mode = "off"
        self.status = {"mode": "off"}
        self.nearby: list[dict] = []

    @staticmethod
    def _sh(*cmd: str, check: bool = False, timeout: float = 20) -> str:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("%s: %s", cmd[0], e)
            return ""
        if check and r.returncode:
            log.warning("%s failed: %s", " ".join(cmd), r.stderr.strip())
        return r.stdout

    def _write_status(self, **extra) -> None:
        data = load(self.networks)
        self.status = {"mode": self.mode, "hotspot": {"ssid": data["hotspot"]["ssid"],
                                                      "password": data["hotspot"]["password"],
                                                      "ip": HOTSPOT_IP},
                       "nearby": self.nearby, "at": time.time(), **extra}
        tmp = self.rundir / "wifi-status.tmp"
        tmp.write_text(json.dumps(self.status))
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.rundir / "wifi-status")

    def _stop_everything(self) -> None:
        for name in ("wpa_supplicant", "udhcpc", "hostapd", "dnsmasq"):
            self.sh("killall", "-q", name)
        self.sh("ip", "addr", "flush", "dev", IFACE)

    def start_station(self) -> None:
        self._stop_everything()
        (self.rundir / "wifi-extra.conf").write_text(extra_conf(load(self.networks)["networks"]))
        self.sh("ip", "link", "set", IFACE, "up")
        self.sh("iw", "dev", IFACE, "set", "power_save", "off")
        extra = self.rundir / "wifi-extra.conf"
        if self.base_conf.is_file():              # the card's networks, plus the page's
            confs = ["-c", str(self.base_conf), "-I", str(extra)]
        else:                                     # no card config: the page's alone
            extra.write_text("ctrl_interface=/run/wpa_supplicant\n" + extra.read_text())
            confs = ["-c", str(extra)]
        self.sh("wpa_supplicant", "-B", "-D", "nl80211", "-i", IFACE, *confs, "-P", "/run/wpa_supplicant.pid",
                "-f", "/var/log/wpa_supplicant.log", check=True)
        self.sh("udhcpc", "-b", "-S", "-i", IFACE, "-x", f"hostname:{os.uname().nodename}",
                "-p", f"/run/udhcpc.{IFACE}.pid")
        self.mode = "connecting"
        self._write_status()
        log.info("wifi: joining a saved network")

    def connected(self) -> dict | None:
        """{"ssid", "ip"} once associated and addressed, else None."""
        st = dict(line.split("=", 1) for line in self.sh("wpa_cli", "-i", IFACE, "status").splitlines()
                  if "=" in line)
        if st.get("wpa_state") != "COMPLETED" or not st.get("ip_address"):
            return None
        return {"ssid": st.get("ssid", ""), "ip": st["ip_address"]}

    def start_hotspot(self) -> None:
        self._stop_everything()
        spot = load(self.networks)["hotspot"]
        (self.rundir / "hostapd.conf").write_text(hostapd_conf(spot["ssid"], spot["password"]))
        (self.rundir / "dnsmasq.conf").write_text(dnsmasq_conf())
        self.sh("ip", "link", "set", IFACE, "up")
        self.sh("ip", "addr", "add", f"{HOTSPOT_IP}/24", "dev", IFACE)
        self.sh("hostapd", "-B", "-P", "/run/hostapd.pid", str(self.rundir / "hostapd.conf"), check=True)
        self.sh("dnsmasq", "-C", str(self.rundir / "dnsmasq.conf"), "-x", "/run/dnsmasq.pid", check=True)
        self.mode = "hotspot"
        self._write_status()
        log.warning("wifi: no saved network; hotspot %s is on at %s", spot["ssid"], HOTSPOT_IP)

    def have_networks(self) -> bool:
        return bool(load(self.networks)["networks"] or card_networks(self.base_conf))

    def join_or_keep_trying(self) -> dict | None:
        """Join a saved network; if none answers, keep trying in the background
        (never the hotspot, unless there's no network to try at all)."""
        got = self.join()
        if got:
            return got
        if not self.have_networks():
            self.start_hotspot()
            return None
        self._reconnecting()
        return None

    def _reconnecting(self) -> None:
        now = self.clock()
        if self.mode != "connecting" or not getattr(self, "lost_since", None):
            self.lost_since, self.nudge_every = now, RECONNECT_S
            self.next_nudge, self.next_restart = now + RECONNECT_S, now + RESTART_S
            log.warning("wifi: no network; reconnecting quietly")
        self.mode = "connecting"
        self._write_status(reconnecting=True, since=time.time() - (now - self.lost_since))

    def _keep_trying(self) -> None:
        """(connecting) A nudge now and then, a full restart now and then."""
        now = self.clock()
        if now >= self.next_restart:
            log.info("wifi: still no network after %d min; restarting the Wi-Fi", (now - self.lost_since) // 60)
            self.start_station()                  # (mode stays "connecting")
            self.next_restart = now + RESTART_S
            self.next_nudge = now + RECONNECT_S
        elif now >= self.next_nudge:
            self.sh("wpa_cli", "-i", IFACE, "reassociate")
            self.nudge_every = min(RECONNECT_MAX_S, self.nudge_every * 2)
            self.next_nudge = now + self.nudge_every
        self._write_status(reconnecting=True, since=time.time() - (now - self.lost_since))

    def hotspot_in_use(self) -> bool:
        return "Station" in self.sh("iw", "dev", IFACE, "station", "dump")

    def join(self, timeout: float = JOIN_S) -> dict | None:
        """Start station mode and wait for a connection."""
        self.start_station()
        end = self.clock() + timeout
        while self.clock() < end:
            got = self.connected()
            if got:
                self.mode = "station"
                self._write_status(**got)
                log.info("wifi: connected to %s (%s)", got["ssid"], got["ip"])
                return got
            self.sleep(1)
        return None

    def scan(self) -> None:
        if self.mode not in ("station", "connecting"):
            return
        self.sh("wpa_cli", "-i", IFACE, "scan")
        self.sleep(4)
        seen: dict[str, int] = {}
        for line in self.sh("wpa_cli", "-i", IFACE, "scan_results").splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) >= 5 and parts[4].strip():
                seen[parts[4]] = max(seen.get(parts[4], -999), int(parts[2]) if parts[2].lstrip("-").isdigit() else -999)
        self.nearby = [{"ssid": s, "signal": sig} for s, sig in sorted(seen.items(), key=lambda kv: -kv[1])][:20]

    def handle(self, action: str) -> None:
        if action == "reload":
            if self.mode in ("station", "connecting"):
                (self.rundir / "wifi-extra.conf").write_text(extra_conf(load(self.networks)["networks"]))
                self.sh("wpa_cli", "-i", IFACE, "reconfigure")
            else:
                self.join_or_keep_trying()
        elif action == "scan":
            self.scan()
        elif action == "hotspot":
            self.start_hotspot()
        elif action == "station":
            self.join_or_keep_trying()
        self._write_status(**(self.connected() or {}))

    def start(self) -> None:
        """At start-up: join a saved network (or keep trying)."""
        self.lost_since, self.retry_at = None, self.clock() + RETRY_S
        self.join_or_keep_trying()

    def step(self) -> None:
        """One turn of the loop (every 2 s): requests, and keeping connected."""
        req = self.run_dir_request()
        if req:
            self.handle(req)
            self.retry_at = self.clock() + RETRY_S
        if self.mode in ("station", "connecting"):
            got = self.connected()
            if got:
                if self.mode == "connecting":
                    log.info("wifi: back on %s (%s)", got["ssid"], got["ip"])
                self.mode, self.lost_since = "station", None
                if got.get("ip") != self.status.get("ip") or got.get("ssid") != self.status.get("ssid") \
                        or self.status.get("reconnecting"):
                    self._write_status(**got)
            elif self.mode == "station":
                self._reconnecting()              # dropped: keep trying, quietly
            else:
                self._keep_trying()
        elif self.mode == "hotspot" and self.clock() >= self.retry_at:
            if not self.hotspot_in_use() and self.have_networks():
                if not self.join():
                    self.start_hotspot()
            self.retry_at = self.clock() + RETRY_S

    def run(self) -> None:
        self.start()
        while True:
            self.sleep(2)
            self.step()

    def run_dir_request(self) -> str | None:
        path = self.rundir / "wifi-request"
        try:
            action = json.loads(path.read_text()).get("action")
        except (OSError, ValueError):
            return None
        path.unlink(missing_ok=True)
        return action if action in ("reload", "scan", "hotspot", "station") else None


def ask(action: str, run: Path = RUN) -> None:
    """(the station) Ask the manager for something."""
    tmp = run / "wifi-request.tmp"
    tmp.write_text(json.dumps({"action": action}))
    os.replace(tmp, run / "wifi-request")


def status(run: Path = RUN) -> dict:
    try:
        return json.loads((run / "wifi-status").read_text())
    except (OSError, ValueError):
        return {"mode": "unknown"}


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if (argv or sys.argv[1:])[:1] == ["manager"]:
        RUN.mkdir(parents=True, exist_ok=True)
        Manager().run()
    else:
        print("usage: python3 -m sleepradiopi.wifi manager", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
