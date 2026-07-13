"""Self-hosted cloud music library for the Neutrino app. Serves your own files
in the shape the app's custom-source API understands (/search, /streams/{id},
/playlists, /upload). Point Settings -> My Server at this box — fully your own
content, nothing fetched from anywhere else.
"""

import base64
import os
import mimetypes
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse, Response
from mutagen import File as MutagenFile
from pydantic import BaseModel

# Online enrichment (cover art + artist) via the public iTunes Search API — no
# key, no auth, and it only ever runs when a file has no embedded art/tags.
# Set LIBRARY_ONLINE_ENRICH=0 to keep the server fully offline.
ONLINE_ENRICH = os.environ.get("LIBRARY_ONLINE_ENRICH", "1") != "0"
ITUNES_SEARCH = "https://itunes.apple.com/search"

MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", "/music")).resolve()
# Extensions we index. Playback is Android platform-codec backed (Media3),
# so this list tracks what phones can actually decode: MP3/AAC/M4A/MP4/WAV
# everywhere, FLAC on 8.1+, Opus on 10+. ALAC/AIFF ride in via the mp4/aiff
# containers. WMA/AC3 would need the NDK ffmpeg extension — deliberately left
# out (see README).
AUDIO_EXTS = {
    ".mp3", ".flac", ".m4a", ".m4b", ".mp4",
    ".ogg", ".oga", ".opus", ".wav", ".aac", ".aiff", ".aif",
}

app = FastAPI()

# Explicit audio MIME types. The slim container's mimetypes table has no entry
# for .m4a and the old fallback served it as audio/mpeg — JavaFX trusts the
# Content-Type header, built an MP3 pipeline for MP4 data, and failed with
# ERROR_MEDIA_INVALID. ("audio/x-m4a" is on JavaFX's supported list.)
_MIME_BY_EXT = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/x-m4a",
    ".m4b": "audio/x-m4a",
    ".mp4": "video/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".aiff": "audio/x-aiff",
    ".aif": "audio/x-aiff",
}


