# Discord Music Bot

A feature-rich Discord bot for music playback, voice announcements, and Suno integration.

## Features

- 🎵 **Music Playback**: Play music from YouTube and SoundCloud
- 📋 **Queue Management**: Add, remove, skip, shuffle, and view songs
- 🔊 **Voice Control**: Volume adjustment, join/leave voice channels
- 📂 **Playlists**: Create, load, and manage custom playlists
- 🎤 **Text-to-Speech**: Voice announcements with native macOS TTS
- 🔗 **Suno Integration**: Map users to Suno usernames and add their music
- 💾 **Persistent Data**: Automatic saving/loading of queues and playlists
- 🎛️ **Controls Interface**: Interactive buttons for playback control

## Commands

### Voice Commands
- `!join [channel]` - Join your voice channel
- `!leave` - Leave current voice channel
- `!create_music_channel` - Create and join #music (admin only)
- `!test_speak` - Play a test TTS message

### Queue Commands
- `!play <url>` - Add song to queue from URL
- `!queue` - Show current queue
- `!skip` - Skip current song
- `!stop` - Stop and clear queue
- `!shuffle` - Shuffle queue
- `!volume <0-200>` - Set volume (100 = default)

### Playlist Commands
- `!playlist_create <name>` - Create playlist
- `!playlist_add <name> <url>` - Add song to playlist
- `!playlist_delete <name>` - Delete playlist
- `!load_playlist <name>` - Load playlist to queue

### Suno Integration
- `!add_user <@user> <suno_username>` - Map Discord user to Suno
- `!add_user_songs <suno_username>` - Add Suno's songs to default playlist

### Admin Commands
- `!reload` - Reload music cog (admin only)

## Installation

### Prerequisites
- Python 3.8+
- FFmpeg (externally installed)
- macOS (recommended for full TTS support)

### Setup
1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd discord-music-bot
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment**
   Create `.env` file:
   ```
   BOT_TOKEN=your_discord_bot_token_here
   ```

4. **Get a Discord bot token**
   - Go to [Discord Developer Portal](https://discord.com/developers/applications)
   - Create a new application
   - Go to Bot section and create a bot
   - Copy the token to `.env`

5. **Invite the bot**
   - Go to OAuth2 → URL Generator
   - Select scopes: `bot`, `applications.commands`
   - Permissions: `Send Messages`, `Use Slash Commands`, `Connect`, `Speak`
   - Use the generated URL to invite the bot

### FFmpeg Setup
The bot requires FFmpeg for audio processing:

**macOS (with Homebrew):**
```bash
brew install ffmpeg
```

**Linux:**
```bash
sudo apt install ffmpeg
```

**Windows:**
Download from [ffmpeg.org](https://ffmpeg.org/download.html)

## Running the Bot

### Development
```bash
python dev.py
```

### Production
```bash
python run.py
```

## Configuration

### Voice Settings
- **Opus Compression**: Automatically detected; falls back to raw PCM if needed
- **Volume**: Adjustable per guild (0-200%)
- **Voice Channels**: Auto-joins user's channel

### Data Storage
- Guild data saved in JSON files in `data/` directory
- Automatic persistence of queues, playlists, and user mappings

## Development

### Project Structure
```
├── src/
│   ├── bot.py              # Main bot file
│   ├── cogs/
│   │   └── music.py        # Music functionality
│   ├── utils/
│   │   ├── test_speak.py   # TTS implementation
│   │   ├── yt_extractor.py # YouTube/SoundCloud scraper
│   │   └── scraper.py      # Suno scraper
│   └── data/
│       └── persistence.py  # Data loading/saving
├── docs/                   # Documentation
├── tests/                  # Unit tests
├── requirements.txt        # Python dependencies
└── .gitignore             # Ignore rules
```

### Testing
```bash
python -m pytest tests/
```

### Key Technologies
- **discord.py**: Bot framework
- **PyObjC**: Native macOS TTS (macOS only)
- **gTTS**: Cross-platform TTS fallback
- **yt-dlp**: Video/audio extraction
- **beautifulsoup4**: HTML parsing for Suno
- **selenium**: Browser automation for JS-heavy sites

## TTS Implementation

The bot supports two TTS engines:
- **Primary**: macOS NSSpeechSynthesizer (via PyObjC) for offline, high-quality speech
- **Fallback**: Google Text-to-Speech (gTTS) for cross-platform compatibility

Audio is processed through FFmpeg for optimal Discord compatibility.

## Permissions

The bot requires these Discord permissions:
- Send Messages
- Use Slash Commands
- Connect
- Speak
- Use Voice Activity
- Manage Channels (for `create_music_channel`)

## Troubleshooting

### Common Issues

**No sound when playing music**
- Ensure FFmpeg is installed
- Check bot has `Connect` and `Speak` permissions
- Verify there's a voice channel connection

**TTS doesn't work**
- On macOS: PyObjC should work automatically
- Cross-platform: gTTS requires internet connection
- Check audio file generation in logs

**Queue/persistence errors**
- `data/` directory must be writable
- Check file permissions
- JSON corruption may need manual cleanup

**Opus errors**
- May occur on certain systems; bot auto-falls back to raw PCM

### Debug Mode
Use `dev.py` for development with additional logging.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## License

This project is licensed under the MIT License.

## Support

For issues or questions:
1. Check existing GitHub issues
2. Provide detailed error messages from logs
3. Include your OS, Python version, and bot version

---

Made with 🎵 using discord.py