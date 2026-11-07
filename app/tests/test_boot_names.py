"""The image's name-lookup set-up: an empty resolv.conf before avahi starts, and mDNS for *.local only (board/.../S02resolvconf,
etc/nsswitch.conf, the defconfig's nss-mdns)."""
import os
import subprocess
from pathlib import Path

OVERLAY = Path(__file__).parent.parent.parent / "board" / "sleepradiopi" / "rootfs-overlay"
DEFCONFIG = Path(__file__).parent.parent.parent / "configs" / "sleepradiopi_zero2w_defconfig"
S02 = OVERLAY / "etc" / "init.d" / "S02resolvconf"


def _run(tmp_path: Path, arg: str = "start") -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(S02), arg], env={**os.environ, "RESOLV_CONF": str(tmp_path / "resolv.conf")}, capture_output=True, text=True)


def test_an_empty_resolv_conf_is_there_before_avahi_and_a_lease_already_there_is_kept(tmp_path: Path) -> None:
    assert os.access(S02, os.X_OK)
    assert "S02resolvconf" < "S35wifi" < "S50avahi-daemon"          # (init runs the scripts in name order: before the network, before avahi's)
    f = tmp_path / "resolv.conf"
    assert _run(tmp_path).returncode == 0 and f.exists() and f.read_text() == ""
    f.write_text("search dylanjones.org # wlan0\nnameserver 192.168.50.2 # wlan0\n")
    assert _run(tmp_path).returncode == 0
    assert f.read_text() == "search dylanjones.org # wlan0\nnameserver 192.168.50.2 # wlan0\n"        # (never touched once it is there)
    assert _run(tmp_path, "stop").returncode == 0 and _run(tmp_path, "nonsense").returncode == 1


def test_the_dhcp_script_works_on_the_empty_file(tmp_path: Path) -> None:
    """The lines udhcpc's script uses on resolv.conf (drop this interface's lines, then add the lease's) on an empty file."""
    f = tmp_path / "resolv.conf"
    _run(tmp_path)
    script = (f'RESOLV_CONF={f}; interface=wlan0; TMPFILE=$(mktemp); grep -vE "# $interface\\$" $RESOLV_CONF > $TMPFILE; '
              f'cat $TMPFILE > $RESOLV_CONF; rm -f $TMPFILE; echo "search dylanjones.org # $interface" >> $RESOLV_CONF; echo "nameserver 192.168.50.2 # $interface" >> $RESOLV_CONF')
    r = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0 and r.stderr == "" and f.read_text() == "search dylanjones.org # wlan0\nnameserver 192.168.50.2 # wlan0\n"


def test_names_ending_local_ask_mdns_and_the_rest_ask_dns_only() -> None:
    hosts = [l for l in (OVERLAY / "etc" / "nsswitch.conf").read_text().splitlines() if l.startswith("hosts:")]
    assert hosts == ["hosts:          files mdns4_minimal [NOTFOUND=return] dns"]          # (no trailing mdns4: a dead name would wait for it)
    text = DEFCONFIG.read_text()
    assert "BR2_PACKAGE_NSS_MDNS=y" in text and "BR2_PACKAGE_AVAHI_DAEMON=y" in text and "GLIBC" in text      # (nss-mdns needs avahi and glibc)
