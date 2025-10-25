import json
from collections import deque, defaultdict
import os

DATA_DIR = 'data'
os.makedirs(DATA_DIR, exist_ok=True)

def load_data(guild_id):
    filename = os.path.join(DATA_DIR, f'guild_{guild_id}.json')
    queues = defaultdict(deque)
    playlists = defaultdict(lambda: defaultdict(deque))
    user_mappings = defaultdict(dict)
    if os.path.exists(filename):
        with open(filename, 'r') as f:
            data = json.load(f)
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
    with open(filename, 'w') as f:
        json.dump(data, f)