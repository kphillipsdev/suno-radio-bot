# src/cogs/music.py
import discord
from discord.ext import commands, tasks
from discord import ui, app_commands
from collections import deque, defaultdict
import asyncio
import random
import os
import concurrent.futures
import time
import re
import datetime
import csv
from pathlib import Path
from discord.utils import escape_markdown
from src.data.persistence import load_data, save_data
from src.utils.extractor import extract_song_info
from src.utils.playlist_fast_scraper import get_playlist_links_api
from src.utils.prefetch import prefetch_to_file
from src.data.db import like_track, unlike_track, get_like_count, get_user_like_count, top_liked_for_users, get_user_liked_tracks_all_guilds, get_conn, recent_plays
from src.utils.shuffle_displacing_first import shuffle_displacing_first_inplace
try:
    from src.utils.image_cache import cache_song_image, get_default_image_path
except ImportError:
    # Fallback if import fails (e.g., during reload)
    from src.utils import image_cache
    cache_song_image = image_cache.cache_song_image
    get_default_image_path = image_cache.get_default_image_path
from src.ui.queue_manager import QueueManagerView, build_queue_embed
from src.ui.pagination import PaginatedView
from src.ui.liked_songs_manager import LikedSongsManagerView

# === Play history DB (safe if module not present) ===========================
try:
    from src.data.db import upsert_track_basic, log_play_start, log_play_end
except Exception:
    upsert_track_basic = lambda **kwargs: None
    def log_play_start(**kwargs): return None
    def log_play_end(**kwargs): return None

# ===== Embed + Formatting Helpers ===========================================
EMBED_COLOR_PLAYING = 0x580fd6  # Purple (default/Suno)
EMBED_COLOR_ADDED   = 0xc1d4d6

# Platform-specific colors for Now Playing embeds
PLATFORM_COLORS = {
    "suno": 0xFD429C,       # Pink (Suno brand) - rgb(253, 66, 156)
    "soundcloud": 0xFF5500, # Orange (SoundCloud brand)
    "bandcamp": 0x1DA0C3,   # Teal/Cyan (Bandcamp brand)
    "mixcloud": 0x5000FF,   # Purple (Mixcloud brand)
    "audiomack": 0xFFA200,  # Orange/Gold (Audiomack brand)
    "direct": 0x2DD4BF,     # Teal (direct MP3/files)
    "default": 0x580fd6,    # Purple (fallback)
}

# Discord file upload limit (8MB for regular servers, using 7MB to be safe)
MAX_THUMBNAIL_SIZE_BYTES = 7 * 1024 * 1024  # 7MB

# Commands whose *text* messages should be auto-deleted after successful run
AUTO_DELETE_COMMANDS: set[str] = {"skip", "stop", "top", "history", "queue", "remove", "join" ,"leave", "autofill_saves"}

# ---- Prefetch config (env-driven) ------------------------------------------
PREFETCH_MODE    = os.getenv("PREFETCH_MODE", "full").lower()  # "none" | "warmup" | "full"
PREFETCH_BYTES   = int(os.getenv("PREFETCH_BYTES", "524288"))    # ~512 KB for warmup
PREFETCH_TIMEOUT = int(os.getenv("PREFETCH_TIMEOUT", "60"))      # seconds
PREFETCH_DIR     = os.getenv("PREFETCH_DIR", "songs") or "songs"

# ---- Startup polish & FFmpeg tuning --------------------------------------
PREBUFFER_SECONDS       = float(os.getenv("PREBUFFER_SECONDS", "0.5"))   # wait before play() to fill buffers
FADE_IN_SECONDS         = float(os.getenv("FADE_IN_SECONDS", "0.5"))     # 0 disables fade-in
FADE_IN_STEPS           = int(os.getenv("FADE_IN_STEPS", "20"))           # number of steps in the fade
FADE_OUT_SECONDS        = float(os.getenv("FADE_OUT_SECONDS", "1.0"))     # 0 disables
FADE_OUT_STEPS          = int(os.getenv("FADE_OUT_STEPS", "20"))
STARTUP_ADELAY_MS       = int(os.getenv("STARTUP_ADELAY_MS", "200"))      # adelay padding in ms for first packets
FFMPEG_PROBESIZE        = os.getenv("FFMPEG_PROBESIZE", "8M")             # probe size; lower = faster start
FFMPEG_ANALYZEDURATION  = os.getenv("FFMPEG_ANALYZEDURATION", "5M")       # analyze duration; lower = faster start
FFMPEG_THREAD_QUEUE_SIZE = int(os.getenv("FFMPEG_THREAD_QUEUE_SIZE", "1024"))
FFMPEG_RW_TIMEOUT_US     = int(os.getenv("FFMPEG_RW_TIMEOUT_US", "15000000"))  # 15s
FFMPEG_NOBUFFER          = os.getenv("FFMPEG_NOBUFFER", "0") == "1"
FFMPEG_BUFFER_SIZE       = os.getenv("FFMPEG_BUFFER_SIZE", "512k")
FFMPEG_MAX_DELAY_US      = int(os.getenv("FFMPEG_MAX_DELAY_US", "5000000"))
VOICE_BITRATE_KBPS = int(os.getenv("VOICE_BITRATE_KBPS", "128"))

# Queue/playlist clear policy toggles
CLEAR_PLAYLISTS_ON_STOP   = os.getenv("CLEAR_PLAYLISTS_ON_STOP", "0") == "1"
CLEAR_PLAYLISTS_ON_RELOAD = os.getenv("CLEAR_PLAYLISTS_ON_RELOAD", "0") == "1"

# ---- Autofill (idle radio) -------------------------------------------------
AUTOFILL_FEATURE   = os.getenv("AUTOFILL_FEATURE", "1") == "1"
AUTOFILL_DELAY_SEC = int(os.getenv("AUTOFILL_DELAY_SEC", "30"))   # wait after finishing
AUTOFILL_MAX_PULL  = int(os.getenv("AUTOFILL_MAX_PULL", "50"))    # how many to enqueue per fill
DEFAULT_AUTOFILL_URL = os.getenv("DEFAULT_AUTOFILL_URL", "").strip()
DEFAULT_AUTOFILL_CSV = os.getenv("DEFAULT_AUTOFILL_CSV", "").strip()
AUTOFILL_LIKES_PER_USER = int(os.getenv("AUTOFILL_LIKES_PER_USER", "5"))

# ---- Requester VC check ---------------------------------------------------
SKIP_IF_REQUESTER_LEFT = os.getenv("SKIP_IF_REQUESTER_LEFT", "1") == "1"
SHOW_SKIP_MESSAGE = os.getenv("SHOW_SKIP_MESSAGE", "1") == "1"

# ---- Queue add limit (peak throttle) ---------------------------------------
QUEUE_LIMIT_DEFAULT_ENABLED = os.getenv("QUEUE_LIMIT_DEFAULT_ENABLED", "1") == "1"
QUEUE_LIMIT_MAX_PER_ADD     = int(os.getenv("QUEUE_LIMIT_MAX_PER_ADD", "200"))  # default cap
QUEUE_MAX_PER_USER          = int(os.getenv("QUEUE_MAX_PER_USER", "3"))        # hard cap per user in queue

# ---- Now Playing pruning ---------------------------------------------------
# Prune NP cards once N subsequent songs have started.
REMOVE_NP_AFTER_SONGS = int(os.getenv("REMOVE_NP_AFTER_SONGS", "1"))  # default=1 song
REMOVE_NON_AUTOFILL_NP = os.getenv("REMOVE_NON_AUTOFILL_NP", "0") == "1"  # default=stay (False)

async def maybe_prefetch(song: dict) -> str | None:
    """
    Uses env PREFETCH_MODE to optionally warm up or fully cache the audio.
    Returns a local file path if a full download happened; otherwise None.
    """
    mode = PREFETCH_MODE
    if mode not in ("warmup", "full"):
        return None

    url = str(song.get("url") or "").strip()
    if not url or url.startswith("songs/"):
        return None  # already local or no url

    # Skip prefetch for yt-dlp sources (SoundCloud, Bandcamp, etc.)
    # These often use HLS streams or expiring URLs that can't be prefetched
    if song.get("_source"):
        return None

    # Skip prefetch for HLS/m3u8 streams (can't download as single file)
    if ".m3u8" in url or "/hls" in url.lower():
        return None

    referer = song.get("suno_url") or "https://suno.com/"
    loop = asyncio.get_running_loop()

    if mode == "warmup":
        # partial download then discard (prime CDN/TLS)
        await loop.run_in_executor(
            None,
            lambda: prefetch_to_file(
                url,
                out_dir=PREFETCH_DIR,
                timeout=min(PREFETCH_TIMEOUT, 15),
                referer=referer,
                full_download=False,
                max_bytes=PREFETCH_BYTES,
            )
        )
        return None

    # mode == "full"
    local_path = await loop.run_in_executor(
        None,
        lambda: prefetch_to_file(
            url,
            out_dir=PREFETCH_DIR,
            timeout=PREFETCH_TIMEOUT,
            referer=referer,
            full_download=True,
        )
    )
    if local_path:
        song["url"] = local_path
        song["local_file"] = local_path
    return local_path


