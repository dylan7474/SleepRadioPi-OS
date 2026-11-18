import json
import random

import pytest

from sleepradiopi import sync as sync_mod
from sleepradiopi.sync import Sync, merge


def st(url, name=None):
    return {"name": name or url, "url": f"http://{url}", "info": ""}


def urls(stations):
    return sorted(s["url"] for s in stations)


# --- the merge itself ---

def test_merge_three_ways() -> None:
    base = [st("a"), st("b"), st("c")]
    mine = [st("a", "A mine"), st("b"), st("d")]          # edited a, deleted c, added d
    theirs = [st("a"), st("c", "C theirs"), st("e")]      # deleted b, edited c, added e
    got, counts = merge("radio_stations", base, mine, theirs)
    by = {s["url"]: s["name"] for s in got}
    assert by == {"http://a": "A mine",                   # only mine changed it: mine
                  "http://c": "C theirs",                 # I deleted it, they edited it: the edit wins
                  "http://d": "d", "http://e": "e"}       # both adds kept; b deleted by them: gone
    assert counts == {"added": 2, "changed": 0, "deleted": 1}
    assert [s["url"] for s in got][:2] == ["http://a", "http://d"]       # (my order first, then theirs' new)


def test_the_same_item_changed_on_both_ends_the_same_on_both() -> None:
    base = [st("a")]
    one, two = [st("a", "One")], [st("a", "Two")]
    assert merge("radio_stations", base, one, two)[0] == merge("radio_stations", base, two, one)[0]


def test_the_first_sync_only_adds() -> None:
    mine, theirs = [st("a"), st("b")], [st("b"), st("c")]
    got, counts = merge("radio_stations", None, mine, theirs)
    assert urls(got) == ["http://a", "http://b", "http://c"] and counts["deleted"] == 0


def test_receiver_names_merge_as_a_dict() -> None:
    got, _ = merge("receiver_names", {"http://x:1": "Old"}, {"http://x:1": "Old"}, {"http://x:1": "New", "http://y:2": "Y"})
    assert got == {"http://x:1": "New", "http://y:2": "Y"}


# --- radios, in memory: each a Sync with its own config; fetch goes straight to the other ---

class Net:
    def __init__(self, lose=lambda: False):
        self.radios = {}
        self.lose = lose                  # lose(): this adopt is taken up, but its reply is lost (a timeout)

    def fetch(self, url, body=None):
        address, _, what = url.removeprefix("http://").partition("/api/sync/")
        what, _, query = what.partition("?for=")
        r = self.radios.get(address)
        if r is None or getattr(r, "off", False):
            raise OSError("not answering")
        if what == "hello":
            return r.hello()
        if what == "state":                                    # (asked from the asking radio's own address)
            asker = next((a for a, x in self.radios.items() if x.id == query), None)
            return r.state(query, asker)
        if what == "paired":
            me = next(a for a, x in self.radios.items() if x.id == body["id"])
            r.paired_with({"id": body["id"], "name": body["name"], "address": me})
            return {"ok": True}
        if what == "unpaired":
            r.unpaired(body["id"])
            return {"ok": True}
        if what == "adopt":
            asker = next((a for a, x in self.radios.items() if x.id == body["id"]), None)
            ok = r.adopt(body["id"], body["merged"], body["was"], asker, body.get("was_media"),
                         body.get("counts"), body.get("usage"))
            if self.lose():
                raise OSError("timed out")
            return {"ok": ok}
        if what == "progress":
            return r.progress_for(query)
        if what == "media-set":
            asker = next((a for a, x in self.radios.items() if x.id == body["id"]), None)
            return {"ok": r.media_set(body["id"], body["kinds"], asker)}
        raise AssertionError(what)


def radio(tmp_path, net, address, stations=None, names=None):
    folder = tmp_path / address.replace(":", "_")
    folder.mkdir()
    conf = folder / "config.json"
    conf.write_text(json.dumps({"radio_stations": stations if stations is not None else [],
                                "receiver_names": names or {}}))
    r = Sync(conf, lambda a=address: a, 80, fetch=net.fetch)
    net.radios[address] = r
    return r


def stations_of(r):
    return json.loads(r.config_file.read_text()).get("radio_stations")


def set_stations(r, stations):
    conf = json.loads(r.config_file.read_text())
    conf["radio_stations"] = stations
    r.config_file.write_text(json.dumps(conf))


