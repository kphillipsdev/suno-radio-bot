import json
from collections import deque, defaultdict
import os

DATA_DIR = 'data'
os.makedirs(DATA_DIR, exist_ok=True)

def fix_utf8_in_dict(data):
    """
    Recursively fix UTF-8 encoding issues in dictionaries and lists.
    Fixes prompts and other string fields that may have been incorrectly encoded.
    """
    from src.utils.song_scraper import fix_utf8_encoding
    
    if isinstance(data, dict):
        return {k: fix_utf8_in_dict(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [fix_utf8_in_dict(item) for item in data]
    elif isinstance(data, str):
        return fix_utf8_encoding(data)
    else:
        return data

def load_data(guild_id):
    filename = os.path.join(DATA_DIR, f'guild_{guild_id}.json')
    queues = defaultdict(deque)
    playlists = defaultdict(lambda: defaultdict(deque))
    user_mappings = defaultdict(dict)
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # Fix UTF-8 encoding issues in loaded data (e.g., corrupted em-dashes)
            data = fix_utf8_in_dict(data)
            # Load queues as deques
            for k, v in data.get('queues', {}).items():
                queues[k] = deque(v)
            # Load playlists as deques
            for k, v in data.get('playlists', {}).items():
                playlists[k] = defaultdict(deque)
                for kk, vv in v.items():
                    playlists[k][kk] = deque(vv)
            user_mappings = defaultdict(dict, data.get('user_mappings', {}))
    return queues, playlists, user_mappings

def save_data(guild_id, queues, playlists, user_mappings):
    filename = os.path.join(DATA_DIR, f'guild_{guild_id}.json')
    data = {
        'queues': {k: list(v) for k, v in queues.items()},
        'playlists': {k: {kk: list(vv) for kk, vv in v.items()} for k, v in playlists.items()},
        'user_mappings': user_mappings
    }
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)