"""Find a receiver: the public directory, searched by words and by distance (no network)."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sleepradiopi.playback import receiver as rx, receivers_dir as rd
from sleepradiopi.web.server import make_handler


def site(label, lon, lat, *receivers):
    return {"label": label, "location": {"type": "Point", "coordinates": [lon, lat]},
            "receivers": [{"label": n, "type": t, "url": u, "version": "1.2"} for n, t, u in receivers]}


SITES = [site("Yarm, England", -1.35, 54.5, ("0-30 MHz SDR, G8AOE | Yarm", "KiwiSDR", "http://kiwi.example:8073")),
         site("Bradford, England", -1.75, 53.8, ("M0VOB <b>Bradford</b> &amp; district", "OpenWebRX", "http://owrx.example:91/"),
              ("A WebSDR", "WebSDR", "http://websdr.example/")),
         site("Tokyo, Japan", 139.7, 35.7, ("JA1 Tokyo", "KiwiSDR", "http://tokyo.example:8073/"),
              ("JA1 Tokyo again", "KiwiSDR", "http://tokyo.example:8073/"), ("no address", "KiwiSDR", "ftp://x/")),
         {"label": "Nowhere", "receivers": [{"label": "lost", "type": "KiwiSDR", "url": "http://lost.example/"}]}]
PAGE = f"<script>\n var receivers = {json.dumps(SITES)};\n var map;</script>"


def padded(n=20):
    """The page with enough receivers for refresh() to believe it."""
    more = [site(f"Place {i}", 10 + i, 10, (f"Kiwi {i}", "KiwiSDR", f"http://k{i}.example:8073/")) for i in range(n)]
    return f"var receivers = {json.dumps(SITES + more)};"


def test_the_map_page_becomes_a_list_of_playable_receivers() -> None:
    found = rd.parse_map(PAGE)
    assert [r["url"] for r in found] == ["http://kiwi.example:8073/", "http://owrx.example:91/", "http://tokyo.example:8073/"]
    assert found[1] == {"name": "M0VOB Bradford & district", "place": "Bradford, England", "url": "http://owrx.example:91/",
                        "kind": "owrx", "lat": 53.8, "lon": -1.75, "version": "1.2"}
    with pytest.raises(ValueError, match="changed"):
        rd.parse_map("<html>a new design</html>")


def test_search_by_words_and_nearest_first(tmp_path: Path) -> None:
    fetched = []
    book = rd.ReceiverDirectory(tmp_path / "rx.json", fetch=lambda u: fetched.append(u) or padded(), clock=lambda: 1000.0)
    assert [r["name"] for r in book.search("tokyo")] == ["JA1 Tokyo"] and fetched == [rd.SOURCE]
    assert [r["url"] for r in book.search("england", kind="owrx")] == ["http://owrx.example:91/"]
    near = book.search("", near=(54.54, -1.05), limit=3)
    assert [r["place"] for r in near[:2]] == ["Yarm, England", "Bradford, England"] and near[0]["km"] == 20
    assert "km" not in book.search("yarm")[0]
    assert rd.km_between(51.5, -0.13, 48.86, 2.35) == pytest.approx(344, abs=3)       # London to Paris

    # kept in a file: the next start has it without asking again
    again = rd.ReceiverDirectory(tmp_path / "rx.json", fetch=lambda u: 1 / 0, clock=lambda: 2000.0)
    assert again.search("yarm")[0]["kind"] == "kiwi" and again.status() == {"count": 23, "age_s": 1000, "error": None}


def test_a_directory_that_cant_be_had_keeps_the_old_list_and_says_why(tmp_path: Path) -> None:
    def down(url):
        raise OSError("no route")
    book = rd.ReceiverDirectory(tmp_path / "rx.json", fetch=down)
    assert book.search("yarm") == [] and "couldn't be reached" in book.status()["error"]
    book._fetch = lambda u: PAGE                               # three receivers: not believed
    assert book.refresh() == 0 and "nearly empty" in book.error
    book._fetch = lambda u: padded()
    assert book.refresh() == 23 and book.error is None
    book._fetch = down
    assert book.refresh() == 0 and len(book.receivers) == 23


def test_a_receivers_own_word_and_only_for_one_in_the_directory(tmp_path: Path) -> None:
    book = rd.ReceiverDirectory(tmp_path / "rx.json", fetch=lambda u: padded())
    book.refresh()
    asked = []

    def get(url):
        asked.append(url)
        if url.endswith("/status"):
            return "status=active\noffline=no\nname=G8AOE &amp; co\nusers=3\nusers_max=4\nbands=0-30000000\nantenna=Long wire\n"
        return json.dumps({"receiver": {"name": "M0VOB"}, "max_clients": 5, "sdrs": [{"profiles": [
            {"name": "2m", "center_freq": 145000000, "sample_rate": 2048000}, {"name": "40m", "center_freq": 7100000, "sample_rate": 250000},
            {"name": "mis-set", "center_freq": 50313000000000, "sample_rate": 250000}]}]})

    kiwi = book.info("http://kiwi.example:8073/", fetch=get)
    assert (kiwi["title"], kiwi["users"], kiwi["users_max"], kiwi["offline"], kiwi["high_khz"]) == ("G8AOE & co", 3, 4, False, 30000)
    owrx = book.info("http://owrx.example:91/", fetch=get)
    assert [b["name"] for b in owrx["bands"]] == ["40m", "2m"] and owrx["max_clients"] == 5
    assert asked == ["http://kiwi.example:8073/status", "http://owrx.example:91/status.json"]

    with pytest.raises(ValueError, match="isn't in the directory"):          # the radio doesn't fetch what a page hands it
        book.info("http://192.168.1.1/", fetch=get)
    with pytest.raises(ValueError, match="couldn't be read"):
        book.info("http://owrx.example:91/", fetch=lambda u: "<html>")

    def dead(url):
        raise OSError("timed out")
    with pytest.raises(ValueError, match="isn't answering"):
        book.info("http://kiwi.example:8073/", fetch=dead)


def test_the_links_the_page_makes_are_ones_the_radio_plays() -> None:
    assert rx.parse("http://kiwi.example:8073/?f=7150.00lsb") == {"kind": "kiwi", "base": "http://kiwi.example:8073/", "freq": 7150000, "mode": "lsb"}
    assert rx.parse("http://kiwi.example:8073/?f=145500.00nbfm")["mode"] == "nbfm"
    assert rx.parse("http://owrx.example:91/#freq=145500000,mod=nfm") == {"kind": "owrx", "base": "http://owrx.example:91/", "freq": 145500000, "mode": "nfm"}


def test_web_api(tmp_path: Path, monkeypatch) -> None:
    book = rd.ReceiverDirectory(tmp_path / "rx.json", fetch=lambda u: padded())
    monkeypatch.setattr(rd, "_shared", book)
    monkeypatch.setattr(rd, "_get_text", lambda url, timeout=8.0: "users=1\nusers_max=4\n")
    conf = tmp_path / "config.json"
    conf.write_text("{}")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None, None, None, conf))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path):
        try:
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        code, reply = call("/api/receivers/search?q=&kind=kiwi&near=54.54,-1.05")
        assert code == 200 and reply["results"][0]["url"] == "http://kiwi.example:8073/" and reply["directory"]["count"] == 23
        assert all(r["kind"] == "kiwi" for r in reply["results"])
        code, reply = call("/api/receivers/search?near=north")
        assert code == 400
        code, reply = call("/api/receivers/search?near=95,0")
        assert code == 400 and "LATITUDE" in reply["error"]
        code, reply = call("/api/receivers/info?url=http%3A%2F%2Fkiwi.example%3A8073%2F")
        assert code == 200 and reply["users_max"] == 4
        code, reply = call("/api/receivers/info?url=http%3A%2F%2F127.0.0.1%2F")
        assert code == 400 and "directory" in reply["error"]
    finally:
        httpd.shutdown()
