"""
Suno Web Scraper (API Version)
Extracts lyrics and style prompts from Suno songs using the direct API endpoint.
Much simpler and more reliable than HTML scraping.
"""

import requests
import re
import logging
from typing import Dict, Optional
from bs4 import BeautifulSoup

# Configure logging
logger = logging.getLogger(__name__)

# Suno API endpoint
SUNO_API_BASE = "https://studio-api.prod.suno.com/api/clip"


# ============================================================================
# Helper Functions
# ============================================================================

def fix_utf8_encoding(text: str) -> str:
    """
    Fix common UTF-8 encoding issues (mojibake).
    """
    if not text:
        return text
    
    # Common mojibake patterns (using unicode escapes for safety)
    replacements = {
        '\u00e2\u20ac\u201d': '\u2014',  # em-dash
        '\u00e2\u20ac\u201c': '\u2013',  # en-dash  
        '\u00e2\u20ac\u2122': '\u2019',  # right single quote
        '\u00e2\u20ac\u02dc': '\u2018',  # left single quote
        '\u00e2\u20ac\u0153': '\u201c',  # left double quote
        '\u00e2\u20ac\u009d': '\u201d',  # right double quote
        '\u00e2\u20ac\u00a6': '\u2026',  # ellipsis
        '\u00e2\u2020\u2019': '\u2192',  # right arrow
        '\u00c3\u00a9': '\u00e9',  # é
        '\u00c3\u00a8': '\u00e8',  # è
        '\u00c3\u00a0': '\u00e0',  # à
        '\u00c3\u00af': '\u00ef',  # ï
        '\u00c3\u00b4': '\u00f4',  # ô
        '\u00c3\u00a2': '\u00e2',  # â
        '\u00c3\u00a7': '\u00e7',  # ç
        '\u00c3\u00bc': '\u00fc',  # ü
        '\u00c3\u00b6': '\u00f6',  # ö
        '\u00c3\u00a4': '\u00e4',  # ä
        '\u00c3\u0178': '\u00df',  # ß
        '\u00c3\u00b1': '\u00f1',  # ñ
        '\u00e2': '\u2014',  # Common single-char mojibake for em-dash
    }
    
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    
    return text


def extract_song_id(url: str) -> Optional[str]:
    """
    Extract the song ID from a Suno URL.
    Handles formats like:
    - https://suno.com/song/7f256816-bbc1-4dc9-a239-946363d6ddaa
    - https://suno.com/s/UOXSSCANDYgn9hFx (short URL - follows redirect)
    """
    # UUID pattern
    uuid_pattern = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
    match = re.search(uuid_pattern, url)
    if match:
        return match.group(0)
    
    # Handle short URLs like /s/UOXSSCANDYgn9hFx - follow redirect to get real URL
    if '/s/' in url and 'suno.com' in url:
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            response = requests.head(url, headers=headers, allow_redirects=True, timeout=10)
            # Check the final URL after redirects
            final_url = response.url
            match = re.search(uuid_pattern, final_url)
            if match:
                return match.group(0)
        except Exception:
            pass
    
    return None


def fetch_song_data(song_id: str) -> Optional[Dict]:
    """
    Fetch song data from Suno API.
    """
    try:
        url = f"{SUNO_API_BASE}/{song_id}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code == 200:
            return response.json()
        else:
            logger.warning(f"API returned status {response.status_code} for song {song_id}")
            return None
            
    except Exception as e:
        logger.error(f"Error fetching song data: {e}")
        return None


# ============================================================================
# Extraction Functions (maintain compatibility with extractor.py)
# ============================================================================

def extract_lyrics(soup: BeautifulSoup, html_content: str, *, _data: Optional[Dict] = None) -> Optional[str]:
    """Extract lyrics from Suno page. Pass _data to avoid redundant API calls."""
    data = _data
    if data is None:
        song_id = extract_song_id(html_content)
        if song_id:
            data = fetch_song_data(song_id)
    if data:
        lyrics = data.get('metadata', {}).get('prompt', '')
        if lyrics:
            return fix_utf8_encoding(lyrics)
    return _extract_lyrics_from_html(soup, html_content)


