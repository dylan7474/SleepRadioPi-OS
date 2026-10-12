"""The music partition, made by the radio itself.

A card flashed from a release has the system and the settings on it (the
card layout: boot, two root slots, and an extended partition holding
"data"), and nothing after that: the music partition, p6, filling the rest
of the card, used to be added on a Linux PC (scripts/provision-media.sh).
This does the same on the radio, at its first start, so that music can be
put on from any computer through the web page:

  1. plan(): look at the partition table. Only a card that is exactly as
     the image leaves it is touched -- four entries, the fourth an extended
     partition holding one logical partition and nothing else, and room
     after it. A card that already has a p6 (every radio set up the old
     way) is left entirely alone, as is anything unexpected.
  2. write(): the extended partition is grown to the end of the card and a
     second logical partition added after the first, 1 MB on and rounded
     up to 4 MB as sfdisk on the PC does it (the same bytes: compared in
     the tests). Three sectors are written, the new one first, so that a
     power cut at any moment leaves either the old table or the new one's
     harmless beginnings; doing it again finishes it.
  3. The kernel only reads a table at start-up while the card is in use, so
     the radio restarts once. Then format(): the new partition is given an
     ext4 filesystem labelled "media" -- but only the partition this made
     (a note in /data says where it begins and how big it is, and it must
     match), and only if there is no filesystem of any kind on it.

A radio set up on a PC is also given its first settings there: where the
music is, and that it has a speaker. So is this one (starter()): without
them the station looks for music in its own home folder, finds none, and
plays through no speaker -- as the first card made this way did.

run() does whichever step is due; /etc/init.d/S00data (root, at start-up)
calls it when there is no music partition, or one is part made:
`python3 -m sleepradiopi.mediapart` -- its exit code 10 asks for a restart.
"""
from __future__ import annotations

import json
import logging
import os
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEVICE = Path("/dev/mmcblk0")
NOTE = Path("/data/media-partition.json")
FOLDERS = ("music", "jingles", "voices", "ondemand", "audiobooks")      # what a new music partition begins with
CONFIG = Path("/data/radio/.config/sleepradiopi/config.json")           # the station's settings (its home is /data/radio)
STARTER = {"music_folder": "/media/music", "voices_folder": "/media/voices", "jingles_folder": "/media/jingles"}
RESTART = 10                 # (the exit code that asks for one)

SECTOR = 512
ALIGN = 8192                 # 4 MB, in sectors: where a partition begins
EBR_GAP = 2048               # 1 MB: a logical partition begins this far after its own little table
ROOM_MIN = 131072            # 64 MB: less free card than this isn't worth a partition
EXTENDED = (0x05, 0x0F)
LINUX = 0x83
HEADS, SECTORS = 255, 63     # (the made-up geometry the old three-byte addresses are worked out with)


def _chs(lba: int) -> bytes:
    c, h, s = lba // (HEADS * SECTORS), (lba // SECTORS) % HEADS, lba % SECTORS + 1
    if c > 1023:
        return b"\xfe\xff\xff"
    return bytes([h, s | ((c >> 8) << 6), c & 0xFF])


def _entries(sector: bytes) -> list[tuple[int, int, int]]:
    """A partition table's four entries -> [(type, start, sectors)]. ValueError if it isn't one."""
    if len(sector) != SECTOR or sector[510:] != b"\x55\xaa":
        raise ValueError("no partition table there")
    return [(sector[446 + 16 * i + 4], *struct.unpack("<II", sector[446 + 16 * i + 8:446 + 16 * i + 16])) for i in range(4)]


def _entry(kind: int, start: int, sectors: int, first_lba: int, boot: int = 0) -> bytes:
    return bytes([boot]) + _chs(first_lba) + bytes([kind]) + _chs(first_lba + sectors - 1) + struct.pack("<II", start, sectors)