def _sniff_container(path: Path) -> Optional[str]:
    """".m4a"/".mp3" from the file's actual leading bytes, None when unclear.
    Downloaded files sometimes lie about their container (AAC/MP4 bytes named
    ".mp3"), and clients that trust the extension or Content-Type then build
    the wrong decode pipeline."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
        if len(head) >= 12 and head[4:8] == b"ftyp":
            return ".m4a"
        if head[:3] == b"ID3" or (
            len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0
        ):
            return ".mp3"
    except Exception:
        pass
    return None


def _mime_of(path: Path) -> str:
    sniffed = _sniff_container(path)
    if sniffed:
        return _MIME_BY_EXT[sniffed]
    return (
        _MIME_BY_EXT.get(path.suffix.lower())
        or mimetypes.guess_type(str(path))[0]
        or "application/octet-stream"
    )

# id <-> path mapping, built at startup and refreshable via /rescan.
_tracks: dict[str, Path] = {}


def _encode_id(rel_path: str) -> str:
    return base64.urlsafe_b64encode(rel_path.encode()).decode().rstrip("=")


def _decode_id(track_id: str) -> str:
    padding = "=" * (-len(track_id) % 4)
    return base64.urlsafe_b64decode(track_id + padding).decode()


UNKNOWN_ARTIST = "Unknown artist"

# "Artist - Title", tolerating the " – " en-dash and surrounding junk like
# "(Official Video)" / "[Lyrics]" that downloaded files often carry.
_NOISE = re.compile(r"[\(\[](official|lyric|audio|video|hd|4k|mv)[^\)\]]*[\)\]]", re.I)


def _parse_filename(stem: str) -> tuple[Optional[str], str]:
    """Best-effort (artist, title) from a bare filename when tags are missing."""
    cleaned = _NOISE.sub("", stem).strip(" -_")
    for sep in (" - ", " – ", " — "):
        if sep in cleaned:
            left, right = cleaned.split(sep, 1)
            left, right = left.strip(), right.strip()
            if left and right:
                return left, right
    return None, cleaned or stem


def _read_tags(path: Path) -> tuple[str, str, Optional[float]]:
    title, artist, duration = path.stem, UNKNOWN_ARTIST, None
    tag_title = tag_artist = None
    try:
        audio = MutagenFile(path, easy=True)
        if audio:
            if audio.tags:
                tag_title = (audio.tags.get("title") or [None])[0]
                tag_artist = (audio.tags.get("artist") or [None])[0]
            if audio.info and hasattr(audio.info, "length"):
                duration = audio.info.length
    except Exception:
        pass

    # Fall back to filename parsing whenever a tag is missing.
    fn_artist, fn_title = _parse_filename(path.stem)
    title = tag_title or fn_title or title
    artist = tag_artist or fn_artist or UNKNOWN_ARTIST
    return title, artist, duration


def _embedded_art(path: Path) -> Optional[tuple[bytes, str]]:
    """Cover art bytes + mime embedded in the file, across mp3/mp4/flac/ogg."""
    try:
        audio = MutagenFile(path)
        if audio is None:
            return None
        tags = getattr(audio, "tags", None)
        # ID3 (mp3): APIC frames
        if tags is not None and hasattr(tags, "getall"):
            for apic in tags.getall("APIC"):
                return apic.data, apic.mime or "image/jpeg"
        # MP4 / M4A / M4B: 'covr' atom
        if tags is not None and "covr" in getattr(tags, "keys", lambda: [])():
            covr = tags["covr"][0]
            fmt = "image/png" if bytes(covr)[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
            return bytes(covr), fmt
        # FLAC: embedded pictures
        pics = getattr(audio, "pictures", None)
        if pics:
            return pics[0].data, pics[0].mime or "image/jpeg"
    except Exception:
        pass
    return None


# Cache online lookups so we hit iTunes at most once per (artist, title).
_online_cache: dict[str, Optional[dict]] = {}


def _itunes_lookup(artist: str, title: str) -> Optional[dict]:
    if not ONLINE_ENRICH:
        return None
    term = f"{artist} {title}".strip() if artist != UNKNOWN_ARTIST else title
    key = term.lower()
    if key in _online_cache:
        return _online_cache[key]
    result = None
    try:
        r = httpx.get(
            ITUNES_SEARCH,
            params={"term": term, "entity": "song", "limit": 1},
            timeout=6.0,
        )
        if r.status_code == 200:
            items = r.json().get("results") or []
            if items:
                it = items[0]
                art = it.get("artworkUrl100")
                # iTunes serves 100px by default; ask for a big banner instead.
                if art:
                    art = art.replace("100x100bb", "600x600bb")
                result = {"artist": it.get("artistName"), "artwork": art}
    except Exception:
        result = None
    _online_cache[key] = result
    return result


def _rescan() -> int:
    _tracks.clear()
    if not MUSIC_DIR.exists():
        return 0
    for path in MUSIC_DIR.rglob("*"):
        if path.is_file() and path.suffix.lower() in AUDIO_EXTS:
            rel = str(path.relative_to(MUSIC_DIR))
            _tracks[_encode_id(rel)] = path
    return len(_tracks)


# ---- faststart remux --------------------------------------------------------
# YouTube-origin M4A/MP4 files usually carry their moov atom at the END, which
# means an HTTP client must download the whole file before it can start
# decoding (the desktop app's JavaFX player errors with MEDIA_INVALID and falls
# back to a full temp download — 30s+ on a slow uplink). Remuxing with
# "-movflags +faststart" moves the moov up front, making the same file
# progressively streamable. Pure copy, no re-encode, ~instant per file.

_FFMPEG = shutil.which("ffmpeg")
_MP4_EXTS = {".m4a", ".mp4", ".m4b"}


def _is_faststart(path: Path) -> bool:
    """True when the moov atom precedes mdat (progressively streamable)."""
    try:
        with open(path, "rb") as f:
            while True:
                header = f.read(8)
                if len(header) < 8:
                    return False
                size = int.from_bytes(header[:4], "big")
                kind = header[4:8]
                if kind == b"moov":
                    return True
                if kind == b"mdat":
                    return False
                if size == 1:  # 64-bit large box
                    size = int.from_bytes(f.read(8), "big")
                    f.seek(size - 16, 1)
                elif size == 0:  # box runs to EOF
                    return False
                else:
                    f.seek(size - 8, 1)
    except Exception:
        return True  # unreadable/odd file → leave it alone


def _ensure_faststart(path: Path) -> None:
    """Remux [path] in place so it streams progressively. No-op when ffmpeg is
    missing, the file isn't MP4-family (by name or sniffed content), or it's
    already faststart."""
    if _FFMPEG is None:
        return
    is_mp4 = path.suffix.lower() in _MP4_EXTS or _sniff_container(path) == ".m4a"
    if not is_mp4 or _is_faststart(path):
        return
    tmp = path.with_name(path.stem + ".faststart" + path.suffix)
    try:
        # "-f mp4" pins the muxer: the tmp name inherits the original extension,
        # which may lie about the container (MP4 bytes named ".mp3").
        proc = subprocess.run(
            [_FFMPEG, "-y", "-loglevel", "error", "-i", str(path),
             "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(tmp)],
            capture_output=True, timeout=300,
        )
        if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(path)
        else:
            tmp.unlink(missing_ok=True)
    except Exception:
        tmp.unlink(missing_ok=True)


def _enrich_file(path: Path) -> None:
    """Embed missing title/artist/cover art into an MP4-family file itself.
    Old uploads carried metadata in a glued-on ID3 header, which the faststart
    remux (correctly) stripped — this puts proper MP4 tags back, from the
    "Artist - Title" filename plus iTunes cover art. Files that already have
    tags/art are untouched. Best-effort: any failure leaves the file as-is."""
    if path.suffix.lower() not in _MP4_EXTS:
        return
    try:
        from mutagen.mp4 import MP4, MP4Cover

        title, artist, _ = _read_tags(path)  # falls back to filename parse
        audio = MP4(path)
        if audio.tags is None:
            audio.add_tags()
        changed = False
        if not audio.tags.get("\xa9nam") and title:
            audio.tags["\xa9nam"] = [title]
            changed = True
        if not audio.tags.get("\xa9ART") and artist and artist != UNKNOWN_ARTIST:
            audio.tags["\xa9ART"] = [artist]
            changed = True
        if not audio.tags.get("covr"):
            hit = _itunes_lookup(artist, title)
            art_url = hit.get("artwork") if hit else None
            if art_url:
                r = httpx.get(art_url, timeout=10.0, follow_redirects=True)
                if r.status_code == 200 and r.content:
                    fmt = (
                        MP4Cover.FORMAT_PNG
                        if r.content[:8] == b"\x89PNG\r\n\x1a\n"
                        else MP4Cover.FORMAT_JPEG
                    )
                    audio.tags["covr"] = [MP4Cover(r.content, imageformat=fmt)]
                    changed = True
        if changed:
            audio.save()
    except Exception:
        pass


def _faststart_sweep() -> None:
    """One pass over the whole library, fixing files uploaded before this
    feature existed (remux to faststart, then restore embedded metadata).
    Runs in a background thread at startup."""
    for p in list(_tracks.values()):
        _ensure_faststart(p)
    for p in list(_tracks.values()):
        _enrich_file(p)


@app.on_event("startup")
def startup():
    _rescan()
    threading.Thread(target=_faststart_sweep, daemon=True).start()


@app.post("/rescan")
def rescan():
    return {"tracks": _rescan()}


# Files uploaded from the app (with no folder given) land here, so they're easy
# to find/clear out separately from files you put in yourself.
IMPORTED_DIR = MUSIC_DIR / "Imported"

_SAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename(name: str) -> str:
    return _SAFE_NAME.sub("_", name).strip(" .") or "track"


def _target_dir(folder: str) -> Path:
    """Where an uploaded file lands. A [folder] (e.g. a playlist name) becomes a
    top-level subfolder of the music dir — so it shows up as its own
    folder-playlist — otherwise files go to the shared Imported/ folder."""
    folder = _SAFE_NAME.sub("_", (folder or "").strip()).strip(" .")
    dest = (MUSIC_DIR / folder) if folder else IMPORTED_DIR
    dest.mkdir(parents=True, exist_ok=True)
    return dest


@app.post("/upload")
async def upload_track(file: UploadFile = File(...), folder: str = Form("")):
    """Accept a raw audio file uploaded from the app (e.g. a phone-local track)
    and save it into this server's library. A [folder] (playlist name) groups it
    into a top-level subfolder; otherwise it lands in the shared Imported/ folder."""
    filename = _safe_filename(file.filename or "upload")
    if not any(filename.lower().endswith(ext) for ext in AUDIO_EXTS):
        filename += ".mp3"

    target = _target_dir(folder)
    dest = target / filename
    # Don't clobber an existing file — append a counter if needed.
    if dest.exists():
        stem, ext = dest.stem, dest.suffix
        n = 1
        while (target / f"{stem} ({n}){ext}").exists():
            n += 1
        dest = target / f"{stem} ({n}){ext}"

    try:
        with open(dest, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                f.write(chunk)
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Save failed: {e}")

    # New uploads with a lying extension (MP4 bytes named ".mp3" etc.) get their
    # real one — safe here because nothing references the file yet. Existing
    # library files keep their names (their ids are path-based and may already
    # live in app playlists); _mime_of's content sniff covers those.
    actual = _sniff_container(dest)
    if actual and dest.suffix.lower() != actual and not (
        actual == ".m4a" and dest.suffix.lower() in {".mp4", ".m4b"}
    ):
        renamed = dest.with_suffix(actual)
        n = 1
        while renamed.exists():
            renamed = dest.with_name(f"{dest.stem} ({n})").with_suffix(actual)
            n += 1
        dest.rename(renamed)
        dest = renamed

    # Make M4A/MP4 uploads progressively streamable before they're indexed,
    # and embed tags/art when the incoming bytes carry none.
    _ensure_faststart(dest)
    _enrich_file(dest)
    _rescan()
    # Report the written size so the client can verify the upload landed complete
    # (and re-upload if it was truncated).
    return {
        "saved": str(dest.relative_to(MUSIC_DIR)),
        "size": dest.stat().st_size,
        "tracks": len(_tracks),
    }


def _track_item(track_id: str, path: Path, base_url: str) -> dict:
    title, artist, duration = _read_tags(path)
    # Fill an unknown artist from online metadata (cached) if enabled.
    if artist == UNKNOWN_ARTIST:
        hit = _itunes_lookup(artist, title)
        if hit and hit.get("artist"):
            artist = hit["artist"]
    return {
        "url": f"/watch?v={track_id}",
        "title": title,
        "uploaderName": artist,
        # Always a resolvable URL; /art decides embedded-vs-online lazily.
        "thumbnail": f"{base_url}/art/{track_id}",
        "duration": int(duration) if duration else None,
        # File size so the client can detect an incomplete/truncated stored copy.
        "size": path.stat().st_size,
        # Last-modified time (epoch seconds) so the app can sort by date.
        "modified": int(path.stat().st_mtime),
        "type": "stream",
    }


def _base_url(request: Request) -> str:
    host = request.headers.get("host", request.url.hostname)
    return f"{request.url.scheme}://{host}"


def _playlist_id_of(path: Path) -> str:
    """Which folder-playlist a track belongs to: its top-level subfolder under
    MUSIC_DIR, or the synthetic "__root__" for loose files at the top level."""
    rel = path.relative_to(MUSIC_DIR)
    parts = rel.parts
    if len(parts) <= 1:
        return "__root__"
    return _encode_id(parts[0])


@app.get("/search")
def search(request: Request, q: str = "", filter: str = ""):
    query = q.lower().strip()
    base = _base_url(request)
    items = []
    for track_id, path in _tracks.items():
        title, artist, _ = _read_tags(path)
        if query and query not in title.lower() and query not in artist.lower():
            continue
        items.append(_track_item(track_id, path, base))
    # Empty query = browse-all: return the whole library, not a search slice.
    limit = len(items) if not query else 50
    return {"items": items[:limit]}


@app.get("/playlists")
def playlists():
    """Folder-as-playlist listing. Each top-level subfolder of the music dir is
    a playlist; loose files at the root collapse into a "Singles" entry. This is
    an optional endpoint — the app falls back to search-only when it 404s."""
    counts: dict[str, int] = {}
    names: dict[str, str] = {}
    for path in _tracks.values():
        pid = _playlist_id_of(path)
        counts[pid] = counts.get(pid, 0) + 1
        if pid == "__root__":
            names[pid] = "Singles"
        else:
            names[pid] = path.relative_to(MUSIC_DIR).parts[0]

    result = []
    # "All tracks" first, then folders alphabetically, Singles last.
    if _tracks:
        result.append({"id": "__all__", "name": "All tracks", "trackCount": len(_tracks)})
    folders = sorted(
        (pid for pid in counts if pid != "__root__"),
        key=lambda p: names[p].lower(),
    )
    for pid in folders:
        result.append({"id": pid, "name": names[pid], "trackCount": counts[pid]})
    if "__root__" in counts:
        result.append({"id": "__root__", "name": "Singles", "trackCount": counts["__root__"]})

    return {"playlists": result}


@app.get("/playlists/{playlist_id}")
def playlist_tracks(playlist_id: str, request: Request):
    """Tracks in one folder-playlist. "__all__" returns everything."""
    base = _base_url(request)
    items = []
    for track_id, path in _tracks.items():
        if playlist_id == "__all__" or _playlist_id_of(path) == playlist_id:
            items.append(_track_item(track_id, path, base))
    return {"items": items}


@app.get("/art/{track_id}")
def art(track_id: str):
    """Cover art for a track: embedded image if present, otherwise the online
    cover (iTunes) proxied through this server. We proxy rather than redirect
    because media players (Media3's DataSourceBitmapLoader) refuse the
    cross-protocol http->https redirect, which would drop the artwork."""
    path = _tracks.get(track_id)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="Track not found")

    embedded = _embedded_art(path)
    if embedded:
        data, mime = embedded
        return Response(content=data, media_type=mime, headers={
            "Cache-Control": "public, max-age=86400",
        })

    title, artist, _ = _read_tags(path)
    hit = _itunes_lookup(artist, title)
    art_url = hit.get("artwork") if hit else None
    if art_url:
        try:
            r = httpx.get(art_url, timeout=8.0, follow_redirects=True)
            if r.status_code == 200:
                return Response(
                    content=r.content,
                    media_type=r.headers.get("content-type", "image/jpeg"),
                    headers={"Cache-Control": "public, max-age=86400"},
                )
        except Exception:
            pass
    raise HTTPException(status_code=404, detail="No artwork")


@app.get("/streams/{track_id}")
def streams(track_id: str, request: Request):
    path = _tracks.get(track_id)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="Track not found")
    mime = _mime_of(path)
    host = request.headers.get("host", request.url.hostname)
    scheme = request.url.scheme
    # Append the real file extension so clients that pick a player by extension
    # (e.g. desktop JavaFX) stream it directly instead of buffering a temp copy.
    ext = path.suffix.lstrip(".").lower() or "mp3"
    return {
        "audioStreams": [{
            "url": f"{scheme}://{host}/file/{track_id}.{ext}",
            "bitrate": 0,
            "mimeType": mime,
        }]
    }


@app.get("/file/{track_id}")
def file(track_id: str, request: Request):
    # The /streams url appends the file extension (…/file/<id>.mp3) so players can
    # detect the format; ids are url-safe base64 (no dots), so strip a trailing
    # ".<ext>" back off before looking the track up.
    if "." in track_id:
        track_id = track_id.rsplit(".", 1)[0]
    path = _tracks.get(track_id)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="Track not found")

    file_size = path.stat().st_size
    mime = _mime_of(path)
    range_header = request.headers.get("range")

    if range_header is None:
        def full_stream():
            with open(path, "rb") as f:
                while chunk := f.read(1024 * 64):
                    yield chunk
        return StreamingResponse(full_stream(), media_type=mime, headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
        })

    start_str, _, end_str = range_header.replace("bytes=", "").partition("-")
    start = int(start_str) if start_str else 0
    end = int(end_str) if end_str else file_size - 1
    end = min(end, file_size - 1)
    length = end - start + 1

    def ranged_stream():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1024 * 64, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        ranged_stream(),
        status_code=206,
        media_type=mime,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(length),
        },
    )


@app.delete("/file/{track_id}")
def delete_file(track_id: str):
    path = _tracks.get(track_id)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="Track not found")
    try:
        path.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Delete failed: {exc}")
    remaining = _rescan()
    return {"deleted": track_id, "tracks": remaining}


@app.get("/health")
def health():
    return {"status": "ok", "tracks": len(_tracks)}
