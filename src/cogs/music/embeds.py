from __future__ import annotations
import os
import re
import time
import datetime
from pathlib import Path

import discord
from discord.utils import escape_markdown

from .constants import (
    EMBED_COLOR_PLAYING,
    EMBED_COLOR_ADDED,
    PLATFORM_COLORS,
    MAX_THUMBNAIL_SIZE_BYTES,
)

try:
    from src.utils.image_cache import cache_song_image, get_default_image_path
except ImportError:
    from src.utils import image_cache
    cache_song_image = image_cache.cache_song_image
    get_default_image_path = image_cache.get_default_image_path


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

    if source:
        if "soundcloud" in source:
            return PLATFORM_COLORS["soundcloud"]
        if "bandcamp" in source:
            return PLATFORM_COLORS["bandcamp"]
        if "mixcloud" in source:
            return PLATFORM_COLORS["mixcloud"]
        if "audiomack" in source:
            return PLATFORM_COLORS["audiomack"]
        return PLATFORM_COLORS["direct"]

    if "suno.ai" in url or "suno.com" in url or "suno.com" in suno_url:
        return PLATFORM_COLORS["suno"]

    if url.endswith((".mp3", ".wav", ".ogg", ".m4a", ".flac", ".opus")):
        return PLATFORM_COLORS["direct"]

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
    if track.get("suno_url"):
        return track["suno_url"]

    url = (track.get("url") or "").strip()
    if url.startswith("songs/") and url.endswith(".mp3"):
        song_id = url[6:-4]
        return f"https://suno.com/song/{song_id}"

    m = re.search(r"/([a-f0-9\-]{8,})\.mp3", url, re.I)
    if m:
        return f"https://suno.com/song/{m.group(1)}"

    page = track.get("page") or track.get("page_url")
    if page and "suno.com" in page:
        return page

    return None

def _canonical_track_id(track: dict) -> str | None:
    if track.get("id"):
        return str(track["id"])

    page = _derive_suno_url(track) or (track.get("url") or "")
    m = re.search(r"/song/([A-Za-z0-9\-]{8,})", page)
    if m:
        return m.group(1)

    url = str(track.get("url") or "")
    m = re.search(r"/([A-Fa-f0-9\-]{8,})\.mp3", url)
    if m:
        return m.group(1)
    if url.startswith("songs/") and url.endswith(".mp3"):
        return Path(url).stem

    return None

def _track_title_link(track: dict) -> str:
    title = escape_markdown((track.get("title") or "Untitled").strip())

    link = None
    if track.get("_original_url"):
        link = track["_original_url"]
    elif track.get("video_url") and not track.get("video_url", "").endswith((".mp3", ".mp4", ".webm")):
        link = track["video_url"]
    elif _derive_suno_url(track):
        link = _derive_suno_url(track)

    if link and any(domain in link for domain in ("suno.com", "soundcloud.com", "bandcamp.com", "mixcloud.com", "audiomack.com", "audius.co", "hearthis.at", "newgrounds.com")):
        return f"[**{title}**]({link})"
    return f"**{title}**"

def _artist_line(track: dict) -> str:
    artist = (track.get("artist") or track.get("author") or "Unknown").strip()
    return f"*by {escape_markdown(artist)}*"

def _filler_badge(track: dict) -> str:
    return " ⟳" if track.get("_autofill") else ""

def _prompt_text(track: dict) -> str:
    prompt = track.get("prompt") or ""
    return _truncate(prompt, 300)

def _thumb(track: dict) -> str | None:
    url = track.get("thumbnail") or track.get("thumb") or track.get("image") or track.get("image_url")
    if not url or not isinstance(url, str) or not url.startswith("http"):
        return url
    buster = f"v={int(time.time())}"
    return f"{url}&{buster}" if "?" in url else f"{url}?{buster}"


def _get_thumbnail_info(track: dict) -> tuple[str | None, str | None]:
    """
    Returns (thumbnail_url, local_file_path).
    Prefers locally cached images, falls back to remote URL, then default image.
    """
    local_path = track.get("local_thumbnail")
    if local_path and os.path.exists(local_path):
        filename = os.path.basename(local_path)
        return f"attachment://{filename}", local_path

    thumb_url = track.get("thumbnail") or track.get("thumb") or track.get("image") or track.get("image_url")
    if thumb_url and isinstance(thumb_url, str) and thumb_url.startswith("http"):
        try:
            local_path = cache_song_image(track, use_default_on_fail=True)
            if local_path and os.path.exists(local_path):
                track["local_thumbnail"] = local_path
                filename = os.path.basename(local_path)
                return f"attachment://{filename}", local_path
        except Exception:
            pass

        buster = f"v={int(time.time())}"
        url_with_buster = f"{thumb_url}&{buster}" if "?" in thumb_url else f"{thumb_url}?{buster}"
        return url_with_buster, None

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
        title = _track_title_link(t) + _filler_badge(t)
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
            parts.append("")
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
        cut = remaining.rfind("\n\n", 0, limit)
        if cut == -1:
            cut = remaining.rfind("\n", 0, limit)
        if cut == -1:
            cut = limit
        out.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        out.append(remaining)
    return out

def _scrape_playlist_to_tracks(playlist_url: str, limit: int = 0) -> list[dict]:
    from src.utils.playlist_fast_scraper import get_playlist_links_api
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