def _fmt_duration(d):
    """Accept seconds or 'MM:SS'/'HH:MM:SS' string; return human readable."""
    if d is None:
        return "—"
    if isinstance(d, str):
        return d
    try:
        sec = int(d)
        m, s = divmod(max(sec, 0), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    except Exception:
        return str(d)

def _duration_to_seconds(d) -> int | None:
    """Return total seconds from int/float or 'HH:MM:SS'/'MM:SS' strings. None if unknown."""
    if d is None:
        return None
    if isinstance(d, (int, float)):
        return max(0, int(d))
    s = str(d).strip()
    if not s:
        return None
    parts = s.split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == 3:
        h, m, sec = parts
        return max(0, h * 3600 + m * 60 + sec)
    if len(parts) == 2:
        m, sec = parts
        return max(0, m * 60 + sec)
    try:
        return max(0, int(s))
    except ValueError:
        return None

def _truncate(text: str | None, limit: int = 300) -> str:
    if not text:
        return "—"
    t = text.strip()
    return t if len(t) <= limit else (t[:limit - 1] + "…")

def _get_platform_color(track: dict) -> int:
    """Get the embed color based on the track's source platform."""
    source = (track.get("_source") or "").lower()
    url = (track.get("url") or "").lower()
    suno_url = track.get("suno_url") or ""
    
    # Check _source field first (for yt-dlp sourced tracks)
    if source:
        if "soundcloud" in source:
            return PLATFORM_COLORS["soundcloud"]
        if "bandcamp" in source:
            return PLATFORM_COLORS["bandcamp"]
        if "mixcloud" in source:
            return PLATFORM_COLORS["mixcloud"]
        if "audiomack" in source:
            return PLATFORM_COLORS["audiomack"]
        # Other yt-dlp sources get the direct/teal color
        return PLATFORM_COLORS["direct"]
    
    # Check if it's a Suno track (CDN URLs, suno_url field, or local prefetched files)
    if "suno.ai" in url or "suno.com" in url or "suno.com" in suno_url:
        return PLATFORM_COLORS["suno"]
    
    # Check for prefetched Suno files (songs/*.mp3 from Suno CDN)
    if url.startswith("songs/") or url.startswith("/") and "/songs/" in url:
        return PLATFORM_COLORS["suno"]
    
    # Check URL for direct media files (non-Suno)
    if url.endswith((".mp3", ".wav", ".ogg", ".m4a", ".flac", ".opus")):
        return PLATFORM_COLORS["direct"]
    
    # Default to Suno color (most tracks are Suno)
    return PLATFORM_COLORS["suno"]

def _get_platform_name(url: str) -> str:
    """Get a friendly platform name from a URL for button labels."""
    if not url:
        return "Source"
    url_lower = url.lower()
    if "suno.com" in url_lower:
        return "Suno"
    if "soundcloud.com" in url_lower:
        return "SoundCloud"
    if "bandcamp.com" in url_lower:
        return "Bandcamp"
    if "mixcloud.com" in url_lower:
        return "Mixcloud"
    if "audiomack.com" in url_lower:
        return "Audiomack"
    if "audius.co" in url_lower:
        return "Audius"
    if "hearthis.at" in url_lower:
        return "Hearthis"
    if "newgrounds.com" in url_lower:
        return "Newgrounds"
    return "Source"

def _derive_suno_url(track: dict) -> str | None:
    """
    Prefer explicit 'suno_url', else derive from known Suno CDN or local cache paths.
    """
    if track.get("suno_url"):
        return track["suno_url"]

    url = (track.get("url") or "").strip()
    # songs/{id}.mp3
    if url.startswith("songs/") and url.endswith(".mp3"):
        song_id = url[6:-4]
        return f"https://suno.com/song/{song_id}"

    # cdn1.suno.ai/.../{id}.mp3
    m = re.search(r"/([a-f0-9\-]{8,})\.mp3", url, re.I)
    if m:
        return f"https://suno.com/song/{m.group(1)}"

    # if track had a page url cached elsewhere
    page = track.get("page") or track.get("page_url")
    if page and "suno.com" in page:
        return page

    return None

def _canonical_track_id(track: dict) -> str | None:
    # 1) explicit id if you already stash one
    if track.get("id"):
        return str(track["id"])

    # 2) try the Suno page URL
    page = _derive_suno_url(track) or (track.get("url") or "")
    m = re.search(r"/song/([A-Za-z0-9\-]{8,})", page)
    if m:
        return m.group(1)

    # 3) audio filename .../{id}.mp3 (including "songs/{id}.mp3")
    url = str(track.get("url") or "")
    m = re.search(r"/([A-Fa-f0-9\-]{8,})\.mp3", url)
    if m:
        return m.group(1)
    if url.startswith("songs/") and url.endswith(".mp3"):
        return Path(url).stem

    return None

def _scrape_playlist_to_tracks(playlist_url: str, limit: int = 0) -> list[dict]:
    """
    Scrape a Suno playlist using the fast API and convert URLs to track dict format.
    Returns list of dicts with 'url' and 'suno_url' fields compatible with _resolve_tracks().
    """
    if not playlist_url or not playlist_url.strip():
        return []
    
    try:
        urls = get_playlist_links_api(playlist_url.strip())
        if limit > 0:
            urls = urls[:limit]
        return [{"url": url, "suno_url": url} for url in urls]
    except Exception as e:
        print(f"[playlist scraper] Error scraping playlist {playlist_url}: {e}")
        return []

def _track_title_link(track: dict) -> str:
    title = escape_markdown((track.get("title") or "Untitled").strip())
    
    # Try to get a linkable URL (prefer page URLs over raw audio URLs)
    link = None
    
    # 1. For yt-dlp sources (SoundCloud, Bandcamp, etc.), use the original page URL
    if track.get("_original_url"):
        link = track["_original_url"]
    # 2. For yt-dlp sources, video_url also contains the page URL
    elif track.get("video_url") and not track.get("video_url", "").endswith((".mp3", ".mp4", ".webm")):
        link = track["video_url"]
    # 3. For Suno tracks, derive the suno.com URL
    elif _derive_suno_url(track):
        link = _derive_suno_url(track)
    
    # Only link if it's a proper page URL (not raw audio)
    if link and any(domain in link for domain in ("suno.com", "soundcloud.com", "bandcamp.com", "mixcloud.com", "audiomack.com", "audius.co", "hearthis.at", "newgrounds.com")):
        return f"[**{title}**]({link})"
    return f"**{title}**"

def _artist_line(track: dict) -> str:
    # Back-compat if older entries still store 'author'
    artist = (track.get("artist") or track.get("author") or "Unknown").strip()
    return f"*by {escape_markdown(artist)}*"

def _filler_badge(track: dict) -> str:
    """
    Returns a short inline badge for autofill tracks.
    """
    return " ⟳" if track.get("_autofill") else ""

def _prompt_text(track: dict) -> str:
    # Prefer 'prompt' if present, otherwise fall back to common fields
    prompt = track.get("prompt") or ""
    return _truncate(prompt, 300)

def _thumb(track: dict) -> str | None:
    """Get thumbnail URL for external URLs (fallback). Use _get_thumbnail_info for local files."""
    url = track.get("thumbnail") or track.get("thumb") or track.get("image") or track.get("image_url")
    if not url or not isinstance(url, str) or not url.startswith("http"):
        return url
    buster = f"v={int(time.time())}"
    return f"{url}&{buster}" if "?" in url else f"{url}?{buster}"


def _get_thumbnail_info(track: dict) -> tuple[str | None, str | None]:
    """
    Get thumbnail info for a track, preferring local cached images.
    Falls back to default image if no thumbnail is available.
    
    Returns:
        tuple of (thumbnail_url, local_file_path)
        - If local file exists: ("attachment://filename.ext", "/path/to/file.ext")
        - If only external URL: ("https://...", None)
        - If no thumbnail but default exists: ("attachment://default.jpg", "/path/to/default.jpg")
        - If nothing available: (None, None)
    """
    # Check for local thumbnail first (most reliable)
    local_path = track.get("local_thumbnail")
    if local_path and os.path.exists(local_path):
        filename = os.path.basename(local_path)
        return f"attachment://{filename}", local_path
    
    # Try to cache if we have a thumbnail URL but no local copy
    thumb_url = track.get("thumbnail") or track.get("thumb") or track.get("image") or track.get("image_url")
    if thumb_url and isinstance(thumb_url, str) and thumb_url.startswith("http"):
        # Try to cache the image (will return default on failure)
        try:
            local_path = cache_song_image(track, use_default_on_fail=True)
            if local_path and os.path.exists(local_path):
                # Update track dict for future use
                track["local_thumbnail"] = local_path
                filename = os.path.basename(local_path)
                return f"attachment://{filename}", local_path
        except Exception:
            pass
        
        # Fallback to external URL with cache-buster
        buster = f"v={int(time.time())}"
        url_with_buster = f"{thumb_url}&{buster}" if "?" in thumb_url else f"{thumb_url}?{buster}"
        return url_with_buster, None
    
    # No thumbnail URL - use default image if available
    default_path = get_default_image_path()
    if default_path and os.path.exists(default_path):
        filename = os.path.basename(default_path)
        return f"attachment://{filename}", default_path
    
    return None, None

def _format_upcoming_list(tracks: list[dict], limit: int = 2) -> str:
    if not tracks:
        return "—"
    lines = []
    for i, t in enumerate(tracks[:limit], start=1):
        title = _track_title_link(t) + _filler_badge(t)  # ⬅️ add badge
        artist = (t.get("artist") or t.get("author") or "Unknown").strip()
        byline = f"*by {escape_markdown(artist)}*"
        requester = (t.get("requester_mention")
                     or (f"<@{t['requester_id']}>" if t.get("requester_id") else None)
                     or t.get("requester_tag")
                     or t.get("requester_name")
                     or "someone")
        lines.append(f"{i}. {title} {byline} / Requested by {requester}")
    return "\n".join(lines)

def _join_info_blocks(prompt: str | None, lyrics: str | None) -> str:
    parts = []
    if prompt and prompt.strip():
        parts.append(prompt.strip())
    if lyrics and lyrics.strip():
        if parts:
            parts.append("")  # spacer line between prompt and lyrics
        parts.append(lyrics.strip())
    return "\n".join(parts).strip() or "*No prompt/lyrics available for this track.*"

def _chunk_text(s: str | None, limit: int = 3900) -> list[str]:
    """Split long text into Discord-safe chunks, preferring paragraph/line breaks."""
    if not s:
        return []
    s = s.strip()
    if len(s) <= limit:
        return [s]

    out: list[str] = []
    remaining = s
    while len(remaining) > limit:
        # try paragraph break
        cut = remaining.rfind("\n\n", 0, limit)
        if cut == -1:
            # try single line break
            cut = remaining.rfind("\n", 0, limit)
        if cut == -1:
            # hard cut
            cut = limit
        out.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        out.append(remaining)
    return out

def build_now_playing_embed(track: dict, requester_mention: str | None, upcoming_tracks: list[dict] | None = None) -> tuple[discord.Embed, discord.File | None]:
    """
    Build the now playing embed with optional local thumbnail file.
    
    Returns:
        tuple of (embed, file) where file is a discord.File if using local thumbnail, else None
    """
    desc = [
        _track_title_link(track) + _filler_badge(track),
        _artist_line(track),
        ""
    ]
    # Use platform-specific color (Suno=pink, SoundCloud=orange, etc.)
    embed_color = _get_platform_color(track)
    embed = discord.Embed(
        title="🎵 Now Playing",
        description="\n".join(desc),
        color=embed_color
    )
    embed.add_field(name="Duration", value=_fmt_duration(track.get("duration")), inline=True)

    ts = int(track.get("requested_at") or datetime.datetime.now(datetime.timezone.utc).timestamp())
    req_val = (requester_mention or "—") + f" at <t:{ts}:t>"
    embed.add_field(name="Requested by", value=req_val, inline=True)

    if upcoming_tracks:
        embed.add_field(
            name="Up next",
            value=_format_upcoming_list(upcoming_tracks, limit=2),
            inline=False
        )

    # Get thumbnail info (prefers local cached images)
    thumb_file = None
    thumb_url, local_path = _get_thumbnail_info(track)
    if thumb_url:
        embed.set_thumbnail(url=thumb_url)
        if local_path:
            try:
                # Check file size before uploading (Discord has 8MB limit)
                file_size = os.path.getsize(local_path)
                if file_size <= MAX_THUMBNAIL_SIZE_BYTES:
                    thumb_file = discord.File(local_path, filename=os.path.basename(local_path))
                else:
                    # File too large, use external URL instead
                    print(f"[thumbnail] Skipping local file ({file_size / 1024 / 1024:.1f}MB > 7MB limit): {local_path}")
                    fallback_thumb = _thumb(track)
                    if fallback_thumb:
                        embed.set_thumbnail(url=fallback_thumb)
            except Exception:
                # Fallback to external URL if file creation fails
                fallback_thumb = _thumb(track)
                if fallback_thumb:
                    embed.set_thumbnail(url=fallback_thumb)

    embed.set_footer(text="Suno Radio")
    embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return embed, thumb_file

def build_added_embed(
    track: dict,
    requester_mention: str | None,
    position: int | None = None,
    eta_seconds: int | None = None,
    eta_unknown: bool = False
) -> tuple[discord.Embed, discord.File | None]:
    """
    Added card: heading = song title (clickable), body = artist,
    fields = Duration, Requested by (with original request time), Position (+ ETA).
    
    Returns:
        tuple of (embed, file) where file is a discord.File if using local thumbnail, else None
    """
    desc = [
        _track_title_link(track) + _filler_badge(track),
        _artist_line(track),
        ""
    ]
    # Use platform-specific color (Suno=pink, SoundCloud=orange, etc.)
    embed_color = _get_platform_color(track)
    embed = discord.Embed(
        title="➕ Added",
        description="\n".join([s for s in desc if s is not None]),
        color=embed_color
    )
    embed.add_field(name="Duration", value=_fmt_duration(track.get("duration")), inline=True)

    ts = int(track.get("requested_at") or datetime.datetime.now(datetime.timezone.utc).timestamp())
    req_val = (requester_mention or "—") + f" at <t:{ts}:t>"
    embed.add_field(name="Requested by", value=req_val, inline=True)

    if isinstance(position, int) and position >= 1:
        eta_label = None
        if eta_seconds is not None:
            eta_label = _fmt_duration(max(0, int(eta_seconds)))
        elif eta_unknown:
            eta_label = "≈unknown"

        pos_val = f"#{position}" + (f" (Up in ~{eta_label})" if eta_label else "")
        embed.add_field(name="Position", value=pos_val, inline=False)

    # Get thumbnail info (prefers local cached images)
    thumb_file = None
    thumb_url, local_path = _get_thumbnail_info(track)
    if thumb_url:
        embed.set_thumbnail(url=thumb_url)
        if local_path:
            try:
                # Check file size before uploading (Discord has 8MB limit)
                file_size = os.path.getsize(local_path)
                if file_size <= MAX_THUMBNAIL_SIZE_BYTES:
                    thumb_file = discord.File(local_path, filename=os.path.basename(local_path))
                else:
                    # File too large, use external URL instead
                    print(f"[thumbnail] Skipping local file ({file_size / 1024 / 1024:.1f}MB > 7MB limit): {local_path}")
                    fallback_thumb = _thumb(track)
                    if fallback_thumb:
                        embed.set_thumbnail(url=fallback_thumb)
            except Exception:
                # Fallback to external URL if file creation fails
                fallback_thumb = _thumb(track)
                if fallback_thumb:
                    embed.set_thumbnail(url=fallback_thumb)

    return embed, thumb_file

# ---- Song Info helpers (module scope) --------------------------------------
def _render_song_header(song: dict) -> str:
    # Reuse existing helpers for safety/consistency
    title_raw = (song.get("title") or "Unknown Title").strip()
    title = escape_markdown(title_raw)
    link  = _derive_suno_url(song) or (song.get("url") or "").strip()

    artist_raw = (song.get("artist") or song.get("author") or "Unknown Artist").strip()
    artist = escape_markdown(artist_raw)

    # Only link if Suno/page URL; avoid raw audio deep links
    if link and ("suno.com" in link):
        title_md = f"**[{title}]({link})**"
    else:
        title_md = f"**{title}**"

    byline_md = f"*By {artist}*"
    
    parts = [title_md, byline_md]
    
    # Build date + model line together: "August 17, 2025 at 12:01 AM (V 3.5 chirp-chirp)"
    date_model_parts = []
    
    # Add created date (date only, no time since it's timezone dependent)
    created_at = song.get("created_at")
    if created_at:
        try:
            from datetime import datetime
            # Parse ISO format: "2025-08-17T04:01:25.427Z"
            dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            # Format as "August 17, 2025"
            formatted_date = dt.strftime("%B %d, %Y").replace(" 0", " ")
            date_model_parts.append(formatted_date)
        except (ValueError, AttributeError):
            pass  # Skip if date parsing fails
    
    # Add model version/name in parentheses
    model_version = song.get("major_model_version")
    model_name = song.get("model_name")
    if model_version or model_name:
        model_parts = []
        if model_version:
            # Format as "v3.5" - lowercase v, no space
            v = model_version.lstrip('vV')  # Remove leading v/V
            model_parts.append(f"v{v}")
        if model_name:
            model_parts.append(model_name)
        if model_parts:
            date_model_parts.append(f"({' '.join(model_parts)})")
    
    if date_model_parts:
        parts.append(" ".join(date_model_parts))
    
    # Add stats
    play_count = song.get("play_count")
    like_count = song.get("like_count")
    if play_count is not None or like_count is not None:
        stats_parts = []
        if play_count is not None:
            stats_parts.append(f"{play_count:,} Plays")
        if like_count is not None:
            stats_parts.append(f"{like_count:,} Likes")
        if stats_parts:
            parts.append(" / ".join(stats_parts))
    
    return "\n".join(parts).strip()


def _render_prompt_lyrics_block(song: dict) -> str:
    prompt = (song.get("prompt") or "").strip()
    lyrics = (song.get("lyrics") or "").strip()

    parts = []
    parts.append("**Prompt**")
    parts.append(prompt if prompt else "_No prompt provided._")
    parts.append("")  # blank line
    parts.append("**Lyrics**")
    parts.append(lyrics if lyrics else "_No lyrics provided._")

    return "\n".join(parts).strip()

def build_song_info_embed(song: dict) -> tuple[discord.Embed, discord.File | None]:
    """
    Build the song info embed (same as song_info command).
    Used for displaying lyrics and prompt information.
    
    Returns:
        tuple of (embed, file) where file is a discord.File if using local thumbnail, else None
    """
    song = song.copy()
    
    # Build the embed
    header = _render_song_header(song)
    prompt_lyrics = _render_prompt_lyrics_block(song)
    
    # Split long content if needed (Discord embed description limit is 4096)
    # We'll use description for header and first chunk, then fields for overflow
    description_parts = [header]
    
    # Try to fit prompt/lyrics in description first
    combined_length = len(header) + len("\n\n") + len(prompt_lyrics)
    if combined_length <= 4000:  # Leave some margin
        description_parts.append("")
        description_parts.append(prompt_lyrics)
        embed = discord.Embed(
            title="📝 Song Information",
            description="\n".join(description_parts),
            color=EMBED_COLOR_PLAYING
        )
    else:
        # Need to chunk it
        embed = discord.Embed(
            title="📝 Song Information",
            description=header,
            color=EMBED_COLOR_PLAYING
        )
        # Split prompt and lyrics separately for better formatting
        prompt = (song.get("prompt") or "").strip()
        lyrics = (song.get("lyrics") or "").strip()
        
        if prompt:
            prompt_chunks = _chunk_text(prompt, limit=1020)  # Field value limit is 1024
            for i, chunk in enumerate(prompt_chunks):
                embed.add_field(
                    name="**Prompt**" if i == 0 else f"",
                    value=chunk,
                    inline=False
                )
        else:
            embed.add_field(name="**Prompt**", value="_No prompt provided._", inline=False)
        
        if lyrics:
            lyrics_chunks = _chunk_text(lyrics, limit=1020)
            for i, chunk in enumerate(lyrics_chunks):
                embed.add_field(
                    name="**Lyrics**" if i == 0 else f"",
                    value=chunk,
                    inline=False
                )
        else:
            embed.add_field(name="**Lyrics**", value="_No lyrics provided._", inline=False)
    
    # Add duration if available
    duration = song.get("duration")
    if duration:
        embed.add_field(name="Duration", value=_fmt_duration(duration), inline=True)
    
    # Get thumbnail info (prefers local cached images)
    thumb_file = None
    thumb_url, local_path = _get_thumbnail_info(song)
    if thumb_url:
        embed.set_thumbnail(url=thumb_url)
        if local_path:
            try:
                thumb_file = discord.File(local_path, filename=os.path.basename(local_path))
            except Exception:
                # Fallback to external URL if file creation fails
                fallback_thumb = _thumb(song)
                if fallback_thumb:
                    embed.set_thumbnail(url=fallback_thumb)
    
    embed.set_footer(text="Suno Radio")
    embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return embed, thumb_file

# ---------------------------------------------------------------------------

LIKE_EMOJI_NAME = "sunobotlike"
LIKE_EMOJI_ID   = 1437172794499534930
LIKE_FALLBACK   = "👍"

class LikeView(discord.ui.View):
    def __init__(
        self,
        *,
        track_id: str,
        guild_id: int,
        bot_user_id: int,
        song_title: str | None = None,
        song_url: str | None = None,
        timeout: float | None = 3600,
        show_count: bool = False,  # toggle here
    ):
        super().__init__(timeout=timeout)
        self.track_id = track_id
        self.guild_id = guild_id
        self.bot_user_id = bot_user_id
        self.song_title = song_title or "Untitled"
        self.song_url = (song_url or "").strip()
        self.show_count = show_count
        # Track which users have clicked in this view instance
        self.user_clicked = set()

        try:
            count = get_like_count(track_id=track_id, guild_id=guild_id)
        except Exception:
            count = 0

        # Set emoji (separate from label)
        try:
            self.like_btn.emoji = discord.PartialEmoji(name=LIKE_EMOJI_NAME, id=LIKE_EMOJI_ID)
        except Exception:
            self.like_btn.emoji = LIKE_FALLBACK

        # Default: hide the count
        self.like_btn.label = "Save for Autofill"
        # For testing, show the count at init:
        # if self.show_count:
        #     self.like_btn.label = str(count)

    @discord.ui.button(
        style=discord.ButtonStyle.primary,
        label="Save for Autofill",
        custom_id="suno_like_btn"
    )
    async def like_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.bot:
            return await interaction.response.defer(ephemeral=True)

        try:
            user_id = interaction.user.id
            has_clicked_before = user_id in self.user_clicked
            
            # Check if user has existing likes for this track
            existing_user_likes = get_user_like_count(
                track_id=self.track_id, guild_id=self.guild_id, user_id=user_id
            )
            
            if has_clicked_before:
                # On subsequent clicks in the same view: toggle like/unlike
                if existing_user_likes > 0:
                    # User has likes, so unlike (remove one like)
                    total = unlike_track(
                        track_id=self.track_id, guild_id=self.guild_id, user_id=user_id
                    )
                    user_count = get_user_like_count(
                        track_id=self.track_id, guild_id=self.guild_id, user_id=user_id
                    )
                    if user_count == 0:
                        msg = f"Removed your like for **{self.song_title}**."
                    else:
                        msg = f"Removed a like for **{self.song_title}**. (You still have {user_count} like{'s' if user_count != 1 else ''})"
                else:
                    # User has no likes, so like it
                    total = like_track(
                        track_id=self.track_id, guild_id=self.guild_id, user_id=user_id,
                        username=str(interaction.user),
                    )
                    user_count = get_user_like_count(
                        track_id=self.track_id, guild_id=self.guild_id, user_id=user_id
                    )
                    if user_count > 1:
                        msg = f"Saved to Autofill **{self.song_title}**! (You've liked this {user_count} times)"
                    else:
                        msg = f"Saved to Autofill **{self.song_title}**!"
            else:
                # First click in this view
                if existing_user_likes > 0:
                    # User already has likes, so add another like
                    total = like_track(
                        track_id=self.track_id, guild_id=self.guild_id, user_id=user_id,
                        username=str(interaction.user),
                    )
                    user_count = get_user_like_count(
                        track_id=self.track_id, guild_id=self.guild_id, user_id=user_id
                    )
                    msg = f"Saved to Autofill **{self.song_title}** again! (You've liked this {user_count} times)"
                else:
                    # User has no likes, so like it
                    total = like_track(
                        track_id=self.track_id, guild_id=self.guild_id, user_id=user_id,
                        username=str(interaction.user),
                    )
                    user_count = get_user_like_count(
                        track_id=self.track_id, guild_id=self.guild_id, user_id=user_id
                    )
                    if user_count > 1:
                        msg = f"Saved to Autofill **{self.song_title}**! (You've liked this {user_count} times)"
                    else:
                        msg = f"Saved to Autofill **{self.song_title}**!"
                
                # Mark that this user has clicked in this view
                self.user_clicked.add(user_id)
            
            button.label = str(total) if self.show_count else "Save for Autofill"

            await interaction.response.edit_message(view=self)

            if self.song_url.startswith("http"):
                link_view = discord.ui.View()
                platform_name = _get_platform_name(self.song_url)
                link_view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, url=self.song_url, label=f"Open on {platform_name}"))
                await interaction.followup.send(msg, view=link_view, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)

        except Exception as e:
            try:
                await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)
            except Exception:
                pass