def test_two_radios_pair_and_keep_the_same(tmp_path) -> None:
    net = Net()
    one = radio(tmp_path, net, "one", [st("a"), st("b")], {"http://rx:8074": "VHF"})
    two = radio(tmp_path, net, "two", [st("b"), st("c")])
    backups = []
    one.backup = backups.append
    one.pair("two")
    assert [p["id"] for p in one.peers()] == [two.id] and [p["id"] for p in two.peers()] == [one.id]
    assert urls(stations_of(one)) == urls(stations_of(two)) == ["http://a", "http://b", "http://c"]
    assert json.loads(two.config_file.read_text())["receiver_names"] == {"http://rx:8074": "VHF"}
    assert backups and "Before syncing" in backups[0]
    set_stations(two, [s for s in stations_of(two) if s["url"] != "http://a"])      # deleted on two
    two.sync_now(one.id)
    assert urls(stations_of(one)) == ["http://b", "http://c"]                         # ...gone from one too
    one.unpair(two.id)
    assert one.peers() == [] and two.peers() == []


def test_a_merge_that_would_delete_a_lot_is_held(tmp_path) -> None:
    net = Net()
    many = [st(f"s{i}") for i in range(10)]
    one, two = radio(tmp_path, net, "one", many), radio(tmp_path, net, "two", many)
    one.pair("two")
    set_stations(two, many[:2])                                   # 8 of 10 deleted on two
    st_ = one.pull(two.id)
    assert st_["held"] and not st_["ok"] and len(stations_of(one)) == 10      # nothing deleted here yet
    one.allow_once(two.id)
    assert one.pull(two.id)["ok"] and len(stations_of(one)) == 2


def test_a_radio_that_is_off_is_said_not_crashed(tmp_path) -> None:
    net = Net()
    one, two = radio(tmp_path, net, "one"), radio(tmp_path, net, "two")
    one.pair("two")
    two.off = True
    st_ = one.pull(two.id)
    assert not st_["ok"] and "not answering" in st_["said"]


def test_a_moved_radio_is_followed_by_its_beacon(tmp_path) -> None:
    net = Net()
    one, two = radio(tmp_path, net, "one"), radio(tmp_path, net, "two")
    one.pair("two")
    one.heard({"sleepradio": sync_mod.PROTOCOL, "id": two.id, "name": "two", "port": 8080}, "10.0.0.9")
    assert one.peers()[0]["address"] == "10.0.0.9:8080"
    one.heard({"sleepradio": sync_mod.PROTOCOL, "id": "stranger", "name": "Kitchen", "port": 80}, "10.0.0.7")
    assert [s["name"] for s in one.overview()["seen"]] == ["Kitchen"]


def test_a_radio_with_a_password_does_not_pair(tmp_path) -> None:
    net = Net()
    one, two = radio(tmp_path, net, "one"), radio(tmp_path, net, "two")
    two.protected = lambda: True
    with pytest.raises(ValueError, match="password"):
        one.pair("two")


@pytest.mark.parametrize("seed", range(25))
def test_three_radios_with_random_edits_end_the_same(tmp_path, monkeypatch, seed) -> None:
    """Random adds, edits and deletes on three paired radios, synced in a random order: once they've all
    synced round, every radio has the same, and nothing added (and not deleted since) is lost."""
    monkeypatch.setattr(sync_mod, "HOLD_MIN", 10 ** 6)             # (the delete guard is tested above)
    rnd = random.Random(seed)
    net = Net()
    rs = [radio(tmp_path, net, n, [st(f"{n}{i}") for i in range(3)]) for n in ("x", "y", "z")]
    rs[0].pair("y"); rs[0].pair("z"); rs[1].pair("z")
    n = 0
    for _ in range(40):
        r = rnd.choice(rs)
        cur = stations_of(r)
        what = rnd.random()
        if what < 0.4:
            n += 1
            cur = cur + [st(f"new{n}")]
        elif what < 0.6 and cur:
            cur = [c for c in cur if c is not rnd.choice(cur)]
        elif cur:
            pick = rnd.randrange(len(cur))
            cur = [dict(c, name=f"edit{rnd.random():.3f}") if i == pick else c for i, c in enumerate(cur)]
        set_stations(r, cur)
        if rnd.random() < 0.5:
            a = rnd.choice(rs)
            peer = rnd.choice(a.peers())
            a.pull(peer["id"])
    for _ in range(3):                                            # everyone syncs round
        for a in rs:
            for p in a.peers():
                a.pull(p["id"])
    first = json.dumps(sorted(stations_of(rs[0]), key=lambda s: s["url"]))
    assert all(json.dumps(sorted(stations_of(r), key=lambda s: s["url"])) == first for r in rs)


def test_a_lost_reply_never_turns_an_add_into_a_delete(tmp_path) -> None:
    """The other radio took up a sync but its reply was lost: the two now disagree on what they last agreed.
    The next sync (from either side) must not take the other's add for a delete."""
    lose = [False]
    net = Net(lose=lambda: lose[0])
    one, two = radio(tmp_path, net, "one", [st("a")]), radio(tmp_path, net, "two", [st("a")])
    one.pair("two")
    set_stations(two, stations_of(two) + [st("b")])             # added on two
    lose[0] = True
    assert not one.pull(two.id)["ok"]                            # two took it up; one never heard back
    lose[0] = False
    two.pull(one.id)                                             # now two syncs first
    assert "http://b" in urls(stations_of(one)) and "http://b" in urls(stations_of(two))


