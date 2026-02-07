# src/utils/image_cache.py
"""
Image caching utility for song thumbnails.
Downloads images locally to avoid Discord embed loading failures from external URLs.
"""
import os
import re
import time
import tempfile
import hashlib
import requests
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional

# Configuration via environment variables
IMAGE_CACHE_DIR = os.getenv("IMAGE_CACHE_DIR", "images")
IMAGE_CACHE_SIZE = int(os.getenv("IMAGE_CACHE_SIZE", "50"))  # Max cached images
IMAGE_DOWNLOAD_TIMEOUT = int(os.getenv("IMAGE_DOWNLOAD_TIMEOUT", "10"))  # seconds

# Default fallback image filename (protected from cleanup)
DEFAULT_IMAGE_FILENAME = "default.jpg"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
}


def get_default_image_path() -> Optional[str]:
    """
    Get the path to the default fallback image if it exists.
    """
    default_path = os.path.join(IMAGE_CACHE_DIR, DEFAULT_IMAGE_FILENAME)
    if os.path.exists(default_path):
        return default_path
    return None


def _guess_image_ext(url: str, content_type: Optional[str]) -> str:
    """
    Determine image extension from URL or content-type.
    """
    # Try URL path first
    path = urlparse(url).path
    m = re.search(r"\.([A-Za-z0-9]{1,5})(?:$|\?)", path)
    if m:
        ext = m.group(1).lower()
        if ext in ("jpg", "jpeg", "png", "gif", "webp", "avif"):
            return "." + ext
    
    # Fallback to content-type
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        ext_map = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/avif": ".avif",
            "image/svg+xml": ".svg",
        }
        if ct in ext_map:
            return ext_map[ct]
    
    return ".jpg"  # Default to jpg for most album art


def _get_cache_path(url: str, song_id: Optional[str] = None) -> str:
    """
    Generate a consistent cache file path for an image URL.
    Uses song_id if available, otherwise hashes the URL.
    """
    os.makedirs(IMAGE_CACHE_DIR, exist_ok=True)
    
    if song_id:
        # Use song_id for predictable, readable filenames
        safe_id = re.sub(r"[^a-zA-Z0-9\-_]", "_", song_id)
        return os.path.join(IMAGE_CACHE_DIR, safe_id)
    else:
        # Hash the URL for a unique but stable filename
        url_hash = hashlib.md5(url.encode()).hexdigest()[:16]
        return os.path.join(IMAGE_CACHE_DIR, url_hash)


def get_cached_image(url: str, song_id: Optional[str] = None) -> Optional[str]:
    """
    Check if an image is already cached. Returns the path if found, None otherwise.
    Also updates the access time for cache management.
    """
    base_path = _get_cache_path(url, song_id)
    
    # Check for any image extension
    for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"):
        full_path = base_path + ext
        if os.path.exists(full_path):
            # Touch the file to update access time for LRU tracking
            try:
                Path(full_path).touch()
            except Exception:
                pass
            return full_path
    
    return None


