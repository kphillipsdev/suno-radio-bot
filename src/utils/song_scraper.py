"""
Suno Web Scraper (BeautifulSoup-only version)
Extracts lyrics and style prompts from Suno song pages using only requests and BeautifulSoup.
Much faster than Selenium since data is embedded in script tags in the HTML source.
"""

import requests
from bs4 import BeautifulSoup
import json
import re
import os
import logging
import codecs
from typing import Dict, Optional

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def clean_lyrics_text(text: str) -> str:
    """
    Clean lyrics text by removing everything before "LyricsComments" and after the emoji marker.
    """
    if not text:
        return text
    
    # Remove everything before "LyricsComments" (including "LyricsComments" itself)
    lyrics_marker = "LyricsComments"
    if lyrics_marker in text:
        index = text.find(lyrics_marker)
        # Start from after "LyricsComments" and any following whitespace/numbers
        text = text[index + len(lyrics_marker):].strip()
        # Remove leading numbers and parentheses like "(1)" if present
        text = text.lstrip('()0123456789 ').strip()
    
    # Remove random alphanumeric patterns with colons ending in comma at the start
    # Pattern like "1b:T5b9," or similar
    if text and text[0].isalnum():
        # Find the first comma in the text
        comma_index = text.find(',')
        if comma_index != -1:
            # Check if everything before the comma is alphanumeric/colon (no spaces)
            prefix = text[:comma_index]
            if prefix and all(c.isalnum() or c == ':' for c in prefix):
                # Remove the prefix and comma, plus any following whitespace
                text = text[comma_index + 1:].strip()
    
    # Emoji marker that appears at the end of lyrics section
    emoji_marker = "🔥😍😱🙌👍👎🥵"
    
    # Find the emoji marker and truncate everything after it
    if emoji_marker in text:
        index = text.find(emoji_marker)
        text = text[:index].strip()
    
    return text


def fix_utf8_encoding(text: str) -> str:
    """
    Fix UTF-8 encoding issues where UTF-8 bytes were incorrectly decoded as separate unicode characters.
    For example, em-dash (—) encoded as UTF-8 bytes 0xE2 0x80 0x94 might appear as \u00e2\u0080\u0094
    which decodes to three separate characters. This function detects and fixes such patterns.
    """
    if not text:
        return text
    
    try:
        # Common UTF-8 byte sequences that were incorrectly decoded
        # Map of (incorrectly decoded sequence) -> (correct character)
        utf8_fixes = {
            '\u00e2\u0080\u0094': '—',  # em-dash
            '\u00e2\u0080\u0093': '–',  # en-dash
            '\u00e2\u0080\u0099': "'",  # right single quotation mark
            '\u00e2\u0080\u009c': '"',  # left double quotation mark
            '\u00e2\u0080\u009d': '"',  # right double quotation mark
            '\u00e2\u0080\u00a6': '…',  # horizontal ellipsis
            '\u00e2\u0080\u0098': "'",  # left single quotation mark
        }
        
        # Apply fixes
        for incorrect, correct in utf8_fixes.items():
            text = text.replace(incorrect, correct)
        
        # More general approach: try to detect and fix UTF-8 byte sequences
        # Pattern: characters in the range \u0080-\u00ff that might be UTF-8 bytes
        # We'll try to re-encode and decode as UTF-8
        try:
            # If the text contains characters that look like they might be UTF-8 bytes
            # that were incorrectly decoded, try to fix them
            # Convert to bytes using latin-1 (which preserves byte values 0-255)
            # Then decode as UTF-8
            if any(ord(c) >= 0x80 and ord(c) < 0x100 for c in text):
                # Try to find sequences of 2-4 bytes that might be UTF-8
                # This is a heuristic approach
                bytes_text = text.encode('latin-1')
                try:
                    fixed_text = bytes_text.decode('utf-8')
                    # Only use the fixed version if it's different and doesn't contain replacement characters
                    if fixed_text != text and '\ufffd' not in fixed_text:
                        text = fixed_text
                except UnicodeDecodeError:
                    pass
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        
        return text
    except Exception:
        # If anything goes wrong, return the original text
        return text