def test_pairing_says_how_the_first_sync_went(tmp_path) -> None:
    net = Net()
    one, two = radio(tmp_path, net, "one", [st("a")]), radio(tmp_path, net, "two", [st("b")])
    r = one.pair("two")
    assert r["sync"]["ok"] and "added" in r["sync"]["said"]


def conf_of(r):
    return json.loads(r.config_file.read_text())


def set_conf(r, **kv):
    c = conf_of(r)
    c.update(kv)
    r.config_file.write_text(json.dumps(c))


def test_everything_shared_is_synced_and_a_radios_own_is_not(tmp_path) -> None:
    net = Net()
    one, two = radio(tmp_path, net, "one"), radio(tmp_path, net, "two")
    set_conf(one, profiles=[{"name": "Default", "artists": [], "all": True}, {"name": "Jazz", "artists": ["Miles Davis"]}],
             playlists=[{"name": "Faves", "tracks": [["music", "A/b.mp3"]]}],
             birthdays=[{"name": "Sam", "day": 1, "month": 2}],
             messages={"on": True, "every_min": 20, "list": [{"text": "Hello"}]},
             podcasts=[{"id": "pod_1", "feed_url": "https://feed.example/a", "title": "A", "author": "B"}],
             broadcast_chattiness="minimal", locator="IO00aa",              # shared settings
             speaker_mono=True, speaker_volume=12, buttons=[{"kind": "show"}])  # this radio's own
    taken = []
    two.apply_live = lambda settings, changed: taken.append(set(changed))
    one.pair("two")
    c = conf_of(two)
    assert [p["name"] for p in c["profiles"]] == ["Default", "Jazz"] and c["playlists"][0]["name"] == "Faves"
    assert c["birthdays"][0]["name"] == "Sam" and c["podcasts"][0]["feed_url"] == "https://feed.example/a"
    assert c["messages"]["list"][0]["text"] == "Hello" and c["messages"]["every_min"] == 20
    assert c["broadcast_chattiness"] == "minimal" and c["locator"] == "IO00aa"
    assert "speaker_mono" not in c and "speaker_volume" not in c and "buttons" not in c       # never sent
    import time as _t
    for _ in range(50):                                         # (taken up in the background)
        if taken:
            break
        _t.sleep(0.02)
    assert taken and {"profiles", "playlists", "birthdays", "messages", "podcasts", "broadcast_chattiness"} <= taken[0]


def test_a_programme_arrives_switched_off_and_on_off_stays_each_radios(tmp_path) -> None:
    net = Net()
    one, two = radio(tmp_path, net, "one"), radio(tmp_path, net, "two")
    wake = {"name": "Wake", "start": "07:00", "auto": True, "days": [], "then": "show", "gap": "show", "blocks": []}
    set_conf(one, programmes=[wake])
    one.pair("two")
    assert conf_of(two)["programmes"][0]["auto"] is False             # arrives off: no alarm sounding on two
    assert conf_of(one)["programmes"][0]["auto"] is True              # still on where it was made
    set_conf(two, programmes=[{**wake, "auto": True, "start": "07:30"}])   # two switches it on, and moves it
    two.sync_now(one.id)
    assert conf_of(one)["programmes"][0]["start"] == "07:30" and conf_of(one)["programmes"][0]["auto"] is True
    set_conf(one, programmes=[{**conf_of(one)["programmes"][0], "auto": False}])   # one switches its own off
    one.sync_now(two.id)
    assert conf_of(two)["programmes"][0]["auto"] is True              # ...two's stays on


def test_a_setting_left_empty_is_a_value_not_a_delete() -> None:
    got, counts = merge("settings", None, {"locator": None, "callsign": "X"}, {"locator": "IO00", "callsign": None})
    assert counts["deleted"] == 0 and set(got) == {"locator", "callsign"}
    got, counts = merge("settings", {"locator": None}, {"locator": None}, {"locator": "IO00"})
    assert got == {"locator": "IO00"} and counts == {"added": 0, "changed": 1, "deleted": 0}


def test_on_a_first_sync_a_setting_someone_set_beats_one_never_set() -> None:
    got, _ = merge("settings", None, {"broadcast_chattiness": "maximum", "locator": None},     # (new-radio values)
                   {"broadcast_chattiness": "minimal", "locator": "IO00"})
    assert got == {"broadcast_chattiness": "minimal", "locator": "IO00"}
    got2, _ = merge("settings", None, {"broadcast_chattiness": "minimal", "locator": "IO00"},
                    {"broadcast_chattiness": "maximum", "locator": None})
    assert got2 == got                                                                        # either way round


