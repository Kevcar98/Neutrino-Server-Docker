"""Private resolver (personal use). Implements the app's Resolver contract
(see ../../PROTOCOL.md): POST {url, downloadMode} -> {status, url, filename}.

It extracts audio server-side with yt-dlp and then serves the finished file
itself, so the phone only ever fetches plain bytes from this box — no expiring
upstream URLs, no cross-origin/IP header problems, no empty tunnels.

Not shipped in the app or the public repo. The operator is responsible for
what they resolve and for having the right to do so.
"""

import os
import re
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from yt_dlp import YoutubeDL
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3NoHeaderError

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Delete resolved files older than this many seconds (they're one-shot: the
# phone/library downloads them right after we return the URL).
MAX_AGE = int(os.environ.get("RESOLVER_MAX_AGE", "3600"))

# YouTube blocks datacenter IPs with "Sign in to confirm you're not a bot". Two
# mitigations, both optional and stackable:
#
#  * COOKIES_FILE — a mounted Netscape cookies.txt from a logged-in account.
#  * POT_PROVIDER_URL — base url of the bgutil PO-token provider sidecar. The
#    bgutil-ytdlp-pot-provider plugin (in requirements) fetches a fresh PO token
#    from it per request, which is the low-maintenance way past the bot check.
COOKIES_FILE = os.environ.get("COOKIES_FILE", "/cookies/cookies.txt")
POT_PROVIDER_URL = os.environ.get("POT_PROVIDER_URL", "").strip().rstrip("/")
# Route yt-dlp's traffic through a proxy so YouTube sees a residential IP instead
# of this datacenter one — the surest way past the bot wall. Typically a SOCKS
# proxy from a reverse SSH tunnel to a home machine, e.g. socks5h://host.docker.internal:1080.
YTDLP_PROXY = os.environ.get("YTDLP_PROXY", "").strip()
# On-device mode: the resolver runs in Termux ON the phone, so it already has a
# residential IP and needs no proxy. POT comes from bgutil's "script" provider
# (a local JS file run by node), not an HTTP server. Set POT_SCRIPT_PATH to the
# generate_once.js and RESOLVER_LOCAL=1.
RESOLVER_LOCAL = os.environ.get("RESOLVER_LOCAL", "").strip() not in ("", "0")
POT_SCRIPT_PATH = os.environ.get("POT_SCRIPT_PATH", "").strip()


def _common_opts() -> dict:
    """yt-dlp options shared by every call: proxy + cookies + PO-token provider."""
    opts: dict = {
        # Let yt-dlp fetch the EJS challenge-solver scripts (runs under Deno) so it
        # can decrypt YouTube's signature / n-param — without this, web-client
        # formats come back as "Only images available". Downloaded once, cached.
        "remote_components": ["ejs:github"],
    }
    if COOKIES_FILE and Path(COOKIES_FILE).is_file():
        opts["cookiefile"] = COOKIES_FILE

    if RESOLVER_LOCAL:
        # Running on the phone itself: residential IP already, no proxy — the SAME
        # situation the PC-hosted resolver ran in, and that was fast. A residential
        # IP is NOT bot-blocked, so default yt-dlp clients hand over formats without
        # the expensive POT token / BotGuard solve or a forced tv-only client. Those
        # were only needed to beat the DATACENTER bot-wall; on-device they were pure
        # overhead (the ~15s). So by default do exactly what the PC did: nothing but
        # Deno/EJS signature solving (from _common_opts).
        #
        # Escape hatch: if this phone's IP *is* flagged (some CGNAT/mobile ranges
        # are), set POT_PROVIDER_URL to re-enable the tv+POT path.
        # No source_address/IPv4 forcing — same phone resolves AND plays, so the
        # url's ip= lock always matches; forcing IPv4 only risks a slower route.
        if POT_PROVIDER_URL or POT_SCRIPT_PATH:
            ea: dict = {"youtube": {"player_client": ["tv"]}}
            if POT_SCRIPT_PATH:
                ea["youtubepot-bgutilscript"] = {"script_path": [POT_SCRIPT_PATH]}
            else:
                ea["youtubepot-bgutilhttp"] = {"base_url": [POT_PROVIDER_URL]}
            opts["extractor_args"] = ea
        else:
            # Pin ONE client. Left unset, yt-dlp walks its whole default client
            # list (tv, web, ios, ...) — each a separate innertube round-trip, and
            # under SABR the audio-less ones make it retry the next, so timings
            # climb (13s → 31s → 42s) over the phone's connection. `web` works on a
            # residential IP without POT and yields audio (via Deno/EJS n-sig).
            opts["extractor_args"] = {"youtube": {"player_client": ["web"]}}
            # Persist yt-dlp's cache (player JS, EJS solver, sig funcs) so it's
            # fetched/compiled once, not re-downloaded over mobile every call.
            opts["cachedir"] = os.path.expanduser("~/.cache/yt-dlp")
    elif YTDLP_PROXY:
        # Residential-IP route (phone/home tunnel). With a GVS PO token (bgutil)
        # + Deno/EJS solving the signature, the `tv` client returns real
        # audio-only formats (itag 140 m4a ~130k) under YouTube's SABR experiment.
        # tv-only on purpose: it's a SINGLE innertube request, whereas `web` fetches
        # the webpage + config + player JSON + player JS — several round-trips that
        # are painfully slow through the phone tunnel. Speed > the muxed fallback.
        opts["proxy"] = YTDLP_PROXY
        # Force IPv4 (source_address 0.0.0.0 == yt-dlp's --force-ipv4). With a
        # socks5:// proxy (client-side DNS), this makes the phone reach YouTube
        # over IPv4, so the stream url is locked to the phone's stable public
        # IPv4 — which the phone CAN reproduce when playing directly (unlike its
        # rotating IPv6). Enables fast direct playback without the relay.
        opts["source_address"] = "0.0.0.0"
        ea: dict = {"youtube": {"player_client": ["tv"]}}
        if POT_PROVIDER_URL:
            ea["youtubepot-bgutilhttp"] = {"base_url": [POT_PROVIDER_URL]}
        opts["extractor_args"] = ea
    elif POT_PROVIDER_URL:
        # Datacenter IP with no proxy: need the PO token + the web client that
        # actually requests it, or YouTube bot-blocks us.
        opts["extractor_args"] = {
            "youtubepot-bgutilhttp": {"base_url": [POT_PROVIDER_URL]},
            "youtube": {"player_client": ["web", "default"]},
        }
    return opts


app = FastAPI()

_SAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe(name: str) -> str:
    return (_SAFE.sub("_", name).strip(" .") or "track")


def _sweep() -> None:
    cutoff = time.time() - MAX_AGE
    for f in DATA_DIR.glob("*"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def _base_url(request: Request) -> str:
    host = request.headers.get("host", request.url.hostname)
    return f"{request.url.scheme}://{host}"


def _extract_title_artist(info: dict) -> tuple[str, str | None]:
    # yt-dlp fills "track"/"artist" from real music metadata (Content-ID etc.)
    # when the platform has it. Fall back to splitting the raw title on the
    # common "Artist - Title" convention, then to the uploader/channel name.
    track = _clean_title((info.get("track") or "").strip()) if info.get("track") else ""
    artist = (info.get("artist") or info.get("creator") or "").strip()
    raw_title = _clean_title((info.get("title") or "track").strip())

    uploader = (info.get("uploader") or info.get("channel") or "").strip()
    uploader = re.sub(r"\s*-\s*Topic$", "", uploader)  # YouTube auto-gen artist channels

    # A metadata "artist" field with 3+ comma-separated names is usually a
    # composer/writer credit list, not a performer — useless for art/lookup.
    # Trust it only when it names one or two people; otherwise prefer the
    # uploader (more likely a real artist/label channel name).
    artist_names = [a.strip() for a in artist.split(",") if a.strip()]
    if len(artist_names) > 2:
        artist = uploader or artist_names[0]

    if track and artist:
        return track, artist

    for sep in (" - ", " – ", " — "):
        if sep in raw_title:
            left, right = raw_title.split(sep, 1)
            left, right = left.strip(), right.strip()
            if left and right:
                return right, left

    return raw_title, (uploader or None)


def _tag_file(path: Path, title: str, artist: str | None) -> None:
    try:
        tags = EasyID3(path)
    except ID3NoHeaderError:
        tags = EasyID3()
        tags.save(path)
        tags = EasyID3(path)
    tags["title"] = title
    if artist:
        tags["artist"] = artist
    tags.save(path)


class ResolveRequest(BaseModel):
    url: str
    downloadMode: str = "audio"


@app.post("/")
def resolve(req: ResolveRequest, request: Request):
    _sweep()
    if not req.url.strip():
        raise HTTPException(status_code=400, detail="No link given")

    file_id = uuid.uuid4().hex
    ydl_opts = {
        **_common_opts(),
        "format": "bestaudio/best",
        "outtmpl": str(DATA_DIR / f"{file_id}.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "0",
        }],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=True)
    except Exception as e:
        # Surface a real error instead of a "successful" empty result.
        raise HTTPException(status_code=502, detail=f"Extraction failed: {e}")

    produced = DATA_DIR / f"{file_id}.mp3"
    if not produced.exists() or produced.stat().st_size == 0:
        produced.unlink(missing_ok=True)
        raise HTTPException(status_code=502, detail="Produced no audio")

    title, artist = _extract_title_artist(info)
    _tag_file(produced, title, artist)

    display_name = f"{artist} - {title}" if artist else title
    return {
        "status": "tunnel",
        "url": f"{_base_url(request)}/file/{file_id}.mp3",
        "filename": f"{_safe(display_name)}.mp3",
    }


# --- Source role: search + streams, shaped like the app's Piped contract ---
#
# These let the app treat this resolver as a normal search source (add its URL
# under Support -> Install a source). The app itself ships no extraction code
# and names no service; it only calls /search and /streams like any backend.

def _thumb(entry: dict) -> str:
    thumbs = entry.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:
        url = thumbs[-1].get("url")
        if url:
            return url
    vid = entry.get("id")
    return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else ""


# Strip common upload-title noise so the banner shows a clean song title.
_NOISE = re.compile(
    r"\s*[\(\[]\s*(official\s*(music\s*)?(video|audio|lyric[s]?\s*video|visuali[sz]er)"
    r"|official\s*video|official\s*audio|lyric[s]?|lyric\s*video|audio|visuali[sz]er"
    r"|m/?v|performance\s*video|color\s*coded[^\)\]]*)\s*[\)\]]",
    re.IGNORECASE,
)


def _clean_title(title: str) -> str:
    cleaned = _NOISE.sub("", title)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -–—")
    return cleaned or title


# Prefer clean full-song uploads (lyric/audio) over video edits that often add
# intros/skits, and over live/performance/other clips. Higher = ranked first.
def _rank(raw_title: str, uploader: str) -> int:
    t = raw_title.lower()
    score = 0
    if "- topic" in uploader.lower():   # auto-generated official audio channels
        score += 5
    if re.search(r"lyric", t):
        score += 4
    if re.search(r"official\s*audio|\baudio\b", t):
        score += 3
    if re.search(r"\bofficial\s*(music\s*)?video\b|\bm/?v\b", t):
        score -= 2
    if re.search(r"\blive\b|performance|concert|tour|dance\s*practice", t):
        score -= 4
    if re.search(r"remix|sped\s*up|slowed|reverb|cover|reaction|teaser|trailer|instrumental", t):
        score -= 3
    return score


@app.get("/search")
def search(q: str = "", filter: str = ""):
    if not q.strip():
        return {"items": []}
    # Search is metadata-only (extract_flat): it never touches formats, so it
    # needs NONE of the extraction stack (Deno/EJS signature solving, POT token,
    # tv-client). Spreading _common_opts() here just added Deno-spawn + provider
    # setup cost to every search for nothing. Keep only cookies (help past bot
    # checks on the search request itself).
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,   # metadata only — fast, no per-video extraction
        "skip_download": True,
        "noplaylist": True,
    }
    if COOKIES_FILE and Path(COOKIES_FILE).is_file():
        opts["cookiefile"] = COOKIES_FILE
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch20:{q}", download=False)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Search failed: {e}")

    ranked = []
    for idx, e in enumerate(info.get("entries") or []):
        vid = e.get("id")
        if not vid:
            continue
        raw_title = e.get("title") or vid
        raw_uploader = e.get("uploader") or e.get("channel") or ""
        uploader = re.sub(r"\s*-\s*Topic$", "", raw_uploader)
        item = {
            "url": f"/watch?v={vid}",
            "title": _clean_title(raw_title),
            "uploaderName": uploader,
            "thumbnail": _thumb(e),
            "duration": int(e.get("duration") or 0),
            "type": "stream",
        }
        # Sort by preference, but keep the original relevance order within ties.
        ranked.append((-_rank(raw_title, raw_uploader), idx, item))

    ranked.sort(key=lambda r: (r[0], r[1]))
    return {"items": [r[2] for r in ranked]}