def clean_style_prompt_text(text: str) -> str:
    """
    Clean style prompt text by starting from after "Follow" and removing extra content at the end.
    """
    if not text:
        return text
    
    # Start from after "Follow" - the style prompt always starts right after "Follow"
    follow_marker = "Follow"
    if follow_marker in text:
        index = text.find(follow_marker)
        # Start from right after "Follow"
        text = text[index + len(follow_marker):].strip()
    
    # Markers that indicate the end of the style prompt
    end_markers = [
        "–Show Summary",
        "Show Summary",
        "LyricsComments",
        "December",
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
    ]
    
    # Find the earliest marker and truncate everything after it
    earliest_index = len(text)
    for marker in end_markers:
        index = text.find(marker)
        if index != -1 and index < earliest_index:
            earliest_index = index
    
    if earliest_index < len(text):
        text = text[:earliest_index].strip()
    
    return text


def debug_script_tags(soup: BeautifulSoup):
    """
    Debug function to print all script tag contents for manual inspection.
    """
    print("\n" + "="*80)
    print("DEBUG: ALL SCRIPT TAG CONTENTS")
    print("="*80)
    
    script_tags = soup.find_all('script')
    print(f"Found {len(script_tags)} script tags\n")
    
    for i, script in enumerate(script_tags, 1):
        script_content = script.string
        if script_content:
            print(f"\n--- Script Tag #{i} (Length: {len(script_content)} chars) ---")
            # Print first 500 characters, or full content if shorter
            preview = script_content[:5000] if len(script_content) > 5000 else script_content
            print(preview)
            if len(script_content) > 5000:
                print(f"... (truncated, {len(script_content) - 5000} more characters)")
        else:
            print(f"\n--- Script Tag #{i} (Empty or no content) ---")
    
    print("\n" + "="*80 + "\n")


def scrape_suno_song(file_path_or_url: str, debug: bool = True) -> Dict[str, Optional[str]]:
    """
    Scrape lyrics and style prompt from a Suno song page using only requests and BeautifulSoup.
    
    Args:
        file_path_or_url: Path to local HTML file or URL to Suno song page
        debug: If True, print all script tag contents for debugging
    
    Returns:
        Dictionary with 'lyrics', 'style_prompt', 'image_url', 'major_model_version',
        'model_name', 'play_count', and 'like_count' keys
    """
    result = {
        'lyrics': None,
        'style_prompt': None,
        'image_url': None,
        'major_model_version': None,
        'model_name': None,
        'play_count': None,
        'like_count': None
    }
    
    try:
        # Get HTML content
        if os.path.exists(file_path_or_url):
            # Local file
            logger.info(f"Loading local file: {file_path_or_url}")
            with open(file_path_or_url, 'r', encoding='utf-8') as f:
                html_content = f.read()
        else:
            # URL
            logger.info(f"Fetching URL: {file_path_or_url}")
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            response = requests.get(file_path_or_url, headers=headers, timeout=10)
            response.raise_for_status()
            html_content = response.text
        
        # Parse with BeautifulSoup
        soup = BeautifulSoup(html_content, 'lxml')
        
        # Debug: Print all script tags if requested
        if debug:
            debug_script_tags(soup)
        
        # Extract lyrics and style prompt
        result['lyrics'] = extract_lyrics(soup, html_content)
        result['style_prompt'] = extract_style_prompt(soup, html_content)
        
        # Extract additional information from clip JSON
        result['image_url'] = extract_image_url(soup)
        model_info = extract_model_info(soup)
        result['major_model_version'] = model_info.get('major_model_version')
        result['model_name'] = model_info.get('model_name')
        result['play_count'] = extract_play_count(soup)
        result['like_count'] = extract_like_count(soup)
        
    except FileNotFoundError:
        logger.error(f"File not found: {file_path_or_url}")
    except requests.RequestException as e:
        logger.error(f"Error fetching URL: {e}")
    except Exception as e:
        logger.error(f"Error during scraping: {e}", exc_info=True)
    
    return result