# --- files (jingles): each radio a pretend card ---

class Card:
    """A pretend music storage: real files in a folder, so checksums are worked out as on a radio."""
    def __init__(self, root, files=None):
        self.root = root
        for p, b in (files or {}).items():
            self._put("jingles", p, b)

    def _put(self, kind, path, data):
        f = self.root / kind / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)

    @property
    def files(self):
        out = {}
        for kind_dir in [self.root / "jingles"]:
            out[kind_dir.name] = {f.relative_to(kind_dir).as_posix(): f.read_bytes()
                                  for f in sorted(kind_dir.rglob("*")) if f.is_file()} if kind_dir.is_dir() else {}
        return out

    def resolve(self, kind, path):
        return self.root / kind / path

    def _root(self, kind):
        (self.root / kind).mkdir(parents=True, exist_ok=True)
        return self.root / kind

    def tree(self, kind):
        return {"files": {p: len(b) for p, b in self.files[kind].items()}}

    def receive(self, kind, folder, name, length, read, replace=False):
        self._put(kind, name, read(length))

    def delete(self, kind, path, prune=False):
        (self.root / kind / path).unlink()

    def done(self):
        return True


class Got:
    def __init__(self, data):
        self.data, self.headers = data, {"Content-Length": str(len(data))}

    def read(self, n=-1):
        d, self.data = self.data, b""
        return d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def carded(tmp_path, net, address, files):
    r = radio(tmp_path, net, address)
    r.media = Card(tmp_path / f"{address}-card", files)

    def get_file(addr, kind, path, net=net):
        other = net.radios[addr]
        if getattr(other, "off", False) or path not in other.media.files[kind]:
            raise OSError("not there")
        return Got(other.media.files[kind][path])
    r.get_file = get_file
    return r


def test_jingles_are_kept_the_same_both_ways(tmp_path) -> None:
    import time as _t
    net = Net()
    one = carded(tmp_path, net, "one", {"Rock/a.mp3": b"AAAA", "Rock/b.mp3": b"BB"})
    two = carded(tmp_path, net, "two", {"Jazz/c.mp3": b"CCC"})
    one.pair("two")
    for _ in range(100):                                       # (the radio asked fetches its files in the background)
        if len(two.media.files["jingles"]) == 3:
            break
        _t.sleep(0.02)
    assert one.media.files == two.media.files == {"jingles": {"Rock/a.mp3": b"AAAA", "Rock/b.mp3": b"BB", "Jazz/c.mp3": b"CCC"}}
    _t.sleep(0.1)
    two.media.delete("jingles", "Rock/b.mp3")                  # deleted on two
    two.media._put("jingles", "Jazz/d.mp3", b"DDDDD")          # ...and one added
    two.sync_now(one.id)
    want = {"Rock/a.mp3": b"AAAA", "Jazz/c.mp3": b"CCC", "Jazz/d.mp3": b"DDDDD"}
    for _ in range(100):                                       # (one, asked, fetches in the background)
        if one.media.files["jingles"] == want:
            break
        _t.sleep(0.02)
    assert one.media.files["jingles"] == want


def test_a_jingle_that_does_not_arrive_is_never_deleted_later(tmp_path) -> None:
    """A file that couldn't be fetched leaves the two disagreeing on what they last agreed: the next sync only
    adds, so the other radio's copy isn't taken for one this radio deleted."""
    import time as _t
    net = Net()
    one = carded(tmp_path, net, "one", {})
    two = carded(tmp_path, net, "two", {"Rock/a.mp3": b"AAAA"})
    real = one.get_file
    one.get_file = lambda *a: (_ for _ in ()).throw(OSError("dropped"))
    one.pair("two")                                            # one couldn't fetch it
    assert one.media.files["jingles"] == {}
    one.get_file = real
    two.sync_now(one.id)                                       # two syncs next: must not delete its own copy
    for _ in range(100):
        if one.media.files["jingles"]:
            break
        _t.sleep(0.02)
    assert two.media.files["jingles"] == {"Rock/a.mp3": b"AAAA"} and one.media.files["jingles"] == {"Rock/a.mp3": b"AAAA"}


class Jobs:
    """A pretend VoiceJobs: voices are folders with a model in voices_dir."""
    def __init__(self, d, names):
        self.voices_dir = d
        d.mkdir(parents=True, exist_ok=True)
        for n, data in names.items():
            (d / n).mkdir()
            (d / n / "model.onnx").write_bytes(data)

    def receive(self, name, filename, length, read):
        data = read(length)                                    # (like the real one: in place only once it's all here)
        (self.voices_dir / name).mkdir()
        (self.voices_dir / name / "model.onnx").write_bytes(data)