@app.get("/streams/{video_id}")
def streams(video_id: str):
    # Resolve a single video id to a directly-playable audio url. yt-dlp does the
    # extraction (handles the tokens Piped couldn't), then we hand the app the
    # best audio-only stream so it plays without downloading the whole file.
    opts = {
        **_common_opts(),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        # Don't let yt-dlp's default format selection raise before we inspect the
        # format list ourselves — we pick the audio format, not yt-dlp.
        "ignore_no_formats_error": True,
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_id, download=False)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Stream lookup failed: {e}")

    pool = _audio_formats(info)
    streams_out = [{
        "url": f["url"],
        "bitrate": int((f.get("abr") or f.get("tbr") or 0) * 1000),
        "mimeType": f.get("mime_type") or "audio/mp4",
    } for f in pool]

    if not streams_out:
        raise HTTPException(status_code=502, detail="No audio stream found")
    return {"audioStreams": streams_out}


# Cache resolved audio urls so seeking (each seek is a fresh Range request →
# otherwise a fresh yt-dlp extraction) reuses the url. googlevideo urls stay
# valid for hours; we expire ours well before that.
_URL_CACHE: dict = {}
_URL_CACHE_TTL = 1800  # seconds


def _audio_formats(info: dict) -> list:
    """Playable formats with audio, best last. Prefer audio-only; if YouTube's
    SABR experiment stripped the audio-only urls, fall back to muxed formats
    that still have audio + a url (larger, but plays)."""
    with_url = [f for f in (info.get("formats") or []) if f.get("url")
                and f.get("acodec") not in (None, "none")]
    audio_only = [f for f in with_url if f.get("vcodec") in (None, "none")]
    pool = audio_only or with_url
    pool.sort(key=lambda f: f.get("abr") or f.get("tbr") or 0)
    return pool


