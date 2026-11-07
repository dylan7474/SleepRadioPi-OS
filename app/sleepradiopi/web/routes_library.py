"""The library: albums, playlists, programmes, books, podcasts, downloads, the media folders, profiles.

Methods of the web handler, moved out of server.py as they were: build(ctx) unpacks what they use of what
make_handler holds and returns them for make_handler to attach to Handler.
"""

from __future__ import annotations

import logging
import json
from pathlib import Path
from sleepradiopi import media as media_mod
from sleepradiopi.broadcast import playlists as playlists_mod
from sleepradiopi.broadcast import profiles
from sleepradiopi.config.settings import save_setting
from sleepradiopi.playback import podcasts as pod_mod
from urllib.parse import parse_qs
from urllib.parse import urlparse
from sleepradiopi.web.common import NO_EBOOKS

log = logging.getLogger("sleepradiopi.web.server")


def build(ctx):
    book_dl, config_file, ebook_dl, media, presets, speaker, station = ctx.book_dl, ctx.config_file, ctx.ebook_dl, ctx.media, ctx.presets, ctx.speaker, ctx.station

    def _selection(self) -> dict:
        return {"artist": station.artist, "profile": station.profile,
                "station_name": station.builder.station}

    def _save_selection(self) -> None:
        if config_file is not None:
            save_setting(config_file, "broadcast_artist", None)    # (artist radio is retired)
            save_setting(config_file, "broadcast_profile", station.profile)

    def _station(self) -> None:
        """/api/station {"profile": "Carisbrooke"}: play that theme; {"profile": null}
        or {"artist": null}: the theme last played, else Default. Saved. (Artist radio,
        {"artist": "The Beatles"}, is retired: make a theme with the artist.)"""
        try:
            body = self._body()
            value = body.get("profile")
            if body.get("artist"):
                self._error("artist radio has gone: make a theme with the artist in it")
                return
            if value is not None and (not isinstance(value, str) or len(value) > 200):
                raise ValueError("a name or null")
        except (ValueError, TypeError, KeyError, AttributeError):
            self.send_error(400)
            return
        value = value.strip() if value else None
        found = station.set_profile(value)
        self._save_selection()
        self._send(json.dumps({"found": found, **self._selection()}).encode(), "application/json")

    def _playlists(self) -> dict:
        return {"playlists": [station.playlist_view(p) for p in station.playlists],
                "playing": station.playlist_status()}

    def _programmes(self) -> dict:
        sched = station.scheduler
        return {"programmes": sched.programmes, "playing": sched.status(), "programme_mode": sched.quiet}

    def _programmes_post(self, path: str) -> None:
        """POST /api/programmes {"programmes": [...]} (the whole list, saved),
        /api/programmes/play {"name"}, /api/programmes/stop {"name"?}."""
        sched = station.scheduler
        try:
            body = self._body()
            if path == "/api/programmes":
                sched.set_programmes(body["programmes"])
                if config_file is not None:
                    save_setting(config_file, "programmes", sched.programmes)
            elif path == "/api/programmes/play":
                sched.play(body["name"])
                if speaker is not None and sched.run is not None:   # (only moments and switches: over what's on)
                    speaker.play()
            else:
                sched.stop(name=body.get("name") if isinstance(body.get("name"), str) else None)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            self._error(str(e) if isinstance(e, ValueError) else "send {\"name\"} or {\"programmes\": [...]}")
            return
        self._send(json.dumps(self._programmes()).encode(), "application/json")

    def _save_playlists(self) -> None:
        if config_file is not None:
            save_setting(config_file, "playlists", station.playlists)

    def _set_playlists(self) -> None:
        """POST /api/playlists {"playlists": [{"name", "tracks": [[root, path], ...]}]}:
        the whole list (new, renamed, reordered, removed), saved."""
        try:
            station.set_playlists(self._body()["playlists"])
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            self._error(str(e) if isinstance(e, ValueError) else "send {\"playlists\": [...]}")
            return
        self._save_playlists()
        self._send(json.dumps(self._playlists()).encode(), "application/json")

    def _rename_playlist(self) -> None:
        """POST /api/playlists/rename {"old", "new"}: buttons and programmes that
        play it follow it. Saved."""
        try:
            body = self._body()
            old, new = body["old"], body["new"]
            if not isinstance(old, str) or not isinstance(new, str):
                raise ValueError("send {\"old\", \"new\"}")
            was = playlists_mod.find(station.playlists, old)
            new = station.rename_playlist(old, new)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            self._error(str(e) if isinstance(e, ValueError) else "send {\"old\", \"new\"}")
            return
        old = was["name"]
        if presets is not None:
            presets.rename_playlist(old, new)
        sched = getattr(station, "scheduler", None)
        renamed = bool(sched and sched.rename_playlist(old, new))
        self._save_playlists()
        if renamed and config_file is not None:
            save_setting(config_file, "programmes", sched.programmes)
        self._send(json.dumps({"name": new, **self._playlists()}).encode(), "application/json")

    def _playlist_add(self) -> None:
        """POST /api/playlists/add {"name", and "root" + "path" (a track), "root" +
        "folder" (+ "deep") (a folder), or "now": true (the song playing)}."""
        try:
            body = self._body()
            refs = station.refs_for(root=body.get("root"), path=body.get("path"), folder=body.get("folder"),
                                    deep=body.get("deep") is True, now=body.get("now") is True)
            p = station.add_to_playlist(body["name"], refs)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            self._error(str(e) if isinstance(e, ValueError) else "send {\"name\", ...}")
            return
        self._save_playlists()
        self._send(json.dumps({"name": p["name"], "added": len(refs), "tracks": len(p["tracks"]),
                               **self._playlists()}).encode(), "application/json")

    def _playlist_play(self) -> None:
        """POST /api/playlists/play {"name", "shuffle": bool}: now, then the show."""
        try:
            body = self._body()
            reply = station.play_playlist(body["name"], body.get("shuffle") is True,
                                          then="pause" if body.get("then") == "pause" else None)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            self._error(str(e) if isinstance(e, ValueError) else "send {\"name\"}")
            return
        if speaker is not None:
            speaker.play()
        self._send(json.dumps({**reply, **self._playlists()}).encode(), "application/json")

    def _album_args(self, body: dict | None = None) -> dict:
        """{"id": n} (from /api/search or /api/albums) or {"root", "folder", "deep"}
        (from /api/browse)."""
        body = self._body() if body is None else body
        if "id" in body:
            return {"album_id": body["id"]}
        if not isinstance(body.get("folder"), str):
            raise ValueError("send {\"id\": n} or {\"root\", \"folder\"}")
        out = {"root": body.get("root") or "music", "folder": body["folder"], "deep": body.get("deep") is True}
        if "track" in body:
            if isinstance(body["track"], bool) or not isinstance(body["track"], int):
                raise ValueError("track is a number")
            out["track"] = body["track"]
        return out

    def _album(self) -> None:
        """POST /api/album: play it next. Music goes into the show, introduced by
        the DJ; On demand plays with no DJ when the song (or album) playing ends."""
        try:
            reply = station.play_next(**self._album_args())
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            self._error(str(e) if isinstance(e, ValueError) else "send {\"id\": n}")
            return
        if reply.get("now") and speaker is not None:
            speaker.play()
        self._send(json.dumps({**reply, "album": station.album_status(),
                               "up_next_source": station.next_source_status(),
                               "requests": station.requests()}).encode(), "application/json")

    def _media(self, action: str) -> None:
        """The music library manager: POST /api/media/upload?kind=&dir=&name= with
        the file as the body (name may include folders, for a folder upload;
        &replace=1: a different file of that name is replaced, not kept beside it);
        /api/media/mkdir {"kind", "path", "name"}; /api/media/delete {"kind",
        "path", "prune"?: true (the folders it leaves empty go too)}; /api/media/done {} (changes finished: read-only again, and
        the library is rescanned)."""
        if action == "upload":
            q = parse_qs(urlparse(self.path).query)
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                reply = media.receive(q.get("kind", ["music"])[0], q.get("dir", [""])[0],
                                      q.get("name", [""])[0], length, self.rfile.read, q.get("replace", [""])[0] == "1")
            except media_mod.MediaError as e:
                self.close_connection = True          # (the rest of the body wasn't read)
                self._error(str(e))
                return
            self._send(json.dumps(reply).encode(), "application/json")
            return
        try:
            body = self._body()
            kind = body.get("kind", "music")
            if action == "mkdir":
                reply = {"path": media.mkdir(kind, body.get("path", ""), body.get("name", ""))}
            elif action == "delete":
                media.delete(kind, body.get("path", ""), body.get("prune") is True)
                reply = {"deleted": body.get("path")}
            elif action in ("move", "rename"):
                old = body.get("path", "")
                to_kind = body.get("to_kind", kind) if action == "move" else kind
                new = media.move(kind, old, to_kind, body.get("to", "")) if action == "move" \
                    else media.rename(kind, old, body.get("name", ""))
                reply = {"path": new, "kind": to_kind}
                old_root, new_root = kind, to_kind          # playlists, programmes and buttons follow it
                if old_root in ("music", "ondemand") and new_root in ("music", "ondemand"):
                    old_rel = "/".join(p for p in old.split("/") if p)
                    kept_before = list(station.kept.paths) if hasattr(station, "kept") else None
                    if station.rename_refs(old_root, old_rel, new_root, new) and config_file is not None:
                        save_setting(config_file, "playlists", station.playlists)
                    if kept_before is not None and station.kept.paths != kept_before and config_file is not None:
                        save_setting(config_file, "ondemand_keep", station.kept.paths)
                    sched = getattr(station, "scheduler", None)
                    if sched and sched.rename_refs(old_root, old_rel, new_root, new) and config_file is not None:
                        save_setting(config_file, "programmes", sched.programmes)
                    if presets is not None:
                        presets.rename_refs(old_root, old_rel, new_root, new)
            elif action == "done":
                changed = media.done()
                if station.retire_artists() and config_file is not None:    # (an artist's old name, if no song has it now)
                    save_setting(config_file, "profiles", station.profiles)
                if presets is not None:
                    presets.bake()                   # (a button's spoken name follows a tag that was changed)
                reply = {"changed": changed,
                         "library": {"tracks": len(station.tracks), "jingles": len(station.jingles),
                                     "books": len(station.books.all()) if hasattr(station, "books") else 0,
                                     "ondemand": len(getattr(station, "ondemand_tracks", []))}}
            else:
                self.send_error(404)
                return
        except (media_mod.MediaError, AttributeError, TypeError) as e:
            self._error(str(e) if isinstance(e, media_mod.MediaError) else "send {\"kind\", \"path\"}")
            return
        self._send(json.dumps(reply).encode(), "application/json")

    def _download(self) -> None:
        """GET /api/media/download?kind=&path=: a file as it is, or a folder as a
        zip (stored, not squeezed: audio doesn't shrink), streamed."""
        import zipfile
        q = parse_qs(urlparse(self.path).query)
        try:
            target = media.resolve(q.get("kind", ["music"])[0], q.get("path", [""])[0])
        except media_mod.MediaError as e:
            self._error(str(e))
            return
        if not target.exists() or target == media._root(q.get("kind", ["music"])[0]):
            self._error("that isn't there to download")
            return
        name = target.name.replace('"', "'")
        from urllib.parse import quote
        self.send_response(200)
        self.send_header("Cache-Control", "no-store")
        if target.is_file():
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(target.stat().st_size))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(target.name)}")
            self.end_headers()
            with open(target, "rb") as f:
                while chunk := f.read(1 << 16):
                    self.wfile.write(chunk)
            return
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(target.name + '.zip')}")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        class Out:                                   # zipfile writes to a stream it can't seek
            def __init__(self, w): self.w, self.n = w, 0
            def write(self, b): self.w.write(b); self.n += len(b); return len(b)
            def tell(self): return self.n
            def flush(self): self.w.flush()
        try:
            with zipfile.ZipFile(Out(self.wfile), "w", zipfile.ZIP_STORED) as z:
                for f in sorted(target.rglob("*")):
                    if f.is_file() and not any(part.startswith(".") for part in f.relative_to(target).parts):
                        z.write(f, str(Path(name) / f.relative_to(target)))
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _podcasts(self, action: str) -> None:
        """POST /api/podcasts/follow {"feed_url"}, /unfollow {"id"}, /play {"id", "guid"?}
        (no guid: the one part-heard, else the newest unheard), /heard {"id", "guid", "heard": bool}."""
        try:
            body = self._body()
            pods = station.podcasts
            if action == "follow":
                reply = {"show": pods.subscribe(str(body.get("feed_url") or ""))}
            elif action == "unfollow":
                pods.unsubscribe(str(body.get("id") or ""))
                reply = {"ok": True}
            elif action == "play":
                station.play_episode(str(body.get("id") or ""), body.get("guid"))
                if speaker is not None:
                    speaker.play()
                reply = {"source": station.status()["source"]}
            else:
                sid, guid = str(body.get("id") or ""), str(body.get("guid") or "")
                key = pod_mod.episode_key(sid, guid)
                station.book_positions.set(key, 0, done=bool(body.get("heard", True)))
                reply = {"ok": True}
        except (ValueError, AttributeError, TypeError) as e:
            self._error(str(e) if isinstance(e, ValueError) else "send {\"id\"}")
            return
        self._send(json.dumps(reply).encode(), "application/json")

    def _book_download(self) -> None:
        """POST /api/librivox/download {"id": <the followed book's id>}: keep that LibriVox book on the radio (a
        folder of its chapters in Audiobooks, played with no Wi-Fi; the streamed copy is replaced) /
        {"cancel": true}. Progress: GET /api/librivox/download. One at a time."""
        try:
            body = self._body()
            reply = book_dl.cancel() if body.get("cancel") else book_dl.start(str(body.get("id") or ""))
        except (ValueError, AttributeError, TypeError) as e:
            self._error(str(e) if isinstance(e, ValueError) else "send {\"id\"}")
            return
        self._send(json.dumps(reply).encode(), "application/json")

    def _ebook_download(self) -> None:
        """POST /api/ebooks/download {"id": <Project Gutenberg number>, "title", "confirm"}: fetch that book into the
        Ebooks folder (a normal novel is under 2 MB; a bigger one answers {"state": "confirm", "bytes"} and waits
        for the same call with "confirm": true) / {"cancel": true}. Progress: GET /api/ebooks/download."""
        if not station.ebooks_on:
            self._error(NO_EBOOKS)
            return
        try:
            body = self._body()
            reply = ebook_dl.cancel() if body.get("cancel") else ebook_dl.start(
                body.get("id"), str(body.get("title") or ""), body.get("confirm") is True)
        except (ValueError, AttributeError, TypeError) as e:
            self._error(str(e) if isinstance(e, ValueError) else "send {\"id\"}")
            return
        self._send(json.dumps(reply).encode(), "application/json")

    def _books(self, action: str) -> None:
        """POST /api/books/play {"key"}: that audiobook, from where it was left;
        /api/books/seek {"delta_ms": -60000} or {"to_ms": n}: move in the book on."""
        try:
            body = self._body()
            if action == "play":
                if not isinstance(body.get("key"), str):
                    raise ValueError("send {\"key\"} from /api/books")
                station.play_book(body["key"])
                if speaker is not None:
                    speaker.play()
                reply = {"source": station.status()["source"]}
            else:
                delta, to = body.get("delta_ms"), body.get("to_ms")
                for v in (delta, to):
                    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                        raise ValueError("delta_ms / to_ms are milliseconds")
                reply = station.book_seek(None if delta is None else int(delta), None if to is None else int(to))
        except (ValueError, AttributeError, TypeError) as e:
            self._error(str(e) if isinstance(e, ValueError) else "send {\"key\"} or {\"delta_ms\"}")
            return
        self._send(json.dumps(reply).encode(), "application/json")

    def _play_album(self) -> None:
        """POST /api/album/play {"id": n} or {"root", "folder", "deep"} (+ "shuffle",
        "then": "pause"): play it straight through instead of the show -- no DJ --
        then back to the show (or pause)."""
        try:
            body = self._body()
            station.play_album(**self._album_args(body), shuffle=body.get("shuffle") is True,
                               then="pause" if body.get("then") == "pause" else None)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            self._error(str(e) if isinstance(e, ValueError) else "send {\"id\": n}")
            return
        if speaker is not None:
            speaker.play()
        self._send(json.dumps(self._radio_state()).encode(), "application/json")

    def _set_profiles(self) -> None:
        """POST /api/profiles {"profiles": [{"name", "artists": [...]}, ...]}:
        replace the lists. Saved; the one playing follows the edit."""
        try:
            entries = profiles.validate(self._body()["profiles"])
            if not any(p["name"].lower() == profiles.DEFAULT.lower() for p in entries):
                raise ValueError(f"{profiles.DEFAULT} is always there: it can't be deleted or renamed")
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            self._error(str(e) if isinstance(e, ValueError) else "send {\"profiles\": [...]}")
            return
        station.set_profiles(entries)
        if config_file is not None:
            save_setting(config_file, "profiles", station.profiles)
        self._save_selection()
        self._send(json.dumps({**self._selection(), "profiles": station.profiles}).encode(),
                   "application/json")

    def _rename_profile(self) -> None:
        """POST /api/profiles/rename {"old", "new"}: rename a theme. Everything of its
        own follows it: its jingles folder, its messages, buttons and programmes that
        play it, and (playing) the station. Saved."""
        try:
            body = self._body()
            old, new = body["old"], body["new"]
            if not isinstance(old, str) or not isinstance(new, str):
                raise ValueError("send {\"old\", \"new\"}")
            new = station.rename_profile(old, new)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            self._error(str(e) if isinstance(e, ValueError) else "send {\"old\", \"new\"}")
            return
        if media is not None and (Path(station.jingles_dir) / old).is_dir() and old != new:
            try:
                media.rename("jingles", old, new)
            except media_mod.MediaError as e:
                log.warning("theme rename: jingles folder: %s", e)
            finally:
                media.done()
        if presets is not None:
            presets.rename_theme(old, new)
        sched = getattr(station, "scheduler", None)
        renamed = bool(sched and sched.rename_theme(old, new))
        if config_file is not None:
            save_setting(config_file, "profiles", station.profiles)
            save_setting(config_file, "messages", station.messages.cfg)
            if renamed:
                save_setting(config_file, "programmes", sched.programmes)
        self._save_selection()
        self._send(json.dumps({"name": new, "profiles": station.profiles}).encode(), "application/json")

    def _request(self) -> None:
        """POST /api/request {"id": n} (from /api/search): play that track next."""
        try:
            track_id = self._body()["id"]
            if isinstance(track_id, bool) or not isinstance(track_id, int):
                raise ValueError("id must be a number from /api/search")
            reply = station.request(track_id)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            self._error(str(e) if isinstance(e, ValueError) else "send {\"id\": n}")
            return
        self._send(json.dumps({**reply, "requests": station.requests()}).encode(), "application/json")

    return {"_selection": _selection, "_save_selection": _save_selection, "_station": _station, "_playlists": _playlists, "_programmes": _programmes, "_programmes_post": _programmes_post, "_save_playlists": _save_playlists, "_set_playlists": _set_playlists, "_rename_playlist": _rename_playlist, "_playlist_add": _playlist_add, "_playlist_play": _playlist_play, "_album_args": _album_args, "_album": _album, "_media": _media, "_download": _download, "_podcasts": _podcasts, "_book_download": _book_download, "_ebook_download": _ebook_download, "_books": _books, "_play_album": _play_album, "_set_profiles": _set_profiles, "_rename_profile": _rename_profile, "_request": _request}
