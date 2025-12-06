# Suno Radio Bot

A self-hosted Discord music bot tuned for **Suno AI “radio”** style playback.  
It can spin up a continuous station from Suno links, CSVs, or profiles, while also handling normal YouTube/SoundCloud queues.

---

## ✨ Highlights

- **Suno-first playback**
  - Plays Suno songs from direct links, playlists, and profiles
  - Scrapes metadata (title, artist, prompts, lyrics where available)
- **Normal music support**
  - Falls back to `yt-dlp` for YouTube / generic URLs
- **Smart queues & playlists**
  - Per-guild queues
  - Named playlists you can create, load, and manage
- **Radio / Autofill mode**
  - When the queue runs dry, the bot can auto-enqueue tracks from:
    - A default Suno URL (playlist/profile/song)
    - A CSV of tracks
    - Or per-user liked tracks
- **Likes & play history**
  - Tracks unique plays per guild in SQLite
  - Like/unlike system for tracks, plus “top” views
  - Slash and prefix commands for history & stats
- **Rich Now Playing cards**
  - Safe embeds with title, artist, duration, requestor, and up-next preview
  - Links back to the Suno page when possible
- **TTS test helper (optional)**
  - macOS-focused TTS via NSSpeechSynthesizer
  - gTTS + ffmpeg fallback where available
- **Slash + prefix commands**
  - `/play`, `/top`, `/history` etc.
  - `!play`, `!queue`, `!history`, `!top`, `!help`, and more
  - Auto-updating help embeds that hide admin-only commands

---

## 🚀 Quick Start

### Prerequisites

- **Python** 3.11+
- **FFmpeg** installed on your system (for audio)
- A **Discord application & bot token**
- (Recommended) A virtualenv

### 1. Clone the repo

```bash
git clone https://github.com/kphillipsdev/suno-radio-bot.git
cd suno-radio-bot
```

### 2. Create & activate a virtualenv (optional but recommended)

```bash
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

Dependencies include:

- `discord.py[voice]`
- `python-dotenv`
- `yt-dlp`
- `beautifulsoup4`
- `requests`
- `watchfiles` (for `dev.py` hot-reload)
- Optional TTS libraries (`pyobjc`, `gTTS`) if you want the TTS helper

### 4. Configure environment

Create a `.env` file in the repo root:

```env
BOT_TOKEN=your_discord_bot_token_here

# Optional: SQLite DB path (defaults to ./suno_radio.db)
SUNO_RADIO_DB=./suno_radio.db

# Optional: radio/autofill behaviour
# DEFAULT_AUTOFILL_URL=https://suno.com/@your-handle
# DEFAULT_AUTOFILL_CSV=/absolute/path/to/tracks.csv
# AUTOFILL_FEATURE=1
# AUTOFILL_DELAY_SEC=30
# AUTOFILL_MAX_PULL=50
# AUTOFILL_LIKES_PER_USER=5

