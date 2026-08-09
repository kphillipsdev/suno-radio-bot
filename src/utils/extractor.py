import os
import json
import logging
import subprocess
import requests
import urllib.parse
from typing import Optional, Dict

logger = logging.getLogger(__name__)

from src.utils.smart_scraper import extract_song_id, fetch_suno_song_info

# yt-dlp based extractor for SoundCloud, Bandcamp, etc.
from src.utils.ytdlp_extractor import (
    extract_with_ytdlp,
    is_ytdlp_supported_url,
    YtDlpExtractionError,
)

# Import image caching utility
from src.utils.image_cache import download_image

# =========================
# Duration helpers
# =========================
def _ffprobe_duration(url_or_path: str, headers: Dict | None = None, timeout: int = 7) -> Optional[int]:
    try:
        hdr_str = None
        if headers:
            hdr_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json"]
        if hdr_str:
            cmd += ["-headers", hdr_str]
        cmd.append(url_or_path)
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=timeout)
        data = json.loads(out.stdout or b"{}")
        dur = data.get("format", {}).get("duration")
        if dur is not None:
            return int(float(dur))
    except Exception:
        pass
    return None

def _ffprobe_metadata(url_or_path: str, headers: Dict | None = None, timeout: int = 10) -> Dict:
    """Extract duration and ID3/metadata tags from a media file using ffprobe."""
    result = {"duration": None, "title": None, "artist": None, "album": None, "has_cover": False}
    try:
        hdr_str = None
        if headers:
            hdr_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:format_tags=title,artist,album,album_artist:stream=index,codec_type:stream_disposition=attached_pic",
            "-of", "json"
        ]
        if hdr_str:
            cmd += ["-headers", hdr_str]
        cmd.append(url_or_path)
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=timeout)
        data = json.loads(out.stdout or b"{}")
        
        fmt = data.get("format", {})
        dur = fmt.get("duration")
        if dur is not None:
            result["duration"] = int(float(dur))
        
        tags = fmt.get("tags", {})
        # ID3 tags can be case-insensitive, so check common variations
        result["title"] = tags.get("title") or tags.get("TITLE")
        result["artist"] = tags.get("artist") or tags.get("ARTIST") or tags.get("album_artist") or tags.get("ALBUM_ARTIST")
        result["album"] = tags.get("album") or tags.get("ALBUM")
        
        # Check for embedded cover art (attached_pic stream)
        streams = data.get("streams", [])
        for stream in streams:
            if stream.get("disposition", {}).get("attached_pic") == 1:
                result["has_cover"] = True
                break
    except Exception:
        pass
    return result

def _extract_embedded_cover(url_or_path: str, output_dir: str = "images", timeout: int = 15) -> Optional[str]:
    """Extract embedded album art from a media file using ffmpeg."""
    try:
        os.makedirs(output_dir, exist_ok=True)
        # Generate a unique filename based on the source path/url
        import hashlib
        url_hash = hashlib.md5(url_or_path.encode()).hexdigest()[:12]
        output_path = os.path.join(output_dir, f"cover_{url_hash}.jpg")
        
        # Skip if already extracted
        if os.path.exists(output_path):
            return output_path
        
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-i", url_or_path,
            "-an",  # No audio
            "-vcodec", "mjpeg",  # Output as JPEG
            "-frames:v", "1",  # Only one frame (the cover)
            output_path
        ]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=timeout)
        
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return output_path
    except Exception:
        pass
    return None

