import json
import stat
from pathlib import Path

import pytest

from sleepradiopi import wifi


def test_psk_matches_the_ieee_test_vector() -> None:
    # IEEE 802.11i-2004, H.4.1: passphrase "password", SSID "IEEE"
    assert wifi.psk("IEEE", "password") == "f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e"


def test_saved_networks(tmp_path: Path) -> None:
    f = tmp_path / "wifi.json"
    wifi.add_network("Home", "correct horse", f)
    wifi.add_network("Cafe", "", f)                          # open
    data = wifi.add_network("Home", "new password!", f)      # re-added: to the top, key updated
    assert [n["ssid"] for n in data["networks"]] == ["Home", "Cafe"]
    assert data["networks"][0]["psk"] == wifi.psk("Home", "new password!")
    assert "correct horse" not in f.read_text() and "new password" not in f.read_text()
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert [n["ssid"] for n in wifi.remove_network("Cafe", f)["networks"]] == ["Home"]
    assert wifi.load(f)["hotspot"] == wifi.HOTSPOT
    for ssid, pw in (("", "password1"), ("x" * 33, "password1"), ("ok", "short"), ("ok", "p" * 64)):
        with pytest.raises(ValueError):
            wifi.add_network(ssid, pw, f)
    with pytest.raises(ValueError):
        wifi.set_hotspot("Mine", "", f)                        # the hotspot needs a password


def test_config_files() -> None:
    conf = wifi.extra_conf([{"ssid": "Home \"quoted\"", "psk": "a" * 64}, {"ssid": "Cafe", "open": True},
                            {"ssid": "Broken", "psk": "not-hex"}])
    assert f"ssid={'Home \"quoted\"'.encode().hex()}" in conf and "priority=100" in conf and f"psk={'a' * 64}" in conf
    assert "key_mgmt=NONE" in conf and "priority=99" in conf and "Broken".encode().hex() not in conf
    h = wifi.hostapd_conf("SleepRadio-Setup", "sleepradio")
    assert "ssid=SleepRadio-Setup" in h and "wpa_passphrase=sleepradio" in h and "wpa=2" in h
    d = wifi.dnsmasq_conf()
    assert "dhcp-range=192.168.4.10,192.168.4.100" in d and "address=/#/192.168.4.1" in d


def test_card_networks(tmp_path: Path) -> None:
    conf = tmp_path / "wpa.conf"
    conf.write_text('ctrl_interface=/run/wpa_supplicant\nnetwork={\n\tssid="CHIGLEY"\n\tpsk="x"\n}\n')
    assert wifi.card_networks(conf) == ["CHIGLEY"]


class FakePi:
    """Stands in for the shell: records commands, answers wpa_cli/iw."""

    def __init__(self):
        self.cmds = []
        self.connected_to = None          # ssid when "in range"
        self.hotspot_clients = False
        self.now = 0.0

    def sh(self, *cmd, check=False, timeout=20):
        self.cmds.append(cmd)
        if cmd[:3] == ("wpa_cli", "-i", "wlan0") and cmd[3] == "status":
            return (f"wpa_state=COMPLETED\nssid={self.connected_to}\nip_address=192.168.1.20\n"
                    if self.connected_to else "wpa_state=SCANNING\n")
        if cmd[:3] == ("iw", "dev", "wlan0") and cmd[3] == "station":
            return "Station aa:bb" if self.hotspot_clients else ""
        if cmd[:4] == ("wpa_cli", "-i", "wlan0", "scan_results"):
            return "bssid / frequency / signal level / flags / ssid\nx\t2412\t-40\t[WPA2]\tHome\ny\t2437\t-70\t[ESS]\tCafe\n"
        return ""

    def clock(self):
        return self.now

    def sleep(self, s):
        self.now += s


def _manager(tmp_path, pi):
    base = tmp_path / "wpa.conf"
    base.write_text('network={\n\tssid="CHIGLEY"\n}\n')
    run = tmp_path / "run"
    run.mkdir()
    return wifi.Manager(sh=pi.sh, base_conf=base, networks=tmp_path / "wifi.json", run=run,
                        clock=pi.clock, sleep=pi.sleep), run


def test_joins_a_saved_network(tmp_path: Path) -> None:
    pi = FakePi()
    pi.connected_to = "CHIGLEY"
    m, run = _manager(tmp_path, pi)
    m.start()
    st = wifi.status(run)
    assert st["mode"] == "station" and st["ssid"] == "CHIGLEY" and st["ip"] == "192.168.1.20"
    started = [c for c in pi.cmds if c[0] == "wpa_supplicant"][0]
    assert "-I" in started and str(run / "wifi-extra.conf") in started
    assert not any(c[0] == "hostapd" for c in pi.cmds)