def voiced(tmp_path, net, address, names):
    r = radio(tmp_path, net, address)
    r.voice_jobs = Jobs(tmp_path / f"{address}-voices", names)

    def get_voice(addr, name, net=net):
        return Got((net.radios[addr].voice_jobs.voices_dir / name / "model.onnx").read_bytes())
    r.get_voice = get_voice
    return r


def test_a_voice_one_radio_lacks_is_added_and_a_same_named_one_never_replaced(tmp_path) -> None:
    import time as _t
    net = Net()
    one = voiced(tmp_path, net, "one", {"personal": b"MINE", "deep": b"DEEP"})
    two = voiced(tmp_path, net, "two", {"personal": b"THEIRS"})
    one.pair("two")
    for _ in range(100):
        if (two.voice_jobs.voices_dir / "deep" / "model.onnx").is_file() and \
                (two.voice_jobs.voices_dir / "deep" / "model.onnx").stat().st_size:
            break
        _t.sleep(0.02)
    assert (two.voice_jobs.voices_dir / "deep" / "model.onnx").read_bytes() == b"DEEP"          # added
    assert (two.voice_jobs.voices_dir / "personal" / "model.onnx").read_bytes() == b"THEIRS"    # never replaced
    assert (one.voice_jobs.voices_dir / "personal" / "model.onnx").read_bytes() == b"MINE"
    import shutil
    shutil.rmtree(two.voice_jobs.voices_dir / "deep")          # gone from one radio (by hand): not deleted on the other
    two.sync_now(one.id)
    assert (one.voice_jobs.voices_dir / "deep").exists()


def test_a_setting_naming_a_voice_this_radio_lacks_keeps_its_own_till_the_voice_is_here(tmp_path) -> None:
    net = Net()
    one, two = radio(tmp_path, net, "one"), radio(tmp_path, net, "two")
    set_conf(one, ebook_voice="personal")
    set_conf(two, ebook_voice="stock")
    have = {"stock"}
    two.voice_ok = lambda key, v: v in have
    one.voice_ok = lambda key, v: True
    taken = []
    two.apply_live = lambda settings, changed: taken.extend(changed)
    one.pair("two")
    import time as _t
    _t.sleep(0.2)
    assert conf_of(two)["ebook_voice"] == "personal"             # agreed and kept on both (nothing bounces back)...
    assert "ebook_voice" not in taken                            # ...but not taken up live: two hasn't that voice yet
    two.sync_now(one.id)
    assert conf_of(one)["ebook_voice"] == "personal"


def test_one_change_the_radio_cannot_take_up_does_not_stop_the_rest(tmp_path) -> None:
    import time as _t
    net = Net()
    one, two = radio(tmp_path, net, "one"), radio(tmp_path, net, "two")
    set_conf(one, birthdays=[{"name": "Sam", "day": 1, "month": 2}], locator="IO00aa")
    taken = []

    def apply_live(settings, changed):
        if "locator" in changed:
            raise ValueError("can't")
        taken.extend(changed)
    two.apply_live = apply_live
    one.pair("two")
    for _ in range(100):
        if "birthdays" in taken:
            break
        _t.sleep(0.02)
    assert "birthdays" in taken


def test_a_jingle_replaced_by_one_of_the_same_size_is_synced_too(tmp_path) -> None:
    import time as _t
    net = Net()
    one = carded(tmp_path, net, "one", {"Rock/a.mp3": b"AAAA"})
    two = carded(tmp_path, net, "two", {})
    one.pair("two")
    for _ in range(100):
        if two.media.files["jingles"]:
            break
        _t.sleep(0.02)
    _t.sleep(0.05)
    import os
    f = one.media.resolve("jingles", "Rock/a.mp3")
    f.write_bytes(b"ZZZZ")                                     # the same size, other words
    os.utime(f, ns=(1, 1))                                     # (and a time of its own)
    one.sync_now(two.id)
    for _ in range(100):
        if two.media.files["jingles"].get("Rock/a.mp3") == b"ZZZZ":
            break
        _t.sleep(0.02)
    assert two.media.files["jingles"]["Rock/a.mp3"] == b"ZZZZ"


def test_wifi_networks_are_added_never_removed_and_only_told_to_a_paired_radio(tmp_path) -> None:
    from sleepradiopi import wifi as wifi_mod
    net = Net()
    one, two = radio(tmp_path, net, "one"), radio(tmp_path, net, "two")
    k = "ab" * 32
    for r, nets in ((one, [{"ssid": "Home", "psk": k}]), (two, [{"ssid": "Shed", "open": True}])):
        r.wifi_path = tmp_path / f"{r.name()}-wifi.json"
        wifi_mod.save({"networks": nets, "offline": False}, r.wifi_path)
        r.ask_wifi = lambda action: None
    assert "wifi" not in one.state(two.id, "10.9.9.9")["collections"]      # not from its address: no keys
    one.pair("two")
    assert [n["ssid"] for n in wifi_mod.load(two.wifi_path)["networks"]] == ["Shed", "Home"]   # added after its own
    assert [n["ssid"] for n in wifi_mod.load(one.wifi_path)["networks"]] == ["Home", "Shed"]
    wifi_mod.save({"networks": [{"ssid": "Shed", "open": True}], "offline": False}, two.wifi_path)  # Home removed on two
    two.sync_now(one.id)
    assert "Home" in [n["ssid"] for n in wifi_mod.load(one.wifi_path)["networks"]]         # never removed by a sync