def _yt_dlp_probe_duration(audio_url: str) -> Optional[int]:
    try:
        import yt_dlp
        ydl_opts = {"quiet": True, "no_warnings": True, "force_generic_extractor": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(audio_url, download=False)
            d = info.get("duration")
            return int(d) if d else None
    except Exception:
        return None

# =========================
# URL normalization
# =========================

def _normalize_suno_short(url: str) -> str:
    """suno.com/s/<short> -> suno.com/song/<uuid> (follow redirect)."""
    try:
        if "suno.com/s/" in url and "suno.com/song/" not in url:
            r = requests.head(url, allow_redirects=True, timeout=10)
            if r.url:
                return r.url
    except Exception:
        pass
    return url

# =========================
# Direct media URL detection
# =========================

def _is_direct_media_url(url: str) -> bool:
    """Check if URL appears to be a direct media file."""
    # Common audio/video extensions
    media_extensions = (
        '.mp3', '.wav', '.ogg', '.m4a', '.flac', '.aac', '.opus',
        '.mp4', '.webm', '.mkv', '.avi', '.mov'
    )
    parsed = urllib.parse.urlparse(url.lower())
    path = parsed.path
    return any(path.endswith(ext) for ext in media_extensions)

def _extract_direct_media_info(url: str) -> dict:
    """Extract info from a direct audio/video URL."""
    # Extract filename from URL for fallback title
    parsed = urllib.parse.urlparse(url)
    filename = os.path.basename(parsed.path)
    fallback_title = os.path.splitext(filename)[0] or "Direct Audio"
    # URL decode the title to handle percent-encoded characters
    fallback_title = urllib.parse.unquote(fallback_title)
    
    # Probe metadata (duration, title, artist, album, has_cover) with ffprobe
    metadata = _ffprobe_metadata(url)
    
    # Use metadata title if available, otherwise fall back to filename
    title = metadata.get("title") or fallback_title
    artist = metadata.get("artist")
    duration = metadata.get("duration")
    
    # Extract embedded cover art if available
    local_thumbnail = None
    if metadata.get("has_cover"):
        local_thumbnail = _extract_embedded_cover(url)
    
    return {
        "title": title,
        "url": url,
        "duration": duration,
        "date": None,
        "artist": artist,
        "suno_url": None,
        "thumbnail": None,
        "local_thumbnail": local_thumbnail,
        "video_url": None,
        "prompt": None,
        "lyrics": None,
        "image_url": None,
        "major_model_version": None,
        "model_name": None,
        "play_count": None,
        "like_count": None,
        "created_at": None,
    }

# =========================
# Main extraction
# =========================

def extract_song_info(url: str) -> dict:
    """Extract rich song metadata and a playable audio URL (Suno via clip API)."""
    os.makedirs("songs", exist_ok=True)

    # Handle direct media URLs first (mp3, wav, ogg, m4a, flac, mp4, webm, etc.)
    if _is_direct_media_url(url):
        return _extract_direct_media_info(url)

    url = _normalize_suno_short(url)

    # Suno: studio clip API only
    song_id = extract_song_id(url)
    if song_id or "suno.com" in (url or "").lower():
        try:
            info = fetch_suno_song_info(url)
            song_id = info.get("song_id") or song_id

            # If API omitted duration, probe the audio as a last resort
            if info.get("duration") is None and info.get("url"):
                try:
                    headers_ff = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Referer": info.get("suno_url") or "https://suno.com/",
                        "Accept": "*/*",
                    }
                    info["duration"] = _ffprobe_duration(info["url"], headers=headers_ff)
                except Exception:
                    pass
                if info.get("duration") is None:
                    try:
                        info["duration"] = _yt_dlp_probe_duration(info["url"])
                    except Exception:
                        pass

            thumbnail = info.get("thumbnail") or info.get("image_url")
            if thumbnail:
                try:
                    info["local_thumbnail"] = download_image(
                        thumbnail,
                        song_id=song_id,
                        referer=info.get("suno_url") or "https://suno.com/",
                    )
                except Exception:
                    pass

            # Drop helper-only keys before returning to callers
            info.pop("song_id", None)
            info.pop("handle", None)
            return info

        except Exception as e:
            logger.error("Suno API extraction failed for %s: %s", url, e)
            raise

    # Try yt-dlp for other platforms (SoundCloud, Bandcamp, Mixcloud, etc.)
    if is_ytdlp_supported_url(url):
        # extract_with_ytdlp now raises YtDlpExtractionError on failure with a
        # human-readable message, which we let propagate up to the caller so
        # the user sees the real reason instead of "Unsupported URL format".
        result = extract_with_ytdlp(url)
        if result:
            return result
        # Shouldn't happen anymore, but keep the safety net.
        raise YtDlpExtractionError(f"yt-dlp returned no result for {url}")

    # Truly unrecognized URL
    raise ValueError(f"Unsupported URL format: {url}")