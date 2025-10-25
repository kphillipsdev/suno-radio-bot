import os
import re
import html
import json
import subprocess
import requests
from typing import Optional, Dict, Tuple, List
from bs4 import BeautifulSoup

# =========================
# Small text/HTML helpers
# =========================

def _collapse_ws(s: str) -> str:
    return re.sub(r"[ \t\u00A0]+", " ", (s or "")).strip()

def _normalize_newlines(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def _text_with_breaks(el) -> str:
    parts = []
    def walk(node):
        name = getattr(node, "name", None)
        if name is None:
            parts.append(str(node))
            return
        if name.lower() == "br":
            parts.append("\n")
        elif name.lower() in {"p", "div", "section", "article", "li", "pre"}:
            for c in node.children:
                walk(c)
            parts.append("\n")
        else:
            for c in node.children:
                walk(c)
    walk(el)
    s = "".join(parts)
    s = html.unescape(s)
    s = "\n".join(line.rstrip() for line in s.split("\n"))
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

# =========================
# Duration helpers
# =========================

def _iso8601_to_seconds(s: str) -> Optional[int]:
    if not s:
        return None
    s = s.strip().upper()
    m = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<mins>\d+)M)?(?:(?P<secs>\d+)S)?)?",
        s,
    )
    if not m:
        return None
    days = int(m.group("days") or 0)
    hours = int(m.group("hours") or 0)
    mins = int(m.group("mins") or 0)
    secs = int(m.group("secs") or 0)
    return days * 86400 + hours * 3600 + mins * 60 + secs

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
# Static (requests + BS4 + JSON scraping)
# =========================

# Looser URL finds in inline JSON/HTML (kept conservative)
_SONG_URL_RE = re.compile(r"https?://(?:www\.)?suno\.com/song/([a-f0-9\-]{8,})", re.I)
_AUDIO_RE = re.compile(r"https?://(?:cdn\d?|static)\.suno\.ai/[A-Za-z0-9\-_]+\.mp3", re.I)

def _safe_json_loads(s: str):
    try:
        return json.loads(s)
    except Exception:
        return None

def _extract_from_ld_json(soup: BeautifulSoup) -> dict:
    out = {"duration": None}
    for script in soup.find_all("script", type="application/ld+json"):
        data = _safe_json_loads(script.string or "")
        if not data:
            continue
        objs = data if isinstance(data, list) else [data]
        for obj in objs:
            d = obj.get("duration")
            sec = _iso8601_to_seconds(d) if d else None
            if sec:
                out["duration"] = sec
    return out

def _extract_prompt_lyrics_from_dom(soup: BeautifulSoup) -> Tuple[Optional[str], Optional[str]]:
    # Your previous SSR heuristics, kept as-is
    prompt = None
    pr = soup.select_one("div.my-2 div[title]")
    if pr:
        prompt = pr.get("title") or pr.get_text(strip=True)
    prompt = _normalize_newlines(prompt)

    lyrics = None
    lyr = soup.select_one("textarea.custom-textarea") or soup.select_one("p.whitespace-pre-wrap")
    if lyr:
        if lyr.name == "textarea":
            lyrics = lyr.string or lyr.get_text()
        else:
            lyrics = _text_with_breaks(lyr)
    lyrics = _normalize_newlines(lyrics)

    return prompt, lyrics