def extract_lyrics(soup: BeautifulSoup, html_content: str) -> Optional[str]:
    """
    Extract lyrics from script tags.
    Lyrics are in format: self.__next_f.push([1,"lyrics text here"])
    Filters out timing shorthand patterns like "\\nDa da" that appear in Suno's structure notation.
    """
    try:
        # Find all script tags
        script_tags = soup.find_all('script')
        
        # Patterns that indicate timing shorthand or other non-lyrics content
        timing_shorthand_patterns = [
            '\\nDa da',
            'react.fragment',
            'static/chunks',
            '__PAGE__',
            'Listen and make your own on Suno',
            'v5',
            'charSet',
            'href',
        ]
        
        # Search all script tags for the pattern
        for script in script_tags:
            script_content = script.string
            if not script_content:
                continue
            
            # Look for the specific pattern: self.__next_f.push([1,"..."])
            start_marker = 'self.__next_f.push([1,"'
            #end_marker = '"])'
            if 'v4.5+' in script_content:
                end_marker = ':['
            elif 'v5' in script_content:
                end_marker = ':['
            elif 'v4' in script_content:
                end_marker = '","'
            elif 'v3' in script_content:
                end_marker = ':['
            else:
                end_marker = '"])'
            # v4.5-all is a special case for the lyrics
            if 'v4.5-all' in script_content:
                start_marker = 'prompt":"'
                end_marker = '","type'
            start_index = script_content.find(start_marker)
            if start_index != -1:
                search_start = start_index + len(start_marker)
                end_index = script_content.find(end_marker, search_start)
                
                if end_index != -1:
                    # Extract lyrics between markers
                    lyrics_raw = script_content[search_start:end_index]
                    
                    # Check if this contains timing shorthand patterns (skip if it does)
                    contains_timing_shorthand = any(pattern in lyrics_raw for pattern in timing_shorthand_patterns)
                    if contains_timing_shorthand:
                        continue  # Skip this match, it's timing shorthand, not lyrics
                    
                    # Check if it has newlines (lyrics should have \n)
                    if '\\n' not in lyrics_raw:
                        continue  # Skip if no newlines (probably not lyrics)
                    
                    # Decode escaped characters
                    lyrics = lyrics_raw.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')
                    
                    # Additional validation: lyrics should be reasonably long and contain actual text
                    if len(lyrics) < 100:
                        continue  # Too short to be lyrics
                    # check if the version is not supported
                    if 'v3.5' in script_content:
                        lyrics = 'version not supported'
                    # Clean the lyrics
                    cleaned = clean_lyrics_text(lyrics)
                    #if cleaned and len(cleaned) > 100:  # Make sure cleaned version is still substantial
                    logger.info("Successfully extracted lyrics from script tag")
                    return cleaned
        
        # Fallback: Check for lyrics in the same script as style prompt
        # Pattern: \"prompt\":\"...\",\"
        for script in script_tags:
            script_content = script.string
            if not script_content:
                continue
            
            # Look for the pattern: \"prompt\":\"...\",\"
            start_marker = '\\"prompt\\":\\"'
            end_marker = '\\",\\"'
            
            start_index = script_content.find(start_marker)
            if start_index != -1:
                search_start = start_index + len(start_marker)
                end_index = script_content.find(end_marker, search_start)
                
                if end_index != -1:
                    # Extract lyrics between markers
                    lyrics_raw = script_content[search_start:end_index]
                    
                    # Check if this contains timing shorthand patterns (skip if it does)
                    contains_timing_shorthand = any(pattern in lyrics_raw for pattern in timing_shorthand_patterns)
                    if contains_timing_shorthand:
                        continue  # Skip this match
                    
                    # Check if it has newlines (lyrics should have \n)
                    if '\\n' not in lyrics_raw:
                        continue  # Skip if no newlines
                    
                    # Decode escaped characters
                    #lyrics = lyrics_raw.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')
                    lyrics = lyrics_raw
                    while '\\\\n' in lyrics:
                        lyrics = lyrics.replace('\\\\n', '\n')
                    lyrics = lyrics.replace('\\n', '\n')
                    lyrics = lyrics.replace('\\"', '"')
                    lyrics = lyrics.replace('\\\\', '\\')
                    # Additional validation: lyrics should be reasonably long
                    if len(lyrics) < 100:
                        continue  # Too short to be lyrics
                   
                    # Clean the lyrics
                    cleaned = clean_lyrics_text(lyrics)
                    if cleaned and len(cleaned) > 100:  # Make sure cleaned version is still substantial
                        logger.info("Successfully extracted lyrics from prompt field (fallback)")
                        return cleaned
        
        logger.warning("Could not find lyrics in script tags")
        return None
        
    except Exception as e:
        logger.error(f"Error extracting lyrics: {e}", exc_info=True)
        return None