def _resolve_audio(video_id: str):
    """(url, mime) for a video's best audio stream, cached briefly."""
    now = time.time()
    hit = _URL_CACHE.get(video_id)
    if hit and hit[2] > now:
        return hit[0], hit[1]

    opts = {
        **_common_opts(),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "ignore_no_formats_error": True,
    }
    t0 = time.time()
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_id, download=False)
    # Print how long the extraction itself took (network + Deno n-sig + POT), so
    # the slow phase is visible in ~/resolver.log instead of guessed at. Cache
    # hits above skip this entirely (near-0), so a slow line here == cold extract.
    print(f"[resolve] {video_id} extract_info {time.time() - t0:.1f}s", flush=True)
    pool = _audio_formats(info)
    if not pool:
        raise HTTPException(status_code=502, detail="No audio stream found")
    # Playback is relayed through the phone tunnel's slow uplink, so pick a small
    # format: the highest-bitrate audio at or under PROXY_ABR_CAP (kbps), which
    # cuts the bytes moved without dropping to the "ultralow" tiers. Falls back to
    # the lowest available if everything is above the cap.
    cap = int(os.environ.get("PROXY_ABR_CAP", "80"))
    under_cap = [f for f in pool if (f.get("abr") or f.get("tbr") or 0) <= cap]
    best = under_cap[-1] if under_cap else pool[0]
    url = best["url"]
    mime = best.get("mime_type") or "audio/mp4"
    _URL_CACHE[video_id] = (url, mime, now + _URL_CACHE_TTL)
    return url, mime