def _extract_prompt_lyrics_from_scripts(raw_html: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Best-effort parse of embedded JSON (e.g., Next.js data) to recover prompt/lyrics
    without executing JS.
    """
    # 1) Look for a big JSON blob (Next.js __NEXT_DATA__)
    prompt = None
    lyrics = None

    # Try to capture JSON blobs from <script> tags even if attributes stripped during SSR/CDN
    script_jsons: List[str] = re.findall(
        r"<script[^>]*>(\s*{[\s\S]*?})\s*</script>", raw_html, flags=re.I
    )

    # Also pick up quoted strings and scan for fields if the JSON is broken across tags
    quoted_strings = re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', raw_html)

    def _maybe_unescape(s: str) -> str:
        try:
            return bytes(s, "utf-8").decode("unicode_escape")
        except Exception:
            return s

    # Pass 1: full JSON blobs
    for blob in script_jsons:
        data = _safe_json_loads(blob)
        if not data:
            continue

        def _walk(o):
            nonlocal prompt, lyrics
            if isinstance(o, dict):
                # common keys we’ve seen in the wild
                for k, v in o.items():
                    lk = str(k).lower()
                    if isinstance(v, (str, bytes)):
                        val = v if isinstance(v, str) else v.decode("utf-8", "ignore")
                        if prompt is None and ("prompt" in lk or "description" in lk) and len(val.strip()) > 10:
                            prompt = val.strip()
                        if lyrics is None and ("lyrics" in lk or "songtext" in lk) and len(val.strip()) > 10:
                            lyrics = val.strip()
                    else:
                        _walk(v)
            elif isinstance(o, list):
                for it in o:
                    _walk(it)

        _walk(data)
        if prompt and lyrics:
            break

    # Pass 2: scan quoted strings for obvious prompt/lyrics content
    if not (prompt and lyrics):
        for qs in quoted_strings:
            s = _maybe_unescape(qs)
            if prompt is None and len(s) > 20 and ("verse" in s.lower() or "chorus" in s.lower() or "prompt:" in s.lower()):
                prompt = s.strip()
            # heuristic: lyrics often multi-line with punctuation/newlines
            if lyrics is None and ("\n" in s or "\\n" in qs) and len(s) > 40 and any(w in s.lower() for w in ("verse", "chorus", "bridge")):
                lyrics = s.strip()
            if prompt and lyrics:
                break

    return _normalize_newlines(prompt), _normalize_newlines(lyrics)

def _extract_audio_url_from_meta_or_html(soup: BeautifulSoup, raw_html: str, song_id: Optional[str]) -> Optional[str]:
    # Preferred: meta tags
    audio_meta = soup.find("meta", property="og:audio")
    if audio_meta and audio_meta.get("content"):
        url = audio_meta["content"].strip()
        if url:
            return url

    tw_stream = soup.find("meta", attrs={"name": "twitter:player:stream"})
    if tw_stream and tw_stream.get("content"):
        url = tw_stream["content"].strip()
        if url:
            return url

    # Fallback: mp3 link present in HTML/JSON
    m = _AUDIO_RE.search(raw_html or "")
    if m:
        return m.group(0)

    # Construct from song id if available
    if song_id:
        return f"https://cdn1.suno.ai/{song_id}.mp3"

    return None

# =========================
# Main extraction
# =========================

def extract_song_info(url: str) -> dict:
    """Extract rich song metadata and a playable audio URL, Playwright-free."""
    os.makedirs("songs", exist_ok=True)
    url = _normalize_suno_short(url)

    # Prefer Suno page path
    if "suno.com/song/" in url:
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Connection": "keep-alive",
            }
            resp = requests.get(url, headers=headers, timeout=12)
            resp.raise_for_status()
            raw_html = resp.text
            soup = BeautifulSoup(raw_html, "html.parser")

            # Title
            title_meta = soup.find("meta", property="og:title")
            title = title_meta.get("content", "Unknown Title") if title_meta else "Unknown Title"

            # Artist (heuristic)
            artist = None
            t_creator = soup.find("meta", attrs={"name": "description"})
            if t_creator and t_creator.get("content"):
                c = t_creator["content"]
                m = re.search(r"\(@\s*([^)]+?)\)|\bby\s+(.+?)(?=\s*(?:[()|.:;·-]|$))", c, re.I)
                if m:
                    artist = (m.group(2) or m.group(1)).strip()
                    if artist.startswith("@"):
                        artist = artist[1:]
            if not artist:
                a = soup.find("a", href=re.compile(r"^/@.+$"))
                if a and a.has_attr("href"):
                    m = re.search(r"^/@(.+)$", a["href"])
                    artist = (m.group(1).strip() if m else a.get_text(strip=True)) or None

            # Song ID
            song_id = None
            m_id = re.search(r"suno\.com/song/([a-f0-9\-]{8,})", url, re.I)
            if m_id:
                song_id = m_id.group(1)

            # Thumbnail
            thumbnail = None
            og_img = soup.find("meta", property="og:image")
            if og_img and og_img.get("content"):
                thumbnail = og_img["content"].strip()

            # Date
            created_date = None
            date_meta = soup.find("meta", attrs={"property": "article:published_time"})
            if date_meta and date_meta.get("content"):
                created_date = date_meta["content"].strip()

            # Duration from meta/ld+json
            duration = None
            meta_dur = (
                soup.find("meta", attrs={"property": "music:duration"})
                or soup.find("meta", attrs={"property": "og:video:duration"})
            )
            if meta_dur and meta_dur.get("content"):
                try:
                    duration = int(float(meta_dur["content"]))
                except Exception:
                    duration = None
            if duration is None:
                ld = _extract_from_ld_json(soup)
                duration = ld.get("duration") or duration

            # Prompt & Lyrics (DOM first)
            prompt, lyrics = _extract_prompt_lyrics_from_dom(soup)

            # If still missing, try embedded JSON/script blobs (Playwright-free)
            if not prompt or not lyrics:
                p2, l2 = _extract_prompt_lyrics_from_scripts(raw_html)
                prompt = prompt or p2
                lyrics = lyrics or l2

            # Audio URL (meta -> html regex -> construct)
            audio_url = _extract_audio_url_from_meta_or_html(soup, raw_html, song_id)
            if not audio_url:
                raise ValueError("Could not extract Suno audio URL")

            # If duration still unknown, probe the audio
            if duration is None and audio_url:
                try:
                    headers_ff = {
                        "User-Agent": headers["User-Agent"],
                        "Referer": f"https://suno.com/song/{song_id}" if song_id else "https://suno.com/",
                        "Accept": "*/*",
                    }
                    duration = _ffprobe_duration(audio_url, headers=headers_ff)
                except Exception:
                    duration = None
            if duration is None and audio_url:
                try:
                    duration = _yt_dlp_probe_duration(audio_url)
                except Exception:
                    duration = None

            return {
                "title": title,
                "url": audio_url,
                "duration": duration,
                "date": created_date,
                "artist": artist,
                "suno_url": f"https://suno.com/song/{song_id}" if song_id else url,
                "thumbnail": thumbnail,
                "prompt": _normalize_newlines(prompt),
                "lyrics": _normalize_newlines(lyrics),
            }

        except Exception as e:
            print(f"Suno direct extraction failed: {e}, falling back to yt_dlp generic")

    # ===== Generic yt-dlp extraction for non-Suno URLs =====
    try:
        import yt_dlp
    except Exception as e:
        raise RuntimeError(
            "yt_dlp is required for this URL type but not installed or failed to import."
        ) from e

    ydl_opts = {
        "format": "bestaudio/best",
        "extractaudio": True,
        "audioquality": 1,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "prefer_ffmpeg": True,
        "retries": 5,
        "fragment_retries": 10,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        title = info.get("title", "Unknown Title")
        audio_url = info.get("url")
        duration = info.get("duration")
        if duration is None and audio_url:
            duration = _ffprobe_duration(audio_url) or _yt_dlp_probe_duration(audio_url)

        artist = info.get("artist") or info.get("uploader") or info.get("channel") or None
        date = info.get("upload_date")
        thumbnail = info.get("thumbnail")
        prompt = info.get("description")

        if not audio_url:
            raise ValueError("No playable URL found in extracted info")

        return {
            "title": title,
            "url": audio_url,
            "duration": duration,
            "date": date,
            "artist": artist,
            "suno_url": None,
            "thumbnail": thumbnail,
            "prompt": _normalize_newlines(prompt),
            "lyrics": None,
        }