def extract_style_prompt(soup: BeautifulSoup, html_content: str) -> Optional[str]:
    """
    Extract style prompt from script tag.
    Style prompt is in metadata.tags field of the clip JSON.
    First tries to extract from parsed JSON, then falls back to string matching.
    """
    try:
        # First, try to get it from the parsed JSON (most reliable)
        clip_data = extract_clip_json(soup)
        if clip_data and 'metadata' in clip_data:
            metadata = clip_data.get('metadata', {})
            if 'tags' in metadata:
                tags = metadata['tags']
                if tags and isinstance(tags, str):
                    # Decode Unicode escape sequences (e.g., \u0026 -> &) and other escapes
                    # codecs.decode with 'unicode_escape' handles \uXXXX, \n, \r, \t, etc.
                    try:
                        tags = codecs.decode(tags, 'unicode_escape')
                    except (UnicodeDecodeError, ValueError):
                        # If unicode_escape fails, manually decode sequences
                        # Decode \uXXXX sequences first
                        def decode_unicode(match):
                            return chr(int(match.group(1), 16))
                        tags = re.sub(r'\\u([0-9a-fA-F]{4})', decode_unicode, tags)
                        # Then decode newlines and other common escapes
                        tags = tags.replace('\\n', '\n')
                        tags = tags.replace('\\r', '\r')
                        tags = tags.replace('\\t', '\t')
                    # Fix UTF-8 encoding issues (e.g., em-dashes incorrectly decoded)
                    tags = fix_utf8_encoding(tags)
                    cleaned = clean_style_prompt_text(tags)
                    if cleaned:
                        logger.info("Successfully extracted style prompt from parsed JSON")
                        return cleaned
        
        # Fallback: Search all script tags for the pattern
        script_tags = soup.find_all('script')
        for script in script_tags:
            script_content = script.string
            if not script_content:
                continue
            
            start_marker = '\\"metadata\\":{\\"tags\\":\\"'
            end_marker = '\\",\\"prompt\\"'
            
            start_index = script_content.find(start_marker)
            if start_index != -1:
                search_start = start_index + len(start_marker)
                end_index = script_content.find(end_marker, search_start)
                
                if end_index != -1:
                    style_prompt = script_content[search_start:end_index]
                    # Decode escape sequences
                    # First decode basic escapes
                    style_prompt = style_prompt.replace('\\"', '"').replace('\\\\', '\\')
                    # Then decode Unicode escape sequences (e.g., \u0026 -> &) and other escapes
                    # codecs.decode with 'unicode_escape' handles \uXXXX, \n, \r, \t, etc.
                    try:
                        style_prompt = codecs.decode(style_prompt, 'unicode_escape')
                    except (UnicodeDecodeError, ValueError):
                        # If unicode_escape fails, manually decode sequences
                        def decode_unicode(match):
                            return chr(int(match.group(1), 16))
                        style_prompt = re.sub(r'\\u([0-9a-fA-F]{4})', decode_unicode, style_prompt)
                        # Then decode newlines and other common escapes
                        style_prompt = style_prompt.replace('\\n', '\n')
                        style_prompt = style_prompt.replace('\\r', '\r')
                        style_prompt = style_prompt.replace('\\t', '\t')
                    # Fix UTF-8 encoding issues (e.g., em-dashes incorrectly decoded)
                    style_prompt = fix_utf8_encoding(style_prompt)
                    cleaned = clean_style_prompt_text(style_prompt)
                    if cleaned:
                        logger.info("Successfully extracted style prompt from script tag")
                        return cleaned
        
        logger.warning("Could not find style prompt in script tags")
        return None
        
    except Exception as e:
        logger.error(f"Error extracting style prompt: {e}", exc_info=True)
        return None