def _run_for(m, pi, seconds):
    end = pi.now + seconds
    while pi.now < end:
        pi.sleep(2)
        m.step()


def test_no_network_in_range_makes_a_hotspot_then_retries(tmp_path: Path) -> None:
    pi = FakePi()
    m, run = _manager(tmp_path, pi)
    m.start()
    assert wifi.status(run)["mode"] == "hotspot" and pi.now >= wifi.JOIN_S
    assert any(c[0] == "hostapd" for c in pi.cmds) and any(c[0] == "dnsmasq" for c in pi.cmds)
    assert ("ip", "addr", "add", "192.168.4.1/24", "dev", "wlan0") in pi.cmds
    assert "ssid=SleepRadio-Setup" in (run / "hostapd.conf").read_text()
    joins = lambda: len([c for c in pi.cmds if c[0] == "wpa_supplicant"])
    pi.hotspot_clients = True                  # someone's on it: never pulled out from under them
    before = joins()
    _run_for(m, pi, 3 * wifi.RETRY_S)
    assert joins() == before and wifi.status(run)["mode"] == "hotspot"
    pi.hotspot_clients = False                 # they've gone; still no known network
    _run_for(m, pi, wifi.RETRY_S + 5)
    assert joins() == before + 1 and wifi.status(run)["mode"] == "hotspot"   # tried, back to hotspot
    pi.connected_to = "CHIGLEY"                # the router's back
    _run_for(m, pi, wifi.RETRY_S + 5)
    assert wifi.status(run)["mode"] == "station"


def test_page_requests(tmp_path: Path) -> None:
    pi = FakePi()
    pi.connected_to = "CHIGLEY"
    m, run = _manager(tmp_path, pi)
    m.start()
    wifi.add_network("Cafe", "", tmp_path / "wifi.json")
    wifi.ask("reload", run)
    m.step()
    assert ("wpa_cli", "-i", "wlan0", "reconfigure") in pi.cmds
    assert "Cafe".encode().hex() in (run / "wifi-extra.conf").read_text()
    wifi.ask("scan", run)
    m.step()
    assert [n["ssid"] for n in wifi.status(run)["nearby"]] == ["Home", "Cafe"]
    wifi.ask("hotspot", run)
    m.step()
    assert wifi.status(run)["mode"] == "hotspot"
    wifi.ask("bogus", run)
    m.step()                                   # ignored
    assert wifi.status(run)["mode"] == "hotspot"


def test_losing_the_network_makes_a_hotspot(tmp_path: Path) -> None:
    pi = FakePi()
    pi.connected_to = "CHIGLEY"
    m, run = _manager(tmp_path, pi)
    m.start()
    pi.connected_to = None
    _run_for(m, pi, wifi.LOST_S - 10)
    assert wifi.status(run)["mode"] == "station"          # a short drop: wait it out
    pi.connected_to = "CHIGLEY"
    _run_for(m, pi, 10)
    pi.connected_to = None                                # the timer started again
    _run_for(m, pi, wifi.LOST_S + 10)
    assert wifi.status(run)["mode"] == "hotspot"


def test_hotspot_announcement_and_captive_redirect(tmp_path: Path, monkeypatch) -> None:
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    from sleepradiopi.io.announce import announcement
    from sleepradiopi.web.server import make_handler

    text = announcement([("wlan0", "192.168.4.1")], "sleepradiopi",
                        {"ssid": "SleepRadio-Setup", "password": "sleepradio", "ip": "192.168.4.1"})
    assert "join SleepRadio-Setup" in text and "s, l, e, e, p" in text and "one nine two, dot, one six eight, dot, four" in text
    assert "made my own" not in announcement([("wlan0", "10.0.0.2")], "sleepradiopi")

    class FakeStation:
        def status(self):
            return {"on_air": False}
    monkeypatch.setattr(wifi, "status", lambda run=None: {"mode": "hotspot"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeStation(), None, None, None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            opener.open(f"http://127.0.0.1:{httpd.server_address[1]}/generate_204")
        assert err.value.code == 302 and err.value.headers["Location"] == "http://192.168.4.1/"
        with opener.open(f"http://127.0.0.1:{httpd.server_address[1]}/api/status") as r:
            assert r.status == 200                                   # the API is untouched
    finally:
        httpd.shutdown()
