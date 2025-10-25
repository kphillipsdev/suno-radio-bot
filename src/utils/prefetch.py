# src/utils/prefetch.py
import os
import re
import tempfile
import requests
from urllib.parse import urlparse

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "*/*",
}

def _guess_ext(url: str, content_type: str | None) -> str:
    """
    Prefer extension from URL; fallback to content-type; default .bin.
    """
    path = urlparse(url).path
    m = re.search(r"\.([A-Za-z0-9]{1,5})(?:$|\?)", path)
    if m:
        return "." + m.group(1).lower()

    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        if ct == "audio/mpeg": return ".mp3"
        if ct == "audio/ogg":  return ".ogg"
        if ct in ("audio/aac", "audio/mp4"): return ".m4a"
        if ct == "audio/wav":  return ".wav"
        if ct == "audio/flac": return ".flac"
    return ".bin"


def prefetch_to_file(
    url: str,
    out_dir: str = "songs",
    *,
    timeout: int = 25,
    headers: dict | None = None,
    referer: str | None = None,
    full_download: bool = True,
    max_bytes: int | None = None,
) -> str | None:
    """
    Stream a remote URL to a local file using an atomic write.

    - full_download=True (default): fetch the entire file and return the final path.
    - full_download=False AND max_bytes set: fetch ONLY up to max_bytes, then delete
      the partial and return None (used for 'warmup' priming; not for playback).

    Never leaves a '.part' file behind on errors. Returns final file path (str) on success
    for full downloads, else None.
    """
    os.makedirs(out_dir, exist_ok=True)

    hdrs = dict(DEFAULT_HEADERS)
    if headers:
        hdrs.update(headers)
    if referer:
        hdrs["Referer"] = referer

    r = None
    fd = None
    tmp_path = None
    try:
        r = requests.get(url, headers=hdrs, stream=True, timeout=timeout)
        r.raise_for_status()

        ext = _guess_ext(url, r.headers.get("Content-Type"))
        fd, tmp_path = tempfile.mkstemp(dir=out_dir, suffix=ext + ".part")
        written = 0

        with os.fdopen(fd, "wb") as f:
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
                if not full_download and max_bytes and written >= max_bytes:
                    break

        # Warmup path: delete partial and return None
        if not full_download and max_bytes:
            try:
                if tmp_path and os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            return None

        # Commit atomically (drop ".part")
        final_path = tmp_path[:-5] if tmp_path and tmp_path.endswith(".part") else tmp_path
        os.replace(tmp_path, final_path)
        return final_path

    except Exception:
        # Ensure we never leave a partial behind
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return None


def prefetch_warmup(
    url: str,
    *,
    timeout: int = 10,
    bytes_to_read: int = 524288,
    headers: dict | None = None,
    referer: str | None = None,
) -> None:
    """
    Convenience wrapper for priming a remote stream without keeping a file.
    Reads up to `bytes_to_read` and discards the result. Always returns None.
    """
    prefetch_to_file(
        url,
        out_dir=tempfile.gettempdir(),
        timeout=timeout,
        headers=headers,
        referer=referer,
        full_download=False,
        max_bytes=max(1, int(bytes_to_read)),
    )
    # Intentionally return None — warmup should not yield a playable path.