def test_a_jingle_that_will_not_arrive_does_not_stop_a_station_delete_reaching_the_other(tmp_path) -> None:
    """Agreement is kept per collection: a file that fails leaves only the files adding-only; a station deleted on
    one radio still goes from the other."""
    net = Net()
    one = carded(tmp_path, net, "one", {})
    two = carded(tmp_path, net, "two", {"Rock/a.mp3": b"AAAA"})
    set_stations(one, [st("a"), st("b")])
    set_stations(two, [st("a"), st("b")])
    one.get_file = lambda *a: (_ for _ in ()).throw(OSError("never arrives"))
    one.pair("two")
    set_stations(two, [st("a")])                                # b deleted on two
    one.sync_now(two.id)
    assert urls(stations_of(one)) == ["http://a"]
    assert two.media.files["jingles"] == {"Rock/a.mp3": b"AAAA"}   # (and two's jingle never deleted)


# --- the media (music, On demand, audiobooks, ebooks): shared once both radios tick them ---

class Library(Card):
    """A pretend music storage with every library, and room on it."""
    def __init__(self, root, files=None, free=10 ** 9):
        self.root, self.free = root, free
        for p, b in (files or {}).items():
            kind, _, path = p.partition("/")
            self._put(kind, path, b)

    @property
    def files(self):
        out = {}
        for kind in ("jingles", *sync_mod.MEDIA_KINDS):
            d = self.root / kind
            out[kind] = {f.relative_to(d).as_posix(): f.read_bytes() for f in sorted(d.rglob("*")) if f.is_file()} \
                if d.is_dir() else {}
        return out

    def usage(self):
        return {"total": 2 * 10 ** 9, "used": 0, "free": self.free}

    def delete(self, kind, path, prune=False):
        f = self.root / kind / path
        f.unlink()
        while prune and f.parent != self.root / kind and not any(f.parent.iterdir()):
            f.parent.rmdir()
            f = f.parent


def libraried(tmp_path, net, address, files, free=10 ** 9):
    r = carded(tmp_path, net, address, {})
    r.media = Library(tmp_path / f"{address}-lib", files, free)
    return r


def settle(*radios, want=None, tries=200):
    import threading
    import time as _t
    for _ in range(tries):
        busy = any(t.name in ("sync-now", "sync-files") for t in threading.enumerate())
        if not busy and all(not r._media_jobs and not r._media_worker_on for r in radios) and (want is None or want()):
            return
        _t.sleep(0.02)


def test_media_entries_are_one_track_whatever_its_format() -> None:
    files = {"A/01 Song.flac": 900, "A/02 Other.mp3": 300, "A/cover.jpg": 10, "A/notes.txt": 5, "A/.hidden.mp3": 3}
    got = sync_mod._media_entries("music", files)
    assert got == {"A/01 Song": "900:.flac", "A/02 Other": "300:.mp3", "A/cover.jpg": "10:"}    # (no .txt, no hidden)
    assert sync_mod._media_paths(got) == {"A/01 Song.flac": 900, "A/02 Other.mp3": 300, "A/cover.jpg": 10}
    both = sync_mod._media_entries("music", {"B/x.flac": 900, "B/x.mp3": 300})       # (both copies on one radio)
    assert both == {"B/x": "300:.mp3", "B/x.flac": "900:"}


def test_where_the_two_differ_the_smaller_file_wins_on_both() -> None:
    a, b = {"A/song": "900:.flac"}, {"A/song": "300:.mp3"}
    assert merge("media:music", None, a, b)[0] == merge("media:music", None, b, a)[0] == {"A/song": "300:.mp3"}


def test_no_media_is_synced_until_both_radios_share_it(tmp_path) -> None:
    net = Net()
    one = libraried(tmp_path, net, "one", {"music/A/a.mp3": b"AAAA"})
    two = libraried(tmp_path, net, "two", {"music/B/b.mp3": b"BB"})
    one.pair("two")
    settle(one, two)
    assert one.media.files["music"] == {"A/a.mp3": b"AAAA"} and two.media.files["music"] == {"B/b.mp3": b"BB"}
    one.share_media(two.id, ["music"])
    assert one.shared(two.id) == ["music"] and two.shared(one.id) == ["music"]      # (set on both)
    one.sync_now(two.id)
    settle(one, two, want=lambda: len(one.media.files["music"]) == len(two.media.files["music"]) == 2)
    assert one.media.files["music"] == two.media.files["music"] == {"A/a.mp3": b"AAAA", "B/b.mp3": b"BB"}
    assert one.media_status[two.id]["done"]