def extract_video_url(soup: BeautifulSoup, html_content: str) -> Optional[str]:
    """
    Extract video URL from script tags.
    Video URL is between markers: video_cover_url and audio_url
    """
    try:
        # Find all script tags
        script_tags = soup.find_all('script')
        
        # Search all script tags for the pattern
        for script in script_tags:
            script_content = script.string
            if not script_content:
                continue
            
            start_marker = '\\"video_cover_url\\":\\"'
            end_marker = '\\",\\"audio_url\\"'
            
            start_index = script_content.find(start_marker)
            if start_index != -1:
                search_start = start_index + len(start_marker)
                end_index = script_content.find(end_marker, search_start)
                
                if end_index != -1:
                    video_url_raw = script_content[search_start:end_index]
                    # Decode escaped characters
                    video_url = video_url_raw.replace('\\"', '"').replace('\\\\', '\\')
                    if video_url and video_url.strip():
                        logger.info("Successfully extracted video URL from script tag")
                        return video_url.strip()
        
        logger.warning("Could not find video URL in script tags")
        return None
        
    except Exception as e:
        logger.error(f"Error extracting video URL: {e}", exc_info=True)
        return None


def extract_clip_json(soup: BeautifulSoup) -> Optional[dict]:
    """
    Extract the clip JSON object from script tags.
    Looks for pattern: self.__next_f.push([1,"5:[\"$\",\"$L1a\",null,{...}])
    Returns the parsed JSON object containing clip data.
    """
    try:
        script_tags = soup.find_all('script')
        
        for script in script_tags:
            script_content = script.string
            if not script_content:
                continue
            
            # Look for the pattern that contains the clip data
            # Pattern: self.__next_f.push([1,"5:[\"$\",\"$L1a\",null,{...}])
            # We'll search for the clip marker directly
            clip_marker = '{\\"clip\\":{'
            clip_index = script_content.find(clip_marker)
            
            if clip_index != -1:
                # Find the opening brace of the object containing "clip"
                # Go back to find the opening brace
                obj_start = script_content.rfind('{', max(0, clip_index - 200), clip_index)
                if obj_start == -1:
                    obj_start = clip_index  # Start from clip marker if no brace found before
                
                # Now find the matching closing brace for the entire object
                brace_count = 0
                obj_end = obj_start
                in_string = False
                escape_next = False
                
                for i in range(obj_start, len(script_content)):
                    char = script_content[i]
                    
                    if escape_next:
                        escape_next = False
                        continue
                    
                    if char == '\\':
                        escape_next = True
                        continue
                    
                    if char == '"' and not escape_next:
                        in_string = not in_string
                        continue
                    
                    if not in_string:
                        if char == '{':
                            brace_count += 1
                        elif char == '}':
                            brace_count -= 1
                            if brace_count == 0:
                                obj_end = i + 1
                                break
                
                if obj_end > obj_start:
                    # Extract the JSON string
                    json_str = script_content[obj_start:obj_end]
                    
                    # Unescape the JSON string carefully
                    # We need to be careful with the order of replacements
                    # First handle double backslashes that aren't escaping quotes
                    json_str = json_str.replace('\\\\n', '\n')
                    json_str = json_str.replace('\\\\r', '\r')
                    json_str = json_str.replace('\\\\t', '\t')
                    # Then handle escaped quotes
                    json_str = json_str.replace('\\"', '"')
                    # Finally handle remaining backslashes (but be careful not to break valid escapes)
                    # We'll leave single backslashes that might be part of URLs
                    
                    try:
                        # Parse the JSON
                        clip_data = json.loads(json_str)
                        if 'clip' in clip_data:
                            logger.info("Successfully extracted clip JSON from script tag")
                            return clip_data['clip']
                    except json.JSONDecodeError as e:
                        logger.debug(f"Failed to parse JSON: {e}")
                        # Try alternative: search for individual fields if full parse fails
                        continue
        
        logger.warning("Could not find clip JSON in script tags")
        return None
        
    except Exception as e:
        logger.error(f"Error extracting clip JSON: {e}", exc_info=True)
        return None


