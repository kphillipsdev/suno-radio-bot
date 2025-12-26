import os
import re
import html
import json
import subprocess
import requests
from typing import Optional, Dict, Tuple, List
from bs4 import BeautifulSoup

# Import extraction functions from extractor module
from src.utils.song_scraper import (
    extract_lyrics, 
    extract_style_prompt, 
    extract_video_url,
    extract_image_url,
    extract_model_info,
    extract_play_count,
    extract_like_count
)

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
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            raw_html = response.text
            # Try lxml first (faster), fall back to html.parser (always available)
            try:
                soup = BeautifulSoup(raw_html, 'lxml')
            except Exception:
                soup = BeautifulSoup(raw_html, 'html.parser')

            # Title
            title_meta = soup.find("meta", property="og:title")
            title = title_meta.get("content", "Unknown Title") if title_meta else "Unknown Title"

            # Artist (strict): take the text after the LAST "by " that appears before "(@"
            artist = None
            t_creator = soup.find("meta", attrs={"name": "description"})
            if t_creator and t_creator.get("content"):
                c = t_creator["content"]
                h = re.search(r"\(\@", c)  # start of "(@handle"
                if h:
                    pre = c[:h.start()]  # everything before the handle block
                    by_hits = list(re.finditer(r"\bby\s+", pre, flags=re.IGNORECASE))
                    if by_hits:
                        start = by_hits[-1].end()  # after the *last* "by "
                        artist = pre[start:].strip()  # exact slice; keeps emojis/specials intact

            # (optional) fallback: pull from /@handle link if no artist found
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


            lyrics = extract_lyrics(soup, raw_html)
            prompt = extract_style_prompt(soup, raw_html)
            video_url = extract_video_url(soup, raw_html)
            
            # Extract additional information
            image_url = extract_image_url(soup)
            model_info = extract_model_info(soup)
            play_count = extract_play_count(soup)
            like_count = extract_like_count(soup)

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
                "video_url": video_url,
                "prompt": prompt,
                "lyrics": lyrics,
                "image_url": image_url,
                "major_model_version": model_info.get("major_model_version"),
                "model_name": model_info.get("model_name"),
                "play_count": play_count,
                "like_count": like_count,
            }

        except Exception as e:
            print(f"Suno direct extraction failed: {e}")
            raise  # Re-raise the exception so it can be handled by the caller
    
    # If URL doesn't match suno.com/song/, raise an error
    raise ValueError(f"Unsupported URL format: {url}")