def _with(sector: bytes, i: int, entry: bytes) -> bytes:
    return sector[:446 + 16 * i] + entry + sector[446 + 16 * (i + 1):]


def plan(device: Path | str, size: int | None = None) -> dict:
    """What would be done to this card -> {"start", "sectors" (the new partition's), "writes": [(sector, bytes)]}:
    the sectors to write, in the order to write them. ValueError says why nothing is to be done."""
    with open(device, "rb") as f:
        total = (size if size is not None else f.seek(0, os.SEEK_END)) // SECTOR
        f.seek(0)
        mbr = f.read(SECTOR)
        parts = _entries(mbr)
        if not (parts[0][0] in (0x0B, 0x0C, 0x0E) and parts[1][0] == LINUX and parts[2][0] == LINUX and parts[3][0] in EXTENDED):
            raise ValueError("not a card laid out as this radio's are")
        kind, ext, ext_size = parts[3]
        f.seek(ext * SECTOR)
        ebr = f.read(SECTOR)
        logical = _entries(ebr)
    if logical[0][0] != LINUX or logical[2][0] or logical[3][0]:
        raise ValueError("not a card laid out as this radio's are")
    data_end = ext + logical[0][1] + logical[0][2]
    start = (data_end + EBR_GAP + ALIGN - 1) // ALIGN * ALIGN
    table = start - EBR_GAP                          # where the new partition's own table goes
    sectors = total - start
    want_link = (EXTENDED[0], table - ext, EBR_GAP + sectors)
    if logical[1][0] and logical[1] != want_link:    # (one this didn't begin: every radio set up on a PC)
        raise ValueError("the card already has a partition after the settings")
    if data_end > ext + ext_size or sectors < ROOM_MIN:
        raise ValueError("no room on the card after the settings")
    new = bytes(446) + _entry(LINUX, EBR_GAP, sectors, start) + bytes(48) + b"\x55\xaa"
    linked = _with(ebr, 1, _entry(EXTENDED[0], table - ext, EBR_GAP + sectors, table))
    grown = _with(mbr, 3, _entry(kind, ext, total - ext, ext, mbr[446 + 48]))
    return {"start": start, "sectors": sectors, "writes": [(table, new), (ext, linked), (0, grown)]}


def write(device: Path | str, todo: dict) -> None:
    """The plan's sectors written, each made sure of before the next."""
    fd = os.open(device, os.O_RDWR)
    try:
        for sector, data in todo["writes"]:
            os.pwrite(fd, data, sector * SECTOR)
            os.fsync(fd)
    finally:
        os.close(fd)


def unused(partition: Path | str) -> bool:
    """Is there no filesystem on this partition? Only then may it be formatted: an ext filesystem (music from
    before the card was flashed again, say) or a FAT, NTFS or exFAT one is left as it is."""
    with open(partition, "rb") as f:
        head = f.read(2048)
    if len(head) < 2048 or head[1080:1082] == b"\x53\xef":
        return False
    return not (head[510:512] == b"\x55\xaa" and any(mark in head[:90] for mark in (b"FAT", b"NTFS", b"EXFAT")))


def format(partition: Path | str, mkfs: str = "mkfs.ext4", mount: bool = True) -> None:
    """An empty ext4 filesystem labelled "media", with the folders a radio's music partition has.
    (One inode to 64 KB is plenty for music; no blocks kept back for root.)"""
    subprocess.run([mkfs, "-q", "-F", "-L", "media", "-m", "0", "-i", "65536", str(partition)], check=True)
    if not mount:
        return
    with tempfile.TemporaryDirectory() as at:
        subprocess.run(["mount", "-t", "ext4", str(partition), at], check=True)
        try:
            for name in FOLDERS:
                os.makedirs(os.path.join(at, name), mode=0o755, exist_ok=True)
        finally:
            subprocess.run(["umount", at], check=True)