def extract_image_url(soup: BeautifulSoup) -> Optional[str]:
    """
    Extract image URL with multiple fallback methods:
    1. Check og:image meta tag
    2. Check twitter:image meta tag
    3. Extract from clip JSON
    4. Direct string extraction from script tags
    """
    try:
        # First, try to get from og:image meta tag
        og_image = soup.find('meta', property='og:image')
        if og_image and og_image.get('content'):
            image_url = og_image.get('content')
            if image_url and image_url.strip():
                logger.info("Successfully extracted image URL from og:image meta tag")
                return image_url.strip()
        
        # Second, try to get from twitter:image meta tag
        twitter_image = soup.find('meta', attrs={'name': 'twitter:image'})
        if twitter_image and twitter_image.get('content'):
            image_url = twitter_image.get('content')
            if image_url and image_url.strip():
                logger.info("Successfully extracted image URL from twitter:image meta tag")
                return image_url.strip()
        
        # Third, try to extract from clip JSON
        clip_data = extract_clip_json(soup)
        if clip_data and 'image_url' in clip_data:
            image_url = clip_data['image_url']
            if image_url:
                logger.info("Successfully extracted image URL from clip JSON")
                return image_url
        
        # Fourth, fallback: direct string extraction from script tags
        script_tags = soup.find_all('script')
        for script in script_tags:
            script_content = script.string
            if not script_content:
                continue
            
            start_marker = '\\"image_url\\":\\"'
            end_marker = '\\"'
            
            start_index = script_content.find(start_marker)
            if start_index != -1:
                search_start = start_index + len(start_marker)
                end_index = script_content.find(end_marker, search_start)
                
                if end_index != -1:
                    image_url_raw = script_content[search_start:end_index]
                    image_url = image_url_raw.replace('\\"', '"').replace('\\\\', '\\')
                    if image_url and image_url.strip():
                        logger.info("Successfully extracted image URL from script tag (fallback)")
                        return image_url.strip()
        
        return None
    except Exception as e:
        logger.error(f"Error extracting image URL: {e}", exc_info=True)
        return None


def extract_model_info(soup: BeautifulSoup) -> Dict[str, Optional[str]]:
    """
    Extract model version and name from clip JSON.
    Returns a dictionary with 'major_model_version' and 'model_name' keys.
    Falls back to direct string extraction if JSON parsing fails.
    """
    result = {
        'major_model_version': None,
        'model_name': None
    }
    
    try:
        clip_data = extract_clip_json(soup)
        if clip_data:
            if 'major_model_version' in clip_data:
                result['major_model_version'] = clip_data['major_model_version']
            if 'model_name' in clip_data:
                result['model_name'] = clip_data['model_name']
            
            if result['major_model_version'] or result['model_name']:
                logger.info("Successfully extracted model info")
                return result
        
        # Fallback: direct string extraction
        script_tags = soup.find_all('script')
        for script in script_tags:
            script_content = script.string
            if not script_content:
                continue
            
            # Extract major_model_version
            if not result['major_model_version']:
                start_marker = '\\"major_model_version\\":\\"'
                end_marker = '\\"'
                start_index = script_content.find(start_marker)
                if start_index != -1:
                    search_start = start_index + len(start_marker)
                    end_index = script_content.find(end_marker, search_start)
                    if end_index != -1:
                        version = script_content[search_start:end_index]
                        version = version.replace('\\"', '"').replace('\\\\', '\\')
                        if version:
                            result['major_model_version'] = version.strip()
            
            # Extract model_name
            if not result['model_name']:
                start_marker = '\\"model_name\\":\\"'
                end_marker = '\\"'
                start_index = script_content.find(start_marker)
                if start_index != -1:
                    search_start = start_index + len(start_marker)
                    end_index = script_content.find(end_marker, search_start)
                    if end_index != -1:
                        model_name = script_content[search_start:end_index]
                        model_name = model_name.replace('\\"', '"').replace('\\\\', '\\')
                        if model_name:
                            result['model_name'] = model_name.strip()
            
            if result['major_model_version'] or result['model_name']:
                logger.info("Successfully extracted model info (fallback)")
                break
                
    except Exception as e:
        logger.error(f"Error extracting model info: {e}", exc_info=True)
    
    return result


