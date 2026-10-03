"""The music library manager behind the web page: browse the music and
jingles folders, upload files (or whole folders) into them, make folders,
and delete.

/media is read-only on the appliance, so a power cut can't catch it
half-written. While files are being changed the station asks the root helper
media-rw-watch to make it writable -- the time goes in LEASE, rewritten on
each change -- and /media goes back to read-only a couple of minutes after
the last one (or when done() says so). Off the appliance (a desktop run)
the folders are simply written.

Uploads go to a hidden .part file on the same partition and are renamed
into place once complete, so a broken upload never leaves half a song in the
library. The station rescans the library (only new files' tags are read)
once changes have settled.

Tags (title, artist, album, track number) can be changed too: the file is
copied to a hidden .part file, the tags are written there, and it's swapped
in, so a power cut never leaves a half-written song. What a file's tags were
before the first change is kept (TAG_LOG), so they can be put back.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import mutagen

from sleepradiopi.broadcast.library import AUDIO_EXTENSIONS
from sleepradiopi.config.atomic import write_atomic
from sleepradiopi.playback.audiobooks import BOOK_FILES

log = logging.getLogger(__name__)

LEASE = Path(os.environ.get("SLEEPRADIOPI_MEDIA_LEASE", "/run/sleepradiopi/media-rw"))
WAIT_WRITABLE_S = 20.0       # for the helper to remount /media (and, the first time, chown it)
SETTLE_S = 45.0              # rescan the library this long after the last change
MAX_FILE = 1 << 30           # 1 GB: a long FLAC is ~0.5 GB
KEEP_FREE = 200 << 20        # never fill the card
CHUNK = 1 << 20
IMAGES = {".jpg", ".jpeg", ".png"}   # cover art comes along in album folders; the library ignores it
MAX_NAME = 200
INCOMING = ".incoming"
TAG_FIELDS = {"title": "title", "artist": "artist", "album": "album", "track": "tracknumber"}   # ours -> mutagen's easy keys
TAGGED = {".mp3", ".m4a", ".m4b", ".flac", ".ogg", ".opus"}      # (a .wav has nowhere to keep them)
TAG_KINDS = ("music", "ondemand", "audiobooks")
MAX_TAG = 200
MAX_TAG_FILES = 2000         # in one folder's list


def _easy(path: Path, suffix: str):
    """The file opened for its tags, by the kind its name says (a .part copy has lost its ending)."""
    suffix = suffix.lower()
    if suffix == ".mp3":
        from mutagen.mp3 import EasyMP3
        return EasyMP3(path)
    if suffix in (".m4a", ".m4b"):
        from mutagen.easymp4 import EasyMP4
        return EasyMP4(path)
    if suffix == ".flac":
        from mutagen.flac import FLAC
        return FLAC(path)
    if suffix == ".opus":
        from mutagen.oggopus import OggOpus
        return OggOpus(path)
    from mutagen.oggopus import OggOpus          # .ogg: Vorbis, or Opus in an .ogg
    from mutagen.oggvorbis import OggVorbis
    for cls in (OggVorbis, OggOpus):
        try:
            return cls(path)
        except mutagen.MutagenError:
            continue
    raise MediaError("the radio can't read that file's tags")


def read_tags(path: Path) -> dict:
    """{"title", "artist", "album", "track"} as the file has them ("" where it has none)."""
    try:
        f = _easy(path, path.suffix)
    except MediaError:
        raise
    except Exception:
        return {k: "" for k in TAG_FIELDS}
    tags = f.tags or {}
    out = {}
    for ours, theirs in TAG_FIELDS.items():
        try:
            v = tags.get(theirs)
        except Exception:
            v = None
        out[ours] = (v[0] if isinstance(v, list) and v else v if isinstance(v, str) else "").strip()
    return out


class MediaError(ValueError):
    """A request the library can't do (a bad path, no room, ...): its text is for the page."""