def starter(config: Path = CONFIG) -> bool:
    """The settings a radio begins with, as scripts/provision-media.sh writes them on a PC: where the music, the
    voices and the jingles are, and that there's a speaker. A radio with no settings yet gets them all; one that
    has some is only told where the folders are, where it doesn't say. -> whether anything was written."""
    try:
        had = json.loads(config.read_text())
        if not isinstance(had, dict):
            return False
    except FileNotFoundError:
        had = None
    except (OSError, ValueError):
        return False                                 # (settings that can't be read: not this one's to mend)
    new = {**STARTER, "speaker_enabled": True, "speaker_mono": False} if had is None else {**had, **{k: v for k, v in STARTER.items() if not had.get(k)}}
    if new == had:
        return False
    from sleepradiopi.config.atomic import write_atomic
    write_atomic(config, json.dumps(new, indent=2) + "\n")
    try:                                             # (the station isn't root: its settings are its own, as its home is)
        owner = os.stat(config.parents[2])
        for path in (config, config.parent, config.parents[1]):
            os.chown(path, owner.st_uid, owner.st_gid)
    except OSError:
        pass
    return True


def _note(note: Path, **what) -> None:
    from sleepradiopi.config.atomic import write_atomic
    write_atomic(note, json.dumps({**what, "t": round(time.time())}))


def run(device: Path = DEVICE, note: Path = NOTE, partition: Path | None = None, where: Path | None = None, formatter=format,
        config: Path = CONFIG) -> str:
    """Do the step that's due. -> "restart" (the table was written: start again and it's there), "ready" (it's
    formatted: mount it), or "left" (nothing to do, or nothing that may be done). partition: the new one's
    device; where: the folder that says where it begins and how big it is (/sys/class/block/mmcblk0p6)."""
    partition = partition or Path(str(device) + "p6")
    where = where or Path("/sys/class/block") / partition.name
    try:
        state = json.loads(note.read_text())
    except FileNotFoundError:
        state = None
    except (OSError, ValueError):
        log.warning("media partition: %s can't be read: nothing done", note)
        return "left"
    if state is not None and state.get("state") in ("ready", "left"):
        return "left"
    if state is None or state.get("state") == "making":
        try:
            todo = plan(device)
        except (OSError, ValueError) as e:
            log.info("media partition: the card is left as it is (%s)", e)
            if state is None:
                _note(note, state="left", why=str(e))
            return "left"
        changes = []
        with open(device, "rb") as f:
            for sector, data in todo["writes"]:
                f.seek(sector * SECTOR)
                if f.read(SECTOR) != data:
                    changes.append(sector)
        if state is None and not changes:            # it has one already, made elsewhere: never this one's to touch
            return "left"
        _note(note, state="making", start=todo["start"], sectors=todo["sectors"])
        write(device, todo)
        _note(note, state="made", start=todo["start"], sectors=todo["sectors"])
        log.warning("media partition: made, %d MB from sector %d; restarting to find it", todo["sectors"] // 2048, todo["start"])
        return "restart"
    # made: formatted if it is the one made here (where it begins, how big) and there's nothing on it
    try:
        there = (int((where / "start").read_text()), int((where / "size").read_text()))
    except (OSError, ValueError):
        log.warning("media partition: made, but not there after the restart: left")
        _note(note, state="left", why="made, but not there after the restart")       # (and not asked for again: no going round and round)
        return "left"
    if there != (state.get("start"), state.get("sectors")):
        log.warning("media partition: the one there isn't the one made here: left")
        _note(note, state="left", why="another partition is there")
        return "left"
    if unused(partition):
        formatter(partition)
        log.warning("media partition: formatted")
    else:
        log.warning("media partition: there's a filesystem on it already (music from before?): kept as it is")
    if starter(config):
        log.warning("media partition: the radio's first settings written (music on /media, a speaker)")
    _note(note, state="ready", start=state["start"], sectors=state["sectors"])
    return "ready"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="media-partition: %(message)s")
    sys.exit(RESTART if run() == "restart" else 0)
