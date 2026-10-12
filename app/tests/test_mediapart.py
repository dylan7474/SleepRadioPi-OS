"""The music partition made by the radio itself (mediapart.py)."""
import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from sleepradiopi import mediapart as mp

MB = 2048                                            # sectors
# The card as the image leaves it (sdcard.img of v1.9.4): boot, two root slots, an extended partition with "data" in it.
BOOT, ROOT_A, ROOT_B, EXT, DATA = (8192, 131072), (139264, 524288), (663552, 524288), (1187840, 532480), (1196032, 524288)


def _card(path: Path, sectors: int, tail: bytes = b"") -> Path:
    """A card of that many sectors with the image's partition table on it (and nothing else: a sparse file)."""
    mbr = bytes(446) + mp._entry(0x0C, *BOOT, BOOT[0], 0x80) + mp._entry(0x83, *ROOT_A, ROOT_A[0]) + mp._entry(0x83, *ROOT_B, ROOT_B[0]) \
        + mp._entry(0x0F, *EXT, EXT[0]) + b"\x55\xaa"
    ebr = bytes(446) + mp._entry(0x83, DATA[0] - EXT[0], DATA[1], DATA[0]) + bytes(48) + b"\x55\xaa"
    with open(path, "wb") as f:
        f.truncate(sectors * 512)
        f.write(mbr)
        f.seek(EXT[0] * 512)
        f.write(ebr)
    return path


