# src/utils/scraper.py
import re
import html
import requests
from urllib.parse import urljoin

# Common UA used for requests
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

_rx_uuid = r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"

# ——— helpers ————————————————————————————————————————————————————————————————

def _make_url(src: str) -> str:
    s = (src or "").strip()
    if not s:
        return "https://suno.com/"
    if s.startswith("http"):
        return s
    if s.startswith("/playlist/"):
        return urljoin("https://suno.com", s)
    if s.startswith("@"):
        s = s[1:]
    # assume username/handle
    return f"https://suno.com/@{s}"


def _dedupe_keep_order(items):
    seen = set()
    out = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _unescape_js(s: str) -> str:
    # flight chunks have lots of escape sequences; this makes nearby titles readable
    try:
        return bytes(s, "utf-8").decode("unicode_escape")
    except Exception:
        return s


# ——— core extractors ————————————————————————————————————————————————————————

def _ids_from_song_hrefs(html_text: str) -> list[str]:
    """Collect IDs from literal /song/<uuid> links in the HTML."""
    ids = re.findall(rf"/song/({_rx_uuid})", html_text, flags=re.I)
    return _dedupe_keep_order([i.lower() for i in ids])


def _ids_from_audio_urls(html_text: str) -> list[str]:
    """Collect IDs from cdn audio/video urls like https://cdn1.suno.ai/<uuid>.mp3."""
    ids = re.findall(rf"https?://cdn\d?\.suno\.ai/({_rx_uuid})\.(?:mp3|mp4)", html_text, flags=re.I)
    return _dedupe_keep_order([i.lower() for i in ids])


def _pairs_from_flight_chunks(html_text: str) -> list[tuple[str, str | None]]:
    """
    Parse React Flight 'self.__next_f.push(...)' blobs to get (id, title).
    We scan locally around 'entity_type":"song_schema"' for a title and an id.
    """
    out: list[tuple[str, str | None]] = []
    # Coarse split on push boundaries to keep regex fast
    for chunk in html_text.split("self.__next_f.push("):
        if "song_schema" not in chunk:
            continue
        # local window to cut the noise, still generous
        window = chunk[:20000]

        # Match pairs where title appears near id inside same record
        # title can be before or after id, allow some distance
        # Example: ..."title":"Feel the Waves",...,"id":"fc2a...","entity_type":"song_schema"...
        for m in re.finditer(
            rf'"title"\s*:\s*"([^"]+?)".{{0,800?}}"id"\s*:\s*"({_rx_uuid})"',
            window, flags=re.S | re.I
        ):
            title = html.unescape(_unescape_js(m.group(1))).strip()
            out.append((m.group(2).lower(), title))

        # Also catch the reverse order (id then title)
        for m in re.finditer(
            rf'"id"\s*:\s*"({_rx_uuid})".{{0,800?}}"title"\s*:\s*"([^"]+?)"',
            window, flags=re.S | re.I
        ):
            title = html.unescape(_unescape_js(m.group(2))).strip()
            out.append((m.group(1).lower(), title))

        # Last-ditch: any id attached to entity_type if title was missed
        for m in re.finditer(
            rf'"entity_type"\s*:\s*"song_schema".{{0,1200?}}"id"\s*:\s*"({_rx_uuid})"',
            window, flags=re.S | re.I
        ):
            out.append((m.group(1).lower(), None))

    # dedupe by id, prefer first non-empty title seen
    seen = {}
    for sid, ttl in out:
        if sid not in seen or (ttl and not seen[sid]):
            seen[sid] = ttl
    return [(sid, seen[sid]) for sid in seen]


# ——— public API ————————————————————————————————————————————————————————————————

def scrape_suno_songs(source: str, limit: int = 100) -> list[dict]:
    """
    Scrape songs from a Suno playlist or profile (or @handle).
    Returns list of dicts: { "title": str|None, "url": str, "suno_url": str }
    """
    url = _make_url(source)
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive",
    }

    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    html_text = r.text

    results: list[tuple[str, str | None]] = []

    # 1) Best: parse React flight chunks for (id, title)
    pairs = _pairs_from_flight_chunks(html_text)
    if pairs:
        results.extend(pairs)

    # 2) Also consider literal links and CDN urls as safety nets
    if not results:
        ids = _ids_from_song_hrefs(html_text)
        if not ids:
            ids = _ids_from_audio_urls(html_text)
        results.extend([(sid, None) for sid in ids])

    # Normalize, de-dup, limit
    seen = set()
    items = []
    for sid, ttl in results:
        if sid in seen:
            continue
        seen.add(sid)
        suno_url = f"https://suno.com/song/{sid}"
        items.append({
            "title": ttl or None,
            "suno_url": suno_url,
            "url": suno_url,  # your extractor will resolve to MP3 + rich meta
        })
        if limit and len(items) >= limit:
            break

    return items


def _get(url, session=None, timeout=15):
    """
    Simple requests-only getter retained for compatibility with prior imports.
    Previously, this could fall back to Playwright when bot protection was detected.
    Now it returns the raw requests.Response object.
    """
    import requests as _requests
    s = session or _requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"})
    resp = s.get(url, timeout=timeout)
    return resp