# Optional: prefetch controls (see Config section)
# PREFETCH_MODE=full
# PREFETCH_DIR=songs
# PREFETCH_BYTES=524288
# PREFETCH_TIMEOUT=25
```

#### Getting a Discord bot token

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Create a new application → add a **Bot**
3. Copy the **bot token** into `BOT_TOKEN` in your `.env`

#### Invite the bot to your server

In the Developer Portal:

1. Go to **OAuth2 → URL Generator**
2. Scopes:
   - `bot`
   - `applications.commands`
3. Bot permissions:
   - Send Messages
   - Use Slash Commands
   - Connect
   - Speak
   - Use Voice Activity
   - Manage Channels (for `create_music_channel`)
4. Use the generated URL to invite the bot

### 5. FFmpeg install (examples)

**macOS (Homebrew):**

```bash
brew install ffmpeg
```

**Ubuntu/Debian:**

```bash
sudo apt update
sudo apt install ffmpeg
```

**Windows:**

Download from [ffmpeg.org](https://ffmpeg.org/) and ensure it’s on your `PATH`.

---

## ▶️ Running the Bot

### Development (auto-reload on code changes)

```bash
python dev.py
```

`dev.py` will:

- Run `run.py`
- Watch the `src/` folder for `.py` changes
- Restart the bot when files change

### Production

```bash
python run.py
```

`run.py`:

- Loads `.env`
- Initializes the SQLite DB
- Starts the bot with the `BOT_TOKEN`

You can wrap `run.py` in a systemd service, pm2, Docker, etc.

---

## 🧠 Core Concepts

### Storage

There are two layers of persistence:

1. **Guild JSON files** (`data/guild_{guild_id}.json`)
   - Per-guild queues
   - Named playlists
   - User → Suno mappings

2. **SQLite DB** (`SUNO_RADIO_DB`, default `./suno_radio.db`)
   - `tracks`: track metadata (id, title, artist, source_url, etc.)
   - `plays`: play history per guild, with timestamps
   - `likes`: per-user likes for tracks

This allows you to:

- Keep queues and playlists across restarts
- See recent plays per guild
- Query “top” tracks over a time range
- Build “liked radio” modes per listener

---

## 🎛 Configuration

Most tunables are driven by environment variables. All of these are optional – defaults are chosen to be reasonable for most servers.

### Required

- `BOT_TOKEN` – Discord bot token

### Storage

- `SUNO_RADIO_DB` – Path to the SQLite DB file  
  Defaults to `./suno_radio.db` in the repo root.

### Prefetch / Caching

Used to warm up or fully cache audio before playback:

- `PREFETCH_MODE` – `none` | `warmup` | `full`
- `PREFETCH_DIR` – directory for cached audio (`songs` by default)
- `PREFETCH_BYTES` – max bytes to pull in “warmup” mode
- `PREFETCH_TIMEOUT` – HTTP timeout for full downloads

### Playback / FFmpeg tuning

Fine-tune startup latency and quality:

- `PREBUFFER_SECONDS` – delay before starting playback (buffer fill)
- `FADE_IN_SECONDS`, `FADE_IN_STEPS` – smooth fade-in
- `FADE_OUT_SECONDS`, `FADE_OUT_STEPS` – smooth fade-out
- `FFMPEG_PROBESIZE`, `FFMPEG_ANALYZEDURATION`
- `FFMPEG_THREAD_QUEUE_SIZE`
- `FFMPEG_RW_TIMEOUT_US`
- `FFMPEG_NOBUFFER`
- `FFMPEG_BUFFER_SIZE`
- `FFMPEG_MAX_DELAY_US`
- `VOICE_BITRATE_KBPS` – Discord voice bitrate

### Queue & Autofill

- `QUEUE_LIMIT_DEFAULT_ENABLED` – enable per-add throttling
- `QUEUE_LIMIT_MAX_PER_ADD` – max tracks one command can enqueue
- `QUEUE_MAX_PER_USER` – max tracks per user in queue
- `AUTOFILL_FEATURE` – enable idle radio / autofill
- `AUTOFILL_DELAY_SEC` – seconds to wait after “queue empty” before filling
- `AUTOFILL_MAX_PULL` – how many tracks to enqueue per autofill
- `DEFAULT_AUTOFILL_URL` – default Suno URL to pull from
- `DEFAULT_AUTOFILL_CSV` – CSV to seed autofill when URL isn’t defined
- `AUTOFILL_LIKES_PER_USER` – how many liked tracks to sample per user
- `REMOVE_NP_AFTER_SONGS` – how many subsequent songs before pruning old Now Playing cards (autofill only)

---

## 🕹 Commands Overview

**Tip:** The bot has a custom `!help` that:

- Auto-discovers commands
- Splits large lists into multiple embed fields
- Hides admin-only commands unless you use `!help admin`

Below is an overview – always trust `!help` for the latest signatures.

### Voice / Session

- `!join [channel]` – Join your voice channel
- `!leave` – Leave the current voice channel
- `!create_music_channel` – Create & join a `#music` channel (admin-only)
- `!test_speak` – Play a test TTS message in the voice channel

### Queue

- `!play <url or search>` – Add a song to the queue  
  - Supports Suno links and generic URLs (YouTube, etc.)