def _clean_part(part: str) -> str:
    part = part.strip()
    if (not part or part in (".", "..") or part.startswith(".") or "/" in part or "\\" in part
            or "\0" in part or len(part) > MAX_NAME):
        raise MediaError(f"{part[:40]!r} isn't a name the radio can use")
    return part


def _clean_rel(rel: str | None) -> list[str]:
    """'Artist/Album' -> ['Artist', 'Album'] (no .., hidden or empty parts)."""
    if not rel:
        return []
    if not isinstance(rel, str) or len(rel) > 1000:
        raise MediaError("that isn't a folder on the radio")
    return [_clean_part(p) for p in rel.replace("\\", "/").split("/") if p not in ("",)]


class MediaLibrary:
    def __init__(self, roots: dict[str, Path], on_changed: Callable[[set[str]], None] | None = None,
                 lease: Path = LEASE, tag_log: Path | None = None) -> None:
        self.roots = {k: Path(v) for k, v in roots.items()}
        self.tag_log = Path(tag_log) if tag_log is not None else None   # what tags were before they were first changed
        self.on_changed = on_changed
        self.lease = lease
        self._lock = threading.Lock()
        self._dirty: set[str] = set()   # the libraries changed (music, jingles...): only they're rescanned
        self._last_change = 0.0
        self._settle: threading.Timer | None = None

    # --- paths --------------------------------------------------------------------------

    def _root(self, kind: str) -> Path:
        if kind not in self.roots:
            raise MediaError("that isn't one of the radio's media folders")
        return self.roots[kind]

    def resolve(self, kind: str, rel: str | None) -> Path:
        root = self._root(kind)
        path = root.joinpath(*_clean_rel(rel))
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError:
            raise MediaError("that isn't inside the library") from None
        return path

    # --- looking --------------------------------------------------------------------------

    def usage(self) -> dict:
        root = next(iter(self.roots.values()))
        try:
            du = shutil.disk_usage(root if root.exists() else root.parent)
        except OSError:
            return {"total": 0, "used": 0, "free": 0}
        return {"total": du.total, "used": du.used, "free": du.free}

    def list(self, kind: str, rel: str | None = "") -> dict:
        path = self.resolve(kind, rel)
        if path == self._root(kind) and not path.exists():      # not made yet: empty
            return {"kind": kind, "path": "", "folders": [], "files": [], "usage": self.usage(),
                    "writable": self._writable(path)}
        if not path.is_dir():
            raise MediaError("that folder isn't there any more")
        folders, files = [], []
        with os.scandir(path) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_dir():
                        with os.scandir(e.path) as inner:
                            n = sum(1 for x in inner if not x.name.startswith("."))
                        folders.append({"name": e.name, "items": n})
                    elif e.is_file():
                        ext = Path(e.name).suffix.lower()
                        files.append({"name": e.name, "size": e.stat().st_size,
                                      "audio": ext in AUDIO_EXTENSIONS or ext in BOOK_FILES})
                except OSError:
                    continue
        key = lambda d: d["name"].lower()
        parts = _clean_rel(rel)
        return {"kind": kind, "path": "/".join(parts), "folders": sorted(folders, key=key),
                "files": sorted(files, key=key), "usage": self.usage(),
                "writable": self._writable(self._root(kind))}

    # --- making it writable ---------------------------------------------------------------

    @staticmethod
    def _writable(root: Path) -> bool:
        return os.access(root if root.exists() else root.parent, os.W_OK)

    def _open(self, root: Path) -> None:
        """Keep /media writable (renewing the lease), waiting for it the first time."""
        try:
            self.lease.parent.mkdir(parents=True, exist_ok=True)
            self.lease.write_text(str(int(time.time())))
        except OSError:
            pass                                  # (a desktop run: no helper, nothing to ask)
        deadline = time.monotonic() + WAIT_WRITABLE_S
        while not self._writable(root):
            if time.monotonic() > deadline:
                raise MediaError("the radio couldn't make its music storage writable")
            time.sleep(0.5)
        try:
            root.mkdir(exist_ok=True)                 # (On demand, on a card from before it)
        except OSError as e:
            raise MediaError(f"couldn't make the folder ({e.strerror})") from None

    def _changed(self, *kinds: str) -> None:
        with self._lock:
            self._dirty.update(kinds)
            self._last_change = time.monotonic()
            if self._settle is not None:
                self._settle.cancel()
            self._settle = threading.Timer(SETTLE_S, self.done)
            self._settle.daemon = True
            self._settle.start()

    def done(self) -> bool:
        """Changes are finished: let /media go read-only and rescan the libraries that
        changed (a jingle upload rescanned all 2,000+ songs too, and the music stuttered).
        True if anything had changed."""
        with self._lock:
            if self._settle is not None:
                self._settle.cancel()
                self._settle = None
            dirty, self._dirty = self._dirty, set()
        try:
            self.lease.unlink()
        except OSError:
            pass
        if dirty and self.on_changed is not None:
            try:
                self.on_changed(dirty)
            except Exception:
                log.exception("library rescan failed")
        return bool(dirty)

    # --- tags -------------------------------------------------------------------------------

    def _originals(self) -> dict:
        if self.tag_log is None:
            return {}
        try:
            return json.loads(self.tag_log.read_text())
        except (OSError, ValueError):
            return {}

    def _tag_file(self, kind: str, rel: str) -> Path:
        if kind not in TAG_KINDS:
            raise MediaError("tags are for the music, On demand and the audiobooks")
        path = self.resolve(kind, rel)
        if not path.is_file():
            raise MediaError("that track isn't there any more")
        if path.suffix.lower() not in TAGGED:
            raise MediaError(f"a {path.suffix.lower() or 'file like that'} has no tags to change")
        return path

    def tags(self, kind: str, rel: str | None) -> dict:
        """The tags of a track, or of every track under a folder (in path order):
        {"folder": bool, "name", "files": [{"path", "title", "artist", "album",
        "track", "changed": its tags aren't the ones it came with, "fixed": it has none to change}]}."""
        if kind not in TAG_KINDS:
            raise MediaError("tags are for the music, On demand and the audiobooks")
        root, path = self._root(kind), self.resolve(kind, rel)
        if not path.exists():
            raise MediaError("that isn't there any more")
        playable = BOOK_FILES if kind == "audiobooks" else AUDIO_EXTENSIONS
        files = [path] if path.is_file() else sorted(
            p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in playable
            and not any(part.startswith(".") for part in p.relative_to(path).parts))
        if len(files) > MAX_TAG_FILES:
            raise MediaError(f"that folder has more than {MAX_TAG_FILES} tracks: open a folder inside it")
        originals = self._originals()
        out = []
        for p in files:
            r = p.relative_to(root).as_posix()
            fixed = p.suffix.lower() not in TAGGED
            out.append({"path": r, **({k: "" for k in TAG_FIELDS} if fixed else read_tags(p)),
                        "changed": f"{kind}/{r}" in originals, **({"fixed": True} if fixed else {})})
        return {"folder": path.is_dir(), "name": path.name, "files": out}

    def retag(self, kind: str, rel: str, tags: dict, log_original: bool = True) -> dict:
        """Change a track's tags: {"title", "artist", "album", "track"}, any of them
        ("" takes one off). The file is rewritten as a copy and swapped in. Returns
        its tags as they are now."""
        path, root = self._tag_file(kind, rel), self._root(kind)
        if not isinstance(tags, dict) or not tags or any(k not in TAG_FIELDS for k in tags):
            raise MediaError(f"send some of: {', '.join(TAG_FIELDS)}")
        new = {}
        for k, v in tags.items():
            if not isinstance(v, str) or len(v) > MAX_TAG:
                raise MediaError(f"the {k} is text, up to {MAX_TAG} letters")
            v = " ".join(v.split())
            if k == "track" and v and not all(p.isdigit() for p in v.split("/")):
                raise MediaError("the track number is a number (3, or 3/12)")
            new[k] = v
        before = read_tags(path)
        if all(before[k] == v for k, v in new.items()):
            return before
        size = path.stat().st_size
        if self.usage()["free"] - size < KEEP_FREE:
            raise MediaError(f"{path.name}: not enough room left on the radio to rewrite it")
        self._open(root)
        incoming = root / INCOMING
        incoming.mkdir(exist_ok=True)
        tmp = incoming / f"{uuid.uuid4().hex}.part"
        try:
            shutil.copyfile(path, tmp)
            f = _easy(tmp, path.suffix)
            if f.tags is None:
                f.add_tags()
            for k, v in new.items():
                if v:
                    f[TAG_FIELDS[k]] = [v]
                elif TAG_FIELDS[k] in f:
                    del f[TAG_FIELDS[k]]
            f.save()
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
            shutil.copymode(path, tmp)
            os.replace(tmp, path)
        except MediaError:
            raise
        except Exception as e:
            raise MediaError(f"{path.name}: couldn't change its tags ({getattr(e, 'strerror', None) or e})") from None
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        key = f"{kind}/{path.relative_to(root).as_posix()}"
        if self.tag_log is not None:
            with self._lock:
                originals = self._originals()
                if log_original and key not in originals:
                    originals[key] = before
                elif not log_original:
                    originals.pop(key, None)
                else:
                    originals = None
                if originals is not None:
                    try:
                        self.tag_log.parent.mkdir(parents=True, exist_ok=True)
                        write_atomic(self.tag_log, json.dumps(originals))
                    except OSError as e:
                        log.warning("couldn't keep the original tags: %s", e)
        log.info("media: tags of %s changed (%s)", key, ", ".join(new))
        self._changed(kind)
        return read_tags(path)

    def untag(self, kind: str, rel: str) -> dict:
        """Put a track's tags back as they were before they were first changed here."""
        path, root = self._tag_file(kind, rel), self._root(kind)
        original = self._originals().get(f"{kind}/{path.relative_to(root).as_posix()}")
        if original is None:
            raise MediaError("its tags are the ones it came with")
        return self.retag(kind, rel, {k: original.get(k, "") for k in TAG_FIELDS}, log_original=False)

    # --- changing ---------------------------------------------------------------------------

    def mkdir(self, kind: str, rel: str, name: str) -> str:
        parent = self.resolve(kind, rel)
        path = parent / _clean_part(name)
        self._open(self._root(kind))
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise MediaError(f"couldn't make the folder ({e.strerror})") from None
        log.info("media: made %s/%s", kind, path.relative_to(self._root(kind)))
        return str(path.relative_to(self._root(kind)))

    def delete(self, kind: str, rel: str) -> None:
        root = self._root(kind)
        path = self.resolve(kind, rel)
        if path == root:
            raise MediaError("the whole library can't be deleted from here")
        if not path.exists():
            raise MediaError("that isn't there any more")
        self._open(root)
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as e:
            raise MediaError(f"couldn't delete it ({e.strerror})") from None
        log.info("media: deleted %s/%s", kind, rel)
        self._changed(kind)

    def rename(self, kind: str, rel: str, new_name: str) -> str:
        """Rename a file or folder where it is (a theme's jingles folder, with the theme).
        Returns its new path."""
        root = self._root(kind)
        src = self.resolve(kind, rel)
        if src == root:
            raise MediaError("a whole library can't be renamed")
        if not src.exists():
            raise MediaError("that isn't there any more")
        dest = src.with_name(_clean_part(new_name))
        if dest == src:
            return str(dest.relative_to(root))
        if dest.exists() and dest.name.lower() != src.name.lower():
            raise MediaError(f"there's already a {dest.name} there")
        self._open(root)
        try:
            os.rename(src, dest)
        except OSError as e:
            raise MediaError(f"couldn't rename it ({e.strerror})") from None
        log.info("media: renamed %s/%s to %s", kind, rel, dest.name)
        self._changed(kind)
        return str(dest.relative_to(root))

    def move(self, kind: str, rel: str, to_kind: str, to_folder: str) -> str:
        """Move a file or folder into another folder, in the same library or
        another (music <-> ondemand): /media is one filesystem, so it's a rename.
        Returns its new path in to_kind."""
        src_root, dst_root = self._root(kind), self._root(to_kind)
        src = self.resolve(kind, rel)
        if src == src_root:
            raise MediaError("a whole library can't be moved")
        if not src.exists():
            raise MediaError("that isn't there any more")
        self._open(dst_root)
        dst_dir = self.resolve(to_kind, to_folder)
        if not dst_dir.is_dir():
            raise MediaError("that folder isn't there any more")
        if src.is_dir() and (dst_dir == src or src in dst_dir.parents):
            raise MediaError("a folder can't go inside itself")
        dest = dst_dir / src.name
        if dest == src:
            return str(dest.relative_to(dst_root))
        if dest.exists():
            raise MediaError(f"there's already a {src.name} there")
        try:
            os.rename(src, dest)
        except OSError as e:
            raise MediaError(f"couldn't move it ({e.strerror})") from None
        log.info("media: moved %s/%s to %s/%s", kind, rel, to_kind, dest.relative_to(dst_root))
        self._changed(kind, to_kind)
        return str(dest.relative_to(dst_root))

    def receive(self, kind: str, folder: str, name: str, length: int, read: Callable[[int], bytes]) -> dict:
        """One uploaded file: `name` may be a path inside `folder` (a folder upload,
        e.g. 'Rubber Soul/01 - Drive My Car.mp3'). Streamed to a hidden .part file,
        then renamed into place. Returns {"path", "status": "added" | "same"}."""
        root = self._root(kind)
        parts = _clean_rel(name)
        if not parts:
            raise MediaError("the file has no name")
        ext = Path(parts[-1]).suffix.lower()
        playable = BOOK_FILES if kind == "audiobooks" else AUDIO_EXTENSIONS
        if ext not in playable and not (kind != "jingles" and ext in IMAGES):
            raise MediaError(f"{parts[-1]}: the radio plays {', '.join(sorted(playable))} files here")
        if length <= 0 or length > MAX_FILE:
            raise MediaError(f"{parts[-1]}: files up to {MAX_FILE >> 30} GB")
        if self.usage()["free"] - length < KEEP_FREE:
            raise MediaError(f"{parts[-1]}: not enough room left on the radio")
        dest = self.resolve(kind, "/".join(_clean_rel(folder) + parts))
        self._open(root)
        if dest.is_file() and dest.stat().st_size == length:
            _drain(read, length)
            return {"path": str(dest.relative_to(root)), "status": "same"}
        incoming = root / INCOMING
        incoming.mkdir(exist_ok=True)
        tmp = incoming / f"{uuid.uuid4().hex}.part"
        try:
            with open(tmp, "wb") as f:
                left, renewed = length, time.monotonic()
                while left:
                    if time.monotonic() - renewed > 30:
                        self._open(root)              # keep the lease fresh on a long upload
                        renewed = time.monotonic()
                    data = read(min(CHUNK, left))
                    if not data:
                        raise MediaError(f"{parts[-1]}: the upload was cut short")
                    f.write(data)
                    left -= len(data)
                f.flush()
                os.fsync(f.fileno())
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():                         # a different file of that name: keep both
                n = 2
                while (alt := dest.with_name(f"{dest.stem} ({n}){dest.suffix}")).exists():
                    n += 1
                dest = alt
            os.replace(tmp, dest)
        except OSError as e:
            raise MediaError(f"{parts[-1]}: couldn't save it ({e.strerror})") from None
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        log.info("media: added %s/%s (%d bytes)", kind, dest.relative_to(root), length)
        self._changed(kind)
        return {"path": str(dest.relative_to(root)), "status": "added"}


def _drain(read: Callable[[int], bytes], length: int) -> None:
    while length > 0:
        data = read(min(CHUNK, length))
        if not data:
            return
        length -= len(data)
