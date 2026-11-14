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
        if what == "state":
            return r.state(query)
        if what == "paired":
            me = next(a for a, x in self.radios.items() if x.id == body["id"])
            r.paired_with({"id": body["id"], "name": body["name"], "address": me})
            return {"ok": True}
        if what == "unpaired":
            r.unpaired(body["id"])
            return {"ok": True}
        if what == "adopt":
            ok = r.adopt(body["id"], body["merged"], body["was"])
            if self.lose():
                raise OSError("timed out")
            return {"ok": ok}
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