- `!queue` – Show current queue
- `!skip` – Skip the current track
- `!stop` – Stop playback and (optionally) clear queue
- `!shuffle` – Shuffle the queue
- `!volume <0-200>` – Set volume (100 = default)

### Playlists

- `!playlist_create <name>`
- `!playlist_add <name> <url>`
- `!playlist_delete <name>`
- `!load_playlist <name>`

### Suno Integration

- `!add_user <@discord_user> <suno_username>` – Map a Discord user to a Suno handle
- `!add_user_songs <suno_username>` – Pull that user’s Suno songs into a default playlist

### Stats & History

Slash commands and prefix pairs:

- `/history [limit]` / `!history [limit]`  
  Show recent radio plays for this server.

- `/top [range] [limit]` / `!top [range] [limit]`  
  Show top tracks for a time window: `day`, `week`, `month`, or `all`.

- `/history_clear [scope]` / `!history_clear [scope]`  
  Admin-only: clear history for the guild or for all data.

### Likes & “Liked Radio”

There is a dedicated likes table in the DB and helper functions for:

- Liking/unliking a track
- Counting likes per track
- Building “top liked for users” selections

Prefix and/or slash commands are wired to these helpers so you can:

- Like/unlike the currently playing song
- Build a queue seeded from everyone’s favourites

Use `!help` to see the exact names & usage for your build.

### Slash Commands

The bot automatically syncs slash commands on startup.  
You should see at least:

- `/play`
- `/history`
- `/top`
- `/history_clear`
- Any other slash commands exposed by the music cog

---

## 🧱 Project Structure

```text
suno-radio-bot/
├── dev.py              # Hot-reload wrapper for development
├── run.py              # Production entry point (loads dotenv and starts bot)
├── pyproject.toml      # Poetry/metadata (alt dep definition)
├── requirements.txt    # Primary Python dependencies
├── suno_radio.db       # Default SQLite DB (can be overridden by env)
├── src/
│   ├── bot.py          # Bot setup, help command, cog loading, DB init
│   ├── cogs/
│   │   ├── music.py    # Music, queue, Suno, autofill, likes, embeds
│   │   └── stats.py    # History and top-track commands
│   ├── data/
│   │   ├── db.py       # SQLite helpers for tracks/plays/likes
│   │   └── persistence.py  # JSON guild data (queues/playlists/user mappings)
│   └── utils/
│       ├── yt_extractor.py # yt-dlp wrapper / generic extractor
│       ├── scraper.py      # Suno page/playlist/profile scraper
│       └── test_speak.py   # TTS helper (macOS + gTTS)
├── docs/               # (Optional) extra documentation
└── data/               # Created at runtime, guild_*.json etc.
```

---

## 🔊 TTS Notes (Optional)

TTS is **not required** for normal music playback.

The helper in `src/utils/test_speak.py`:

- Uses **macOS NSSpeechSynthesizer** via `AppKit` when available
- Falls back to **gTTS** + `ffmpeg` for non-macOS setups

If you want to use `!test_speak`, you’ll need:

- macOS + `pyobjc` installed **or**
- `gtts` + `ffmpeg` available on your system

---

## 🔐 Discord Permissions

The bot expects at least:

- Send Messages
- Use Slash Commands
- Connect
- Speak
- Use Voice Activity
- Manage Channels (for `create_music_channel`)

---

## 🩹 Troubleshooting

**No sound when playing music**

- Confirm FFmpeg is installed and on your `PATH`
- Check the bot has `Connect` and `Speak` permissions
- Make sure it’s actually in a voice channel

**Queue / persistence issues**

- Ensure the `data/` directory is writable by the bot process
- If JSON gets corrupted, you may need to delete the affected `data/guild_*.json`

**Opus errors**

- The bot tries to load `libopus` for compressed voice
- If it fails, it falls back to raw PCM (higher bandwidth but should still work)

**TTS fails**

- On macOS: confirm `pyobjc` is installed
- Elsewhere: install `gtts` and confirm `ffmpeg` is available

---

## 🤝 Contributing

1. Fork this repository
2. Create a feature branch
3. Make your changes
4. Add or adjust tests/docs where relevant
5. Submit a pull request

---

## 📜 License

This project is licensed under the **MIT License**.