def build_now_playing_embed(track: dict, requester_mention: str | None, upcoming_tracks: list[dict] | None = None) -> tuple[discord.Embed, discord.File | None]:
    desc = [
        _track_title_link(track) + _filler_badge(track),
        _artist_line(track),
        ""
    ]
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

    thumb_file = None
    thumb_url, local_path = _get_thumbnail_info(track)
    if thumb_url:
        embed.set_thumbnail(url=thumb_url)
        if local_path:
            try:
                file_size = os.path.getsize(local_path)
                if file_size <= MAX_THUMBNAIL_SIZE_BYTES:
                    thumb_file = discord.File(local_path, filename=os.path.basename(local_path))
                else:
                    print(f"[thumbnail] Skipping local file ({file_size / 1024 / 1024:.1f}MB > 7MB limit): {local_path}")
                    fallback_thumb = _thumb(track)
                    if fallback_thumb:
                        embed.set_thumbnail(url=fallback_thumb)
            except Exception:
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
    desc = [
        _track_title_link(track) + _filler_badge(track),
        _artist_line(track),
        ""
    ]
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

    thumb_file = None
    thumb_url, local_path = _get_thumbnail_info(track)
    if thumb_url:
        embed.set_thumbnail(url=thumb_url)
        if local_path:
            try:
                file_size = os.path.getsize(local_path)
                if file_size <= MAX_THUMBNAIL_SIZE_BYTES:
                    thumb_file = discord.File(local_path, filename=os.path.basename(local_path))
                else:
                    print(f"[thumbnail] Skipping local file ({file_size / 1024 / 1024:.1f}MB > 7MB limit): {local_path}")
                    fallback_thumb = _thumb(track)
                    if fallback_thumb:
                        embed.set_thumbnail(url=fallback_thumb)
            except Exception:
                fallback_thumb = _thumb(track)
                if fallback_thumb:
                    embed.set_thumbnail(url=fallback_thumb)

    return embed, thumb_file


def _render_song_header(song: dict) -> str:
    title_raw = (song.get("title") or "Unknown Title").strip()
    title = escape_markdown(title_raw)
    link  = _derive_suno_url(song) or (song.get("url") or "").strip()

    artist_raw = (song.get("artist") or song.get("author") or "Unknown Artist").strip()
    artist = escape_markdown(artist_raw)

    if link and ("suno.com" in link):
        title_md = f"**[{title}]({link})**"
    else:
        title_md = f"**{title}**"

    byline_md = f"*By {artist}*"

    parts = [title_md, byline_md]

    date_model_parts = []

    created_at = song.get("created_at")
    if created_at:
        try:
            from datetime import datetime as _dt
            dt = _dt.fromisoformat(created_at.replace('Z', '+00:00'))
            formatted_date = dt.strftime("%B %d, %Y").replace(" 0", " ")
            date_model_parts.append(formatted_date)
        except (ValueError, AttributeError):
            pass

    model_version = song.get("major_model_version")
    model_name = song.get("model_name")
    if model_version or model_name:
        model_parts = []
        if model_version:
            v = model_version.lstrip('vV')
            model_parts.append(f"v{v}")
        if model_name:
            model_parts.append(model_name)
        if model_parts:
            date_model_parts.append(f"({' '.join(model_parts)})")

    if date_model_parts:
        parts.append(" ".join(date_model_parts))

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
    parts.append("")
    parts.append("**Lyrics**")
    parts.append(lyrics if lyrics else "_No lyrics provided._")

    return "\n".join(parts).strip()

def build_song_info_embed(song: dict, include_lyrics: bool = True) -> tuple[discord.Embed, discord.File | None]:
    song = song.copy()

    header = _render_song_header(song)

    if include_lyrics:
        prompt_lyrics = _render_prompt_lyrics_block(song)

        combined_length = len(header) + len("\n\n") + len(prompt_lyrics)
        if combined_length <= 4000:
            description_parts = [header, "", prompt_lyrics]
            embed = discord.Embed(
                title="📝 Song Information",
                description="\n".join(description_parts),
                color=EMBED_COLOR_PLAYING
            )
        else:
            embed = discord.Embed(
                title="📝 Song Information",
                description=header,
                color=EMBED_COLOR_PLAYING
            )
            prompt = (song.get("prompt") or "").strip()
            lyrics = (song.get("lyrics") or "").strip()

            if prompt:
                prompt_chunks = _chunk_text(prompt, limit=1020)
                for i, chunk in enumerate(prompt_chunks):
                    embed.add_field(
                        name="**Prompt**" if i == 0 else "",
                        value=chunk,
                        inline=False
                    )
            else:
                embed.add_field(name="**Prompt**", value="_No prompt provided._", inline=False)

            if lyrics:
                lyrics_chunks = _chunk_text(lyrics, limit=1020)
                for i, chunk in enumerate(lyrics_chunks):
                    embed.add_field(
                        name="**Lyrics**" if i == 0 else "",
                        value=chunk,
                        inline=False
                    )
            else:
                embed.add_field(name="**Lyrics**", value="_No lyrics provided._", inline=False)
    else:
        embed = discord.Embed(
            title="📝 Song Information",
            description=header,
            color=EMBED_COLOR_PLAYING
        )

    duration = song.get("duration")
    if duration:
        embed.add_field(name="Duration", value=_fmt_duration(duration), inline=True)

    thumb_file = None
    thumb_url, local_path = _get_thumbnail_info(song)
    if thumb_url:
        embed.set_thumbnail(url=thumb_url)
        if local_path:
            try:
                file_size = os.path.getsize(local_path) if os.path.exists(local_path) else 0
                if file_size <= MAX_THUMBNAIL_SIZE_BYTES:
                    thumb_file = discord.File(local_path, filename=os.path.basename(local_path))
                else:
                    fallback_thumb = _thumb(song)
                    if fallback_thumb:
                        embed.set_thumbnail(url=fallback_thumb)
            except Exception:
                fallback_thumb = _thumb(song)
                if fallback_thumb:
                    embed.set_thumbnail(url=fallback_thumb)

    embed.set_footer(text="Suno Radio")
    embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return embed, thumb_file