def extract_style_prompt(soup: BeautifulSoup, html_content: str, *, _data: Optional[Dict] = None) -> Optional[str]:
    """Extract style prompt/tags from Suno page. Pass _data to avoid redundant API calls."""
    data = _data
    if data is None:
        song_id = extract_song_id(html_content)
        if song_id:
            data = fetch_song_data(song_id)
    if data:
        tags = data.get('metadata', {}).get('tags', '')
        if tags:
            return fix_utf8_encoding(tags)
        display_tags = data.get('display_tags', '')
        if display_tags:
            return fix_utf8_encoding(display_tags)
    return None


def extract_video_url(soup: BeautifulSoup, html_content: str, *, _data: Optional[Dict] = None) -> Optional[str]:
    """Extract video URL from Suno page. Pass _data to avoid redundant API calls."""
    data = _data
    if data is None:
        song_id = extract_song_id(html_content)
        if song_id:
            data = fetch_song_data(song_id)
    return data.get('video_url') if data else None


def extract_image_url(soup: BeautifulSoup, html_content: str, *, _data: Optional[Dict] = None) -> Optional[str]:
    """Extract image URL from Suno page. Pass _data to avoid redundant API calls."""
    data = _data
    if data is None:
        song_id = extract_song_id(html_content)
        if song_id:
            data = fetch_song_data(song_id)
    return (data.get('image_large_url') or data.get('image_url')) if data else None


def extract_model_info(soup: BeautifulSoup, html_content: str, *, _data: Optional[Dict] = None) -> Dict[str, Optional[str]]:
    """Extract model version and name from Suno page. Pass _data to avoid redundant API calls."""
    data = _data
    if data is None:
        song_id = extract_song_id(html_content)
        if song_id:
            data = fetch_song_data(song_id)
    if data:
        major_version = data.get('major_model_version', '')
        model_name = data.get('model_name', '')
        return {
            'major_model_version': major_version or None,
            'model_name': model_name or None
        }
    return {'major_model_version': None, 'model_name': None}


def extract_play_count(soup: BeautifulSoup, html_content: str, *, _data: Optional[Dict] = None) -> Optional[int]:
    """Extract play count from Suno page. Pass _data to avoid redundant API calls."""
    data = _data
    if data is None:
        song_id = extract_song_id(html_content)
        if song_id:
            data = fetch_song_data(song_id)
    return data.get('play_count') if data else None


def extract_like_count(soup: BeautifulSoup, html_content: str, *, _data: Optional[Dict] = None) -> Optional[int]:
    """Extract like/upvote count from Suno page. Pass _data to avoid redundant API calls."""
    data = _data
    if data is None:
        song_id = extract_song_id(html_content)
        if song_id:
            data = fetch_song_data(song_id)
    return data.get('upvote_count') if data else None


def extract_created_at(soup: BeautifulSoup, html_content: str, *, _data: Optional[Dict] = None) -> Optional[str]:
    """Extract created_at timestamp from Suno page. Pass _data to avoid redundant API calls."""
    data = _data
    if data is None:
        song_id = extract_song_id(html_content)
        if song_id:
            data = fetch_song_data(song_id)
    return data.get('created_at') if data else None


# ============================================================================
# Legacy HTML Extraction (fallback)
# ============================================================================

