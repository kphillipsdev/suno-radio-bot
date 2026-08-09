# Suno Radio Bot

A self-hosted Discord music bot built for **Suno AI radio**: continuous autofill playback, song requests, anonymous contests, guessing games, and play stats — with YouTube / SoundCloud support via `yt-dlp`.

This repo matches the live **SubotLive** deployment.

---

## Highlights

- **Suno-first playback** — play songs, playlists, and profiles from Suno links; scrape title, artist, prompts, and lyrics when available
- **Idle radio (autofill)** — when the queue empties, pull the next batch from a CSV seed list, a Suno URL, and/or liked tracks
- **Song requests** — `/request`, `/myrequests`, `/unrequest` (and `!` equivalents) for community autofill submissions
- **Queue & voice** — play, skip, shuffle, remove, volume, join/leave; rich Now Playing cards with interactive controls
- **Anonymous contests** — submit Suno songs privately, host listening parties, vote, then reveal the winner
- **Guessing games** — guess the song or artist against the radio queue
- **Play history & tops** — SQLite-backed `/history` and `/top` (day / week / month / year / all)
- **Wrapped dashboard** (optional) — FastAPI web UI with Discord OAuth for personal + server stats

---

## Commands

Prefix (`!`) and slash (`/`) are both available where noted. Run `!help` in Discord for the live list (admin-only commands are hidden from non-admins).

### Playback & queue
| Command | Description |
|--------|-------------|
| `!play <url>` / `!p` | Queue a Suno / YouTube / SoundCloud / direct audio URL |
| `!playlist <url> [max]` / `!pl` | Bulk-enqueue a Suno playlist, profile, or handle |
| `!queue` / `!q` | Show the queue |
| `!skip` / `!s` | Skip the current track |
| `!remove <n>` / `!rm` | Remove a queue item by position |
| `!shuffle` | Shuffle the queue |
| `!stop` | Stop playback and clear the queue |
| `!queue_clear` | Clear the queue without the full stop flow |
| `!volume <0-100>` | Set playback volume |
| `!join [channel]` | Join a voice channel |
| `!leave` | Leave voice (disables auto-rejoin) |
| `!song_info` / `!si` | Show lyrics / prompt for the current track |

### Radio & requests
| Command | Description |
|--------|-------------|
| `!autofill on\|off\|set\|unset\|reload\|status` | Manage idle radio |
| `/request` / `!request <url>` | Add a song to the autofill CSV |
| `/myrequests` / `!myrequests` | List your autofill songs |
| `/unrequest` / `!unrequest` | Remove one (or all) of your requests |
| `/autofill_saves` / `!mylikes` | Manage liked songs used by autofill |
| `!autofill_health` | Find broken-looking autofill entries (admin cleanup) |

### Contests
| Command | Description |
|--------|-------------|
| `!contest help` | Full contest help |
| `!contest add <suno url>` | Submit a song anonymously |
| `!contest play` | Host a listening party, then open voting |
| `!contest status` | Entry count and state |
| `!contest new [name]` | Start a fresh contest (admin) |
| `!contest close` / `deadline` / `name` | Manage submissions (admin) |
| `!contest entries` / `submitters` | Private admin lists |
| `!contest results` | Reveal entries and announce the winner (admin) |
| `!contest cancel` | Abort the contest (admin) |

### Games & stats
| Command | Description |
|--------|-------------|
| `!startgame [rounds]` / `!guess` | Guess the song |
| `!startgame artist [rounds]` | Guess the artist |
| `!stopgame` | End the current game |
| `/history` / `!history` | Recent plays for this server |
| `/top` / `!top [day\|week\|month\|year\|all]` | Most-played tracks |
| `/history_clear` / `!history_clear` | Clear history (admin) |

### Admin extras
| Command | Description |
|--------|-------------|
| `!reload` | Reload the music cog |
| `!reset_state` | Reset in-memory guild state |
| `!radio_cleanup [limit]` | Delete noisy bot messages in the radio channel |
| `!queue_limit …` | Per-user queue caps |
| `!np_clean …` | Now Playing card cleanup toggles |
| `!ping` | Latency info |

---

## Quick start