class LyricsButton(discord.ui.Button):
    """Button for viewing lyrics/prompt."""
    def __init__(self, song: dict):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="View Lyrics/Prompt"
        )
        self.song = song.copy()
    
    async def callback(self, interaction: discord.Interaction):
        """Handle the View Lyrics/Prompt button click."""
        if interaction.user.bot:
            return await interaction.response.defer(ephemeral=True)
        
        try:
            embed, thumb_file = build_song_info_embed(self.song)
            if thumb_file:
                await interaction.response.send_message(embed=embed, file=thumb_file, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            try:
                await interaction.response.send_message(
                    f"❌ Failed to load song information: {e}",
                    ephemeral=True
                )
            except Exception:
                pass


class LikeButton(discord.ui.Button):
    """Button for liking/saving songs."""
    def __init__(
        self,
        track_id: str,
        guild_id: int,
        song_title: str,
        song_url: str,
        view_instance
    ):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Save for Autofill",
            custom_id="suno_like_btn"
        )
        self.track_id = track_id
        self.guild_id = guild_id
        self.song_title = song_title
        self.song_url = song_url
        self.view_instance = view_instance
        
        # Set emoji
        try:
            self.emoji = discord.PartialEmoji(name=LIKE_EMOJI_NAME, id=LIKE_EMOJI_ID)
        except Exception:
            self.emoji = LIKE_FALLBACK
    
    async def callback(self, interaction: discord.Interaction):
        """Handle the like button click (same logic as LikeView)."""
        if interaction.user.bot:
            return await interaction.response.defer(ephemeral=True)
        
        try:
            user_id = interaction.user.id
            has_clicked_before = user_id in self.view_instance.user_clicked
            
            # Check if user has existing likes for this track
            existing_user_likes = get_user_like_count(
                track_id=self.track_id, guild_id=self.guild_id, user_id=user_id
            )
            
            if has_clicked_before:
                # On subsequent clicks in the same view: toggle like/unlike
                if existing_user_likes > 0:
                    # User has likes, so unlike (remove one like)
                    total = unlike_track(
                        track_id=self.track_id, guild_id=self.guild_id, user_id=user_id
                    )
                    user_count = get_user_like_count(
                        track_id=self.track_id, guild_id=self.guild_id, user_id=user_id
                    )
                    if user_count == 0:
                        msg = f"Removed your like for **{self.song_title}**."
                    else:
                        msg = f"Removed a like for **{self.song_title}**. (You still have {user_count} like{'s' if user_count != 1 else ''})"
                else:
                    # User has no likes, so like it
                    total = like_track(
                        track_id=self.track_id, guild_id=self.guild_id, user_id=user_id,
                        username=str(interaction.user),
                    )
                    user_count = get_user_like_count(
                        track_id=self.track_id, guild_id=self.guild_id, user_id=user_id
                    )
                    if user_count > 1:
                        msg = f"Saved to Autofill **{self.song_title}**! (You've added this {user_count} times)"
                    else:
                        msg = f"Saved to Autofill **{self.song_title}**!"
            else:
                # First click in this view
                if existing_user_likes > 0:
                    # User already has likes, so add another like
                    total = like_track(
                        track_id=self.track_id, guild_id=self.guild_id, user_id=user_id,
                        username=str(interaction.user),
                    )
                    user_count = get_user_like_count(
                        track_id=self.track_id, guild_id=self.guild_id, user_id=user_id
                    )
                    msg = f"Saved to Autofill **{self.song_title}** again! (You've added this {user_count} times)"
                else:
                    # User has no likes, so like it
                    total = like_track(
                        track_id=self.track_id, guild_id=self.guild_id, user_id=user_id,
                        username=str(interaction.user),
                    )
                    user_count = get_user_like_count(
                        track_id=self.track_id, guild_id=self.guild_id, user_id=user_id
                    )
                    if user_count > 1:
                        msg = f"Saved to Autofill **{self.song_title}**! (You've added this {user_count} times)"
                    else:
                        msg = f"Saved to Autofill **{self.song_title}**!"
                
                # Mark that this user has clicked in this view
                self.view_instance.user_clicked.add(user_id)
            
            self.label = "Save for Autofill"
            
            await interaction.response.edit_message(view=self.view_instance)
            
            if self.song_url.startswith("http"):
                link_view = discord.ui.View()
                platform_name = _get_platform_name(self.song_url)
                link_view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, url=self.song_url, label=f"Open on {platform_name}"))
                await interaction.followup.send(msg, view=link_view, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        
        except Exception as e:
            try:
                await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)
            except Exception:
                pass


class NowPlayingView(discord.ui.View):
    """
    View for the now playing card that includes both the like button (if applicable)
    and a "View Lyrics/Prompt" button.
    """
    def __init__(
        self,
        *,
        song: dict,
        track_id: str | None = None,
        guild_id: int | None = None,
        bot_user_id: int | None = None,
        song_title: str | None = None,
        song_url: str | None = None,
        timeout: float | None = 3600,
    ):
        super().__init__(timeout=timeout)
        self.song = song.copy()
        self.track_id = track_id
        self.guild_id = guild_id
        self.bot_user_id = bot_user_id
        self.song_title = song_title or (song.get("title") or "Untitled")
        self.song_url = (song_url or "").strip()
        self.user_clicked = set()
        
        # Add like button if track_id is provided AND it's a Suno track
        # Hide for external sources (SoundCloud, Bandcamp, direct URLs) since they can't be used in autofill
        is_external_source = bool(song.get("_source"))  # yt-dlp sourced tracks have _source
        
        if track_id and guild_id and bot_user_id and not is_external_source:
            like_btn = LikeButton(
                track_id=track_id,
                guild_id=guild_id,
                song_title=self.song_title,
                song_url=self.song_url,
                view_instance=self
            )
            self.add_item(like_btn)
        
        # Add lyrics/prompt button only for Suno tracks (external sources don't have this metadata)
        if not is_external_source:
            lyrics_btn = LyricsButton(song=self.song)
            self.add_item(lyrics_btn)


class PaginatedQueueView(PaginatedView):
    """
    Paginated view for queue display with Previous/Next navigation.
    """
    
    def __init__(
        self,
        *,
        queue: deque,
        eta_list: list[int | None],
        timeout: float | None = 300.0,
    ):
        self.queue = queue
        self.eta_list = eta_list
        super().__init__(
            total_items=len(queue),
            items_per_page=12,
            timeout=timeout,
        )
    
    def _build_embed(self) -> discord.Embed:
        """Build the embed for the current page."""
        queue_list = list(self.queue)
        total_items = len(queue_list)
        
        if total_items == 0:
            return discord.Embed(
                title="📋 Queue",
                description="Queue is empty! Add songs with `!play`.",
                color=0x0099ff
            )
        
        # Calculate start and end indices for current page
        start_idx = self.current_page * self.items_per_page
        end_idx = min(start_idx + self.items_per_page, total_items)
        
        lines = []
        for i in range(start_idx, end_idx):
            song = queue_list[i]
            song_num = i + 1
            eta_sec = self.eta_list[i] if i < len(self.eta_list) else None
            
            title_link = _track_title_link(song) + _filler_badge(song)
            artist_raw = (song.get("artist") or song.get("author") or "Unknown Artist").strip()
            artist = escape_markdown(artist_raw)
            requester = (
                song.get("requester_mention")
                or (f"<@{song['requester_id']}>" if song.get("requester_id") else None)
                or song.get("requester_tag")
                or song.get("requester_name")
                or ""
            )
            
            if eta_sec is None:
                eta_str = "≈unknown"
            else:
                eta_str = _fmt_duration(max(0, int(eta_sec)))
            
            lines.append(
                f"**{song_num}.** {title_link} by {artist}\n"
                f"   Up in ~{eta_str} / Requested by {requester}"
            )
        
        description = "\n".join(lines) if lines else "No items on this page."
        
        embed = discord.Embed(
            title="📋 Current Queue",
            description=description,
            color=0x0099ff
        )
        
        # Add footer with page info
        if self.total_pages > 1:
            embed.set_footer(
                text=f"Page {self.current_page + 1} of {self.total_pages} • "
                     f"Showing items {start_idx + 1}-{end_idx} of {total_items}"
            )
        else:
            embed.set_footer(text=f"Total: {total_items} item{'s' if total_items != 1 else ''}")
        
        return embed


