# src/utils/ytdlp_extractor.py
"""
yt-dlp based extractor for platforms like SoundCloud, Bandcamp, Mixcloud, etc.
This is separate from the Suno extractor to keep concerns isolated.
"""

import os
import hashlib
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy import to avoid startup cost if not used
_yt_dlp = None

# Directory for downloaded HLS files (tracks that can't be streamed directly)
YTDLP_DOWNLOAD_DIR = os.getenv("YTDLP_DOWNLOAD_DIR", "songs")


class YtDlpExtractionError(Exception):
    """Raised when yt-dlp extraction fails for a reason the user should see."""
    pass


def _friendly_ytdlp_error(url: str, exc: BaseException) -> str:
    """Translate noisy yt-dlp errors into messages safe to show to a Discord user."""
    raw = str(exc)
    lowered = raw.lower()
    is_soundcloud = "soundcloud.com" in url.lower()

    # SoundCloud-specific: format-info JSON 404 / no formats found.
    # These both mean the track is protected (Go+ / all-rights-reserved) or
    # otherwise not streamable without account cookies.
    if is_soundcloud and (
        "unable to download json metadata" in lowered
        or "no video formats found" in lowered
        or "no streamable" in lowered
    ):
        return (
            "This SoundCloud track has no free stream available "
            "(likely Go+ / all-rights-reserved or region-locked, so the bot "
            "cannot play it without account cookies)"
        )

    if "private video" in lowered or "private track" in lowered:
        return "This track is marked private and cannot be played"

    if "geo" in lowered and ("restrict" in lowered or "block" in lowered):
        return "This track is geo-restricted and cannot be played from this server"

    # Fall back to the raw error, trimmed to something sensible for a Discord
    # error embed (Discord caps descriptions at 4096 chars but shorter is nicer).
    raw = raw.replace("\n", " ").strip().rstrip(".")
    if len(raw) > 300:
        raw = raw[:297] + "..."
    return raw

def _get_ytdlp():
    """Lazy load yt-dlp module."""
    global _yt_dlp
    if _yt_dlp is None:
        import yt_dlp
        _yt_dlp = yt_dlp
    return _yt_dlp


def _download_hls_track(url: str, info: dict) -> Optional[str]:
    """
    Download an HLS track using yt-dlp when direct streaming isn't possible.
    Returns the local file path on success, None on failure.
    """
    yt_dlp = _get_ytdlp()
    os.makedirs(YTDLP_DOWNLOAD_DIR, exist_ok=True)
    
    # Generate a unique filename based on the URL
    url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
    output_template = os.path.join(YTDLP_DOWNLOAD_DIR, f"ytdlp_{url_hash}.%(ext)s")
    
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "hls_mp3_1_0/bestaudio",  # Prefer MP3 HLS format
        "outtmpl": output_template,
        "geo_bypass": True,
    }
    
    try:
        logger.info("Downloading HLS track: %s (%s)", info.get("title", "?"), url)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        for ext in (".mp3", ".m4a", ".opus", ".webm", ".ogg"):
            expected_path = os.path.join(YTDLP_DOWNLOAD_DIR, f"ytdlp_{url_hash}{ext}")
            if os.path.exists(expected_path) and os.path.getsize(expected_path) > 0:
                logger.info("Downloaded HLS track to %s", expected_path)
                return expected_path

        logger.warning("HLS download completed but no output file found for %s", url)
        return None

    except Exception as e:
        logger.warning("HLS download failed for %s: %s", url, e)
        return None


def is_ytdlp_supported_url(url: str) -> bool:
    """
    Check if URL looks like it could be handled by yt-dlp.
    Returns True for known platforms, False for Suno/direct media.
    """
    url_lower = url.lower()
    
    # Exclude Suno URLs (handled by main extractor)
    if "suno.com" in url_lower or "suno.ai" in url_lower:
        return False
    
    # Exclude direct media files (handled by main extractor)
    media_extensions = (
        '.mp3', '.wav', '.ogg', '.m4a', '.flac', '.aac', '.opus',
        '.mp4', '.webm', '.mkv', '.avi', '.mov'
    )
    if any(url_lower.endswith(ext) for ext in media_extensions):
        return False
    
    # Known supported platforms (not exhaustive, yt-dlp supports 1000+)
    supported_domains = (
        "soundcloud.com",
        "bandcamp.com",
        "mixcloud.com",
        "audiomack.com",
        "audius.co",
        "hearthis.at",
        "newgrounds.com",  # audio portal
    )
    
    return any(domain in url_lower for domain in supported_domains)