def _sector(path: Path, n: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(n * 512)
        return f.read(512)


def _whole(path: Path) -> bytes:
    """Every sector that isn't blank, with where it is (the files are sparse and mostly nothing)."""
    out = []
    for n in (0, EXT[0], 1726464):
        out.append(_sector(path, n))
    return b"".join(out)


def test_the_table_is_written_as_sfdisk_on_a_pc_writes_it(tmp_path: Path) -> None:
    card = _card(tmp_path / "card.img", 4 * 1024 * MB)            # a 4 GB card
    todo = mp.plan(card)
    assert (todo["start"], todo["sectors"]) == (1728512, 6660096) and [s for s, _ in todo["writes"]] == [1726464, EXT[0], 0]
    mp.write(card, todo)
    # (what `sfdisk` made of the same card, as scripts/provision-media.sh uses it: taken from a real run)
    as_sfdisk = {(0, 3): "00ef27490f2aa00a0020120000e06d00", (EXT[0], 1): "00770d6b052aa00a0038080000a86500", (1726464, 0): "00972d6b832aa00a0008000000a06500"}
    for (sector, entry), want in as_sfdisk.items():
        assert _sector(card, sector)[446 + 16 * entry:462 + 16 * entry].hex() == want, (sector, entry)
    assert _sector(card, 1726464)[510:] == b"\x55\xaa" and _sector(card, 1726464)[462:510] == bytes(48)
    assert _sector(card, 0)[:494] == _sector(_card(tmp_path / "as-it-was.img", 4 * 1024 * MB), 0)[:494]     # the first three partitions untouched
    assert _sector(card, EXT[0])[446:462] == mp._entry(0x83, DATA[0] - EXT[0], DATA[1], DATA[0])               # ...and "data"
    if shutil.which("sfdisk"):                       # ...and if it's on this computer, from it now
        ref = _card(tmp_path / "ref.img", 4 * 1024 * MB)
        subprocess.run(["sfdisk", "--no-reread", "-q", "-N", "4", str(ref)], input=",+\n", text=True, check=True, capture_output=True)
        subprocess.run(["sfdisk", "--no-reread", "-q", "-N", "6", str(ref)], input="1728512,+,L\n", text=True, check=True, capture_output=True)
        assert _whole(card) == _whole(ref)
    big = mp.plan(_card(tmp_path / "big.img", 59_000 * MB))       # a 64 GB card: past what the old three-byte addresses hold
    assert big["start"] == 1728512 and big["writes"][0][1][446 + 5:446 + 8] == b"\xfe\xff\xff"


def test_a_card_that_isnt_exactly_as_the_image_leaves_it_is_left_alone(tmp_path: Path) -> None:
    for name, sectors, change in (
            ("no room after the settings (a card the size of the image)", EXT[0] + EXT[1], None),
            ("hardly any room", EXT[0] + EXT[1] + 20 * MB, None),
            ("not a partition table", 4 * 1024 * MB, (0, 510, b"\0\0")),
            ("the fourth isn't an extended partition", 4 * 1024 * MB, (0, 446 + 48 + 4, b"\x83")),
            ("the second isn't a root slot", 4 * 1024 * MB, (0, 446 + 16 + 4, b"\x07")),
            ("something else in the extended partition's table", 4 * 1024 * MB, (EXT[0], 446 + 32 + 4, b"\x83")),
            ("a second partition already there, somewhere else", 4 * 1024 * MB, (EXT[0], 446 + 16, mp._entry(0x05, 600000, 9000, 1787840)))):
        card = _card(tmp_path / "card.img", sectors)
        if change:
            with open(card, "r+b") as f:
                f.seek(change[0] * 512 + change[1])
                f.write(change[2])
        before = _whole(card)
        with pytest.raises(ValueError):
            mp.plan(card)
        note = tmp_path / "note.json"
        note.unlink(missing_ok=True)
        assert mp.run(card, note, tmp_path / "p6", tmp_path / "sys", formatter=lambda p: pytest.fail("formatted!")) == "left", name
        assert _whole(card) == before and json.loads(note.read_text())["state"] == "left", name
        assert mp.run(card, note, tmp_path / "p6", tmp_path / "sys") == "left"       # ...and not looked at again


def test_a_radio_set_up_on_a_pc_is_never_touched(tmp_path: Path) -> None:
    card = _card(tmp_path / "card.img", 4 * 1024 * MB)
    mp.write(card, mp.plan(card))                    # as provision-media.sh left it: the same table, made elsewhere
    part = tmp_path / "p6"
    part.write_bytes(bytes(1080) + b"\x53\xef" + bytes(4096))     # ...with its ext4 filesystem, full of music
    before, music = _whole(card), part.read_bytes()
    note = tmp_path / "data" / "media-partition.json"
    assert mp.run(card, note, part, tmp_path / "sys", formatter=lambda p: pytest.fail("formatted!")) == "left"
    assert _whole(card) == before and part.read_bytes() == music and not note.exists()      # nothing written anywhere


def _sys(folder: Path, start: int, sectors: int) -> Path:
    folder.mkdir(exist_ok=True)
    (folder / "start").write_text(f"{start}\n")
    (folder / "size").write_text(f"{sectors}\n")
    return folder


def test_a_new_card_is_given_its_music_partition_in_two_starts(tmp_path: Path) -> None:
    card, part, note = _card(tmp_path / "card.img", 4 * 1024 * MB), tmp_path / "p6", tmp_path / "data" / "media-partition.json"
    formatted = []
    assert mp.run(card, note, part, tmp_path / "sys", formatter=formatted.append, config=tmp_path / "radio" / ".config" / "sleepradiopi" / "config.json") == "restart"       # the first start: the table
    state = json.loads(note.read_text())
    assert (state["state"], state["start"], state["sectors"]) == ("made", 1728512, 6660096) and formatted == []
    assert mp.plan(card)["writes"][2][1] == _sector(card, 0)      # (written: doing it again changes nothing)
    part.write_bytes(bytes(8192))                    # the second: the partition is there, and blank
    conf = tmp_path / "radio" / ".config" / "sleepradiopi" / "config.json"
    assert not conf.exists()                         # (nothing of the radio's own is written before there's somewhere for music)
    assert mp.run(card, note, part, _sys(tmp_path / "sys", 1728512, 6660096), formatter=formatted.append, config=conf) == "ready" and formatted == [part]
    assert json.loads(note.read_text())["state"] == "ready"
    # ...and its first settings, as a PC would have given it: where the music is, and that it has a speaker
    assert json.loads(conf.read_text()) == {"music_folder": "/media/music", "voices_folder": "/media/voices", "jingles_folder": "/media/jingles",
                                            "speaker_enabled": True, "speaker_mono": False}
    assert mp.run(card, note, part, tmp_path / "sys", formatter=lambda p: pytest.fail("formatted again!"), config=conf) == "left"       # every start after: nothing


def test_the_first_settings_are_given_and_a_radios_own_are_kept(tmp_path: Path) -> None:
    conf = tmp_path / "radio" / ".config" / "sleepradiopi" / "config.json"
    assert mp.starter(conf) is True and json.loads(conf.read_text())["speaker_enabled"] is True
    assert mp.starter(conf) is False                 # (already there: nothing written)
    # settings the station wrote for itself before there was a music partition (every folder "not set"): told where they are, the rest kept
    conf.write_text(json.dumps({"music_folder": None, "jingles_folder": None, "voices_folder": None, "speaker_enabled": False, "station_name": "Carisbrooke", "volume": 40}))
    assert mp.starter(conf) is True
    assert json.loads(conf.read_text()) == {"music_folder": "/media/music", "jingles_folder": "/media/jingles", "voices_folder": "/media/voices",
                                            "speaker_enabled": False, "station_name": "Carisbrooke", "volume": 40}
    conf.write_text(json.dumps({"music_folder": "/media/music", "voices_folder": "/somewhere/else", "jingles_folder": "/media/jingles"}))
    assert mp.starter(conf) is False and json.loads(conf.read_text())["voices_folder"] == "/somewhere/else"      # a choice already made is left
    conf.write_text("{ not json")                    # settings that can't be read: not mended here
    assert mp.starter(conf) is False and conf.read_text() == "{ not json"


def test_only_the_partition_made_here_is_formatted_and_only_if_its_empty(tmp_path: Path) -> None:
    card, part, note = _card(tmp_path / "card.img", 4 * 1024 * MB), tmp_path / "p6", tmp_path / "note.json"
    no = lambda p: pytest.fail("formatted!")
    assert mp.run(card, note, part, tmp_path / "sys", formatter=no) == "restart"
    made = note.read_text()
    # music from before the card was flashed again: an ext filesystem is already where the partition is
    part.write_bytes(bytes(1080) + b"\x53\xef" + bytes(4096))
    assert mp.run(card, note, part, _sys(tmp_path / "sys", 1728512, 6660096), formatter=no, config=tmp_path / "c" / "x" / "y" / "config.json") == "ready" and json.loads(note.read_text())["state"] == "ready"
    note.write_text(made)                            # a FAT filesystem there (somebody's memory card): kept too
    part.write_bytes(bytes(82) + b"FAT32   " + bytes(420) + b"\x55\xaa" + bytes(4096))
    assert mp.run(card, note, part, tmp_path / "sys", formatter=no, config=tmp_path / "c" / "x" / "y" / "config.json") == "ready"
    note.write_text(made)                            # the partition there isn't the one made here: left
    part.write_bytes(bytes(8192))
    assert mp.run(card, note, part, _sys(tmp_path / "sys", 1728512, 999), formatter=no) == "left" and json.loads(note.read_text())["state"] == "left"
    note.write_text(made)                            # made, but not there after the restart: left, and not asked for again
    shutil.rmtree(tmp_path / "sys")
    assert mp.run(card, note, part, tmp_path / "sys", formatter=no) == "left" and json.loads(note.read_text())["state"] == "left"
    note.write_text("{ not json")                    # a note that can't be read: nothing done
    assert mp.run(card, note, part, tmp_path / "sys", formatter=no) == "left"
    assert mp.unused(part) and not mp.unused(tmp_path / "card.img") is None


def test_a_power_cut_part_way_through_is_finished_at_the_next_start(tmp_path: Path) -> None:
    card, note = _card(tmp_path / "card.img", 4 * 1024 * MB), tmp_path / "note.json"
    todo = mp.plan(card)
    for done in (0, 1, 2):                           # the plug pulled after none, one or two of the three sectors
        card = _card(tmp_path / "card.img", 4 * 1024 * MB)
        note.write_text(json.dumps({"state": "making", "start": todo["start"], "sectors": todo["sectors"]}))
        mp.write(card, {"writes": todo["writes"][:done]})
        assert mp._entries(_sector(card, 0))[3][2] == EXT[1]      # (the old table still: the card starts as it did)
        assert mp.run(card, note, tmp_path / "p6", tmp_path / "sys") == "restart" and json.loads(note.read_text())["state"] == "made"
        assert [_sector(card, s) for s, _ in todo["writes"]] == [d for _, d in todo["writes"]]


def test_a_real_filesystem_is_made_where_there_is_the_program_for_it(tmp_path: Path) -> None:
    if not shutil.which("mkfs.ext4"):
        pytest.skip("no mkfs.ext4 on this computer")
    part = tmp_path / "p6"
    with open(part, "wb") as f:
        f.truncate(64 * 1024 * 1024)
    assert mp.unused(part)
    mp.format(part, mount=False)                     # (mounting it needs root: the folders are made on the radio)
    assert not mp.unused(part)
    with open(part, "rb") as f:
        f.seek(1024 + 120)
        assert f.read(5) == b"media"                 # its label
