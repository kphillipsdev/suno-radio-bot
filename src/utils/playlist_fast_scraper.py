import requests
import sys
import time
from typing import List, Dict

def get_playlist_links_api(playlist_url: str) -> List[str]:
    """
    Scrapes a Suno playlist URL using their internal API.
    Much faster and uses fewer resources than Selenium.
    """
    # Extract playlist ID from URL
    playlist_id = playlist_url.split('/')[-1].split('?')[0]
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    all_song_links = []
    page = 1
    
    while True:
        # Suno's studio API endpoint for playlists
        api_url = f'https://studio-api.prod.suno.com/api/playlist/{playlist_id}/?page={page}'
        
        try:
            response = requests.get(api_url, headers=headers, timeout=10)
            
            if response.status_code != 200:
                break
                
            data = response.json()
            clips = data.get('playlist_clips', [])
            
            if not clips:
                break
                
            for entry in clips:
                clip = entry.get('clip', {})
                song_id = clip.get('id')
                if song_id:
                    all_song_links.append(f"https://suno.com/song/{song_id}")
            
            # If we got fewer than 50 (typical page size), we're likely on the last page
            if len(clips) < 50:
                break
                
            page += 1
            # Small delay to be respectful
            time.sleep(0.2)
            
        except Exception as e:
            print(f"Error fetching page {page}: {e}")
            break
            
    return all_song_links

def main():
    if len(sys.argv) > 1:
        playlist_url = sys.argv[1]
    else:
        playlist_url = input("Please enter the Suno playlist URL: ").strip()
        
    if not playlist_url:
        print("No URL provided. Exiting.")
        return

    print(f"\nStarting fast scraper for: {playlist_url}")
    start_time = time.time()
    
    links = get_playlist_links_api(playlist_url)
    
    end_time = time.time()
    
    if not links:
        print("No song links found. The playlist might be private or the ID is invalid.")
        return
        
    print(f"\nSuccessfully found {len(links)} songs in {end_time - start_time:.2f} seconds:\n")
    for i, link in enumerate(links, 1):
        print(f"{i}. {link}")

if __name__ == "__main__":
    main()