def extract_with_ytdlp(url: str, download_thumbnail: bool = True) -> Optional[dict]:
    """
    Extract audio info from URLs supported by yt-dlp.
    
    Args:
        url: The URL to extract from
        download_thumbnail: Whether to download and cache the thumbnail locally
        
    Returns:
        Dict with song info compatible with the bot's format, or None if extraction fails.
    """
    yt_dlp = _get_ytdlp()
    
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        # Prefer direct HTTP streams over HLS (m3u8) for better compatibility with FFmpeg
        # Format preference: http_mp3 > http audio > any audio > best
        "format": "http_mp3_1_0/bestaudio[protocol=http]/bestaudio[protocol=https]/bestaudio/best",
        "skip_download": True,
        # Avoid geo-restrictions where possible
        "geo_bypass": True,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise YtDlpExtractionError(f"yt-dlp returned no metadata for {url}")

            audio_url = info.get("url")

            # If the selected URL is HLS, try to find a direct HTTP alternative
            if audio_url and (".m3u8" in audio_url or info.get("protocol") == "m3u8_native"):
                formats = info.get("formats", [])
                http_formats = [
                    f for f in formats
                    if f.get("acodec") != "none" and f.get("protocol") in ("http", "https")
                ]
                if http_formats:
                    http_formats.sort(key=lambda f: f.get("abr") or f.get("tbr") or 0, reverse=True)
                    audio_url = http_formats[0].get("url")
                    logger.info("Switched from HLS to HTTP format for %s", url)

            if not audio_url:
                formats = info.get("formats", [])
                audio_formats = [f for f in formats if f.get("acodec") != "none"]
                http_formats = [f for f in audio_formats if f.get("protocol") in ("http", "https")]
                hls_formats = [f for f in audio_formats if f.get("protocol") not in ("http", "https")]

                preferred_formats = http_formats if http_formats else hls_formats
                if preferred_formats:
                    preferred_formats.sort(key=lambda f: f.get("abr") or f.get("tbr") or 0, reverse=True)
                    audio_url = preferred_formats[0].get("url")

            if not audio_url:
                # No streamable URL at all -- usually means the track is protected
                # (e.g. SoundCloud "all-rights-reserved" / Go+ only, geo-blocked, etc.)
                formats_count = len(info.get("formats") or [])
                logger.warning(
                    "No streamable audio URL for %s (formats=%d, title=%r, license=%r)",
                    url, formats_count, info.get("title"), info.get("license"),
                )
                hint = ""
                if "soundcloud.com" in url.lower():
                    hint = (
                        " This SoundCloud track does not expose a free stream "
                        "(likely Go+ / all-rights-reserved or region-locked)."
                    )
                raise YtDlpExtractionError(
                    f"No streamable audio formats available for this track.{hint}"
                )

            # Detect HLS-only tracks (common for major label content on SoundCloud)
            # that FFmpeg can't stream directly and require downloading first.
            is_hls_only = ".m3u8" in audio_url or info.get("protocol") == "m3u8_native"
            downloaded_file = None

            if is_hls_only:
                formats = info.get("formats", [])
                has_http = any(
                    f.get("protocol") in ("http", "https") and f.get("acodec") != "none"
                    for f in formats
                )

                if not has_http:
                    logger.info("Track only has HLS streams, downloading first: %s", url)
                    downloaded_file = _download_hls_track(url, info)
                    if downloaded_file:
                        audio_url = downloaded_file
                    else:
                        raise YtDlpExtractionError(
                            "Failed to download HLS-only track (no direct stream available)."
                        )
            
            # Extract metadata
            title = info.get("title") or info.get("track") or "Unknown Title"
            artist = (
                info.get("artist") or 
                info.get("uploader") or 
                info.get("creator") or
                info.get("channel")
            )
            
            duration = info.get("duration")
            if duration:
                duration = int(duration)
            
            thumbnail = info.get("thumbnail")
            
            # Download and cache thumbnail if requested
            local_thumbnail = None
            if download_thumbnail and thumbnail:
                local_thumbnail = _download_thumbnail(thumbnail, url)
            
            # Determine the source platform
            extractor = info.get("extractor_key") or info.get("extractor") or "yt-dlp"
            
            return {
                "title": title,
                "url": audio_url,
                "duration": duration,
                "date": info.get("upload_date"),
                "artist": artist,
                "suno_url": None,  # Not a Suno track
                "thumbnail": thumbnail,
                "local_thumbnail": local_thumbnail,
                "_downloaded_file": downloaded_file,  # Track if we downloaded for cleanup
                "video_url": info.get("webpage_url") or url,  # Link back to original page
                "prompt": None,  # Not applicable
                "lyrics": None,  # Could potentially extract from some platforms
                "image_url": thumbnail,
                "major_model_version": None,
                "model_name": None,
                "play_count": info.get("view_count"),
                "like_count": info.get("like_count"),
                "created_at": info.get("upload_date"),
                # Extra fields for non-Suno sources
                "_source": extractor,
                "_original_url": url,
                "_album": info.get("album"),
                "_genre": info.get("genre"),
            }
            
    except YtDlpExtractionError:
        raise
    except Exception as e:
        logger.error("yt-dlp extraction failed for %s: %s", url, e)
        raise YtDlpExtractionError(_friendly_ytdlp_error(url, e)) from e


def _download_thumbnail(thumbnail_url: str, source_url: str) -> Optional[str]:
    """Download and cache thumbnail image locally."""
    try:
        # Import the image cache utility from the existing codebase
        from src.utils.image_cache import download_image
        
        # Generate a unique ID based on the source URL
        url_hash = hashlib.md5(source_url.encode()).hexdigest()[:12]
        
        return download_image(
            thumbnail_url,
            song_id=f"ytdlp_{url_hash}",
            referer=source_url
        )
    except Exception as e:
        logger.warning("Thumbnail download failed for %s: %s", source_url, e)
        return None


