"""Radios that know each other: find the others on the network, pair, and keep
some of their things the same both ways (two-way sync).

Each radio has an id of its own (radio_id, made once). It says it's there on
the network now and then (a small UDP broadcast, BEACON_PORT: the stdlib only,
no daemon): the desktop's Other radios lists the ones heard, and one can be
added by its address too. Pairing is both ways: each keeps the other in
sync_peers (by id -- an address can change; a beacon keeps it current).

Sync is pull-based and clock-free. A radio asks a paired one for its state
(GET /api/sync/state) and merges it with its own, three ways: against the
state the two last agreed on (the base, kept in sync-base.json beside the
config). It sends the result back (POST /api/sync/adopt: taken up only if
that radio hasn't changed meanwhile), takes it up itself, and both keep it
as the new base. An item only the other side changed is taken; one only this side
changed is kept; added on either, it's added; deleted on one and untouched on
the other, it goes. The same item changed differently on both: a change beats
a delete, otherwise a fixed rule every radio applies alike (the larger, as
text) -- so both end up the same with no clocks (a radio with no RTC can't be
trusted for "the latest").

Guards: with no base (the first sync with a radio, or after a card is
re-flashed) the merge only adds -- never deletes. A merge that would delete
much of a collection is held and shown on the page instead (Sync anyway lets
it through once). A settings backup is saved before the first sync with a
radio. A radio with a web password doesn't take part (it says so).

What's synced is COLLECTIONS; each radio's own set-up, buttons, volume and
what's playing never are. Writes happen only when something actually changed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import socket
import threading
import time
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path

from sleepradiopi.config.atomic import write_atomic
from sleepradiopi.config.settings import CONFIG_LOCK, read_config, save_setting
from sleepradiopi.playback import radio as radio_mod

log = logging.getLogger(__name__)

PROTOCOL = 1                    # what a sync request looks like (not the app's version: radios mid-update still sync)
BEACON_PORT = 47474
BEACON_EVERY_S = 60
SEEN_FOR_S = 3 * BEACON_EVERY_S + 30     # a radio not heard for this long is no longer listed as on the network
SYNC_EVERY_S = 300
TIMEOUT_S = 6
HOLD_DELETES = 0.3              # a merge deleting more than this share of a collection (and more than HOLD_MIN) waits
HOLD_MIN = 3
BASE_FILE = "sync-base.json"


# --- The collections synced: how each is read, written, keyed and checked ---------------------------------------

def _stations_in(conf: dict) -> list[dict]:
    saved = conf.get("radio_stations")
    if saved is None:                            # (never saved: the starter set, as the page shows it)
        return [dict(s) for s in radio_mod.DEFAULT_STATIONS]
    try:
        return radio_mod.validate_stations(saved)
    except ValueError:
        return []


def _names_in(conf: dict) -> dict:
    names = conf.get("receiver_names")
    return {str(k): str(v) for k, v in names.items()} if isinstance(names, dict) else {}


def _check_names(value) -> dict:
    if not isinstance(value, dict) or len(value) > 200:
        raise ValueError("receiver_names: {address: name}")
    out = {}
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str) or not k.startswith(("http://", "https://", "ysf://", "fcs://")) \
                or len(k) > 300 or len(v) > 60:
            raise ValueError("receiver_names: {address: name}")
        out[k] = v
    return out


COLLECTIONS = {
    # name: (read from the config, check what another radio sent, the item's key -- None: a dict, keyed already)
    "radio_stations": (_stations_in, radio_mod.validate_stations, lambda s: s["url"]),
    "receiver_names": (_names_in, _check_names, None),
}
WORDS = {"radio_stations": "stations", "receiver_names": "receiver names"}


def _as_map(name: str, value) -> dict:
    """A collection as {key: item} (a list's order aside)."""
    key = COLLECTIONS[name][2]
    return {key(x): x for x in value} if key is not None else dict(value)


def _canon(x) -> str:
    return json.dumps(x, sort_keys=True, ensure_ascii=False)


def merge(name: str, base, mine, theirs) -> tuple[object, dict]:
    """Three-way merge of one collection. base None: the first time (only adds). Returns (the merged
    collection -- a list keeps this radio's order, then theirs' new ones in their order -- and
    {"added", "changed", "deleted": counts against mine})."""
    b = _as_map(name, base) if base is not None else None
    m, t = _as_map(name, mine), _as_map(name, theirs)
    out: dict = {}
    for k in list(m) + [k for k in t if k not in m]:
        mi, ti = m.get(k), t.get(k)
        bi = b.get(k) if b is not None else None
        if b is None:                                    # first time: everything either has, nothing goes
            got = mi if ti is None else ti if mi is None else (mi if _canon(mi) == _canon(ti) else max(mi, ti, key=_canon))
        elif mi is not None and ti is not None:
            if _canon(mi) == _canon(ti) or _canon(ti) == _canon(bi):
                got = mi                                 # (the same, or only mine changed)
            elif _canon(mi) == _canon(bi):
                got = ti                                 # only theirs changed
            else:
                got = max(mi, ti, key=_canon)            # both changed it: the same rule on every radio
        elif mi is not None:                             # theirs hasn't it
            got = None if bi is not None and _canon(mi) == _canon(bi) else mi    # they deleted it (I hadn't changed it): gone
        else:                                            # mine hasn't it
            got = None if bi is not None and _canon(ti) == _canon(bi) else ti    # I deleted it (they hadn't changed it): stays gone
        if got is not None:
            out[k] = got
    counts = {"added": sum(1 for k in out if k not in m),
              "changed": sum(1 for k in out if k in m and _canon(out[k]) != _canon(m[k])),
              "deleted": sum(1 for k in m if k not in out)}
    merged = list(out.values()) if COLLECTIONS[name][2] is not None else out
    return merged, counts


def same(name: str, a, b) -> bool:
    """The same items (a list's order doesn't count)."""
    return _canon(_as_map(name, a)) == _canon(_as_map(name, b))


# --- One radio's side ---------------------------------------------------------------------------------------------

class Sync:
    """This radio in the sync: its id, the radios it's paired with, what it's heard on the network, and
    the merges. fetch(url, body=None) -> parsed JSON (raises OSError/ValueError): how it talks to another
    radio (a test passes its own). protected() -> True: this radio has a web password (no sync)."""

    def __init__(self, config_file: Path, name: Callable[[], str], port: int, version: str = "",
                 fetch: Callable | None = None, protected: Callable[[], bool] = lambda: False,
                 backup: Callable[[str], None] | None = None) -> None:
        self.config_file = Path(config_file)
        self.base_file = self.config_file.parent / BASE_FILE
        self.name, self.port, self.version = name, port, version
        self.fetch = fetch or _fetch
        self.protected = protected
        self.backup = backup                     # backup(label): a named settings backup (before a first sync)
        self.seen: dict[str, dict] = {}          # id -> {"id", "name", "address", "version", "at"} (heard lately)
        self.status: dict[str, dict] = {}        # peer id -> {"at", "ok", "said", "held"?}
        self._allow_once: set[str] = set()       # peers whose held merge may go through once
        self._lock = threading.Lock()            # (one merge at a time)
        self._stop = threading.Event()
        conf = self._conf()
        self.id = conf.get("radio_id") if isinstance(conf.get("radio_id"), str) and conf.get("radio_id") else ""
        if not self.id:                          # (made once, kept: who this radio is to the others)
            self.id = uuid.uuid4().hex[:12]
            save_setting(self.config_file, "radio_id", self.id)

    # (the config, read fresh: other parts of the radio change it)
    def _conf(self) -> dict:
        try:
            return read_config(self.config_file)
        except (OSError, ValueError):
            return {}

    def peers(self) -> list[dict]:
        p = self._conf().get("sync_peers")
        return [x for x in p if isinstance(x, dict) and isinstance(x.get("id"), str)] if isinstance(p, list) else []

    def _save_peers(self, peers: list[dict]) -> None:
        save_setting(self.config_file, "sync_peers", peers)

    def _bases(self) -> dict:
        try:
            b = json.loads(self.base_file.read_text())
            return b if isinstance(b, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_bases(self, bases: dict) -> None:
        write_atomic(self.base_file, json.dumps(bases, ensure_ascii=False) + "\n")

    # --- what's asked of this radio ---

    def hello(self) -> dict:
        return {"sleepradio": PROTOCOL, "id": self.id, "name": self.name(), "port": self.port,
                "version": self.version, "protected": bool(self.protected())}

    def state(self, for_id: str = "") -> dict:
        """This radio's collections; and (for_id: the radio asking) a fingerprint of the state this radio last
        agreed on with it -- if the two radios' fingerprints differ (a reply lost mid-sync, a re-pair, a card
        re-flashed), the next merge between them only adds: neither can tell a delete from a missed add."""
        conf = self._conf()
        base = self._bases().get(for_id) if for_id else None
        return {**self.hello(), "collections": {n: c[0](conf) for n, c in COLLECTIONS.items()},
                "base": _digest(base) if isinstance(base, dict) and base else ""}

    def paired_with(self, peer: dict) -> None:
        """Another radio paired with this one (its id, name, address): keep it."""
        peers = [p for p in self.peers() if p["id"] != peer["id"]]
        self._save_peers(peers + [{"id": peer["id"], "name": peer.get("name", ""), "address": peer["address"]}])
        log.info("sync: paired with %s (%s)", peer.get("name"), peer["address"])

    def unpaired(self, peer_id: str) -> None:
        self._save_peers([p for p in self.peers() if p["id"] != peer_id])
        bases = self._bases()
        if bases.pop(peer_id, None) is not None:
            self._save_bases(bases)
        self.status.pop(peer_id, None)
        log.info("sync: unpaired from %s", peer_id)

    # --- what this radio does ---

    def pair(self, address: str) -> dict:
        """Pair with the radio at address ("host:port"): both keep each other, then sync."""
        address = _clean_address(address)
        if self.protected():
            raise ValueError("this radio has a password: it can't be paired")
        them = self.fetch(f"http://{address}/api/sync/hello")
        if not isinstance(them, dict) or them.get("sleepradio") != PROTOCOL or not isinstance(them.get("id"), str):
            raise ValueError("that isn't a Sleep Radio that can sync (or it's a different version of the sync)")
        if them["id"] == self.id:
            raise ValueError("that's this radio")
        if them.get("protected"):
            raise ValueError(f"{them.get('name') or address} has a password: it can't be paired")
        self.fetch(f"http://{address}/api/sync/paired", {"id": self.id, "name": self.name(), "port": self.port})
        self.paired_with({"id": them["id"], "name": them.get("name", ""), "address": address})
        st = self.sync_now(them["id"])
        return {"id": them["id"], "name": them.get("name", ""), "sync": st}

    def unpair(self, peer_id: str) -> None:
        peer = next((p for p in self.peers() if p["id"] == peer_id), None)
        if peer is None:
            raise ValueError("that radio isn't paired with this one")
        try:
            self.fetch(f"http://{peer['address']}/api/sync/unpaired", {"id": self.id})
        except (OSError, ValueError):
            pass                                  # (it's off: it'll find it's no longer paired when it asks)
        self.unpaired(peer_id)

    def allow_once(self, peer_id: str) -> None:
        self._allow_once.add(peer_id)

    def sync_now(self, peer_id: str) -> dict:
        return self.pull(peer_id)

    def pull(self, peer_id: str) -> dict:
        """One exchange with a paired radio: read its state, merge it with this radio's against the last
        state the two agreed on (the base), send it the result to take up (only if it hasn't changed
        meanwhile), then take it up here too: both end the same, and both keep it as the new base."""
        peer = next((p for p in self.peers() if p["id"] == peer_id), None)
        if peer is None:
            raise ValueError("that radio isn't paired with this one")
        if self.protected():
            return self._said(peer_id, False, "this radio has a password: no sync")
        try:
            them = self.fetch(f"http://{peer['address']}/api/sync/state?for={self.id}")
        except (OSError, ValueError) as e:
            return self._said(peer_id, False, f"not answering ({str(getattr(e, 'reason', e))[:60]})")
        if not isinstance(them, dict) or them.get("id") != peer_id:
            return self._said(peer_id, False, "a different radio is at that address now")
        if them.get("sleepradio") != PROTOCOL:
            return self._said(peer_id, False, "it has a different version of the sync: update both radios")
        if them.get("protected"):
            return self._said(peer_id, False, "it has a password: no sync")
        try:
            theirs = {n: COLLECTIONS[n][1](them["collections"][n]) for n in COLLECTIONS}
        except (KeyError, TypeError, ValueError) as e:
            return self._said(peer_id, False, f"what it sent didn't check out ({e})")
        their_name = str(them.get("name") or "")
        with self._lock:
            base = self._bases().get(peer_id)
            base = base if isinstance(base, dict) and base else None
            if base is not None and them.get("base") != _digest(base):
                log.info("sync with %s: the last agreed state differs between the two: this time only adds", their_name)
                base = None                              # (see state(): a delete can't be told from a missed add)
            if base is None and self.backup is not None:     # (the first sync with that radio: a backup first)
                try:
                    self.backup(f"Before syncing with {their_name or peer.get('name') or 'another radio'}")
                except Exception:
                    log.exception("sync: the backup before the first sync wasn't made")
            with CONFIG_LOCK:                             # (worked out under the lock; the other radio asked without it)
                conf = read_config(self.config_file)
                mine = {n: c[0](conf) for n, c in COLLECTIONS.items()}
                merged, here, there, held = {}, {}, {}, []
                for n, (_read, check, _key) in COLLECTIONS.items():
                    merged[n], here[n] = merge(n, (base or {}).get(n), mine[n], theirs[n])
                    there[n] = _gone(n, theirs[n], merged[n])                  # (what the other radio would lose)
                    for where, count, of in (("here", here[n]["deleted"], mine[n]), ("there", there[n], theirs[n])):
                        if count > HOLD_MIN and count > HOLD_DELETES * max(1, len(_as_map(n, of))) \
                                and peer_id not in self._allow_once:
                            held.append(f"{count} of {len(_as_map(n, of))} {WORDS[n]} {where}")
                    try:
                        merged[n] = check(merged[n])
                    except ValueError as e:                # (e.g. more stations together than a radio keeps)
                        return self._said(peer_id, False, f"together they don't fit: {e}")
            if held:
                return self._said(peer_id, False, "held: it would delete " + ", ".join(held) +
                                  ". Sync anyway to let it through", held=True)
            if not all(same(n, merged[n], theirs[n]) for n in COLLECTIONS) or base is None \
                    or not all(same(n, base.get(n, []), merged[n]) for n in COLLECTIONS):
                try:                                       # (the other radio takes it up, if it hasn't changed since)
                    ok = self.fetch(f"http://{peer['address']}/api/sync/adopt",
                                    {"id": self.id, "merged": merged, "was": _digest(theirs)})
                except (OSError, ValueError) as e:
                    return self._said(peer_id, False, f"it didn't take the sync ({str(getattr(e, 'reason', e))[:60]})")
                if not (isinstance(ok, dict) and ok.get("ok")):
                    return self._said(peer_id, False, "it changed meanwhile: it'll sync next time")
            with CONFIG_LOCK:
                conf = read_config(self.config_file)
                now = {n: c[0](conf) for n, c in COLLECTIONS.items()}
                if _digest(now) != _digest(mine):          # (changed here while it was asked: kept; the next sync merges it)
                    return self._said(peer_id, False, "changed here meanwhile: it'll sync next time")
                if not all(same(n, merged[n], mine[n]) for n in COLLECTIONS):
                    conf.update(merged)
                    write_atomic(self.config_file, json.dumps(conf, indent=2) + "\n")
            self._keep_base(peer_id, merged)
            self._allow_once.discard(peer_id)
        if their_name and their_name != peer.get("name"):
            self._save_peers([{**p, "name": their_name} if p["id"] == peer_id else p for p in self.peers()])
        words = lambda counts: ", ".join(f"{v} {k} {WORDS[n]}" for n, c in counts.items() for k, v in c.items() if v)
        here_w, there_w = words(here), words({n: _changes(n, theirs[n], merged[n]) for n in COLLECTIONS})
        said = "; ".join(x for x in (here_w and f"here: {here_w}", there_w and f"on {their_name or 'it'}: {there_w}") if x)
        log.info("sync with %s: %s", their_name or peer_id, said or "already the same")
        return self._said(peer_id, True, said or "already the same")

    def adopt(self, peer_id: str, merged: dict, was: str) -> bool:
        """Asked by a paired radio: take up the merged collections it worked out -- if this radio's are still
        what it read (was: their digest); else no (it syncs again later). Kept as the base with it too."""
        if not any(p["id"] == peer_id for p in self.peers()):
            raise ValueError("that radio isn't paired with this one")
        if not isinstance(merged, dict):
            raise ValueError("send the merged collections")
        new = {n: COLLECTIONS[n][1](merged[n]) for n in COLLECTIONS}      # (KeyError/ValueError: refused)
        if not self._lock.acquire(timeout=2):        # (this radio is syncing with that one itself: no -- try later)
            return False
        try:
            if not self._bases().get(peer_id) and self.backup is not None:     # (its first sync with that radio: a backup first)
                peer = next(p for p in self.peers() if p["id"] == peer_id)
                try:
                    self.backup(f"Before syncing with {peer.get('name') or 'another radio'}")
                except Exception:
                    log.exception("sync: the backup before the first sync wasn't made")
            with CONFIG_LOCK:
                conf = read_config(self.config_file)
                mine = {n: c[0](conf) for n, c in COLLECTIONS.items()}
                if _digest(mine) != was:
                    return False
                if not all(same(n, new[n], mine[n]) for n in COLLECTIONS):
                    conf.update(new)
                    write_atomic(self.config_file, json.dumps(conf, indent=2) + "\n")
            self._keep_base(peer_id, new)
        finally:
            self._lock.release()
        return True

    def _keep_base(self, peer_id: str, agreed: dict) -> None:
        bases = self._bases()
        old = bases.get(peer_id)
        if isinstance(old, dict) and all(n in old and same(n, old[n], agreed[n]) for n in COLLECTIONS):
            return                                         # (unchanged: not written -- few writes to the card)
        bases[peer_id] = agreed
        self._save_bases(bases)

    def _said(self, peer_id: str, ok: bool, said: str, held: bool = False) -> dict:
        st = {"at": time.time(), "ok": ok, "said": said, **({"held": True} if held else {})}
        self.status[peer_id] = st
        if not ok:
            log.info("sync: %s: %s", peer_id, said)
        return st

    def overview(self) -> dict:
        now = time.time()
        peers = self.peers()
        paired = {p["id"] for p in peers}
        return {"me": self.hello(),
                "peers": [{**p, "online": p["id"] in self.seen and now - self.seen[p["id"]]["at"] < SEEN_FOR_S,
                           "status": self.status.get(p["id"])} for p in peers],
                "seen": [{k: v for k, v in s.items() if k != "at"} for s in self.seen.values()
                         if s["id"] not in paired and now - s["at"] < SEEN_FOR_S]}

    # --- on the network: the beacon, and syncing now and then ---

    def heard(self, msg: dict, ip: str) -> None:
        """A beacon from another radio."""
        if not isinstance(msg, dict) or msg.get("sleepradio") != PROTOCOL or not isinstance(msg.get("id"), str) \
                or msg["id"] == self.id or not isinstance(msg.get("port"), int):
            return
        address = ip if msg["port"] == 80 else f"{ip}:{msg['port']}"
        if msg.get("ask") and time.time() - getattr(self, "_answered", 0) > 5:     # (a radio just started: answer now)
            self._answered = time.time()
            threading.Thread(target=self._send_beacon, daemon=True).start()
        was = self.seen.get(msg["id"])
        self.seen[msg["id"]] = {"id": msg["id"], "name": str(msg.get("name", ""))[:60], "address": address,
                                "version": str(msg.get("version", ""))[:20], "at": time.time()}
        peers = self.peers()
        mine = next((p for p in peers if p["id"] == msg["id"]), None)
        if mine is not None and mine.get("address") != address:      # (it moved: DHCP gave it another address)
            self._save_peers([{**p, "address": address} if p["id"] == msg["id"] else p for p in peers])
            log.info("sync: %s is at %s now", mine.get("name"), address)
        if mine is not None and was is None:                          # (back on the network: catch up)
            threading.Thread(target=self._quietly, args=(msg["id"],), daemon=True).start()

    def _quietly(self, peer_id: str) -> None:
        try:
            self.pull(peer_id)
        except Exception:
            log.exception("sync with %s failed", peer_id)

    def start(self) -> None:
        threading.Thread(target=self._beacon, name="sync-beacon", daemon=True).start()
        threading.Thread(target=self._listen, name="sync-listen", daemon=True).start()
        threading.Thread(target=self._every, name="sync", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _send_beacon(self, ask: bool = False) -> None:
        """Say this radio's here (ask: and ask the others to say so too -- just started, it wants to know now)."""
        try:
            if not self.protected():
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    s.sendto(json.dumps({**self.hello(), **({"ask": True} if ask else {})}).encode(),
                             ("255.255.255.255", BEACON_PORT))
        except OSError:
            pass                                     # (no network: nothing to tell)

    def _beacon(self) -> None:
        self._stop.wait(1)                           # (the listener first, to hear the answers)
        self._send_beacon(ask=True)
        while not self._stop.wait(BEACON_EVERY_S):
            self._send_beacon()

    def _listen(self) -> None:
        while not self._stop.is_set():
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind(("", BEACON_PORT))
                    s.settimeout(5)
                    while not self._stop.is_set():
                        try:
                            data, (ip, _port) = s.recvfrom(2048)
                        except socket.timeout:
                            continue
                        try:
                            self.heard(json.loads(data), ip)
                        except ValueError:
                            pass
            except OSError:
                self._stop.wait(30)                  # (no network yet, or the port is taken: try again later)

    def _every(self) -> None:
        self._stop.wait(20 + random.uniform(0, 60))  # (let the radio settle first; and not in step with the others)
        while not self._stop.is_set():
            for p in self.peers():
                self._quietly(p["id"])
            self._stop.wait(SYNC_EVERY_S)


def _changes(name: str, before, after) -> dict:
    """{"added", "changed", "deleted"}: how after differs from before."""
    b, a = _as_map(name, before), _as_map(name, after)
    return {"added": sum(1 for k in a if k not in b),
            "changed": sum(1 for k in a if k in b and _canon(a[k]) != _canon(b[k])),
            "deleted": sum(1 for k in b if k not in a)}


def _gone(name: str, before, after) -> int:
    """How many of before's items aren't in after."""
    a = _as_map(name, after)
    return sum(1 for k in _as_map(name, before) if k not in a)


def _digest(collections: dict) -> str:
    """The collections' contents, as a short fingerprint (a list's order doesn't count)."""
    return hashlib.sha256(_canon({n: _as_map(n, v) for n, v in collections.items()}).encode()).hexdigest()[:24]


def _clean_address(address: str) -> str:
    a = (address or "").strip().removeprefix("http://").removeprefix("https://").strip("/")
    if not a or "/" in a or " " in a or len(a) > 100:
        raise ValueError("an address like 192.168.1.20, or 192.168.1.20:8080")
    return a


def _fetch(url: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read())


