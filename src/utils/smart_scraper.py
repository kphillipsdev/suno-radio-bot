"""
Suno song metadata via the public clip API.

Uses https://studio-api.prod.suno.com/api/clip/{id} — no HTML scraping.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

SUNO_API_BASE = "https://studio-api.prod.suno.com/api/clip"
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def fix_utf8_encoding(text: str) -> str:
    """Fix common UTF-8 mojibake in Suno text fields."""
    if not text:
        return text

    replacements = {
        "\u00e2\u20ac\u201d": "\u2014",
        "\u00e2\u20ac\u201c": "\u2013",
        "\u00e2\u20ac\u2122": "\u2019",
        "\u00e2\u20ac\u02dc": "\u2018",
        "\u00e2\u20ac\u0153": "\u201c",
        "\u00e2\u20ac\u009d": "\u201d",
        "\u00e2\u20ac\u00a6": "\u2026",
        "\u00e2\u2020\u2019": "\u2192",
        "\u00c3\u00a9": "\u00e9",
        "\u00c3\u00a8": "\u00e8",
        "\u00c3\u00a0": "\u00e0",
        "\u00c3\u00af": "\u00ef",
        "\u00c3\u00b4": "\u00f4",
        "\u00c3\u00a2": "\u00e2",
        "\u00c3\u00a7": "\u00e7",
        "\u00c3\u00bc": "\u00fc",
        "\u00c3\u00b6": "\u00f6",
        "\u00c3\u00a4": "\u00e4",
        "\u00c3\u0178": "\u00df",
        "\u00c3\u00b1": "\u00f1",
        "\u00e2": "\u2014",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    return text


def extract_song_id(url: str) -> Optional[str]:
    """
    Extract a Suno song UUID from a URL.

    Handles:
    - https://suno.com/song/<uuid>
    - https://suno.com/s/<short> (follows redirect)
    - bare UUIDs / CDN URLs that embed the UUID
    """
    if not url:
        return None

    match = _UUID_RE.search(url)
    if match:
        return match.group(0)

    if "/s/" in url and "suno.com" in url:
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            response = requests.head(url, headers=headers, allow_redirects=True, timeout=10)
            match = _UUID_RE.search(response.url or "")
            if match:
                return match.group(0)
        except Exception:
            pass

    return None


def fetch_song_data(song_id: str) -> Optional[Dict]:
    """Fetch clip JSON from the Suno studio API."""
    try:
        url = f"{SUNO_API_BASE}/{song_id}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = requests.get(url, headers=headers, timeout=30)

        if response.status_code == 200:
            return response.json()

        logger.warning("API returned status %s for song %s", response.status_code, song_id)
        return None
    except Exception as e:
        logger.error("Error fetching song data: %s", e)
        return None


def clip_to_song_info(data: Dict, *, original_url: Optional[str] = None) -> Dict:
    """Map a clip API payload into the dict shape used by extract_song_info / the bot."""
    song_id = data.get("id") or extract_song_id(original_url or "")
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    title = data.get("title") or "Unknown Title"
    if isinstance(title, str):
        title = fix_utf8_encoding(title.strip()) or "Unknown Title"

    artist = data.get("display_name") or data.get("handle")
    if isinstance(artist, str):
        artist = fix_utf8_encoding(artist.strip()) or None

    lyrics = metadata.get("prompt") or ""
    lyrics = fix_utf8_encoding(lyrics) if lyrics else None

    prompt = metadata.get("tags") or data.get("display_tags") or ""
    prompt = fix_utf8_encoding(prompt) if prompt else None

    duration = None
    raw_dur = metadata.get("duration")
    if raw_dur is not None:
        try:
            duration = int(float(raw_dur))
        except (TypeError, ValueError):
            duration = None

    audio_url = (data.get("audio_url") or "").strip() or None
    if not audio_url and song_id:
        audio_url = f"https://cdn1.suno.ai/{song_id}.mp3"

    image_url = data.get("image_large_url") or data.get("image_url")
    thumbnail = image_url
    video_url = data.get("video_url") or None
    if isinstance(video_url, str) and not video_url.strip():
        video_url = None

    created_at = data.get("created_at")
    suno_url = f"https://suno.com/song/{song_id}" if song_id else original_url

    return {
        "title": title,
        "url": audio_url,
        "duration": duration,
        "date": created_at,
        "artist": artist,
        "suno_url": suno_url,
        "thumbnail": thumbnail,
        "local_thumbnail": None,  # filled by caller via image cache
        "video_url": video_url,
        "prompt": prompt,
        "lyrics": lyrics,
        "image_url": image_url,
        "major_model_version": data.get("major_model_version") or None,
        "model_name": data.get("model_name") or None,
        "play_count": data.get("play_count"),
        "like_count": data.get("upvote_count"),
        "created_at": created_at,
        "handle": data.get("handle") or None,
        "song_id": song_id,
    }


def fetch_suno_song_info(url: str) -> Dict:
    """
    Resolve a Suno song URL (or UUID) via the clip API.

    Raises ValueError if the song id or API payload cannot be obtained.
    """
    song_id = extract_song_id(url)
    if not song_id:
        raise ValueError(f"Could not extract Suno song id from: {url}")

    data = fetch_song_data(song_id)
    if not data:
        raise ValueError(f"Suno API returned no data for song {song_id}")

    info = clip_to_song_info(data, original_url=url)
    if not info.get("url"):
        raise ValueError(f"Suno API response missing audio URL for song {song_id}")
    return info


def scrape_suno_song(file_path_or_url: str, debug: bool = False) -> Dict[str, Optional[str]]:
    """CLI/helper wrapper returning a compact field set from the clip API."""
    result = {
        "lyrics": None,
        "style_prompt": None,
        "image_url": None,
        "model_version": None,
        "model_name": None,
        "play_count": None,
        "like_count": None,
        "created_at": None,
    }

    try:
        info = fetch_suno_song_info(file_path_or_url)
    except Exception as e:
        logger.error("Error scraping song: %s", e)
        if debug:
            import traceback
            traceback.print_exc()
        return result

    result["lyrics"] = info.get("lyrics")
    result["style_prompt"] = info.get("prompt")
    result["image_url"] = info.get("image_url")
    result["model_name"] = info.get("model_name") or "Not found"
    major = info.get("major_model_version")
    result["model_version"] = f"v{major}" if major and not str(major).startswith("v") else (major or "Not found")
    result["play_count"] = info.get("play_count")
    result["like_count"] = info.get("like_count")
    result["created_at"] = info.get("created_at")

    if debug:
        print(
            f"Extracted: lyrics={len(result['lyrics'] or '')} chars, "
            f"style={len(result['style_prompt'] or '')} chars"
        )
    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m src.utils.smart_scraper <suno_url>")
        print("Example: python -m src.utils.smart_scraper https://suno.com/song/abc123...")
        sys.exit(1)

    url = sys.argv[1]
    debug_mode = "--debug" in sys.argv
    result = scrape_suno_song(url, debug=debug_mode)

    print("\n" + "=" * 50)
    print("LYRICS:")
    print("=" * 50)
    print(result["lyrics"] or "Not found")

    print("\n" + "=" * 50)
    print("STYLE PROMPT:")
    print("=" * 50)
    print(result["style_prompt"] or "Not found")

    print("\n" + "=" * 50)
    print("ADDITIONAL INFO:")
    print("=" * 50)
    print(f"Image URL: {result['image_url'] or 'Not found'}")
    print(f"Model Version: {result['model_version'] or 'Not found'}")
    print(f"Model Name: {result['model_name'] or 'Not found'}")
    print(f"Play Count: {result['play_count'] or 'Not found'}")
    print(f"Like Count: {result['like_count'] or 'Not found'}")
    print(f"Created At: {result['created_at'] or 'Not found'}")
    print("=" * 50)