@app.get("/proxy/{video_id}")
def proxy(video_id: str, request: Request):
    """Stream audio for a video THROUGH this server, so the phone only ever talks
    to the resolver — never directly to googlevideo. This matters when a phone
    tunnel (YTDLP_PROXY) is used: the googlevideo url is IP-locked to the tunnel's
    exit IP, which the phone can't reproduce on its own. We fetch it through the
    same proxy (so the source IP matches the lock) and relay the bytes, forwarding
    Range headers so the player can seek."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise HTTPException(status_code=404, detail="Bad video id")

    try:
        upstream_url, mime = _resolve_audio(video_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Resolve failed: {e}")

    import requests
    # Fetch through the SAME proxy as extraction so the source IP matches the
    # url's ip= lock. Without a proxy configured, a direct fetch also works.
    proxies = {"http": YTDLP_PROXY, "https": YTDLP_PROXY} if YTDLP_PROXY else None
    fwd_headers = {}
    if request.headers.get("range"):
        fwd_headers["Range"] = request.headers["range"]
    try:
        upstream = requests.get(
            upstream_url, headers=fwd_headers, stream=True, proxies=proxies, timeout=30
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream fetch failed: {e}")

    out_headers = {}
    for h in ("Content-Length", "Content-Range", "Accept-Ranges"):
        if h in upstream.headers:
            out_headers[h] = upstream.headers[h]
    out_headers.setdefault("Accept-Ranges", "bytes")
    ct = upstream.headers.get("Content-Type") or mime

    def body():
        try:
            for chunk in upstream.iter_content(64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return StreamingResponse(
        body(), status_code=upstream.status_code, media_type=ct, headers=out_headers
    )


@app.get("/prefetch/{video_id}")
def prefetch(video_id: str):
    """Resolve (and cache) a video's audio url WITHOUT streaming it — the app
    calls this for the next track while the current one plays, so by the time you
    hit Next the slow extraction is already done and playback starts instantly."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise HTTPException(status_code=404, detail="Bad video id")
    try:
        _resolve_audio(video_id)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