def _extract_lyrics_from_html(soup: BeautifulSoup, html_content: str) -> Optional[str]:
    """
    Fallback: Extract lyrics from HTML when API is unavailable.
    """
    try:
        # Look for prompt in script tags
        for script in soup.find_all('script'):
            content = script.string or ""
            if '\\"prompt\\":\\"' in content:
                # Find prompt value
                start = content.find('\\"prompt\\":\\"') + len('\\"prompt\\":\\"')
                # Find end
                pos = start
                while pos < len(content):
                    if content[pos:pos+2] == '\\"':
                        after = content[pos+2:pos+4] if pos+4 <= len(content) else ''
                        if after.startswith(',') or after.startswith('}'):
                            break
                        pos += 2
                    elif content[pos] == '\\':
                        pos += 2
                    else:
                        pos += 1
                
                if pos > start:
                    lyrics = content[start:pos]
                    # Unescape
                    lyrics = lyrics.replace('\\\\n', '\n').replace('\\n', '\n')
                    lyrics = lyrics.replace('\\\\', '\\').replace('\\"', '"')
                    if '\n' in lyrics and len(lyrics) > 50:
                        return fix_utf8_encoding(lyrics)
        
        return None
    except Exception as e:
        logger.error(f"Error extracting lyrics from HTML: {e}")
        return None


# ============================================================================
# Main Scraping Function
# ============================================================================

def scrape_suno_song(file_path_or_url: str, debug: bool = False) -> Dict[str, Optional[str]]:
    """
    Main function to scrape a Suno song page.
    
    Args:
        file_path_or_url: URL to a Suno song page or path to saved HTML file
        debug: Enable debug output
        
    Returns:
        Dictionary with lyrics, style_prompt, image_url, model info, play/like counts
    """
    result = {
        'lyrics': None,
        'style_prompt': None,
        'image_url': None,
        'model_version': None,
        'model_name': None,
        'play_count': None,
        'like_count': None,
        'created_at': None
    }
    
    try:
        # Extract song ID
        song_id = extract_song_id(file_path_or_url)
        
        if not song_id:
            logger.error(f"Could not extract song ID from: {file_path_or_url}")
            return result
        
        if debug:
            print(f"Extracted song ID: {song_id}")
        
        # Fetch from API
        data = fetch_song_data(song_id)
        
        if not data:
            logger.warning(f"Could not fetch data for song {song_id}")
            return result
        
        if debug:
            print(f"API returned {len(data)} fields")
        
        # Extract all fields
        metadata = data.get('metadata', {})
        
        # Lyrics (stored in metadata.prompt)
        lyrics = metadata.get('prompt', '')
        if lyrics:
            result['lyrics'] = fix_utf8_encoding(lyrics)
        
        # Style prompt (stored in metadata.tags - the full description)
        style = metadata.get('tags', '')
        if style:
            result['style_prompt'] = fix_utf8_encoding(style)
        elif data.get('display_tags'):
            # Fallback to display_tags if metadata.tags is empty
            result['style_prompt'] = fix_utf8_encoding(data['display_tags'])
        
        # Image URL
        result['image_url'] = data.get('image_large_url') or data.get('image_url')
        
        # Model info
        major_version = data.get('major_model_version', '')
        model_name = data.get('model_name', '')
        
        if major_version:
            result['model_version'] = f"v{major_version}"
        elif model_name:
            if 'bluejay' in model_name.lower():
                result['model_version'] = "v4.5+"
            elif 'chirp-v4' in model_name.lower():
                result['model_version'] = "v4"
            elif 'chirp-v3' in model_name.lower():
                result['model_version'] = "v3.5" if '3.5' in model_name else "v3"
            else:
                result['model_version'] = "Not found"
        else:
            result['model_version'] = "Not found"
        
        result['model_name'] = model_name or "Not found"
        
        # Play and like counts
        result['play_count'] = data.get('play_count')
        result['like_count'] = data.get('upvote_count')
        
        # Created date
        result['created_at'] = data.get('created_at')
        
        if debug:
            print(f"Extracted: lyrics={len(result['lyrics'] or '')} chars, style={len(result['style_prompt'] or '')} chars")
        
        return result
        
    except Exception as e:
        logger.error(f"Error scraping song: {e}")
        if debug:
            import traceback
            traceback.print_exc()
        return result


# ============================================================================
# CLI Entry Point
# ============================================================================

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
    print(result['lyrics'] or "Not found")
    
    print("\n" + "=" * 50)
    print("STYLE PROMPT:")
    print("=" * 50)
    print(result['style_prompt'] or "Not found")
    
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