def test_a_delete_goes_from_both_and_an_add_reaches_both(tmp_path) -> None:
    net = Net()
    files = {f"music/A/{i}.mp3": b"x" * (i + 1) for i in range(10)}
    one, two = libraried(tmp_path, net, "one", files), libraried(tmp_path, net, "two", files)
    one.pair("two")
    one.share_media(two.id, ["music"])
    settle(one, two)
    one.sync_now(two.id)
    settle(one, two)
    two.sync_now(one.id)                                       # (both agreed: a base on both)
    settle(one, two)
    two.media.delete("music", "A/3.mp3")                       # deleted on two
    one.media._put("music", "C/new.mp3", b"NEW")               # added on one
    one.sync_now(two.id)
    settle(one, two, want=lambda: "C/new.mp3" in two.media.files["music"] and "A/3.mp3" not in one.media.files["music"])
    assert "A/3.mp3" not in one.media.files["music"] and two.media.files["music"]["C/new.mp3"] == b"NEW"
    assert one.media.files["music"] == two.media.files["music"]


def test_a_flac_is_replaced_by_the_other_radios_mp3(tmp_path) -> None:
    net = Net()
    one = libraried(tmp_path, net, "one", {"music/A/song.flac": b"F" * 90})
    two = libraried(tmp_path, net, "two", {"music/A/song.mp3": b"M" * 30})
    one.pair("two")
    one.share_media(two.id, ["music"])
    settle(one, two)
    one.sync_now(two.id)
    settle(one, two, want=lambda: one.media.files["music"] == {"A/song.mp3": b"M" * 30})
    assert one.media.files["music"] == two.media.files["music"] == {"A/song.mp3": b"M" * 30}


def test_media_that_will_not_fit_is_held_not_half_copied(tmp_path) -> None:
    from sleepradiopi import media as media_mod
    net = Net()
    one = libraried(tmp_path, net, "one", {}, free=media_mod.KEEP_FREE + 5)
    two = libraried(tmp_path, net, "two", {"music/A/big.mp3": b"B" * 50})
    one.pair("two")
    one.share_media(two.id, ["music"])
    settle(one, two)
    one.sync_now(two.id)
    settle(one, two)
    assert one.media.files["music"] == {} and "room" in one.media_status[two.id]["said"]
    assert two.media.files["music"] == {"A/big.mp3": b"B" * 50}       # (and nothing taken from two)


def test_the_copying_waits_while_the_radio_plays(tmp_path, monkeypatch) -> None:
    import time as _t
    monkeypatch.setattr(sync_mod, "MEDIA_WAIT_S", 0.02)
    net = Net()
    one = libraried(tmp_path, net, "one", {})
    two = libraried(tmp_path, net, "two", {"music/A/a.mp3": b"AAAA"})
    playing = [True]
    one.busy = lambda: playing[0]
    one.pair("two")
    one.share_media(two.id, ["music"])
    settle(one, two)
    one.sync_now(two.id)
    _t.sleep(0.2)
    assert one.media.files["music"] == {} and "plays" in one.media_status[two.id]["said"]
    playing[0] = False
    settle(one, two, want=lambda: bool(one.media.files["music"]))
    assert one.media.files["music"] == {"A/a.mp3": b"AAAA"}


def test_a_copy_cut_short_never_turns_into_deletes(tmp_path) -> None:
    net = Net()
    one = libraried(tmp_path, net, "one", {})
    two = libraried(tmp_path, net, "two", {"music/A/a.mp3": b"AAAA", "music/A/b.mp3": b"BB"})
    real = one.get_file
    one.get_file = lambda addr, kind, path: (_ for _ in ()).throw(OSError("dropped")) if path == "A/b.mp3" else real(addr, kind, path)
    one.pair("two")
    one.share_media(two.id, ["music"])
    settle(one, two)
    one.sync_now(two.id)
    settle(one, two)
    assert one.media.files["music"] == {"A/a.mp3": b"AAAA"}
    two.sync_now(one.id)                                       # two asks next: must not take b as deleted on one
    settle(one, two)
    assert two.media.files["music"] == {"A/a.mp3": b"AAAA", "A/b.mp3": b"BB"}
    one.get_file = real
    one.sync_now(two.id)
    settle(one, two, want=lambda: len(one.media.files["music"]) == 2)
    assert one.media.files["music"] == two.media.files["music"]