def download_image(
    url: str,
    song_id: Optional[str] = None,
    *,
    timeout: int = IMAGE_DOWNLOAD_TIMEOUT,
    headers: Optional[dict] = None,
    referer: Optional[str] = None,
    use_default_on_fail: bool = True,
) -> Optional[str]:
    """
    Download an image to the local cache.
    
    Returns the local file path on success.
    If download fails and use_default_on_fail is True, returns the default image path.
    Uses atomic writes to prevent partial files.
    """
    # If no URL provided, return default image
    if not url or not url.startswith("http"):
        return get_default_image_path() if use_default_on_fail else None
    
    # Check if already cached
    cached = get_cached_image(url, song_id)
    if cached:
        return cached
    
    os.makedirs(IMAGE_CACHE_DIR, exist_ok=True)
    
    # Build headers
    hdrs = dict(DEFAULT_HEADERS)
    if headers:
        hdrs.update(headers)
    if referer:
        hdrs["Referer"] = referer
    
    tmp_path = None
    try:
        r = requests.get(url, headers=hdrs, stream=True, timeout=timeout)
        r.raise_for_status()
        
        # Get extension from response
        ext = _guess_image_ext(url, r.headers.get("Content-Type"))
        base_path = _get_cache_path(url, song_id)
        final_path = base_path + ext
        
        # Write to temp file first (atomic)
        fd, tmp_path = tempfile.mkstemp(dir=IMAGE_CACHE_DIR, suffix=ext + ".part")
        
        with os.fdopen(fd, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    f.write(chunk)
        
        # Atomic rename
        os.replace(tmp_path, final_path)
        
        # Enforce cache size limit
        _enforce_cache_limit()
        
        return final_path
    
    except Exception as e:
        # Clean up temp file on error
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        # Return default image on failure (404, timeout, etc.)
        return get_default_image_path() if use_default_on_fail else None


def _is_protected_file(filename: str) -> bool:
    """Check if a file should be protected from cleanup (e.g., default image)."""
    return filename == DEFAULT_IMAGE_FILENAME


def _enforce_cache_limit():
    """
    Remove oldest images if cache exceeds the size limit.
    Uses modification time for LRU-style eviction.
    Protects the default image from deletion.
    """
    try:
        if not os.path.exists(IMAGE_CACHE_DIR):
            return
        
        # Get all image files with their modification times (excluding protected files)
        image_files = []
        for f in os.listdir(IMAGE_CACHE_DIR):
            if f.endswith(".part") or _is_protected_file(f):
                continue
            full_path = os.path.join(IMAGE_CACHE_DIR, f)
            if os.path.isfile(full_path):
                try:
                    mtime = os.path.getmtime(full_path)
                    image_files.append((full_path, mtime))
                except Exception:
                    pass
        
        # If under limit, nothing to do
        if len(image_files) <= IMAGE_CACHE_SIZE:
            return
        
        # Sort by modification time (oldest first)
        image_files.sort(key=lambda x: x[1])
        
        # Remove oldest files until under limit
        to_remove = len(image_files) - IMAGE_CACHE_SIZE
        for path, _ in image_files[:to_remove]:
            try:
                os.remove(path)
            except Exception:
                pass
    
    except Exception:
        pass  # Cache cleanup is best-effort


def cleanup_cache():
    """
    Remove all cached images except the default image.
    Called on bot shutdown or explicit cleanup.
    """
    try:
        if not os.path.exists(IMAGE_CACHE_DIR):
            return
        
        for f in os.listdir(IMAGE_CACHE_DIR):
            # Skip protected files (like default.jpg)
            if _is_protected_file(f):
                continue
            try:
                full_path = os.path.join(IMAGE_CACHE_DIR, f)
                if os.path.isfile(full_path):
                    os.remove(full_path)
            except Exception:
                pass
    except Exception:
        pass


def cache_song_image(song: dict, use_default_on_fail: bool = True) -> Optional[str]:
    """
    Convenience function to cache a song's thumbnail.
    Extracts the image URL and song_id from the song dict.
    
    Returns the local path if successful, or default image path if use_default_on_fail is True.
    """
    # Get image URL from various possible fields
    image_url = (
        song.get("thumbnail") 
        or song.get("image_url") 
        or song.get("image") 
        or song.get("thumb")
    )
    
    # If no image URL, return default
    if not image_url:
        return get_default_image_path() if use_default_on_fail else None
    
    # Get song ID for consistent filenames
    song_id = song.get("song_id") or song.get("track_id") or song.get("_track_id")
    
    # Extract song_id from suno_url if not present
    if not song_id:
        suno_url = song.get("suno_url") or ""
        m = re.search(r"suno\.com/song/([a-f0-9\-]+)", suno_url, re.I)
        if m:
            song_id = m.group(1)
    
    # Download with suno referer for better success rate
    referer = song.get("suno_url") or "https://suno.com/"
    
    return download_image(image_url, song_id=song_id, referer=referer, use_default_on_fail=use_default_on_fail)