def extract_play_count(soup: BeautifulSoup) -> Optional[int]:
    """
    Extract play count from clip JSON.
    Falls back to direct string extraction if JSON parsing fails.
    """
    try:
        clip_data = extract_clip_json(soup)
        if clip_data and 'play_count' in clip_data:
            play_count = clip_data['play_count']
            if play_count is not None:
                logger.info(f"Successfully extracted play count: {play_count}")
                return play_count
        
        # Fallback: direct string extraction
        script_tags = soup.find_all('script')
        for script in script_tags:
            script_content = script.string
            if not script_content:
                continue
            
            start_marker = '\\"play_count\\":'
            end_marker = ','
            start_index = script_content.find(start_marker)
            if start_index != -1:
                search_start = start_index + len(start_marker)
                # Find the next comma or closing brace
                comma_index = script_content.find(end_marker, search_start)
                brace_index = script_content.find('}', search_start)
                end_index = min(comma_index, brace_index) if comma_index != -1 and brace_index != -1 else (comma_index if comma_index != -1 else brace_index)
                
                if end_index != -1:
                    play_count_str = script_content[search_start:end_index].strip()
                    try:
                        play_count = int(play_count_str)
                        logger.info(f"Successfully extracted play count (fallback): {play_count}")
                        return play_count
                    except ValueError:
                        continue
        
        return None
    except Exception as e:
        logger.error(f"Error extracting play count: {e}", exc_info=True)
        return None


def extract_like_count(soup: BeautifulSoup) -> Optional[int]:
    """
    Extract like count (upvote_count) from clip JSON.
    Falls back to direct string extraction if JSON parsing fails.
    """
    try:
        clip_data = extract_clip_json(soup)
        if clip_data and 'upvote_count' in clip_data:
            like_count = clip_data['upvote_count']
            if like_count is not None:
                logger.info(f"Successfully extracted like count: {like_count}")
                return like_count
        
        # Fallback: direct string extraction
        script_tags = soup.find_all('script')
        for script in script_tags:
            script_content = script.string
            if not script_content:
                continue
            
            start_marker = '\\"upvote_count\\":'
            end_marker = ','
            start_index = script_content.find(start_marker)
            if start_index != -1:
                search_start = start_index + len(start_marker)
                # Find the next comma or closing brace
                comma_index = script_content.find(end_marker, search_start)
                brace_index = script_content.find('}', search_start)
                end_index = min(comma_index, brace_index) if comma_index != -1 and brace_index != -1 else (comma_index if comma_index != -1 else brace_index)
                
                if end_index != -1:
                    like_count_str = script_content[search_start:end_index].strip()
                    try:
                        like_count = int(like_count_str)
                        logger.info(f"Successfully extracted like count (fallback): {like_count}")
                        return like_count
                    except ValueError:
                        continue
        
        return None
    except Exception as e:
        logger.error(f"Error extracting like count: {e}", exc_info=True)
        return None


if __name__ == "__main__":
    # Example usage
    import sys
    
    # Check for debug flag
    debug_mode = '--debug' in sys.argv or '-d' in sys.argv
    if debug_mode:
        sys.argv = [arg for arg in sys.argv if arg not in ['--debug', '-d']]
    
    if len(sys.argv) < 2:
        # Prompt for URL or file path for debugging
        file_path_or_url = input("Enter file path or URL: ").strip()
        if not file_path_or_url:
            print("No input provided. Exiting.")
            sys.exit(1)
        # Ask about debug mode
        debug_input = input("Enable debug mode to see all script tags? (y/n): ").strip().lower()
        debug_mode = debug_input == 'y'
    else:
        file_path_or_url = sys.argv[1]
    
    result = scrape_suno_song(file_path_or_url, debug=debug_mode)
    
    print("\n" + "="*50)
    print("LYRICS:")
    print("="*50)
    print(result['lyrics'] if result['lyrics'] else "Not found")
    
    print("\n" + "="*50)
    print("STYLE PROMPT:")
    print("="*50)
    print(result['style_prompt'] if result['style_prompt'] else "Not found")
    
    print("\n" + "="*50)
    print("ADDITIONAL INFO:")
    print("="*50)
    print(f"Image URL: {result['image_url'] if result['image_url'] else 'Not found'}")
    print(f"Model Version: {result['major_model_version'] if result['major_model_version'] else 'Not found'}")
    print(f"Model Name: {result['model_name'] if result['model_name'] else 'Not found'}")
    print(f"Play Count: {result['play_count'] if result['play_count'] is not None else 'Not found'}")
    print(f"Like Count: {result['like_count'] if result['like_count'] is not None else 'Not found'}")
    print("="*50)