# ===== Music Cog =============================================================
class RadioBot(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queues = defaultdict(deque)
        self.playlists = defaultdict(lambda: defaultdict(deque))
        self.user_mappings = defaultdict(dict)
        self.volumes = defaultdict(lambda: float(os.getenv("DEFAULT_VOLUME", "1.0")))
        self.current_song = None
        self.song_start_time = None
        self.activity_task = None
        self.auto_play_enabled = {}
        self.auto_play_tasks = {}
        self.auto_playlist_urls = {}
        self._autofill_feature_on = AUTOFILL_FEATURE
        self.autofill_seed_rows = {}
        self._autofill_row_cursor = {}
        self.queue_limit_enabled = {}
        self.queue_limit_max = {}
        self.queue_per_user_max = {}
        self._fadeout_active = defaultdict(bool)

        # --- Playback overlap guard (fixes double-play jitter) -----------------
        self._play_locks = defaultdict(asyncio.Lock)

        # --- Now Playing tracking for pruning ----------------------------------
        self._song_index = defaultdict(int)
        self._np_track = defaultdict(list)
        self._np_retention_n = REMOVE_NP_AFTER_SONGS

        # --- QPanel message tracking (for cleanup) -----------------------------
        self._qpanel_messages = {}  # guild_id -> discord.Message
        
        # --- Autofill saves DM message tracking (for cleanup) -------------------
        self._autofill_dm_messages = {}  # user_id -> discord.Message

        self.np_clean_non_autofill = {} # guild_id -> bool

        # --- Autofill queue recalculation debouncing ----------------------------
        self._autofill_recalc_timers = {}  # guild_id -> asyncio.Task for debouncing

    def _is_admin(self, member: discord.Member) -> bool:
        """Admins bypass queue limitations."""
        try:
            perms = member.guild_permissions
            return bool(perms.administrator or perms.manage_guild)
        except Exception:
            return False

    async def _fade_out_and_stop(self, ctx, *, duration=None, steps=None):
        gid = ctx.guild.id
        vc = ctx.voice_client

        # no voice client at all → nothing to fade
        if not vc:
            return

        # allow fading if playing OR paused
        is_active = vc.is_playing() or vc.is_paused()
        if not is_active:
            try:
                vc.stop()
            except Exception:
                pass
            return

        # prevent double fades
        if self._fadeout_active[gid]:
            try:
                vc.stop()
            except Exception:
                pass
            return

        self._fadeout_active[gid] = True
        try:
            d = FADE_OUT_SECONDS if duration is None else float(duration)
            if d <= 0:
                vc.stop()
                return

            s = FADE_OUT_STEPS if steps is None else int(steps)
            s = max(1, s)

            # get the actual PCMVolumeTransformer
            transformer = getattr(vc, "source", None)
            if not transformer or not hasattr(transformer, "volume"):
                # fallback — cannot fade, just stop
                vc.stop()
                return

            try:
                start_vol = float(transformer.volume or 1.0)
            except Exception:
                start_vol = 1.0

            delay = d / s
            for i in range(s):
                await asyncio.sleep(delay)
                try:
                    transformer.volume = max(0.0, start_vol * (1.0 - (i + 1) / s))
                except Exception:
                    break

            try:
                transformer.volume = 0.0
            except Exception:
                pass

            vc.stop()

        except Exception as e:
            # log it to console but don't break playback system
            print(f"[fadeout] error: {e}")
            try:
                vc.stop()
            except Exception:
                pass

        finally:
            self._fadeout_active[gid] = False

    def _pick_song_from_context(self, ctx, position: int | None):
        gid = ctx.guild.id
        if position is None:
            if self.current_song:
                return self.current_song, "Now Playing"
            q = self.queues[gid]
            if q:
                return q[0], "Next Up"
            return None, "No song is playing and the queue is empty."

        try:
            idx = int(position) - 1
        except Exception:
            return None, f"Invalid position."
        q = self.queues[gid]
        if 0 <= idx < len(q):
            return list(q)[idx], f"Queued song #{idx+1}"
        return None, f"Invalid position. Must be between 1 and {len(q)}."

    def _estimate_eta_seconds(self, gid: int, position: int) -> tuple[int | None, bool]:
        eta = 0
        had_unknown = False
        had_known = False

        if self.current_song and self.song_start_time:
            cur_dur = _duration_to_seconds(self.current_song.get("duration"))
            if cur_dur is None:
                had_unknown = True
            else:
                elapsed = int(max(0, time.time() - self.song_start_time))
                eta += max(0, cur_dur - elapsed)
                had_known = True

        q = self.queues.get(gid, deque())
        ahead = list(q)[:max(0, position - 1)]
        for t in ahead:
            td = _duration_to_seconds(t.get("duration"))
            if td is None:
                had_unknown = True
            else:
                eta += td
                had_known = True

        if not had_known and had_unknown:
            return None, True
        return eta, had_unknown

    def _count_user_queued(self, gid: int, user_id: int, include_filler: bool = False) -> int:
        q = self.queues[gid]
        if not q:
            return 0
        n = 0
        for t in q:
            if (not include_filler) and t.get("_autofill"):
                continue
            if t.get("requester_id") == user_id:
                n += 1
        return n

    def _user_slots_remaining(self, gid: int, user_id: int) -> int:
        have = self._count_user_queued(gid, user_id, include_filler=False)
        return max(0, self._per_user_max(gid) - have)

    async def _check_requester_in_vc(self, ctx, song: dict) -> bool:
        """
        Check if the requester of a song is still in the voice channel.
        Returns True if requester is in VC (or if check should be skipped), False otherwise.
        
        Skips check for:
        - Autofill tracks (they have _autofill flag)
        - Tracks without a requester_id
        - If SKIP_IF_REQUESTER_LEFT is disabled
        """
        # Skip check if feature is disabled
        if not SKIP_IF_REQUESTER_LEFT:
            return True
        
        # Skip check for autofill tracks
        if song.get("_autofill"):
            return True
        
        # Skip check if no requester_id
        requester_id = song.get("requester_id")
        if not requester_id:
            return True
        
        # Skip check if no voice client or channel
        if not ctx.voice_client or not ctx.voice_client.channel:
            return True
        
        # Check if requester is in the voice channel
        vc_members = ctx.voice_client.channel.members
        requester_in_vc = any(member.id == requester_id for member in vc_members)
        
        return requester_in_vc

    def _deny_user_cap_embed(self, requester_mention: str | None = None, gid: int | None = None) -> discord.Embed:
        cap = self._per_user_max(gid) if gid is not None else QUEUE_MAX_PER_USER
        who = requester_mention or "You"
        return discord.Embed(
            title="🚫 Per-User Queue Limit",
            description=f"{who} already {'have' if requester_mention else 'has'} **{cap}** song(s) in the queue. "
                        f"Please wait until one finishes before adding more.",
            color=0xe74c3c
        )

    def _queue_eta_list(self, gid: int) -> list[int | None]:
        etas: list[int | None] = []
        base = 0
        if self.current_song and self.song_start_time:
            cur = _duration_to_seconds(self.current_song.get("duration"))
            if cur is not None:
                elapsed = int(max(0, time.time() - self.song_start_time))
                base = max(0, cur - elapsed)
            else:
                return [None for _ in range(len(self.queues.get(gid, [])))]

        acc = base
        q = list(self.queues.get(gid, []))
        for t in q:
            etas.append(acc if acc is not None else None)
            d = _duration_to_seconds(t.get("duration"))
            if d is None:
                acc = None
            else:
                if acc is not None:
                    acc += d
        return etas

    # ===== AUTOFILL (Idle Radio) ============================================
    def _is_autofill_enabled(self, gid: int) -> bool:
        return (
            self._autofill_feature_on
            and bool(self.auto_play_enabled.get(gid))
            and (
                bool(self.auto_playlist_urls.get(gid)) or
                bool(self.autofill_seed_rows.get(gid))
            )
        )

    def _cancel_autofill_task(self, gid: int):
        task = self.auto_play_tasks.get(gid)
        if task and not task.done():
            task.cancel()
        self.auto_play_tasks[gid] = None

    def _clear_autofill_from_queue(self, gid: int):
        dq = self.queues[gid]
        if not dq:
            return
        kept = [t for t in dq if not t.get("_autofill")]
        dq.clear()
        dq.extend(kept)

    def _load_autofill_csv(self, path: str) -> list[dict]:
        rows = []
        if not path or not os.path.exists(path):
            return rows
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                sniffer = csv.Sniffer()
                sample = f.read(2048)
                f.seek(0)
                has_header = False
                try:
                    has_header = sniffer.has_header(sample)
                except Exception:
                    pass

                reader = csv.reader(f)
                for r in reader:
                    if not r or all(not (c or "").strip() for c in r):
                        continue
                    if has_header and reader.line_num == 1:
                        headers = [h.strip().lower() for h in r]
                        try:
                            url_idx = headers.index("url")
                        except ValueError:
                            url_idx = 0
                        requested_by_idx = None
                        for cand in ("requested by", "requested_by", "requestedby", "by"):
                            if cand in headers:
                                requested_by_idx = headers.index(cand)
                                break
                        continue

                    url = (r[0] if len(r) >= 1 else "").strip()
                    rb = (r[1] if len(r) >= 2 else "").strip()
                    if url:
                        rows.append({"url": url, "requested_by": rb})
        except Exception as e:
            print(f"[autofill CSV] Failed to load {path}: {e}")
        return rows

    async def _get_autofill_liked_raw(self, ctx, gid: int) -> list[dict]:
        try:
            vc = ctx.voice_client
        except AttributeError:
            vc = None

        if not vc or not getattr(vc, "channel", None):
            return []

        members = [m for m in vc.channel.members if not getattr(m, "bot", False)]
        user_ids = [m.id for m in members]
        if not user_ids:
            return []

        try:
            # Get all liked songs with aggregate like counts (high limit for weighted selection)
            all_songs = top_liked_for_users(
                guild_id=gid,
                user_ids=user_ids,
                limit=1000,  # High limit to get all available songs for weighted selection
            )
        except Exception as e:
            print(f"[autofill likes] failed to fetch liked tracks: {e}")
            return []

        if not all_songs:
            return []

        # Extract weights (like counts) - minimum weight of 1 ensures all songs are selectable
        weights = [max(1, song.get("like_count", 1)) for song in all_songs]

        # Weighted random selection - select up to AUTOFILL_MAX_PULL
        selected_count = min(AUTOFILL_MAX_PULL, len(all_songs))
        selected = random.choices(all_songs, weights=weights, k=selected_count)

        # Convert to expected format and deduplicate by URL
        seen_urls = set()
        raw: list[dict] = []
        for song in selected:
            url = (song.get("source_url") or "").strip()
            if not url:
                continue
            if url in seen_urls:
                continue  # Skip duplicates
            seen_urls.add(url)
            raw.append(
                {
                    "id": song.get("track_id"),
                    "url": url,
                    "suno_url": url,
                    "_liked_weight": song.get("like_count", 0),
                }
            )

        return raw

    def _apply_recently_played_guard(self, gid: int, tracks: list[dict]) -> list[dict]:
        """
        Reorder tracks so recently played songs (last 10) are moved to the end.
        The recently played portion is shuffled before appending.
        """
        try:
            recent = recent_plays(guild_id=gid, limit=10, include_autofill=True)
            recent_track_ids = {str(r["track_id"]) for r in recent if r.get("track_id")}
        except Exception:
            return tracks  # If query fails, return unchanged
        
        if not recent_track_ids:
            return tracks
        
        not_recent = []
        recently_played = []
        
        for t in tracks:
            track_id = _canonical_track_id(t)
            if track_id and track_id in recent_track_ids:
                recently_played.append(t)
            else:
                not_recent.append(t)
        
        # Shuffle the recently played list before appending
        random.shuffle(recently_played)
        
        return not_recent + recently_played

    async def _enqueue_autofill_batch(self, ctx, gid: int):
        liked_raw = await self._get_autofill_liked_raw(ctx, gid)
        # Shuffle the liked songs separately (after weighted selection)
        random.shuffle(liked_raw)
        liked_raw = liked_raw[:AUTOFILL_MAX_PULL]
        remaining = max(0, AUTOFILL_MAX_PULL - len(liked_raw))

        url = (self.auto_playlist_urls.get(gid) or "").strip()
        fallback_raw: list[dict] = []

        if remaining > 0:
            if url:
                raw_from_url = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: _scrape_playlist_to_tracks(url, limit=AUTOFILL_MAX_PULL)
                )
                if raw_from_url:
                    # Shuffle CSV/fallback separately
                    random.shuffle(raw_from_url)
                    fallback_raw = raw_from_url[:remaining]
            else:
                seed = self.autofill_seed_rows.get(gid) or []
                if seed:
                    pick = seed[:]
                    # Shuffle CSV/fallback separately
                    random.shuffle(pick)
                    pick = pick[:remaining]
                    fallback_raw = [
                        {"url": r["url"], "requested_by_note": r.get("requested_by", "")}
                        for r in pick
                    ]

        # Combine: user songs first, then CSV (preserve order after separate shuffles)
        # Deduplicate by URL to avoid duplicates between liked_raw and fallback_raw
        seen_urls = set()
        combined_raw = []
        
        # Add liked_raw items first (preserve order, skip duplicates)
        for it in liked_raw:
            url = str(it.get("url") or it.get("suno_url") or "").strip()
            if url and url not in seen_urls:
                seen_urls.add(url)
                combined_raw.append(it)
        
        # Add fallback_raw items (skip duplicates)
        for it in fallback_raw:
            url = str(it.get("url") or it.get("suno_url") or "").strip()
            if not url:
                # If no URL, skip it (will be filtered later anyway)
                continue
            if url in seen_urls:
                continue  # Skip duplicates
            seen_urls.add(url)
            combined_raw.append(it)
        
        if not combined_raw:
            return 0

        cleaned_raw = []
        for it in combined_raw:
            u = str(it.get("url") or it.get("suno_url") or "").strip()
            if not u:
                continue
            if not (u.startswith("http://") or u.startswith("https://") or u.startswith("songs/")):
                continue
            it["url"] = u
            cleaned_raw.append(it)

        if not cleaned_raw:
            return 0

        tracks = await self._resolve_tracks(cleaned_raw, max_workers=6)
        # Don't shuffle here - we've already shuffled separately and want to preserve order (user songs first, then CSV)

        # Filter out tracks with BOTH "Unknown Title" and "Unknown" artist for autofill queue
        valid_tracks = []
        for t in tracks:
            title = (t.get("title") or "").strip().lower()
            artist = (t.get("artist") or t.get("author") or "").strip().lower()
            
            # Check if track has BOTH unknown title and artist
            is_unknown_title = title in ("unknown title", "untitled", "")
            is_unknown_artist = artist in ("unknown", "unknown artist", "", "none")
            
            if is_unknown_title and is_unknown_artist:
                continue  # Skip this track for autofill, but keep in DB
            
            valid_tracks.append(t)

        # Apply recently played guard - move recently played songs to the end
        valid_tracks = self._apply_recently_played_guard(gid, valid_tracks)

        now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        for t in valid_tracks:
            t["_autofill"] = True
            t.setdefault("tags", []).append("filler")

            t["requester_id"] = self.bot.user.id if self.bot.user else None
            t["requester_tag"] = "Autofill"
            t["requester_name"] = "Autofill"
            t["requester_mention"] = None
            t["requested_at"] = now_ts

            self.queues[gid].append(t)

        save_data(gid, self.queues, self.playlists, self.user_mappings)
        return len(valid_tracks)

    async def _autofill_after_delay(self, ctx, gid: int, delay: int):
        try:
            await asyncio.sleep(max(0, delay))
            if self.queues[gid] or self.current_song:
                return
            if not self._is_autofill_enabled(gid):
                return
            
            # Check if voice_client exists before trying to enqueue
            if not ctx.voice_client:
                return

            added = await self._enqueue_autofill_batch(ctx, gid)
            if added > 0 and ctx.voice_client:
                await self.play_next(ctx)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[autofill] failed: {e}")
        finally:
            self.auto_play_tasks[gid] = None

    def _schedule_autofill_if_idle(self, ctx, delay: int | None = None):
        gid = ctx.guild.id
        if self.auto_play_tasks.get(gid):
            return
        if not self._is_autofill_enabled(gid):
            return
        use_delay = AUTOFILL_DELAY_SEC if (delay is None) else max(0, int(delay))
        self.auto_play_tasks[gid] = self.bot.loop.create_task(
            self._autofill_after_delay(ctx, gid, use_delay)
        )

    async def _recalculate_autofill_queue(self, guild: discord.Guild):
        """
        Completely rebuild the autofill queue based on current listeners in VC.
        
        When users join or leave while autofill is playing:
        1. Keep manual songs as-is
        2. Completely regenerate the autofill portion using the new user list
        3. Use weighted selection based on combined likes from current VC users
        4. Shuffle and deduplicate the new autofill list
        5. Replace the autofill portion of the queue
        """
        gid = guild.id
        
        # Check if autofill is enabled
        if not self._is_autofill_enabled(gid):
            return
        
        # Check if there are any autofill songs in queue
        queue = self.queues.get(gid, deque())
        has_autofill = any(song.get("_autofill") for song in queue)
        if not has_autofill:
            return  # Only manual songs, no need to recalculate
        
        # Check if bot is in VC
        try:
            vc = guild.voice_client
            if not vc or not getattr(vc, "channel", None):
                return
        except Exception:
            return
        
        # Split queue into manual and autofill songs (keep manual songs unchanged)
        manual_songs = [s for s in queue if not s.get("_autofill")]
        
        # Create a mock context for methods that need it
        class MockContext:
            def __init__(self, guild, voice_client):
                self.guild = guild
                self.voice_client = voice_client
        
        ctx = MockContext(guild, vc)
        
        # --- Completely regenerate autofill from scratch ---
        
        # Step 1: Get weighted liked songs for current VC users
        liked_raw = await self._get_autofill_liked_raw(ctx, gid)
        random.shuffle(liked_raw)
        liked_raw = liked_raw[:AUTOFILL_MAX_PULL]
        remaining = max(0, AUTOFILL_MAX_PULL - len(liked_raw))
        
        # Step 2: Fill remaining slots with CSV/URL fallback
        fallback_raw: list[dict] = []
        if remaining > 0:
            url = (self.auto_playlist_urls.get(gid) or "").strip()
            if url:
                raw_from_url = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: _scrape_playlist_to_tracks(url, limit=AUTOFILL_MAX_PULL)
                )
                if raw_from_url:
                    random.shuffle(raw_from_url)
                    fallback_raw = raw_from_url[:remaining]
            else:
                seed = self.autofill_seed_rows.get(gid) or []
                if seed:
                    pick = seed[:]
                    random.shuffle(pick)
                    pick = pick[:remaining]
                    fallback_raw = [
                        {"url": r["url"], "requested_by_note": r.get("requested_by", "")}
                        for r in pick
                    ]
        
        # Step 3: Combine and deduplicate by URL
        seen_urls = set()
        combined_raw = []
        
        for it in liked_raw:
            url = str(it.get("url") or it.get("suno_url") or "").strip()
            if url and url not in seen_urls:
                seen_urls.add(url)
                combined_raw.append(it)
        
        for it in fallback_raw:
            url = str(it.get("url") or it.get("suno_url") or "").strip()
            if url and url not in seen_urls:
                seen_urls.add(url)
                combined_raw.append(it)
        
        if not combined_raw:
            # No autofill songs available - just keep manual songs
            queue.clear()
            queue.extend(manual_songs)
            save_data(gid, self.queues, self.playlists, self.user_mappings)
            return
        
        # Step 4: Clean URLs
        cleaned_raw = []
        for it in combined_raw:
            u = str(it.get("url") or it.get("suno_url") or "").strip()
            if not u:
                continue
            if not (u.startswith("http://") or u.startswith("https://") or u.startswith("songs/")):
                continue
            it["url"] = u
            cleaned_raw.append(it)
        
        if not cleaned_raw:
            queue.clear()
            queue.extend(manual_songs)
            save_data(gid, self.queues, self.playlists, self.user_mappings)
            return
        
        # Step 5: Resolve tracks
        tracks = await self._resolve_tracks(cleaned_raw, max_workers=6)
        
        # Step 6: Filter out tracks with BOTH "Unknown Title" and "Unknown" artist
        valid_tracks = []
        for t in tracks:
            title = (t.get("title") or "").strip().lower()
            artist = (t.get("artist") or t.get("author") or "").strip().lower()
            
            is_unknown_title = title in ("unknown title", "untitled", "")
            is_unknown_artist = artist in ("unknown", "unknown artist", "", "none")
            
            if is_unknown_title and is_unknown_artist:
                continue
            
            valid_tracks.append(t)
        
        # Step 7: Mark as autofill and set metadata
        now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        for t in valid_tracks:
            t["_autofill"] = True
            t.setdefault("tags", []).append("filler")
            t["requester_id"] = self.bot.user.id if self.bot.user else None
            t["requester_tag"] = "Autofill"
            t["requester_name"] = "Autofill"
            t["requester_mention"] = None
            t["requested_at"] = now_ts
        
        # Step 8: Final shuffle
        random.shuffle(valid_tracks)
        
        # Step 9: Final deduplication check by track_id and URL (safety net)
        final_autofill = []
        seen_track_ids = set()
        seen_final_urls = set()
        
        for t in valid_tracks:
            track_id = _canonical_track_id(t)
            track_url = str(t.get("url") or t.get("suno_url") or "").strip()
            
            # Skip if we've seen this track_id before
            if track_id and track_id in seen_track_ids:
                continue
            
            # Skip if we've seen this URL before
            if track_url and track_url in seen_final_urls:
                continue
            
            # Add to seen sets
            if track_id:
                seen_track_ids.add(track_id)
            if track_url:
                seen_final_urls.add(track_url)
            
            final_autofill.append(t)
        
        # Apply recently played guard - move recently played songs to the end
        final_autofill = self._apply_recently_played_guard(gid, final_autofill)
        
        # Step 10: Reconstruct queue - manual songs first, then new autofill songs
        queue.clear()
        queue.extend(manual_songs)
        queue.extend(final_autofill)
        
        # Save queue state
        save_data(gid, self.queues, self.playlists, self.user_mappings)

    # ===== Queue add limit helpers ==========================================
    def _limit_is_on(self, gid: int) -> bool:
        return bool(self.queue_limit_enabled.get(gid, QUEUE_LIMIT_DEFAULT_ENABLED))

    def _limit_max(self, gid: int) -> int:
        return int(self.queue_limit_max.get(gid, QUEUE_LIMIT_MAX_PER_ADD))

    def _per_user_max(self, gid: int) -> int:
        return int(self.queue_per_user_max.get(gid, QUEUE_MAX_PER_USER))

    def _enforce_queue_add_limit(self, gid: int, intended_count: int, *, bypass: bool = False) -> tuple[int, str | None]:
        if bypass or (not self._limit_is_on(gid)):
            return intended_count, None
        cap = self._limit_max(gid)
        if intended_count <= cap:
            return intended_count, None
        if cap == 3:
            msg = "You can only enter 3 songs at a time into the queue."
        else:
            msg = f"You can only enter up to **{cap}** songs at a time into the queue."
        return cap, msg

    # ========================================================================

    def get_radio_channel(self, ctx):
        RADIO_CONTROL_CHANNEL = os.getenv("RADIO_CONTROL_CHANNEL")
        if RADIO_CONTROL_CHANNEL:
            try:
                radio_channel = ctx.guild.get_channel(int(RADIO_CONTROL_CHANNEL))
                return radio_channel if radio_channel else ctx.channel
            except:
                pass
        return ctx.channel

    def format_time(self, seconds):
        mins, secs = divmod(int(seconds), 60)
        return f"{mins:02d}:{secs:02d}"

    async def _resolve_tracks(self, items: list[dict], max_workers: int = 6) -> list[dict]:
        loop = asyncio.get_event_loop()

        def _resolve_one(item: dict) -> dict:
            try:
                info = extract_song_info(item.get("url") or item.get("suno_url") or "")
                if info:
                    item.update(info)
            except Exception as e:
                print(f"[resolver] failed on {item.get('url')}: {e}")
            item.setdefault("title", "Unknown Title")
            item.setdefault("artist", "Unknown")
            item.setdefault("duration", None)
            item.setdefault("thumbnail", None)
            return item

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [loop.run_in_executor(ex, _resolve_one, it) for it in items]
            return await asyncio.gather(*futures)

    async def set_song_activity(self, song, elapsed_seconds):
        try:
            title = song.get('title', 'Unknown Song')
            duration = song.get('duration', 0) or 0
            current_time = self.format_time(elapsed_seconds)
            total_time = self.format_time(duration)

            activity_name = f"🎶 {title} - {current_time} / {total_time}"
            activity = discord.Activity(
                type=discord.ActivityType.listening,
                name=activity_name[:128]
            )
            await self.bot.change_presence(activity=activity)
        except Exception as e:
            print(f"Error setting song activity: {e}")

    async def _fade_in_volume(self, transformer, target, duration, steps):
        try:
            if duration <= 0 or transformer is None:
                if transformer is not None:
                    transformer.volume = target
                return
            initial = 0.0001
            transformer.volume = initial
            steps = max(1, int(steps))
            delay = max(0.001, float(duration) / steps)
            delta = (target - initial) / steps
            for i in range(steps):
                await asyncio.sleep(delay)
                transformer.volume = max(0.0, initial + delta * (i + 1))
        except Exception:
            try:
                transformer.volume = target
            except Exception:
                pass

    @tasks.loop(seconds=30)
    async def update_song_activity(self):
        if self.current_song and self.song_start_time:
            elapsed = time.time() - self.song_start_time
            await self.set_song_activity(self.current_song, elapsed)

    async def cog_load(self):
        for guild in self.bot.guilds:
            loaded_queues, loaded_playlists, loaded_user_mappings = load_data(guild.id)
            if guild.id in loaded_queues:
                self.queues[guild.id] = loaded_queues[guild.id]
            if guild.id in loaded_playlists:
                self.playlists[guild.id] = loaded_playlists[guild.id]
            if guild.id in loaded_user_mappings:
                self.user_mappings[guild.id] = loaded_user_mappings[guild.id]

            gid = guild.id
            amap = self.user_mappings[gid]
            ainfo = amap.get("autofill") if isinstance(amap, dict) else None

            enabled_default = True

            if isinstance(ainfo, dict):
                url = (ainfo.get("url") or "").strip()
                enabled = bool(ainfo.get("enabled", enabled_default))
                csv_path = (ainfo.get("csv") or "").strip() if isinstance(ainfo, dict) else ""

                if url:
                    self.auto_playlist_urls[gid] = url
                self.auto_play_enabled[gid] = enabled

                if not url and csv_path:
                    self.autofill_seed_rows[gid] = self._load_autofill_csv(csv_path)
            else:
                if not isinstance(amap, dict):
                    amap = {}
                    self.user_mappings[gid] = amap
                self.auto_play_enabled[gid] = enabled_default

            if not self.auto_playlist_urls.get(gid):
                if DEFAULT_AUTOFILL_URL:
                    self.auto_playlist_urls[gid] = DEFAULT_AUTOFILL_URL
                    amap = self.user_mappings[gid]
                    amap["autofill"] = {
                        "url": DEFAULT_AUTOFILL_URL,
                        "enabled": self.auto_play_enabled.get(gid, enabled_default),
                    }
                    save_data(gid, self.queues, self.playlists, self.user_mappings)
                elif DEFAULT_AUTOFILL_CSV:
                    rows = self._load_autofill_csv(DEFAULT_AUTOFILL_CSV)
                    if rows:
                        self.autofill_seed_rows[gid] = rows
                        amap = self.user_mappings[gid]
                        amap["autofill"] = {
                            "csv": DEFAULT_AUTOFILL_CSV,
                            "enabled": self.auto_play_enabled.get(gid, enabled_default),
                        }
                        save_data(gid, self.queues, self.playlists, self.user_mappings)

    async def cog_unload(self):
        if self.update_song_activity.is_running():
            self.update_song_activity.cancel()
        try:
            await self.bot.change_presence(activity=None)
        except Exception:
            pass
        # Clean up all prefetched files on shutdown
        self._cleanup_all_prefetched_files()

    @commands.hybrid_command(name='join', description='Join a voice channel')
    @app_commands.describe(channel='Voice channel to join (optional, defaults to your current)')
    async def join(self, ctx, channel: discord.VoiceChannel = None):
        """
        Join a voice channel
        """
        if not channel:
            if ctx.author.voice:
                channel = ctx.author.voice.channel
            else:
                guild = ctx.guild
                voice_channels = [c for c in guild.channels if isinstance(c, discord.VoiceChannel)]
                if not voice_channels:
                    embed = discord.Embed(title="❌ Error", description="No voice channels available!", color=0xff0000)
                    await ctx.send(embed=embed)
                    return
                channel = None
                for vc in voice_channels:
                    if ctx.guild.me.permissions_in(vc).connect:
                        channel = vc
                        break
                if not channel:
                    embed = discord.Embed(title="❌ Error", description="No voice channels I have permission to join!", color=0xff0000)
                    await ctx.send(embed=embed)
                    return

        try:
            if ctx.voice_client:
                await ctx.voice_client.move_to(channel)
            else:
                await channel.connect()

            # ✅ bump Opus bitrate a bit for cleaner highs
            try:
                vc = ctx.voice_client
                if vc and getattr(vc, "encoder", None):
                    vc.encoder.bitrate = VOICE_BITRATE_KBPS * 1000  # discord expects bps
            except Exception as e:
                print(f"[bitrate] couldn't set encoder bitrate: {e}")

            embed = discord.Embed(title="✅ Joined", description=f"Joined {channel.name} 🎧", color=0x00ff00)
            await ctx.send(embed=embed)

            gid = ctx.guild.id
            if not self.queues[gid] and not (ctx.voice_client and ctx.voice_client.is_playing()):
                self._cancel_autofill_task(gid)
                self._schedule_autofill_if_idle(ctx, delay=AUTOFILL_DELAY_SEC)
        except Exception as e:
            embed = discord.Embed(title="❌ Voice Connection Error", description=f"Failed to join {channel.name}: {str(e)}.", color=0xff0000)
            await ctx.send(embed=embed)

    @commands.hybrid_command(name='leave', description='Leave the current voice channel')
    async def leave(self, ctx):
        """
        Leave the current voice channel
        """
        if not ctx.voice_client:
            embed = discord.Embed(title="❌ Error", description="I'm not connected to a voice channel!", color=0xff0000)
            await ctx.send(embed=embed)
            return
        channel_name = ctx.voice_client.channel.name
        await ctx.voice_client.disconnect()

        gid = ctx.guild.id
        self._cancel_autofill_task(gid)
        self._clear_autofill_from_queue(gid)

        self.current_song = None
        self.song_start_time = None
        if self.update_song_activity.is_running():
            self.update_song_activity.stop()
        await self.bot.change_presence(activity=None)
        # Clean up all prefetched files when leaving
        self._cleanup_all_prefetched_files()
        embed = discord.Embed(title="👋 Left", description=f"Left {channel_name} 🎧", color=0xff0000)
        await ctx.send(embed=embed)

    @commands.command(name='play', aliases=['Play'])
    @app_commands.describe(channel='Play a song using !play [url]')
    async def play(self, ctx, url: str = ""):
        """
        Plays a song by url (Suno song or playlist URL supported).
        """
        if not ctx.voice_client:
            await ctx.invoke(self.join)

        guild_id = ctx.guild.id
        queue = self.queues[guild_id]

        self._cancel_autofill_task(guild_id)
        self._clear_autofill_from_queue(guild_id)

        requester_id = ctx.author.id
        requester_tag = str(ctx.author)
        requester_name = ctx.author.display_name
        requester_mention = ctx.author.mention
        requested_at = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        remaining_user_slots = self._user_slots_remaining(guild_id, requester_id)
        is_admin = self._is_admin(ctx.author)
        if is_admin:
            remaining_user_slots = 10**9  # effectively unlimited

        try:
            if not url.strip():
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    raw_tracks = await asyncio.get_event_loop().run_in_executor(
                        executor, _scrape_playlist_to_tracks, "", 5
                    )
                if not raw_tracks:
                    embed = discord.Embed(title="❌ Missing URL", description="Please provide a Suno song or playlist URL.\n\nUsage: `!play <url>`", color=0xff0000)
                    await ctx.send(embed=embed)
                    return

                intended = len(raw_tracks)
                allowed_by_add, notice = self._enforce_queue_add_limit(
                    guild_id, intended, bypass=is_admin
                )

                if remaining_user_slots <= 0:
                    await ctx.send(embed=self._deny_user_cap_embed(requester_mention, gid=guild_id))
                    return

                allowed_total = min(allowed_by_add, remaining_user_slots)
                if allowed_total <= 0:
                    await ctx.send(embed=self._deny_user_cap_embed(requester_mention, gid=guild_id))
                    return
                if allowed_total < intended:
                    raw_tracks = raw_tracks[:allowed_total]

                tracks = await self._resolve_tracks(raw_tracks, max_workers=4)

                for song in tracks:
                    song["requester_id"] = requester_id
                    song["requester_tag"] = requester_tag
                    song["requester_name"] = requester_name
                    song["requester_mention"] = requester_mention
                    song["requested_at"] = requested_at
                    self.queues[guild_id].append(song)

                save_data(guild_id, self.queues, self.playlists, self.user_mappings)

                desc = f"Added {len(tracks)} songs"
                if notice:
                    desc += f"\n\n{notice}"
                embed = discord.Embed(
                    title="➕ Added",
                    description=desc,
                    color=EMBED_COLOR_ADDED
                )
                embed.add_field(
                    name="Requested by",
                    value=f"{requester_mention} at <t:{requested_at}:t>",
                    inline=True
                )
                await ctx.send(embed=embed)
            else:
                song = extract_song_info(url)
                if song is None:
                    raise ValueError("Failed to extract song information: extract_song_info returned None")
                
                # Guard against SoundCloud PRO previews (30 seconds or shorter)
                if "soundcloud.com" in url.lower():
                    duration = song.get("duration")
                    if duration is not None and duration <= 30:
                        raise ValueError(
                            "This appears to be a SoundCloud PRO preview (30 seconds or shorter). "
                            "The full track is only available to SoundCloud Go+ subscribers"
                        )
                
                song.setdefault("artist", song.pop("author", None))

                song["requester_id"] = requester_id
                song["requester_tag"] = requester_tag
                song["requester_name"] = requester_name
                song["requester_mention"] = requester_mention
                song["requested_at"] = requested_at

                if remaining_user_slots <= 0:
                    await ctx.send(embed=self._deny_user_cap_embed(requester_mention))
                    return

                queue.append(song)
                position = len(queue)
                save_data(guild_id, self.queues, self.playlists, self.user_mappings)

                eta_sec, eta_unknown = self._estimate_eta_seconds(guild_id, position)
                embed, thumb_file = build_added_embed(
                    song,
                    requester_mention=requester_mention,
                    position=position,
                    eta_seconds=eta_sec,
                    eta_unknown=eta_unknown
                )
                await ctx.send(embed=embed, file=thumb_file) if thumb_file else await ctx.send(embed=embed)

            if not ctx.voice_client.is_playing():
                await self.play_next(ctx)

        except Exception as e:
            embed = discord.Embed(
                title="❌ Error",
                description=f"Failed to add song: {str(e)}.",
                color=0xff0000
            )
            await ctx.send(embed=embed)

    def _prune_non_autofill(self, gid: int) -> bool:
        """Check if non-autofill NP cards should be pruned for this guild."""
        if gid in self.np_clean_non_autofill:
            return self.np_clean_non_autofill[gid]
        
        # Fallback to user_mappings if loaded
        amap = self.user_mappings.get(gid) or {}
        if isinstance(amap, dict) and "np_clean_non_autofill" in amap:
            self.np_clean_non_autofill[gid] = bool(amap["np_clean_non_autofill"])
            return self.np_clean_non_autofill[gid]
        
        return REMOVE_NON_AUTOFILL_NP

    async def _cleanup_now_playing_messages(self, gid: int):
        """
        Delete Now Playing messages that are older than the last N songs.
        Autofill messages are always cleaned up; non-autofill messages
        are only cleaned up if REMOVE_NON_AUTOFILL_NP is True or toggled ON.
        """
        if self._np_retention_n <= 0:
            return
        entries = self._np_track.get(gid) or []
        if not entries:
            return

        current_idx = self._song_index.get(gid, 0)
        keep = []
        
        prune_non_autofill = self._prune_non_autofill(gid)

        for e in entries:
            is_autofill = e.get("is_autofill", False)
            # Delete if the gap is >= retention_n AND (it's autofill OR toggle is ON)
            if (current_idx - e.get("song_index", current_idx)) >= self._np_retention_n:
                if is_autofill or prune_non_autofill:
                    try:
                        ch = self.bot.get_channel(e["channel_id"])
                        if ch:
                            # Fetch the message to check reactions
                            msg = await ch.fetch_message(e["message_id"])
                            # Count total reactions (sum of all reaction counts)
                            reaction_count = sum(reaction.count for reaction in msg.reactions)
                            # Skip deletion if message has more than 2 reactions
                            if reaction_count > 2:
                                keep.append(e)
                                continue
                            await msg.delete()
                    except Exception:
                        pass
                # Once it's outside the retention window, we stop tracking it regardless
                continue
            
            keep.append(e)
        self._np_track[gid] = keep

    def _cleanup_prefetched_file(self, local_path: str | None) -> None:
        """
        Safely remove a prefetched file if it exists.
        Used when playback fails or is skipped before starting.
        """
        if not local_path:
            return
        try:
            if os.path.exists(local_path):
                os.remove(local_path)
        except Exception as e:
            print(f"Prefetch cleanup failed: {local_path}: {e}")

    def _cleanup_all_prefetched_files(self) -> None:
        """
        Clean up all files in the prefetch directory.
        Used when the bot stops/resets/leaves to prevent accumulation.
        """
        if not PREFETCH_DIR or not os.path.exists(PREFETCH_DIR):
            return
        try:
            for filename in os.listdir(PREFETCH_DIR):
                file_path = os.path.join(PREFETCH_DIR, filename)
                try:
                    if os.path.isfile(file_path):
                        os.remove(file_path)
                except Exception as e:
                    print(f"Failed to remove prefetched file {file_path}: {e}")
        except Exception as e:
            print(f"Failed to clean prefetch directory {PREFETCH_DIR}: {e}")

    async def play_next(self, ctx):
        gid = ctx.guild.id
        lock = self._play_locks[gid]

        async with lock:
            queue = self.queues[gid]
            if not queue:
                return
            if not ctx.voice_client:
                embed = discord.Embed(title="❌ Connection Lost", description="Bot lost voice connection!", color=0xff0000)
                channel = self.get_radio_channel(ctx)
                await channel.send(embed=embed)
                self.current_song = None
                self.song_start_time = None
                if self.update_song_activity.is_running():
                    self.update_song_activity.stop()
                await self.bot.change_presence(activity=None)
                return

            channel = self.get_radio_channel(ctx)
            song = queue.popleft()

            # Check if requester is still in VC before playing
            requester_in_vc = await self._check_requester_in_vc(ctx, song)
            if not requester_in_vc:
                # Requester left, skip this song
                requester_mention = (
                    song.get("requester_mention")
                    or (f"<@{song['requester_id']}>" if song.get("requester_id") else None)
                    or song.get("requester_tag")
                    or song.get("requester_name")
                    or "someone"
                )
                song_title = song.get("title") or "Unknown"
                
                if SHOW_SKIP_MESSAGE:
                    skip_embed = discord.Embed(
                        title="⏭️ Skipped",
                        description=f"Skipped **{_truncate(song_title, 200)}** - {requester_mention} is no longer in the voice channel.",
                        color=0xff9900
                    )
                    try:
                        await channel.send(embed=skip_embed)
                    except Exception:
                        pass
                
                # If there are other songs in queue, continue to next song
                if queue and ctx.voice_client:
                    self.bot.loop.create_task(self.play_next(ctx))
                else:
                    # No other songs follow - trigger queue empty behavior to resume autofill
                    self.current_song = None
                    self.song_start_time = None
                    if self.update_song_activity.is_running():
                        self.update_song_activity.stop()
                    await self.bot.change_presence(activity=None)
                    
                    embed2 = discord.Embed(title="⏹️ Queue Empty", description="Finished playing! 🎉", color=0x00ff00)
                    try:
                        await channel.send(embed=embed2)
                    except Exception:
                        pass
                    try:
                        self._schedule_autofill_if_idle(ctx)
                    except Exception:
                        pass
                return

            track_id = _canonical_track_id(song)
            if track_id:
                try:
                    title = song.get("title")
                    artist = song.get("artist") or song.get("author")
                    
                    # Check if track has BOTH unknown title and artist - skip adding to DB
                    title_lower = (title or "").strip().lower()
                    artist_lower = (artist or "").strip().lower()
                    
                    is_unknown_title = title_lower in ("unknown title", "untitled", "")
                    is_unknown_artist = artist_lower in ("unknown", "unknown artist", "", "none")
                    
                    if is_unknown_title and is_unknown_artist:
                        # Don't add to database/log if both are unknown, but don't delete existing
                        song["_track_id"] = None
                        song["_play_id"] = None
                    else:
                        upsert_track_basic(
                            track_id=track_id,
                            title=title,
                            artist=artist,
                            cover_url=song.get("thumbnail") or song.get("thumb") or song.get("image"),
                            source_url=_derive_suno_url(song),
                            duration_sec=_duration_to_seconds(song.get("duration")),
                        )
                        play_id = log_play_start(
                            track_id=track_id,
                            guild_id=ctx.guild.id,
                            channel_id=ctx.channel.id,
                            requested_by=str(song.get("requester_id") or getattr(ctx.author, "id", "")),
                            context="autofill" if song.get("_autofill") else "queue",
                        )
                        song["_track_id"] = track_id
                        song["_play_id"] = play_id
                except Exception as e:
                    print(f"[history] start log failed: {e}")
            else:
                song["_track_id"] = None
                song["_play_id"] = None

            local_to_delete = None
            url_val = str(song.get("url", "")).strip()
            original_url = url_val  # Save original URL in case prefetch file is corrupted
            
            # For yt-dlp sources (SoundCloud, Bandcamp, etc.), re-extract fresh URL at playback time
            # because these platforms use signed URLs that expire after ~15-30 minutes
            if song.get("_source") and song.get("_original_url"):
                try:
                    from src.utils.ytdlp_extractor import extract_with_ytdlp
                    fresh_info = extract_with_ytdlp(song["_original_url"], download_thumbnail=False)
                    if fresh_info and fresh_info.get("url"):
                        url_val = fresh_info["url"]
                        song["url"] = url_val  # Update song dict with fresh URL
                        # Also update _downloaded_file if a new one was created
                        if fresh_info.get("_downloaded_file"):
                            local_to_delete = fresh_info["_downloaded_file"]
                        print(f"[play_next] Refreshed URL for {song.get('title', 'Unknown')[:50]}")
                    else:
                        print(f"[play_next] Failed to refresh URL for {song.get('_original_url')}, using cached URL")
                except Exception as e:
                    print(f"[play_next] URL refresh failed for {song.get('_original_url')}: {e}")
                    # Continue with the cached URL - it may still work if not expired
            
            try:
                lp = await maybe_prefetch(song)
                if lp and PREFETCH_MODE == "full":
                    # Validate prefetched file before using it
                    if os.path.exists(lp) and os.path.getsize(lp) > 0:
                        local_to_delete = lp
                        url_val = str(lp)  # Use prefetched file
                    else:
                        print(f"Prefetched file is corrupted or empty: {lp}, falling back to original URL")
                        self._cleanup_prefetched_file(lp)
                        url_val = original_url  # Fall back to original URL
            except Exception as e:
                print(f"Prefetch failed for {song.get('url')}: {e}")
                url_val = original_url  # Fall back to original URL on any error

            # ---- FFmpeg options (latency + stability tuned) ------------------
            is_http = url_val.startswith(("http://", "https://"))
            is_hls = ".m3u8" in url_val or "/hls" in url_val.lower()
            is_ytdlp_source = bool(song.get("_source"))  # SoundCloud, Bandcamp, etc.

            # For local prefetched files - simpler, more reliable options
            # No async resampling needed since the file is complete and consistent
            if not is_http:
                local_af = f"adelay={STARTUP_ADELAY_MS}|{STARTUP_ADELAY_MS}"
                base_opts = (
                    f"-vn "
                    f"-probesize {FFMPEG_PROBESIZE} "
                    f"-analyzeduration {FFMPEG_ANALYZEDURATION} "
                    f"-af {local_af}"
                )
                ffmpeg_options = {
                    "before_options": "-nostdin",
                    "options": base_opts,
                }
            elif is_ytdlp_source and not is_hls:
                # For yt-dlp HTTP streams (SoundCloud direct, Bandcamp, etc.)
                # These are generally stable - no async resampling needed
                local_af = f"adelay={STARTUP_ADELAY_MS}|{STARTUP_ADELAY_MS}"
                base_opts = (
                    f"-vn "
                    f"-probesize {FFMPEG_PROBESIZE} "
                    f"-analyzeduration {FFMPEG_ANALYZEDURATION} "
                    f"-af {local_af}"
                )
                before_opts = (
                    "-reconnect 1 "
                    "-reconnect_streamed 1 "
                    "-reconnect_delay_max 5 "
                    f"-rw_timeout {FFMPEG_RW_TIMEOUT_US} "
                    "-nostdin"
                )
                ffmpeg_options = {
                    "before_options": before_opts,
                    "options": base_opts,
                }
            elif is_hls:
                # For HLS streams (SoundCloud, etc.) - special handling
                # HLS manages its own segments, so we use different options
                af_filter = (
                    "aresample=async=1000:min_hard_comp=0.01:first_pts=0,"
                    f"adelay={STARTUP_ADELAY_MS}|{STARTUP_ADELAY_MS}"
                )
                base_opts = (
                    f"-vn "
                    f"-probesize {FFMPEG_PROBESIZE} "
                    f"-analyzeduration {FFMPEG_ANALYZEDURATION} "
                    f"-af {af_filter}"
                )
                # HLS needs protocol whitelist and different reconnect handling
                before_opts = (
                    "-protocol_whitelist file,http,https,tcp,tls,crypto "
                    f"-rw_timeout {FFMPEG_RW_TIMEOUT_US} "
                    "-nostdin"
                )
                ffmpeg_options = {
                    "before_options": before_opts,
                    "options": base_opts,
                }
            else:
                # For regular HTTP streams (Suno CDN, direct files)
                # async=1000 gives more tolerance, min_hard_comp=0.01 reduces artifacts
                af_filter = (
                    "aresample=async=1000:min_hard_comp=0.01:first_pts=0,"
                    f"adelay={STARTUP_ADELAY_MS}|{STARTUP_ADELAY_MS}"
                )
                base_opts = (
                    f"-vn "
                    f"-probesize {FFMPEG_PROBESIZE} "
                    f"-analyzeduration {FFMPEG_ANALYZEDURATION} "
                    f"-thread_queue_size {FFMPEG_THREAD_QUEUE_SIZE} "
                    f"-buffer_size {FFMPEG_BUFFER_SIZE} "
                    f"-max_delay {FFMPEG_MAX_DELAY_US} "
                    f"-af {af_filter}"
                )
                if FFMPEG_NOBUFFER:
                    base_opts += " -fflags +nobuffer"

                before_opts = (
                    "-reconnect 1 "
                    "-reconnect_streamed 1 "
                    "-reconnect_at_eof 1 "
                    "-reconnect_delay_max 5 "
                    f"-rw_timeout {FFMPEG_RW_TIMEOUT_US} "
                    "-nostdin"
                )
                ffmpeg_options = {
                    "before_options": before_opts,
                    "options": base_opts,
                }

            try:
                source = discord.FFmpegPCMAudio(url_val, **ffmpeg_options)
                volume_transformer = discord.PCMVolumeTransformer(source, volume=self.volumes[gid])
            except Exception as e:
                print(f"Audio source error: {e} for {song.get('url')}")
                # Clean up prefetched file before returning
                self._cleanup_prefetched_file(local_to_delete)
                embed = discord.Embed(
                    title="❌ Playback Error",
                    description=f"Failed to play {song.get('title','Unknown')}: {str(e)}",
                    color=0xff0000
                )
                try:
                    await ctx.send(embed=embed)
                except Exception:
                    pass
                if queue and ctx.voice_client:
                    self.bot.loop.create_task(self.play_next(ctx))
                return

            def after_playing(error):
                try:
                    if error:
                        print(f"Player error: {error}")

                    try:
                        if song.get("_track_id") and song.get("_play_id"):
                            log_play_end(track_id=song["_track_id"], play_id=song["_play_id"])
                    except Exception as e_end:
                        print(f"[history] end log failed: {e_end}")

                    # Use the helper method for cleanup
                    self._cleanup_prefetched_file(local_to_delete)

                    self.current_song = None
                    self.song_start_time = None
                    if self.update_song_activity.is_running():
                        self.update_song_activity.stop()
                    asyncio.run_coroutine_threadsafe(self.bot.change_presence(activity=None), self.bot.loop)

                    # Check voice client state more carefully before continuing
                    vc = ctx.voice_client
                    vc_valid = vc is not None and getattr(vc, "channel", None) is not None
                    
                    if queue and vc_valid:
                        # Double-check we're still connected before playing next
                        try:
                            # Only continue if voice client is actually connected
                            if hasattr(vc, 'ws') and vc.ws and not vc.ws.closed:
                                self.bot.loop.call_soon_threadsafe(lambda: self.bot.loop.create_task(self.play_next(ctx)))
                            else:
                                print(f"[after_playing] Voice client not connected, skipping next song")
                        except Exception:
                            # If we can't check connection state, try anyway (better than stopping)
                            try:
                                self.bot.loop.call_soon_threadsafe(lambda: self.bot.loop.create_task(self.play_next(ctx)))
                            except Exception as e_vc:
                                print(f"[after_playing] Failed to schedule next song: {e_vc}")
                    
                    # Always try to schedule autofill when queue is empty (it will check voice client internally)
                    if not queue:
                        if vc_valid:
                            embed2 = discord.Embed(title="⏹️ Queue Empty", description="Finished playing! 🎉", color=0x00ff00)
                            try:
                                asyncio.run_coroutine_threadsafe(self.get_radio_channel(ctx).send(embed=embed2), self.bot.loop)
                            except Exception:
                                pass
                        
                        # Schedule autofill (it will check voice client and enabled state internally)
                        try:
                            self.bot.loop.call_soon_threadsafe(lambda: self._schedule_autofill_if_idle(ctx))
                        except Exception as _e:
                            print(f"[autofill schedule] {_e}")
                    elif not vc_valid:
                        # Voice client disconnected, don't try to continue
                        print(f"[after_playing] Voice client disconnected, not continuing playback")
                except Exception as e2:
                    print(f"after_playing crashed: {e2}")
                    import traceback
                    traceback.print_exc()

            target_vol = self.volumes[gid]
            start_muted = (PREBUFFER_SECONDS > 0) or (FADE_IN_SECONDS > 0)
            if start_muted:
                try:
                    volume_transformer.volume = 0.0001
                except Exception:
                    pass

            ctx.voice_client.play(volume_transformer, after=after_playing)

            if PREBUFFER_SECONDS > 0:
                try:
                    await asyncio.sleep(PREBUFFER_SECONDS)
                except Exception:
                    pass

            if FADE_IN_SECONDS > 0:
                asyncio.create_task(
                    self._fade_in_volume(
                        volume_transformer,
                        target_vol,
                        FADE_IN_SECONDS,
                        FADE_IN_STEPS
                    )
                )
            else:
                try:
                    volume_transformer.volume = target_vol
                except Exception:
                    pass

            self._song_index[gid] += 1
            current_song_index = self._song_index[gid]

            if self.update_song_activity.is_running():
                self.update_song_activity.stop()
            self.current_song = song
            self.song_start_time = time.time()
            await self.set_song_activity(song, 0.0)
            if not self.update_song_activity.is_running():
                self.update_song_activity.start()

            requester = (song.get("requester_mention")
                         or song.get("requester_name")
                         or song.get("requester_tag"))
            upcoming_two = list(self.queues[gid])[:2]
            np_embed, thumb_file = build_now_playing_embed(song, requester_mention=requester, upcoming_tracks=upcoming_two)

            # Get the best page URL for the song (supports Suno, SoundCloud, Bandcamp, etc.)
            song_url = (
                song.get("_original_url") or  # yt-dlp sources (SoundCloud, Bandcamp, etc.)
                song.get("video_url") or      # yt-dlp alternative
                _derive_suno_url(song) or     # Suno tracks
                (song.get("url") or "")
            )
            song_title = song.get("title") or song.get("track_id") or "Untitled"

            view = NowPlayingView(
                song=song,
                track_id=song.get("_track_id"),
                guild_id=ctx.guild.id,
                bot_user_id=(self.bot.user.id if self.bot.user else 0),
                song_title=song_title,
                song_url=song_url,
            )

            ch = self.get_radio_channel(ctx)
            sent_message = await ch.send(embed=np_embed, view=view, file=thumb_file) if thumb_file else await ch.send(embed=np_embed, view=view)

            try:
                if sent_message:
                    entry = {
                        "message_id": sent_message.id,
                        "channel_id": sent_message.channel.id,
                        "song_index": current_song_index,
                        "is_autofill": bool(song.get("_autofill")),
                    }
                    self._np_track[gid].append(entry)
            except Exception:
                pass

            await self._cleanup_now_playing_messages(gid)

    @commands.command(name='queue')
    async def show_queue(self, ctx):
        """
            Shows the current queue with estimated time to start for each item.
            Supports pagination for large queues.
        """
        guild_id = ctx.guild.id
        queue = self.queues[guild_id]
        
        if not queue:
            embed = discord.Embed(
                title="📋 Queue",
                description="Queue is empty! Add songs with `!play`.",
                color=0x0099ff
            )
            await ctx.send(embed=embed)
            return
        
        eta_list = self._queue_eta_list(guild_id)
        
        view = PaginatedQueueView(
            queue=queue,
            eta_list=eta_list,
        )
        
        # Delete the command message if it should be auto-deleted
        try:
            if ctx.message and ctx.command and ctx.command.name in AUTO_DELETE_COMMANDS:
                await ctx.message.delete()
        except Exception:
            pass
        
        await view.send(ctx.channel)

    @commands.command(name='skip')
    async def skip(self, ctx, target: str = ""):
        """
        Skip the currently playing track.
        Usage:
          !skip             -> skip current track (any)
          !skip autofill    -> if current is filler, stop it; also purge filler from queue
        """
        gid = ctx.guild.id
        target = (target or "").strip().lower()

        def _purge_filler_from_queue() -> int:
            q = self.queues[gid]
            if not q:
                return 0
            kept = []
            removed = 0
            for item in q:
                if item.get("_autofill"):
                    removed += 1
                else:
                    kept.append(item)
            q.clear()
            q.extend(kept)
            if removed:
                save_data(gid, self.queues, self.playlists, self.user_mappings)
            return removed

        if target == "autofill":
            removed_current = False
            if self.current_song and self.current_song.get("_autofill") and ctx.voice_client and ctx.voice_client.is_playing():
                await self._fade_out_and_stop(ctx)
                removed_current = True

            removed_queued = _purge_filler_from_queue()

            desc = []
            if removed_current:
                desc.append("Skipped the **current autofill** track.")
            if removed_queued:
                desc.append(f"🧹 Removed **{removed_queued}** autofill track(s) from the queue.")
            if not desc:
                desc.append("No autofill tracks were playing or queued.")

            await ctx.send(embed=discord.Embed(
                title="📻 Autofill Skip",
                description="\n".join(desc),
                color=0x9b59b6
            ))
            return

        if ctx.voice_client and ctx.voice_client.is_playing():
            await self._fade_out_and_stop(ctx)
            await ctx.send(embed=discord.Embed(
                title="⏭️ Skipped",
                description="Skipped the current track! 🚀",
                color=0x0099ff
            ))

    @commands.command(name='stop')
    async def stop(self, ctx):
        """
        Stops all playback and clears the playlist queue
        """
        if ctx.voice_client:
            await self._fade_out_and_stop(ctx)

        gid = ctx.guild.id
        self.queues[gid].clear()

        if CLEAR_PLAYLISTS_ON_STOP:
            self.playlists[gid].clear()

        self._cancel_autofill_task(gid)
        self._clear_autofill_from_queue(gid)

        self.current_song = None
        self.song_start_time = None
        if self.update_song_activity.is_running():
            self.update_song_activity.stop()
        await self.bot.change_presence(activity=None)

        save_data(gid, self.queues, self.playlists, self.user_mappings)

        msg = "Stopped and cleared queue! 😴"
        if CLEAR_PLAYLISTS_ON_STOP:
            msg += " (Playlists cleared)"
        embed = discord.Embed(title="⏹️ Stopped", description=msg, color=0xff0000)
        await ctx.send(embed=embed)

        try:
            if self._is_autofill_enabled(gid):
                self._schedule_autofill_if_idle(ctx, delay=AUTOFILL_DELAY_SEC)
        except Exception as _e:
            print(f"[autofill after stop] {_e}")

    @commands.command(name="stahp", hidden=True)
    async def stahp(self, ctx):
        """STAHP"""
        await self.stop(ctx)

    @commands.command(name="hush", hidden=True)
    async def hush(self, ctx):
        """Shh"""
        await self.stop(ctx)

    @commands.command(name='shuffle')
    async def shuffle_queue(self, ctx):
        """
        Shuffles the current queue
        """
        guild_id = ctx.guild.id
        queue = self.queues[guild_id]
        if not queue:
            embed = discord.Embed(title="❌ Error", description="Queue is empty! No songs to shuffle.", color=0xff0000)
            await ctx.send(embed=embed)
            return
        items = list(queue)
        
        # random.shuffle(items) vvv changed by Paul Schirf
        shuffle_displacing_first_inplace(items)

        queue.clear()
        queue.extend(items)
        save_data(guild_id, self.queues, self.playlists, self.user_mappings)
        embed = discord.Embed(title="🔀 Shuffled", description="Queue has been shuffled! 🎲", color=0x00ff00)
        await ctx.send(embed=embed)

    @commands.command(name='volume')
    async def volume(self, ctx, *, vol: int):
        """
        Set volume from 0 to 100 (defaults to 69)
        """
        guild_id = ctx.guild.id
        if not (0 <= vol <= 200):
            embed = discord.Embed(title="❌ Error", description="Volume must be between 0 and 200 (100 = default).", color=0xff0000)
            await ctx.send(embed=embed)
            return
        self.volumes[guild_id] = vol / 100.0
        embed = discord.Embed(title="🔊 Volume", description=f"Volume set to {vol}%! 🎙️", color=0x00ff00)
        await ctx.send(embed=embed)
        if ctx.voice_client and ctx.voice_client.source:
            if hasattr(ctx.voice_client.source, 'volume'):
                ctx.voice_client.source.volume = self.volumes[guild_id]

    @commands.command(name='song_info')
    async def song_info(self, ctx):
        """
        Display detailed information about the currently playing song, including lyrics and prompt.
        """
        if not self.current_song:
            embed = discord.Embed(
                title="❌ No Song Playing",
                description="There's no song currently playing. Use `!play` to start playing music!",
                color=0xff0000
            )
            await ctx.send(embed=embed)
            return

        embed, thumb_file = build_song_info_embed(self.current_song)
        await ctx.send(embed=embed, file=thumb_file) if thumb_file else await ctx.send(embed=embed)

    @commands.command(name='playlist')
    async def playlist(self, ctx, url: str, max_items: int = 200):
        """
        Enqueue tracks from a Suno playlist/profile/handle in bulk
        Usage: !playlist https://suno.com/playlist/##"
        """
        is_admin = self._is_admin(ctx.author)

        if not ctx.voice_client:
            await ctx.invoke(self.join)

        guild_id = ctx.guild.id
        queue = self.queues[guild_id]

        self._cancel_autofill_task(guild_id)
        self._clear_autofill_from_queue(guild_id)

        status_msg = await ctx.send(embed=discord.Embed(
            title="⏳ Processing Playlist",
            description="Fetching tracks from Suno, please wait...",
            color=0xf1c40f
        ))

        try:
            async with ctx.typing():
                loop = asyncio.get_event_loop()
                raw_tracks = await loop.run_in_executor(
                    None, lambda: _scrape_playlist_to_tracks(url, limit=max_items)
                )
                if not raw_tracks:
                    await status_msg.delete()
                    embed = discord.Embed(
                        title="❌ No Tracks Found",
                        description="Couldn't find songs on that page.",
                        color=0xff0000
                    )
                    await ctx.send(embed=embed)
                    return

                intended = len(raw_tracks)
                allowed, notice = self._enforce_queue_add_limit(
                    guild_id, intended, bypass=is_admin
                )

                if allowed <= 0:
                    await status_msg.delete()
                    await ctx.send(embed=discord.Embed(
                        title="🚫 Queue Limit",
                        description=notice or "Queue limit reached for bulk adds.",
                        color=0xe74c3c
                    ))
                    return
                if allowed < intended:
                    raw_tracks = raw_tracks[:allowed]

                tracks = await self._resolve_tracks(raw_tracks, max_workers=6)

                # ✅ define timestamp once
                now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())

                # Build set of existing track IDs in queue to check for duplicates
                existing_track_ids = set()
                for existing_track in queue:
                    track_id = _canonical_track_id(existing_track)
                    if track_id:
                        existing_track_ids.add(track_id)

                start_pos = len(queue) + 1
                added_count = 0
                duplicate_count = 0
                
                for t in tracks:
                    # Check for duplicates
                    track_id = _canonical_track_id(t)
                    if track_id and track_id in existing_track_ids:
                        duplicate_count += 1
                        continue
                    
                    # Add track to queue
                    t["requester_id"] = ctx.author.id
                    t["requester_tag"] = str(ctx.author)
                    t["requester_name"] = ctx.author.display_name
                    t["requester_mention"] = ctx.author.mention
                    t["requested_at"] = now_ts
                    t["_from_playlist"] = True  # optional but nice if you want later filtering
                    queue.append(t)
                    added_count += 1
                    
                    # Add to existing set to prevent duplicates within the same batch
                    if track_id:
                        existing_track_ids.add(track_id)

                end_pos = len(queue)
                save_data(guild_id, self.queues, self.playlists, self.user_mappings)

                await status_msg.delete()

                desc = f"Added {added_count} tracks!"
                if duplicate_count > 0:
                    desc += f" ({duplicate_count} duplicate{'s' if duplicate_count != 1 else ''} skipped)"
                if end_pos >= start_pos:
                    desc += f" (positions #{start_pos}–#{end_pos})"
                if notice:
                    desc += f"\n\n{notice}"

                embed = discord.Embed(title="➕ Added Playlist", description=desc, color=0x0099ff)
                await ctx.send(embed=embed)

                if not ctx.voice_client.is_playing():
                    await self.play_next(ctx)

        except Exception as e:
            try:
                await status_msg.delete()
            except:
                pass
            embed = discord.Embed(
                title="❌ Error",
                description=f"Failed to add playlist: {e}",
                color=0xff0000
            )
            await ctx.send(embed=embed)

    @commands.command(name='remove')
    async def remove_from_queue(self, ctx, position: str = ""):
        """Remove a song from the queue by position."""
        try:
            position = int(position)
        except ValueError:
            embed = discord.Embed(title="❌ Error", description="Invalid position! Use a number (e.g., !remove 1).", color=0xff0000)
            await ctx.send(embed=embed)
            return

        guild_id = ctx.guild.id
        queue = self.queues[guild_id]
        if not queue:
            embed = discord.Embed(title="❌ Error", description="Queue is empty!", color=0xff0000)
            await ctx.send(embed=embed)
            return

        if position < 1 or position > len(queue):
            embed = discord.Embed(title="❌ Error", description=f"Invalid position! Must be between 1 and {len(queue)}.", color=0xff0000)
            await ctx.send(embed=embed)
            return

        idx = position - 1
        queue_list = list(queue)
        removed_song = queue_list[idx]
        queue_list.pop(idx)
        queue.clear()
        queue.extend(queue_list)
        save_data(guild_id, self.queues, self.playlists, self.user_mappings)

        embed = discord.Embed(title="🗑️ Removed", description=f"Removed: {removed_song.get('title','Untitled')} from position {position}", color=0x00ff00)
        await ctx.send(embed=embed)

    @commands.command(name='reload')
    @commands.has_permissions(administrator=True)
    async def reload(self, ctx):
        """
        Reload the song cog activity (Admin Only)
        """
        try:
            if ctx.voice_client and ctx.voice_client.is_playing():
                await self._fade_out_and_stop(ctx)
            else:
                try:
                    if ctx.voice_client:
                        ctx.voice_client.stop()
                except Exception:
                    pass
            self.current_song = None
            self.song_start_time = None
            if self.update_song_activity.is_running():
                self.update_song_activity.stop()
            await self.bot.change_presence(activity=None)

            gid = ctx.guild.id
            self.queues[gid].clear()

            if CLEAR_PLAYLISTS_ON_RELOAD:
                self.playlists[gid].clear()

            self._cancel_autofill_task(gid)
            self._clear_autofill_from_queue(gid)

            save_data(gid, self.queues, self.playlists, self.user_mappings)

            await self.bot.unload_extension('src.cogs.music')
            await self.bot.load_extension('src.cogs.music')

            msg = "Music cog reloaded successfully!"
            if CLEAR_PLAYLISTS_ON_RELOAD:
                msg += " (Playlists cleared)"
            embed = discord.Embed(title="✅ Reloaded", description=msg, color=0x00ff00)
            await ctx.send(embed=embed)
        except Exception as e:
            embed = discord.Embed(title="❌ Reload Failed", description=f"Error: {str(e)}", color=0xff0000)
            await ctx.send(embed=embed)

    @commands.command(name='queue_clear')
    async def queue_clear(self, ctx):
        """
        Clears the song queue
        """
        gid = ctx.guild.id
        self.queues[gid].clear()
        save_data(gid, self.queues, self.playlists, self.user_mappings)
        await ctx.send(embed=discord.Embed(title="🧹 Queue Cleared", description="All queued tracks removed.", color=0x00ff00))

    @commands.command(name='playlist_clear')
    async def playlist_clear(self, ctx):
        """
        Clears the song queue of playlists (experimental)
        """
        gid = ctx.guild.id
        q = self.queues[gid]

        if not q:
            await ctx.send(embed=discord.Embed(
                title="🧹 Playlist Tracks Cleared",
                description="Queue is empty — nothing to clear.",
                color=0x00ff00
            ))
            return

        before = len(q)

        kept = [t for t in q if not t.get("_from_playlist")]
        removed = before - len(kept)

        q.clear()
        q.extend(kept)

        save_data(gid, self.queues, self.playlists, self.user_mappings)

        desc = (
            f"Removed **{removed}** playlist-added track(s) from the queue."
            if removed else
            "No playlist-added tracks were in the queue."
        )

        await ctx.send(embed=discord.Embed(
            title="🧹 Playlist Tracks Cleared",
            description=desc,
            color=0x00ff00
        ))

    @commands.has_permissions(administrator=True)
    @commands.command(name='reset_state')
    async def reset_state(self, ctx):
        """
        Resets the bot state for debugging or issues (Admin Only)
        """
        gid = ctx.guild.id
        self.queues[gid].clear()
        self.playlists[gid].clear()
        self.user_mappings[gid].clear()
        self._cancel_autofill_task(gid)
        self._clear_autofill_from_queue(gid)
        save_data(gid, self.queues, self.playlists, self.user_mappings)
        await ctx.send(embed=discord.Embed(title="♻️ State Reset", description="Queues, playlists, and mappings wiped.", color=0xff9900))

    # ========== Autofill Admin/User Commands =================================
    @commands.has_permissions(administrator=True)
    @commands.command(name="autofill_set")
    async def autofill_set(self, ctx, url: str):
        """
        Set the playlist/profile URL to use for idle autofill radio. (Admin only)
        Usage: !autofill_set https://suno.com/playlist/XXXX  or  https://suno.com/@handle  or @handle
        """
        if not self._autofill_feature_on:
            await ctx.send(embed=discord.Embed(title="Feature Disabled", description="Autofill is disabled.", color=0xe74c3c))
            return
        gid = ctx.guild.id
        the_url = url.strip()
        self.auto_playlist_urls[gid] = the_url
        self.auto_play_enabled[gid] = True

        amap = self.user_mappings[gid]
        if not isinstance(amap, dict):
            amap = {}
            self.user_mappings[gid] = amap
        amap["autofill"] = {"url": the_url, "enabled": True}
        save_data(gid, self.queues, self.playlists, self.user_mappings)

        await ctx.send(embed=discord.Embed(
            title="🟢 Autofill Source Set",
            description=f"Autofill will pull from:\n`{the_url}`\n(Starts **{AUTOFILL_DELAY_SEC}s** after finishing when the queue is empty.)",
            color=0x2ecc71
        ))

    @commands.has_permissions(administrator=True)
    @commands.command(name="autofill_on")
    async def autofill_on(self, ctx):
        """
        Turns on Autofill (Admin only)
        """
        if not self._autofill_feature_on:
            await ctx.send(embed=discord.Embed(title="Feature Disabled", description="Autofill is disabled.", color=0xe74c3c))
            return
        gid = ctx.guild.id
        self.auto_play_enabled[gid] = True

        amap = self.user_mappings[gid]
        if not isinstance(amap, dict):
            amap = {}
            self.user_mappings[gid] = amap
        ainfo = amap.get("autofill", {})
        ainfo["enabled"] = True
        amap["autofill"] = ainfo
        save_data(gid, self.queues, self.playlists, self.user_mappings)

        await ctx.send(embed=discord.Embed(
            title="🟢 Autofill Enabled",
            description="Idle radio will resume after the queue finishes.",
            color=0x2ecc71
        ))

    @commands.has_permissions(administrator=True)
    @commands.command(name="autofill_off")
    async def autofill_off(self, ctx):
        """
        Turns off Autofill (Admin only)
        """
        gid = ctx.guild.id
        self.auto_play_enabled[gid] = False
        self._cancel_autofill_task(gid)
        self._clear_autofill_from_queue(gid)

        amap = self.user_mappings[gid]
        if not isinstance(amap, dict):
            amap = {}
            self.user_mappings[gid] = amap
        ainfo = amap.get("autofill", {})
        ainfo["enabled"] = False
        amap["autofill"] = ainfo
        save_data(gid, self.queues, self.playlists, self.user_mappings)

        await ctx.send(embed=discord.Embed(
            title="🔴 Autofill Disabled",
            description="Idle radio will no longer auto-resume.",
            color=0xe74c3c
        ))

    @commands.command(name="autofill_status")
    async def autofill_status(self, ctx):
        """
        Show Autofill Status
        """
        gid = ctx.guild.id
        enabled = bool(self.auto_play_enabled.get(gid, True)) and self._autofill_feature_on

        url = self.auto_playlist_urls.get(gid)
        csv_rows = self.autofill_seed_rows.get(gid)
        if url:
            src_str = url
        elif csv_rows:
            src_str = f"CSV ({len(csv_rows)} items)"
        else:
            src_str = (DEFAULT_AUTOFILL_URL or ("CSV" if DEFAULT_AUTOFILL_CSV else "—"))

        await ctx.send(embed=discord.Embed(
            title="ℹ️ Autofill Status",
            description=f"**Feature:** {'ON' if self._autofill_feature_on else 'OFF'}\n"
                        f"**State:** {'Enabled' if enabled else 'Disabled'}\n"
                        f"**Source:** {src_str}\n"
                        f"**Delay:** {AUTOFILL_DELAY_SEC}s",
            color=0x7289DA
        ))

    @commands.has_permissions(administrator=True)
    @commands.command(name="autofill_reload")
    async def autofill_reload(self, ctx):
        """
        Reload the Autofill CSV (Admin only) and report the total number of
        usable song URLs found. Resolves the *active* CSV path first.
        """
        gid = ctx.guild.id

        csv_path = None
        try:
            amap = self.user_mappings.get(gid) or {}
            ainfo = amap.get("autofill") or {}
            csv_path = (ainfo.get("csv") or "").strip()
        except Exception:
            csv_path = ""

        if not csv_path:
            csv_path = (
                os.getenv("AUTOFILL_CSV_PATH", "").strip()
                or os.getenv("DEFAULT_AUTOFILL_CSV", "").strip()
                or getattr(self, "_autofill_csv_path", "").strip()
                or "autofill.csv"
            )

        path = os.path.abspath(os.path.expanduser(csv_path))

        try:
            rows = []
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                rdr = csv.reader(f)
                first_row = True
                for r in rdr:
                    if not r:
                        continue
                    cell0 = (r[0] or "").strip()
                    cell0_norm = cell0.lower().replace(" ", "")

                    if first_row and cell0_norm in ("url", "songurl", "trackurl"):
                        first_row = False
                        continue
                    first_row = False

                    if not cell0 or cell0.startswith("#"):
                        continue
                    rows.append({"url": cell0})
        except FileNotFoundError:
            await ctx.send(embed=discord.Embed(
                title="❌ Autofill CSV Reload Failed",
                description=f"CSV not found at `{path}`.",
                color=0xe74c3c
            ))
            return
        except Exception as e:
            await ctx.send(embed=discord.Embed(
                title="❌ Autofill CSV Reload Failed",
                description=f"{type(e).__name__}: {e}",
                color=0xe74c3c
            ))
            return

        total = len(rows)

        self._autofill_csv_cache = rows
        self.autofill_seed_rows[gid] = rows[:]
        try:
            self._autofill_csv_last_mtime = os.path.getmtime(path)
        except Exception:
            pass

        try:
            size = os.path.getsize(path)
            mtime = int(os.path.getmtime(path))
            diag = f"Size: {size} bytes • Updated: <t:{mtime}:t>"
        except Exception:
            diag = "Size/mtime unavailable"

        await ctx.send(embed=discord.Embed(
            title="✅ Autofill CSV Reloaded",
            description=(
                f"Path: `{path}`\n"
                f"Found **{total}** song URL(s).\n"
                f"{diag}"
            ),
            color=0x2ecc71
        ))

    @commands.has_permissions(administrator=True)
    @commands.command(name="autofill_unset")
    async def autofill_unset(self, ctx):
        """
        Unset the saved playlist/profile URL so CSV becomes the source again (Admin only).
        Keeps the 'enabled' flag; just removes the URL override.
        """
        gid = ctx.guild.id

        if gid in self.auto_playlist_urls:
            try:
                self.auto_playlist_urls.pop(gid, None)
            except Exception:
                self.auto_playlist_urls[gid] = ""

        amap = self.user_mappings.get(gid)
        if not isinstance(amap, dict):
            amap = {}
            self.user_mappings[gid] = amap
        ainfo = amap.get("autofill", {})
        enabled_state = bool(self.auto_play_enabled.get(gid, ainfo.get("enabled", True)))
        ainfo["enabled"] = enabled_state
        ainfo["url"] = ""
        amap["autofill"] = ainfo

        save_data(gid, self.queues, self.playlists, self.user_mappings)

        desc_lines = [
            "Cleared the **autofill URL override**.",
            f"**Enabled:** {'Yes' if enabled_state else 'No'}",
            "Source will now come from the **CSV** (or `DEFAULT_AUTOFILL_URL` if CSV is not set)."
        ]
        await ctx.send(embed=discord.Embed(
            title="🔄 Autofill Source Unset",
            description="\n".join(desc_lines),
            color=0x3498db
        ))

    @commands.command(name="twss", hidden=True)
    async def twss(self, ctx):
        """
        Post a random GIF URL from twss.csv located in the same directory as the autofill CSV.
        If the command is used as a reply, the bot will reply to the same message.
        """
        # Get the reference from the command message (if it was a reply)
        reference = ctx.message.reference
        
        # Get the autofill CSV path to determine the directory
        gid = ctx.guild.id
        csv_path = None
        
        try:
            amap = self.user_mappings.get(gid) or {}
            ainfo = amap.get("autofill") or {}
            csv_path = (ainfo.get("csv") or "").strip()
        except Exception:
            csv_path = ""
        
        if not csv_path:
            csv_path = (
                os.getenv("AUTOFILL_CSV_PATH", "").strip()
                or os.getenv("DEFAULT_AUTOFILL_CSV", "").strip()
                or getattr(self, "_autofill_csv_path", "").strip()
                or "autofill.csv"
            )
        
        # Get the directory from the autofill CSV path
        if csv_path:
            autofill_dir = os.path.dirname(os.path.abspath(os.path.expanduser(csv_path)))
            twss_path = os.path.join(autofill_dir, "twss.csv")
        else:
            # Fallback to current directory or same location as autofill.csv
            twss_path = "twss.csv"
        
        twss_path = os.path.abspath(os.path.expanduser(twss_path))
        
        # Load URLs from twss.csv
        urls = []
        try:
            if not os.path.exists(twss_path):
                await ctx.send(embed=discord.Embed(
                    title="❌ TWSS CSV Not Found",
                    description=f"Could not find `twss.csv` at `{twss_path}`.",
                    color=0xe74c3c
                ), reference=reference)
                return
            
            with open(twss_path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.reader(f)
                for row in reader:
                    if not row:
                        continue
                    # Get first column as URL
                    url = (row[0] or "").strip()
                    # Skip empty rows and header rows
                    if url and not url.lower().startswith("url") and not url.startswith("#"):
                        urls.append(url)
        except Exception as e:
            await ctx.send(embed=discord.Embed(
                title="❌ Error Reading TWSS CSV",
                description=f"Failed to read `twss.csv`: {type(e).__name__}: {e}",
                color=0xe74c3c
            ), reference=reference)
            return
        
        if not urls:
            await ctx.send(embed=discord.Embed(
                title="❌ No URLs Found",
                description=f"No valid URLs found in `twss.csv` at `{twss_path}`.",
                color=0xe74c3c
            ), reference=reference)
            return
        
        # Pick a random URL and send it
        random_url = random.choice(urls)
        await ctx.send(random_url, reference=reference)

    @commands.has_permissions(administrator=True)
    @commands.command(name="queue_limit_on")
    async def queue_limit_on(self, ctx):
        """
        Turn the queue limit on (Admin only)
        """
        gid = ctx.guild.id
        self.queue_limit_enabled[gid] = True
        amap = self.user_mappings[gid]
        if not isinstance(amap, dict):
            amap = {}
            self.user_mappings[gid] = amap
        amap["queue_limit"] = {"enabled": True, "max": self._limit_max(gid)}
        save_data(gid, self.queues, self.playlists, self.user_mappings)
        await ctx.send(embed=discord.Embed(
            title="📦 Queue Limit",
            description=f"Queue limit is **ON** (max {self._limit_max(gid)} per add).",
            color=0x2ecc71
        ))

    @commands.has_permissions(administrator=True)
    @commands.command(name="queue_limit_off")
    async def queue_limit_off(self, ctx, per_user_max: int | None = None):
        """
        Turn the queue limit off (Admin only).
        Optionally also set the per-user cap while limits are off, e.g. !queue_limit_off 5
        """
        gid = ctx.guild.id
        self.queue_limit_enabled[gid] = False

        if per_user_max is not None:
            per_user_max = max(1, int(per_user_max))
            self.queue_per_user_max[gid] = per_user_max

        amap = self.user_mappings[gid]
        if not isinstance(amap, dict):
            amap = {}
            self.user_mappings[gid] = amap

        amap["queue_limit"] = {
            "enabled": False,
            "max": self._limit_max(gid),
            "per_user_max": self._per_user_max(gid),
        }
        save_data(gid, self.queues, self.playlists, self.user_mappings)
        await ctx.send(embed=discord.Embed(
            title="📦 Queue Limit",
            description=f"Queue limit is **OFF**.\nPer-user cap: **{self._per_user_max(gid)}**",
            color=0xe67e22
        ))

    @commands.has_permissions(administrator=True)
    @commands.command(name="queue_limit_set")
    async def queue_limit_set(self, ctx, max_per_add: int, per_user_max: int | None = None):
        """
        Set the queue limit amount (Admin only).
        Usage:
          !queue_limit_set 5 -> sets max per add to 5, leaves per-user cap as-is
          !queue_limit_set 5 3 -> sets max per add to 5 and per-user cap to 3
        """
        gid = ctx.guild.id
        max_per_add = max(1, int(max_per_add))
        self.queue_limit_max[gid] = max_per_add

        if per_user_max is not None:
            per_user_max = max(1, int(per_user_max))
            self.queue_per_user_max[gid] = per_user_max

        amap = self.user_mappings[gid]
        if not isinstance(amap, dict):
            amap = {}
            self.user_mappings[gid] = amap
        enabled = self._limit_is_on(gid)
        amap["queue_limit"] = {
            "enabled": enabled,
            "max": max_per_add,
            "per_user_max": self._per_user_max(gid),
        }
        save_data(gid, self.queues, self.playlists, self.user_mappings)

        desc = [f"Max songs per add set to **{max_per_add}**."]
        desc.append(f"Per-user cap: **{self._per_user_max(gid)}**")
        await ctx.send(embed=discord.Embed(
            title="📦 Queue Limit",
            description="\n".join(desc),
            color=0x3498db
        ))

    @commands.command(name="queue_limit_status")
    async def queue_limit_status(self, ctx):
        """
        Queue Limit Status (Admin only)
        """
        gid = ctx.guild.id
        enabled = self._limit_is_on(gid)
        maxn = self._limit_max(gid)
        per_user_cap = self._per_user_max(gid)
        await ctx.send(embed=discord.Embed(
            title="ℹ️ Queue Limit Status",
            description=f"**State:** {'ON' if enabled else 'OFF'}\n"
                        f"**Max per add:** {maxn}\n"
                        f"**Max per user:** {per_user_cap}",
            color=0x7289DA
        ))

    @commands.has_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @commands.command(name="np_clean_on")
    async def np_clean_on(self, ctx):
        """
        Turn on cleanup for ALL Now Playing cards (Admin only).
        """
        gid = ctx.guild.id
        self.np_clean_non_autofill[gid] = True
        
        amap = self.user_mappings[gid]
        if not isinstance(amap, dict):
            amap = {}
            self.user_mappings[gid] = amap
        amap["np_clean_non_autofill"] = True
        
        save_data(gid, self.queues, self.playlists, self.user_mappings)
        
        await ctx.send(embed=discord.Embed(
            title="🧹 Now Playing Cleanup",
            description="Cleanup is now **ON** for all tracks (manual and autofill).",
            color=0x2ecc71
        ))

    @commands.has_permissions(administrator=True)
    @commands.command(name="np_clean_off")
    async def np_clean_off(self, ctx):
        """
        Turn off cleanup for manual tracks; only autofill will be cleaned (Admin only).
        """
        gid = ctx.guild.id
        self.np_clean_non_autofill[gid] = False
        
        amap = self.user_mappings[gid]
        if not isinstance(amap, dict):
            amap = {}
            self.user_mappings[gid] = amap
        amap["np_clean_non_autofill"] = False
        
        save_data(gid, self.queues, self.playlists, self.user_mappings)
        
        await ctx.send(embed=discord.Embed(
            title="🧹 Now Playing Cleanup",
            description="Cleanup is now **OFF** for manual tracks. Autofill tracks will still be cleaned up.",
            color=0xe67e22
        ))

    @commands.command(name="ping")
    async def ping(self, ctx):
        """
            Shows Server Ping information (Admin only)
        """
        gid = ctx.guild.id
        vc = ctx.voice_client

        # --- Core latency ---
        ws_ms = round(self.bot.latency * 1000)

        # --- Shard info (safe if not sharded) ---
        shard_id = getattr(ctx.guild, "shard_id", None)
        shard_str = f"{shard_id}" if shard_id is not None else "—"

        # --- Voice / playback state ---
        if vc and vc.channel:
            voice_state = f"Connected to **{vc.channel.name}**"
            is_playing = vc.is_playing()
        else:
            voice_state = "Not connected"
            is_playing = False

        current_title = None
        if self.current_song:
            current_title = self.current_song.get("title") or "Untitled"

        q_len = len(self.queues.get(gid, []))

        # --- Feature toggles ---
        autofill_enabled = self._is_autofill_enabled(gid)
        queue_limit_on = self._limit_is_on(gid)
        max_per_add = self._limit_max(gid)
        per_user_cap = self._per_user_max(gid)

        desc_lines = [
            f"**WebSocket:** `{ws_ms} ms`",
            f"**Shard:** `{shard_str}`",
            f"**Voice:** {voice_state}",
            f"**Playing:** `{'yes' if is_playing else 'no'}`",
        ]

        if current_title:
            desc_lines.append(f"**Now playing:** {_truncate(current_title, 80)}")

        desc_lines += [
            f"**Queue size:** `{q_len}`",
            f"**Autofill:** `{'on' if autofill_enabled else 'off'}`",
            f"**Queue limit:** `{'on' if queue_limit_on else 'off'}` (max/add `{max_per_add}`, per-user `{per_user_cap}`)",
        ]

        embed = discord.Embed(
            title="🏓 Pong",
            description="\n".join(desc_lines),
            color=0x2ecc71
        )
        await ctx.send(embed=embed)

    @commands.has_permissions(administrator=True)
    @commands.command(
        name="qpanel",
        help="Open an interactive queue management panel (admins only).",
        hidden=True,
    )
    async def qpanel(self, ctx: commands.Context) -> None:
        """Open the queue manager panel for this guild."""
        if not ctx.guild:
            await ctx.send("This command can only be used in a server.")
            return

        guild_id = ctx.guild.id
        queue = self.queues.get(guild_id)

        if not queue or len(queue) == 0:
            await ctx.send("The queue is currently empty.")
            return

        # Delete previous qpanel message if it exists
        if guild_id in self._qpanel_messages:
            old_msg = self._qpanel_messages[guild_id]
            try:
                await old_msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                # Message already deleted or we don't have permission - ignore
                pass
            finally:
                # Remove from tracking even if deletion failed
                del self._qpanel_messages[guild_id]

        # Cleanup callback for when view times out
        async def cleanup_callback(gid: int) -> None:
            if gid in self._qpanel_messages:
                del self._qpanel_messages[gid]

        view = QueueManagerView(
            guild=ctx.guild,
            queue=queue,          # this is your live deque
            invoker=ctx.author,
            on_timeout_callback=cleanup_callback,
        )
        embed = build_queue_embed(ctx.guild, queue)
        msg = await ctx.send(embed=embed, view=view)
        view.message = msg
        
        # Track the new qpanel message
        self._qpanel_messages[guild_id] = msg

    def _create_autofill_saves_view(self, user: discord.User, is_dm: bool = False) -> LikedSongsManagerView:
        """Helper method to create the autofill saves view."""
        user_id = user.id
        
        # Get all liked tracks for this user
        tracks = get_user_liked_tracks_all_guilds(user_id)
        
        # Filter out deleted tracks (those with Unknown Title/Unknown) but keep them in DB
        def is_deleted_track(track: dict) -> bool:
            """Check if a track appears to be deleted (has Unknown Title/Unknown).
            
            Tracks are considered deleted if both title and artist are missing/unknown.
            This filters them from display while keeping them in the database.
            """
            title_raw = track.get("title") or ""
            artist_raw = track.get("artist") or ""
            
            title = str(title_raw).strip().lower() if title_raw else ""
            artist = str(artist_raw).strip().lower() if artist_raw else ""
            
            # Check for common "deleted" indicators
            deleted_titles = {"unknown title", "untitled", ""}
            deleted_artists = {"unknown", "unknown artist", "", "none"}
            
            # Track is deleted if both title and artist are unknown/missing
            return title in deleted_titles and artist in deleted_artists
        
        # Filter out deleted tracks from display (but they remain in the database)
        visible_tracks = [t for t in tracks if not is_deleted_track(t)]
        
        # Delete callback for removing likes
        async def delete_likes_callback(user_id_str: str, track_id: str) -> None:
            """Remove all likes from user for a specific track across all guilds."""
            conn = get_conn()
            conn.execute(
                "DELETE FROM likes WHERE user_id = ? AND track_id = ?",
                (user_id_str, track_id)
            )
        
        # Timeout callback for cleaning up DM message tracking (only for DMs)
        on_timeout_callback = None
        if is_dm:
            async def timeout_cleanup_callback(uid: int) -> None:
                """Clean up DM message tracking when view times out."""
                if uid in self._autofill_dm_messages:
                    del self._autofill_dm_messages[uid]
            on_timeout_callback = timeout_cleanup_callback
        
        # Create and return the view with filtered tracks
        return LikedSongsManagerView(
            user=user,
            tracks=visible_tracks,
            delete_callback=delete_likes_callback,
            on_timeout_callback=on_timeout_callback,
        )

    @commands.hybrid_command(
        name="autofill_saves",
        description="Manage your autofill liked songs (private message in channel)",
        aliases=["mylikes", "autofill"]
    )
    async def autofill_saves(self, ctx: commands.Context) -> None:
        """Show and manage user's autofill liked songs as an ephemeral message."""
        user = ctx.author
        
        # Check if we have an interaction (available for slash command invocations)
        # Hybrid commands provide ctx.interaction when invoked as slash commands
        interaction = getattr(ctx, 'interaction', None)
        
        if interaction is not None:
            # Slash command invocation - send ephemeral response
            view = self._create_autofill_saves_view(user, is_dm=False)
            await interaction.response.send_message(
                embed=view._build_embed(), 
                view=view, 
                ephemeral=True
            )
            view.message = await interaction.original_response()
        else:
            # Prefix command invocation - send DM to user (no public response)
            try:
                # Delete previous DM message if it exists
                user_id = user.id
                if user_id in self._autofill_dm_messages:
                    old_msg = self._autofill_dm_messages[user_id]
                    try:
                        await old_msg.delete()
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        # Message already deleted or we don't have permission - ignore
                        pass
                    finally:
                        # Remove from tracking even if deletion failed
                        del self._autofill_dm_messages[user_id]
                
                # Create view with timeout callback for DM cleanup
                view = self._create_autofill_saves_view(user, is_dm=True)
                
                # Send new DM message
                msg = await user.send(embed=view._build_embed(), view=view)
                view.message = msg
                # Track the new message
                self._autofill_dm_messages[user_id] = msg
            except (discord.Forbidden, Exception):
                # User has DMs disabled or other error - silently fail (no public messages)
                pass

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context) -> None:
        """
        Auto-delete certain prefix command messages (e.g. !play, !skip)
        after they successfully complete.

        - Only runs for prefix commands (not slash commands).
        - Only affects commands on this cog.
        - Only deletes if the bot has Manage Messages in that channel.
        """
        # Safety: sometimes this event can fire with non-Context or no command
        if not isinstance(ctx, commands.Context):
            return
        if ctx.command is None:
            return

        # Only care about commands on this cog
        if ctx.cog is not self:
            return

        # Only delete selected commands (play, skip, etc.)
        cmd_name = ctx.command.name
        if cmd_name not in AUTO_DELETE_COMMANDS:
            return

        # Grab the original message (prefix command)
        msg = getattr(ctx, "message", None)
        if not msg or not msg.guild:
            return

        # Ensure we actually have perms to delete
        me = msg.guild.me
        if me is None:
            return

        perms = msg.channel.permissions_for(me)
        if not perms.manage_messages:
            return

        try:
            await msg.delete()
        except (discord.Forbidden, discord.HTTPException):
            # Silently ignore if we can't delete for some reason
            return

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """
        Listen for voice state updates (users joining/leaving VC) and recalculate
        autofill queue if autofill is active. Includes debouncing to prevent rapid recalculations.
        """
        # Ignore bot's own voice state changes
        if member.bot:
            return
        
        # Check if the change affects a VC where the bot is connected
        guild = member.guild
        gid = guild.id
        
        # Get the voice channel from before or after
        vc_channel = None
        if before.channel:
            vc_channel = before.channel
        elif after.channel:
            vc_channel = after.channel
        
        if not vc_channel:
            return
        
        # Check if bot is in this voice channel
        try:
            bot_vc = guild.voice_client
            if not bot_vc or bot_vc.channel != vc_channel:
                return
        except Exception:
            return
        
        # Check if autofill is active (enabled AND has autofill songs)
        if not self._is_autofill_enabled(gid):
            return
        
        queue = self.queues.get(gid, deque())
        has_autofill = any(song.get("_autofill") for song in queue)
        if not has_autofill:
            return  # Only manual songs, no need to recalculate
        
        # Debounce: cancel existing timer if any
        existing_timer = self._autofill_recalc_timers.get(gid)
        if existing_timer and not existing_timer.done():
            existing_timer.cancel()
        
        # Create new debounced task (2 second delay)
        async def debounced_recalc():
            try:
                await asyncio.sleep(2.0)  # 2 second debounce
                await self._recalculate_autofill_queue(guild)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"[autofill recalc] failed: {e}")
            finally:
                self._autofill_recalc_timers[gid] = None
        
        self._autofill_recalc_timers[gid] = self.bot.loop.create_task(debounced_recalc())

async def setup(bot):
    await bot.add_cog(RadioBot(bot))