### Prerequisites
- Python 3.10+ recommended
- [FFmpeg](https://ffmpeg.org/download.html) on `PATH`
- A Discord bot token ([Developer Portal](https://discord.com/developers/applications))

### Setup
```bash
git clone https://github.com/kphillipsdev/suno-radio-bot.git
cd suno-radio-bot

python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env and set at least BOT_TOKEN
```

Invite the bot with scopes `bot` + `applications.commands`, and permissions: **Send Messages**, **Embed Links**, **Connect**, **Speak**, **Use Voice Activity**. Enable **Message Content Intent** and **Server Members Intent** if you use features that need them.

### Run
```bash
# Development (extra logging / reload helpers)
python dev.py

# Production
python run.py
```

Example systemd units live under [`docs/`](docs/) (`subot-live.service`, `subot-web.service`).

---

## Configuration

Copy [`.env.example`](.env.example) → `.env`. Important knobs:

| Variable | Purpose |
|----------|---------|
| `BOT_TOKEN` | Discord bot token (**required**) |
| `SUNO_RADIO_DB` | SQLite path (default `./suno_radio.db`) |
| `RADIO_CONTROL_CHANNEL` | Text channel for Now Playing / autofill / contest cards |
| `RADIO_VC_ID` | Pinned voice channel for autofill radio |
| `AUTOFILL_FEATURE` | `1` to enable idle radio |
| `AUTOFILL_CSV_PATH` / `DEFAULT_AUTOFILL_CSV` | Seed list for `/request` + autofill |
| `REQUEST_MAX_PER_USER` | Cap on autofill requests per member |
| `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET` | OAuth for the Wrapped dashboard |
| `DASHBOARD_BASE_URL` / `DASHBOARD_SECRET_KEY` | Dashboard URL + session signing |
| `WRAPPED_DASHBOARD_ENABLED` | `1` to serve the web UI |

Never commit `.env`. Runtime data (DB, `data/`, `images/`, `logs/`, autofill CSVs) is gitignored.

---

## Wrapped dashboard (optional)

```bash
# With WRAPPED_DASHBOARD_ENABLED=1 and OAuth vars set in .env
uvicorn web.app:app --host 127.0.0.1 --port 8100
```

Serves public stats plus personal stats behind Discord OAuth. The app opens the bot DB **read-only** (WAL-safe alongside the bot process). See `docs/subot-web.service` and `docs/radio.vectorsofstars.ca.conf` for a production reverse-proxy example.

---

## Project layout

```
├── run.py / dev.py          # Entrypoints
├── requirements.txt         # Python deps
├── .env.example             # Config template (no secrets)
├── docs/                    # systemd + nginx examples
├── web/                     # FastAPI Wrapped dashboard
└── src/
    ├── bot.py               # Bot bootstrap, cog loading
    ├── cogs/
    │   ├── music/           # Playback, queue, autofill, requests
    │   ├── contest.py       # Anonymous song contests
    │   ├── games.py         # Guess song / artist
    │   └── stats.py         # History & top charts
    ├── data/                # SQLite + guild JSON persistence
    ├── migrations/          # SQL schema
    └── utils/               # Scrapers, extractors, contest art
```

---

## Stack

- **discord.py** — bot + voice
- **yt-dlp** — YouTube / SoundCloud / generic URLs
- **BeautifulSoup / custom scrapers** — Suno metadata & playlists
- **SQLite** — play history, likes, contests
- **FFmpeg** — Discord audio pipeline
- **FastAPI + uvicorn** — optional Wrapped dashboard
- **Pillow** — contest card visuals

---

## Troubleshooting

**No audio**
- Confirm FFmpeg is installed and on `PATH`
- Check Connect / Speak permissions and that the bot is in a voice channel

**Autofill never starts**
- `AUTOFILL_FEATURE=1` and `!autofill on`
- Set a source with `!autofill set <url>` and/or populate the autofill CSV
- Prefer setting `RADIO_VC_ID` so radio stays pinned to the right channel

**Contests / Now Playing in the wrong channel**
- Set `RADIO_CONTROL_CHANNEL` to the radio text channel ID

**Dashboard 503**
- Set `WRAPPED_DASHBOARD_ENABLED=1` and restart the web process

**Logs (systemd)**
```bash
journalctl -u subot-live -f
journalctl -u subot-web -f
```

---

## License

MIT — see repository license if present.

---

Made for Suno radio nights · [github.com/kphillipsdev/suno-radio-bot](https://github.com/kphillipsdev/suno-radio-bot)
