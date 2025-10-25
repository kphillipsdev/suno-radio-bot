import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.scraper import scrape_suno_user_songs

song_links = scrape_suno_user_songs('huzzy', limit=5)
print('Scraped songs from @huzzy:')
for song in song_links:
    print(f"- {song['title']}: {song['url']}")
if not song_links:
    print('No song links found. Profile may be private or scraping method failed.')