@app.get("/file/{name}")
def file(name: str):
    # Only ever serve files we created (hex-id.mp3), never arbitrary paths.
    if not re.fullmatch(r"[0-9a-f]{32}\.mp3", name):
        raise HTTPException(status_code=404, detail="Not found")
    path = DATA_DIR / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, media_type="audio/mpeg", filename=name)


# --- Cookie upload (lets the app push YouTube cookies without server access) ---
#
# The Android app signs into a throwaway account in a WebView, harvests the
# cookies, and POSTs them here — so YouTube stops blocking this datacenter IP
# with no FileZilla/SSH.
#
# Trust-on-first-use pairing: nothing to configure. The app makes its own random
# token and sends it with the first cookie push; that token is saved to PAIR_FILE
# and trusted from then on. No value to copy between app and server — but whoever
# pushes FIRST becomes the trusted device, so sign in from the app right after
# standing the server up. To re-pair, delete the pair file (rm cookies/.paired_token).

PAIR_FILE = Path(os.environ.get("COOKIES_PAIR_FILE", "/cookies/.paired_token"))


def _paired_token():
    return PAIR_FILE.read_text(encoding="utf-8").strip() if PAIR_FILE.is_file() else None


class CookieUpload(BaseModel):
    cookies: str
    # Client-generated pairing token; the first one received is trusted for good.
    device_token: str = ""


@app.post("/cookies")
def set_cookies(req: CookieUpload, x_cookie_token: str = Header("")):
    token = (x_cookie_token or req.device_token).strip()
    if not token:
        raise HTTPException(status_code=400, detail="Missing device token")

    paired = _paired_token()
    if paired is None:
        # First-ever call claims this server.
        PAIR_FILE.parent.mkdir(parents=True, exist_ok=True)
        PAIR_FILE.write_text(token, encoding="utf-8")
    elif token != paired:
        raise HTTPException(status_code=401, detail="Server already paired with another device")

    body = req.cookies.strip()
    if "youtube.com" not in body and "google.com" not in body:
        raise HTTPException(status_code=400, detail="That doesn't look like YouTube/Google cookies")

    path = Path(COOKIES_FILE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Ensure a Netscape header line so yt-dlp accepts the file.
        if not body.startswith("# Netscape") and not body.startswith("# HTTP Cookie"):
            body = "# Netscape HTTP Cookie File\n" + body
        path.write_text(body + "\n", encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Couldn't save cookies: {e}")
    return {"status": "ok", "lines": body.count("\n") + 1}


@app.get("/cookies/status")
def cookies_status(x_cookie_token: str = Header("")):
    paired = _paired_token()
    if paired is None or x_cookie_token != paired:
        raise HTTPException(status_code=401, detail="Not paired with this device")
    p = Path(COOKIES_FILE)
    if not p.is_file():
        return {"present": False}
    return {"present": True, "updated": int(p.stat().st_mtime), "bytes": p.stat().st_size}


@app.get("/tunnel-status")
def tunnel_status():
    """Whether YTDLP_PROXY (e.g. the phone/home reverse-SSH SOCKS tunnel) is
    actually reachable right now — a plain TCP connect, not a full proxy test.
    Lets the app show "tunnel connected" without doing a real YouTube request."""
    if not YTDLP_PROXY:
        return {"configured": False, "connected": False}
    m = re.match(r"^[a-zA-Z0-9]+://([^:/]+):(\d+)", YTDLP_PROXY)
    if not m:
        return {"configured": True, "connected": False, "detail": "Couldn't parse YTDLP_PROXY"}
    host, port = m.group(1), int(m.group(2))
    import socket
    try:
        with socket.create_connection((host, port), timeout=3):
            return {"configured": True, "connected": True}
    except OSError as e:
        return {"configured": True, "connected": False, "detail": str(e)}


@app.get("/health")
def health():
    import yt_dlp
    return {"status": "ok", "yt_dlp": yt_dlp.version.__version__}