def test_sharing_needs_the_other_radio_answering(tmp_path) -> None:
    net = Net()
    one, two = libraried(tmp_path, net, "one", {}), libraried(tmp_path, net, "two", {})
    one.pair("two")
    two.off = True
    with pytest.raises(ValueError, match="answering"):
        one.share_media(two.id, ["music"])
    assert one.shared(two.id) == [] and two.shared(one.id) == []


def test_only_the_paired_radio_itself_can_share_with_this_one(tmp_path) -> None:
    net = Net()
    one, two = libraried(tmp_path, net, "one", {}), libraried(tmp_path, net, "two", {})
    one.pair("two")
    with pytest.raises(ValueError):
        two.media_set(one.id, ["music"], "10.9.9.9")           # (asked from somewhere else)
    assert two.shared(one.id) == []



# --- 1.9.96: what the Other radios window shows, and the room's settings kept to each radio ---

def set_conf(r, **kv):
    conf = json.loads(r.config_file.read_text())
    conf.update(kv)
    r.config_file.write_text(json.dumps(conf))


def conf_of(r):
    return json.loads(r.config_file.read_text())


def test_the_rooms_settings_stay_each_radios_own(tmp_path) -> None:
    net = Net()
    one, two = radio(tmp_path, net, "one"), radio(tmp_path, net, "two")
    set_conf(one, room_level=-30, news_quiet_hours=True, notify="muted", news_speed=1.1)
    set_conf(two, room_level=-50, news_quiet_hours=False, notify="armed")
    one.pair("two")
    assert conf_of(two)["room_level"] == -50 and conf_of(two)["news_quiet_hours"] is False and conf_of(two)["notify"] == "armed"
    assert conf_of(two)["news_speed"] == 1.1                    # (the DJ and news settings still go across)
    for key in ("news_quiet_hours", "room_level", "noise_reduction", "broadcast_announcer_volume", "stream_evenness",
                "startup_sound", "notify", "lightning_bell", "background_url"):
        assert key not in sync_mod._synced_settings()


def test_both_radios_know_how_much_each_has_after_a_sync(tmp_path) -> None:
    net = Net()
    one = libraried(tmp_path, net, "one", {"music/A/a.mp3": b"AAAA"})
    two = libraried(tmp_path, net, "two", {"music/A/a.mp3": b"AAAA", "music/B/b.mp3": b"BB"})
    set_stations(one, [st("a"), st("b")])
    one.pair("two")
    settle(one, two)
    for r, other in ((one, two), (two, one)):
        items = {i["name"]: i for i in r.items(other.id)}
        assert items["radio_stations"]["here"]["n"] == items["radio_stations"]["there"]["n"] == 2
        assert items["radio_stations"]["state"] == "same"
        assert items["media:music"]["state"] == "off" and items["media:music"]["shared"] is False
    one.share_media(two.id, ["music"])
    settle(one, two)
    one.sync_now(two.id)
    settle(one, two, want=lambda: len(one.media.files["music"]) == 2)
    one._live = (0.0, None, {})                                 # (read again, not the 15 s copy)
    m = {i["name"]: i for i in one.items(two.id)}["media:music"]
    assert m["shared"] and m["here"] == {"n": 2, "bytes": 6} and m["state"] == "same"
    set_stations(one, [st("a")])                                # changed here since the sync
    one._live = (0.0, None, {})
    assert {i["name"]: i for i in one.items(two.id)}["radio_stations"]["state"] == "changed"
    ov = one.overview(live=True)
    p = ov["peers"][0]
    assert p["items"] and "progress" in p and ov["me"]["free"] is not None


def test_a_copy_shows_its_progress_by_bytes(tmp_path, monkeypatch) -> None:
    import threading
    net = Net()
    one = libraried(tmp_path, net, "one", {})
    two = libraried(tmp_path, net, "two", {"music/A/a.mp3": b"A" * 1000, "music/A/b.mp3": b"B" * 500})
    seen, gate = [], threading.Event()
    real = one.get_file

    def slow(addr, kind, path):
        got = real(addr, kind, path)
        seen.append(dict(one.copying.get(two.id) or {}))
        gate.wait(2)
        return got
    one.get_file = slow
    one.pair("two")
    one.share_media(two.id, ["music"])                         # (its sync starts the copy)
    import time as _t
    for _ in range(100):
        if one.busy_summary():
            break
        _t.sleep(0.02)
    b = one.busy_summary()
    assert b and b["step"] == "copying" and b["pct"] is not None
    assert two.progress_for(one.id)["copying"] is None        # (the copying is one's)
    gate.set()
    settle(one, two, want=lambda: len(one.media.files["music"]) == 2)
    assert seen and seen[0]["files"] == 2 and seen[0]["bytes"] == 1500
    assert one.copying.get(two.id) is None and one.busy_summary() is None
