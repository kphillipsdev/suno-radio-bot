# src/cogs/music.py

import discord
from discord.ext import commands, tasks
from discord import ui, app_commands
from collections import deque, defaultdict
import asyncio
import random
import logging
import os
import concurrent.futures
import time
import re
import datetime
from discord.utils import escape_markdown

from src.data.persistence import load_data, save_data
from src.utils.yt_extractor import extract_song_info
from src.utils.scraper import scrape_suno_songs
from src.utils.prefetch import prefetch_to_file

# ===== Embed + Formatting Helpers ===========================================
EMBED_COLOR_PLAYING = 0x580fd6
EMBED_COLOR_ADDED   = 0xc1d4d6

# ---- Prefetch config (env-driven) ------------------------------------------
PREFETCH_MODE    = os.getenv("PREFETCH_MODE", "full").lower()  # "none" | "warmup" | "full"
PREFETCH_BYTES   = int(os.getenv("PREFETCH_BYTES", "524288"))  # ~512 KB for warmup
PREFETCH_TIMEOUT = int(os.getenv("PREFETCH_TIMEOUT", "25"))    # seconds
PREFETCH_DIR     = os.getenv("PREFETCH_DIR", "songs") or "songs"

# Queue/playlist clear policy toggles
CLEAR_PLAYLISTS_ON_STOP   = os.getenv("CLEAR_PLAYLISTS_ON_STOP", "0") == "1"
CLEAR_PLAYLISTS_ON_RELOAD = os.getenv("CLEAR_PLAYLISTS_ON_RELOAD", "0") == "1"

# ---- Autofill (idle radio) -------------------------------------------------
AUTOFILL_DELAY_SEC = int(os.getenv("AUTOFILL_DELAY_SEC", "30"))   # wait after finishing
AUTOFILL_MAX_PULL  = int(os.getenv("AUTOFILL_MAX_PULL", "25"))    # how many to enqueue per fill
DEFAULT_AUTOFILL_URL = os.getenv("DEFAULT_AUTOFILL_URL", "").strip()

# ---- Feature flags ----------------------------------------------------------
# When OFF, the autofill commands are hidden and disabled, and autofill won't trigger.
AUTOFILL_FEATURE = os.getenv("AUTOFILL_FEATURE", "1") == "1"

# ---- Queue add limit (peak throttle) ---------------------------------------
QUEUE_LIMIT_DEFAULT_ENABLED = os.getenv("QUEUE_LIMIT_DEFAULT_ENABLED", "1") == "1"
QUEUE_LIMIT_MAX_PER_ADD = int(os.getenv("QUEUE_LIMIT_MAX_PER_ADD", "10"))  # default cap
QUEUE_MAX_PER_USER = int(os.getenv("QUEUE_MAX_PER_USER", "3"))  # hard cap per user in queue

async def maybe_prefetch(song: dict) -> str | None:
    """
    Uses env PREFETCH_MODE to optionally warm up or fully cache the audio.
    Returns a local file path if a full download happened; otherwise None.
    """
    mode = PREFETCH_MODE
    if mode not in ("warmup", "full"):
        return None

    url = str(song.get("url") or "").strip()
    if not url or url.startswith("songs/"):
        return None  # already local or no url

    referer = song.get("suno_url") or "https://suno.com/"
    loop = asyncio.get_running_loop()

    if mode == "warmup":
        # partial download then discard (prime CDN/TLS)
        await loop.run_in_executor(
            None,
            lambda: prefetch_to_file(
                url,
                out_dir=PREFETCH_DIR,
                timeout=min(PREFETCH_TIMEOUT, 15),
                referer=referer,
                full_download=False,
                max_bytes=PREFETCH_BYTES,
            )
        )
        return None

    # mode == "full"
    local_path = await loop.run_in_executor(
        None,
        lambda: prefetch_to_file(
            url,
            out_dir=PREFETCH_DIR,
            timeout=PREFETCH_TIMEOUT,
            referer=referer,
            full_download=True,
        )
    )
    if local_path:
        song["url"] = local_path
        song["local_file"] = local_path
    return local_path


def _fmt_duration(d):
    """Accept seconds or 'MM:SS'/'HH:MM:SS' string; return human readable."""
    if d is None:
        return "—"
    if isinstance(d, str):
        return d
    try:
        sec = int(d)
        m, s = divmod(max(sec, 0), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    except Exception:
        return str(d)

def _truncate(text: str | None, limit: int = 300) -> str:
    if not text:
        return "—"
    t = text.strip()
    return t if len(t) <= limit else (t[:limit - 1] + "…")

def _derive_suno_url(track: dict) -> str | None:
    """
    Prefer explicit 'suno_url', else derive from known Suno CDN or local cache paths.
    """
    if track.get("suno_url"):
        return track["suno_url"]

    url = (track.get("url") or "").strip()
    # songs/{id}.mp3
    if url.startswith("songs/") and url.endswith(".mp3"):
        song_id = url[6:-4]
        return f"https://suno.com/song/{song_id}"

    # cdn1.suno.ai/.../{id}.mp3
    m = re.search(r"/([a-f0-9\-]{8,})\.mp3", url, re.I)
    if m:
        return f"https://suno.com/song/{m.group(1)}"

    # if track had a page url cached elsewhere
    page = track.get("page") or track.get("page_url")
    if page and "suno.com" in page:
        return page

    return None

def _track_title_link(track: dict) -> str:
    title = escape_markdown((track.get("title") or "Untitled").strip())
    link  = _derive_suno_url(track) or (track.get("url") or "").strip()
    # Only link if it's a Suno/page URL; avoid deep linking raw audio if ugly
    if link and ("suno.com" in link):
        return f"[**{title}**]({link})"
    return f"**{title}**"

def _artist_line(track: dict) -> str:
    # Back-compat if older entries still store 'author'
    artist = (track.get("artist") or track.get("author") or "Unknown").strip()
    return f"*by {escape_markdown(artist)}*"

def _filler_badge(track: dict) -> str:
    """
    Returns a short inline badge for autofill tracks.
    """
    return "  *(~filler~)*" if track.get("_autofill") else ""

def _prompt_text(track: dict) -> str:
    # Prefer 'prompt' if present, otherwise fall back to common fields
    prompt = track.get("prompt") or ""
    return _truncate(prompt, 300)

def _thumb(track: dict) -> str | None:
    return track.get("thumbnail") or track.get("thumb") or track.get("image")

def _format_upcoming_list(tracks: list[dict], limit: int = 2) -> str:
    if not tracks:
        return "—"
    lines = []
    for i, t in enumerate(tracks[:limit], start=1):
        title = _track_title_link(t) + _filler_badge(t)  # ⬅️ add badge
        artist = (t.get("artist") or t.get("author") or "Unknown").strip()
        byline = f"*by {escape_markdown(artist)}*"
        requester = (t.get("requester_mention")
                     or (f"<@{t['requester_id']}>" if t.get("requester_id") else None)
                     or t.get("requester_tag")
                     or t.get("requester_name")
                     or "someone")
        lines.append(f"{i}. {title} {byline} • requested by {requester}")
    return "\n".join(lines)

def _join_info_blocks(prompt: str | None, lyrics: str | None) -> str:
    parts = []
    if prompt and prompt.strip():
        parts.append(prompt.strip())
    if lyrics and lyrics.strip():
        if parts:
            parts.append("")  # spacer line between prompt and lyrics
        parts.append(lyrics.strip())
    return "\n".join(parts).strip() or "*No prompt/lyrics available for this track.*"

def _chunk_text(s: str | None, limit: int = 3900) -> list[str]:
    """Split long text into Discord-safe chunks, preferring paragraph/line breaks."""
    if not s:
        return []
    s = s.strip()
    if len(s) <= limit:
        return [s]

    out: list[str] = []
    remaining = s
    while len(remaining) > limit:
        # try paragraph break
        cut = remaining.rfind("\n\n", 0, limit)
        if cut == -1:
            # try single line break
            cut = remaining.rfind("\n", 0, limit)
        if cut == -1:
            # hard cut
            cut = limit
        out.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        out.append(remaining)
    return out

def build_now_playing_embed(track: dict, requester_mention: str | None, upcoming_tracks: list[dict] | None = None):
    desc = [
        _track_title_link(track) + _filler_badge(track),
        _artist_line(track),
        ""
    ]
    embed = discord.Embed(
        title="🎵 Now Playing",
        description="\n".join(desc),
        color=EMBED_COLOR_PLAYING
    )
    embed.add_field(name="Duration", value=_fmt_duration(track.get("duration")), inline=True)

    ts = int(track.get("requested_at") or datetime.datetime.now(datetime.timezone.utc).timestamp())
    req_val = (requester_mention or "—") + f" at <t:{ts}:t>"
    embed.add_field(name="Requested by", value=req_val, inline=True)

    if upcoming_tracks:
        embed.add_field(
            name="Up next",
            value=_format_upcoming_list(upcoming_tracks, limit=2),
            inline=False
        )

    thumb = _thumb(track)
    if thumb:
        embed.set_thumbnail(url=thumb)

    embed.set_footer(text="Suno Music Bot")
    embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return embed

def build_added_embed(track: dict, requester_mention: str | None, position: int | None = None):
    """
    Added card: heading = song title (clickable), body = artist,
    fields = Duration, Requested by (with original request time), Position if provided.
    """
    desc = [
        _track_title_link(track) + _filler_badge(track),
        _artist_line(track),
        ""
    ]
    embed = discord.Embed(
        title="➕ Added",
        description="\n".join([s for s in desc if s is not None]),
        color=EMBED_COLOR_ADDED
    )
    # Core fields
    embed.add_field(name="Duration", value=_fmt_duration(track.get("duration")), inline=True)

    ts = int(track.get("requested_at") or datetime.datetime.now(datetime.timezone.utc).timestamp())
    req_val = (requester_mention or "—") + f" at <t:{ts}:t>"
    embed.add_field(name="Requested by", value=req_val, inline=True)

    if isinstance(position, int) and position >= 1:
        embed.add_field(name="Position", value=f"#{position}", inline=False)

    thumb = _thumb(track)
    if thumb:
        embed.set_thumbnail(url=thumb)

    return embed


# ---- Song Info helpers (module scope) --------------------------------------
def _render_song_header(song: dict) -> str:
    # Reuse existing helpers for safety/consistency
    title_raw = (song.get("title") or "Unknown Title").strip()
    title = escape_markdown(title_raw)
    link  = _derive_suno_url(song) or (song.get("url") or "").strip()

    artist_raw = (song.get("artist") or song.get("author") or "Unknown Artist").strip()
    artist = escape_markdown(artist_raw)

    # Only link if Suno/page URL; avoid raw audio deep links
    if link and ("suno.com" in link):
        title_md = f"**[{title}]({link})**"
    else:
        title_md = f"**{title}**"

    byline_md = f"*By {artist}*"
    return f"{title_md}\n{byline_md}".strip()


def _render_prompt_lyrics_block(song: dict) -> str:
    prompt = (song.get("prompt") or "").strip()
    lyrics = (song.get("lyrics") or "").strip()

    parts = []
    parts.append("**Prompt**")
    parts.append(prompt if prompt else "_No prompt provided._")
    parts.append("")  # blank line
    parts.append("**Lyrics**")
    parts.append(lyrics if lyrics else "_No lyrics provided._")

    return "\n".join(parts).strip()
# ---------------------------------------------------------------------------


# ===== Music Cog =============================================================
class MusicCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queues = defaultdict(deque)
        self.playlists = defaultdict(lambda: defaultdict(deque))
        self.user_mappings = defaultdict(dict)
        self.volumes = defaultdict(lambda: float(os.getenv("DEFAULT_VOLUME", "1.0")))
        self.current_song = None
        self.song_start_time = None
        self.activity_task = None
        self.auto_play_enabled = {}
        self.auto_play_tasks = {}
        self.auto_playlist_urls = {}

        # feature flags (runtime)
        self._autofill_feature_on = AUTOFILL_FEATURE

        # queue limit (runtime)
        self.queue_limit_enabled = {}
        self.queue_limit_max = {}

    # ---------- moved INSIDE class ----------
    def _pick_song_from_context(self, ctx, position: int | None):
        """
        Return (song_dict, label) from current song or queue position.
        label is a short descriptor for embeds.
        """
        gid = ctx.guild.id
        if position is None:
            if self.current_song:
                return self.current_song, "Now Playing"
            q = self.queues[gid]
            if q:
                return q[0], "Next Up"
            return None, "No song is playing and the queue is empty."

        # explicit queue position (1-based)
        try:
            idx = int(position) - 1
        except Exception:
            return None, f"Invalid position."
        q = self.queues[gid]
        if 0 <= idx < len(q):
            return list(q)[idx], f"Queued song #{idx+1}"
        return None, f"Invalid position. Must be between 1 and {len(q)}."

    async def _ensure_song_metadata(self, song: dict) -> dict:
        """
        If prompt/lyrics are missing, try to refresh from the Suno page (or URL we have).
        Runs extractor in a thread.
        """
        need_prompt = not song.get("prompt")
        need_lyrics = not song.get("lyrics")
        if not (need_prompt or need_lyrics):
            return song

        page_url = song.get("suno_url") or _derive_suno_url(song) or song.get("url") or ""
        if not page_url:
            return song

        loop = asyncio.get_running_loop()

        def _do_extract():
            try:
                return extract_song_info(page_url)
            except Exception as e:
                print(f"[song_info] refresh extract failed for {page_url}: {e}")
                return None

        info = await loop.run_in_executor(None, _do_extract)
        if not info:
            return song

        for k in ("prompt", "lyrics", "title", "artist", "duration", "thumbnail"):
            if (k not in song or song.get(k) in (None, "", 0)) and info.get(k):
                song[k] = info[k]
        if not song.get("suno_url") and info.get("suno_url"):
            song["suno_url"] = info["suno_url"]
        return song
    # ---------- end moved helpers ----------

    def _count_user_queued(self, gid: int, user_id: int, include_filler: bool = False) -> int:
        """
        Count how many tracks in the queue belong to a given requester.
        By default ignores autofill/filler tracks.
        """
        q = self.queues[gid]
        if not q:
            return 0
        n = 0
        for t in q:
            if (not include_filler) and t.get("_autofill"):
                continue
            if t.get("requester_id") == user_id:
                n += 1
        return n

    def _user_slots_remaining(self, gid: int, user_id: int) -> int:
        """
        How many more tracks the user may add before hitting QUEUE_MAX_PER_USER.
        """
        have = self._count_user_queued(gid, user_id, include_filler=False)
        return max(0, int(QUEUE_MAX_PER_USER) - have)

    def _deny_user_cap_embed(self, requester_mention: str | None = None) -> discord.Embed:
        who = requester_mention or "You"
        return discord.Embed(
            title="🚫 Per-User Queue Limit",
            description=f"{who} already {'have' if requester_mention else 'has'} **{QUEUE_MAX_PER_USER}** song(s) in the queue. "
                        f"Please wait until one finishes before adding more.",
            color=0xe74c3c
        )

    # ===== Feature visibility =================================================
    def _apply_feature_visibility(self):
        """
        Hide/disable commands for features that are OFF.
        Your custom HelpCommand already skips hidden/disabled.
        """
        autofill_names = {"autofill_set", "autofill_on", "autofill_off", "autofill_status"}
        for cmd in self.get_commands():
            if cmd.name in autofill_names:
                cmd.hidden = not self._autofill_feature_on
                cmd.enabled = self._autofill_feature_on

    # ===== AUTOFILL (Idle Radio) ============================================
    def _is_autofill_enabled(self, gid: int) -> bool:
        return (
            self._autofill_feature_on
            and bool(self.auto_play_enabled.get(gid))
            and bool(self.auto_playlist_urls.get(gid))
        )

    def _cancel_autofill_task(self, gid: int):
        task = self.auto_play_tasks.get(gid)
        if task and not task.done():
            task.cancel()
        self.auto_play_tasks[gid] = None

    def _clear_autofill_from_queue(self, gid: int):
        """Remove any 'autofill' flagged tracks from the queue."""
        dq = self.queues[gid]
        if not dq:
            return
        kept = [t for t in dq if not t.get("_autofill")]
        dq.clear()
        dq.extend(kept)

    async def _enqueue_autofill_batch(self, ctx, gid: int):
        """Scrape and resolve tracks from the configured autofill URL."""
        url = (self.auto_playlist_urls.get(gid) or "").strip()
        if not url:
            return 0

        # run sync scraper off the loop
        raw = await asyncio.get_event_loop().run_in_executor(
            None, lambda: scrape_suno_songs(url, limit=AUTOFILL_MAX_PULL)
        )
        if not raw:
            return 0

        # Shuffle the raw order before resolving, so every fill feels fresh
        random.shuffle(raw)

        tracks = await self._resolve_tracks(raw, max_workers=6)

        # small shuffle again post-resolve to break any resolver ordering
        random.shuffle(tracks)

        now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        for t in tracks:
            t["_autofill"] = True                     # internal flag
            t.setdefault("tags", []).append("filler") # simple tag list
            t["requester_id"] = self.bot.user.id if self.bot.user else None
            t["requester_tag"] = "Autofill"
            t["requester_name"] = "Autofill"
            t["requester_mention"] = None
            t["requested_at"] = now_ts
            self.queues[gid].append(t)

        save_data(gid, self.queues, self.playlists, self.user_mappings)
        return len(tracks)

    async def _autofill_after_delay(self, ctx, gid: int, delay: int):
        """Wait, then seed queue from autofill and kick playback if still idle."""
        try:
            await asyncio.sleep(max(0, delay))
            # If anything got queued in the meantime, bail.
            if self.queues[gid] or self.current_song:
                return
            if not self._is_autofill_enabled(gid):
                return

            added = await self._enqueue_autofill_batch(ctx, gid)
            if added > 0 and ctx.voice_client:
                await self.play_next(ctx)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[autofill] failed: {e}")
        finally:
            # Clear the handle so we can schedule again later
            self.auto_play_tasks[gid] = None

    def _schedule_autofill_if_idle(self, ctx, delay: int | None = None):
        gid = ctx.guild.id
        # Don’t double schedule
        if self.auto_play_tasks.get(gid):
            return
        if not self._is_autofill_enabled(gid):
            return
        use_delay = AUTOFILL_DELAY_SEC if (delay is None) else max(0, int(delay))
        self.auto_play_tasks[gid] = self.bot.loop.create_task(
            self._autofill_after_delay(ctx, gid, use_delay)
        )

    # ===== Queue add limit helpers ==========================================
    def _limit_is_on(self, gid: int) -> bool:
        return bool(self.queue_limit_enabled.get(gid))

    def _limit_max(self, gid: int) -> int:
        return int(self.queue_limit_max.get(gid, QUEUE_LIMIT_MAX_PER_ADD))

    def _enforce_queue_add_limit(self, gid: int, intended_count: int) -> tuple[int, str | None]:
        """
        Returns (allowed_count, notice_message|None).
        If allowed_count == 0, caller should block and show the message.
        """
        if not self._limit_is_on(gid):
            return intended_count, None
        cap = self._limit_max(gid)
        if intended_count <= cap:
            return intended_count, None
        # Nice message (exact phrasing when cap == 3)
        if cap == 3:
            msg = "You can only enter 3 songs at a time into the queue."
        else:
            msg = f"You can only enter up to **{cap}** songs at a time into the queue."
        return cap, msg

    # ========================================================================

    def get_radio_channel(self, ctx):
        RADIO_CONTROL_CHANNEL = os.getenv("RADIO_CONTROL_CHANNEL")
        if RADIO_CONTROL_CHANNEL:
            try:
                radio_channel = ctx.guild.get_channel(int(RADIO_CONTROL_CHANNEL))
                return radio_channel if radio_channel else ctx.channel
            except:
                pass
        return ctx.channel

    def format_time(self, seconds):
        """Format seconds into MM:SS."""
        mins, secs = divmod(int(seconds), 60)
        return f"{mins:02d}:{secs:02d}"

    async def _resolve_tracks(self, items: list[dict], max_workers: int = 6) -> list[dict]:
        """Given items with 'url' (Suno page), populate full metadata via extract_song_info."""
        loop = asyncio.get_event_loop()

        def _resolve_one(item: dict) -> dict:
            try:
                info = extract_song_info(item.get("url") or item.get("suno_url") or "")
                if info:
                    item.update(info)
            except Exception as e:
                print(f"[resolver] failed on {item.get('url')}: {e}")
            item.setdefault("title", "Unknown Title")
            item.setdefault("artist", "Unknown")
            item.setdefault("duration", None)
            item.setdefault("thumbnail", None)
            return item

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [loop.run_in_executor(ex, _resolve_one, it) for it in items]
            return await asyncio.gather(*futures)

    async def set_song_activity(self, song, elapsed_seconds):
        """Set the bot's Discord activity during playback."""
        try:
            title = song.get('title', 'Unknown Song')
            duration = song.get('duration', 0) or 0
            current_time = self.format_time(elapsed_seconds)
            total_time = self.format_time(duration)

            activity_name = f"🎵 {title} - {current_time} / {total_time}"
            activity = discord.Activity(
                type=discord.ActivityType.listening,
                name=activity_name[:128]
            )
            await self.bot.change_presence(activity=activity)
        except Exception as e:
            print(f"Error setting song activity: {e}")

    @tasks.loop(seconds=30)
    async def update_song_activity(self):
        """Background task to update song activity every 30 seconds."""
        if self.current_song and self.song_start_time:
            elapsed = time.time() - self.song_start_time
            await self.set_song_activity(self.current_song, elapsed)

    async def cog_load(self):
        # Restore persisted state for each guild
        for guild in self.bot.guilds:
            loaded_queues, loaded_playlists, loaded_user_mappings = load_data(guild.id)
            if guild.id in loaded_queues:
                self.queues[guild.id] = loaded_queues[guild.id]
            if guild.id in loaded_playlists:
                self.playlists[guild.id] = loaded_playlists[guild.id]
            if guild.id in loaded_user_mappings:
                self.user_mappings[guild.id] = loaded_user_mappings[guild.id]

            # --- restore autofill settings from user_mappings ---------------
            gid = guild.id
            amap = self.user_mappings[gid]
            ainfo = amap.get("autofill") if isinstance(amap, dict) else None

            enabled_default = True  # autofill ON by default

            if isinstance(ainfo, dict):
                # restore last URL & enabled
                url = (ainfo.get("url") or "").strip()
                enabled = bool(ainfo.get("enabled", enabled_default))
                if url:
                    self.auto_playlist_urls[gid] = url
                self.auto_play_enabled[gid] = enabled
            else:
                # initialize map
                if not isinstance(amap, dict):
                    amap = {}
                    self.user_mappings[gid] = amap
                self.auto_play_enabled[gid] = enabled_default

            # If no saved URL, try DEFAULT_AUTOFILL_URL and persist it so it survives restarts
            if not self.auto_playlist_urls.get(gid) and DEFAULT_AUTOFILL_URL:
                self.auto_playlist_urls[gid] = DEFAULT_AUTOFILL_URL
                # write-through to persistence
                amap = self.user_mappings[gid]
                amap["autofill"] = {
                    "url": DEFAULT_AUTOFILL_URL,
                    "enabled": self.auto_play_enabled.get(gid, enabled_default),
                }
                save_data(gid, self.queues, self.playlists, self.user_mappings)

            # --- restore queue limit settings --------------------------------
            linfo = amap.get("queue_limit") if isinstance(amap, dict) else None
            if isinstance(linfo, dict):
                self.queue_limit_enabled[gid] = bool(linfo.get("enabled", QUEUE_LIMIT_DEFAULT_ENABLED))
                self.queue_limit_max[gid] = int(linfo.get("max", QUEUE_LIMIT_MAX_PER_ADD))
            else:
                self.queue_limit_enabled[gid] = QUEUE_LIMIT_DEFAULT_ENABLED
                self.queue_limit_max[gid] = QUEUE_LIMIT_MAX_PER_ADD

        # apply feature visibility after commands are bound
        self._apply_feature_visibility()

    async def cog_unload(self):
        """Clean up resources when the cog is unloaded/reloaded."""
        if self.update_song_activity.is_running():
            self.update_song_activity.cancel()
        try:
            await self.bot.change_presence(activity=None)
        except Exception:
            pass

    @commands.hybrid_command(name='join', description='Join a voice channel')
    @app_commands.describe(channel='Voice channel to join (optional, defaults to your current)')
    async def join(self, ctx, channel: discord.VoiceChannel = None):
        """
        Join a voice channel
        """
        if not channel:
            if ctx.author.voice:
                channel = ctx.author.voice.channel
            else:
                guild = ctx.guild
                voice_channels = [c for c in guild.channels if isinstance(c, discord.VoiceChannel)]
                if not voice_channels:
                    embed = discord.Embed(title="❌ Error", description="No voice channels available!", color=0xff0000)
                    await ctx.send(embed=embed)
                    return
                channel = None
                for vc in voice_channels:
                    if ctx.guild.me.permissions_in(vc).connect:
                        channel = vc
                        break
                if not channel:
                    embed = discord.Embed(title="❌ Error", description="No voice channels I have permission to join!", color=0xff0000)
                    await ctx.send(embed=embed)
                    return

        try:
            if ctx.voice_client:
                await ctx.voice_client.move_to(channel)
            else:
                await channel.connect()
            embed = discord.Embed(title="✅ Joined", description=f"Joined {channel.name} 🎧", color=0x00ff00)
            await ctx.send(embed=embed)

            # NEW: if idle and autofill is configured+enabled, schedule it immediately
            gid = ctx.guild.id
            if not self.queues[gid] and not (ctx.voice_client and ctx.voice_client.is_playing()):
                # ensure we don't have a stale pending task and then schedule a fresh one
                self._cancel_autofill_task(gid)
                # start soon after join; use the normal delay, or change 0 to “instant start”
                self._schedule_autofill_if_idle(ctx, delay=AUTOFILL_DELAY_SEC)  # or delay=0 for instant
        except Exception as e:
            embed = discord.Embed(title="❌ Voice Connection Error", description=f"Failed to join {channel.name}: {str(e)}\nPlease check bot permissions and try again.", color=0xff0000)
            await ctx.send(embed=embed)

    @commands.hybrid_command(name='leave', description='Leave the current voice channel')
    async def leave(self, ctx):
        """
        Leave the current voice channel
        """
        if not ctx.voice_client:
            embed = discord.Embed(title="❌ Error", description="I'm not connected to a voice channel!", color=0xff0000)
            await ctx.send(embed=embed)
            return
        channel_name = ctx.voice_client.channel.name
        await ctx.voice_client.disconnect()

        gid = ctx.guild.id
        self._cancel_autofill_task(gid)
        self._clear_autofill_from_queue(gid)

        # Clear activity
        self.current_song = None
        self.song_start_time = None
        if self.update_song_activity.is_running():
            self.update_song_activity.stop()
        await self.bot.change_presence(activity=None)
        embed = discord.Embed(title="👋 Left", description=f"Left {channel_name} 🎧", color=0xff0000)
        await ctx.send(embed=embed)

    @commands.command(name='play')
    async def play(self, ctx, url: str = ""):
        """
        Plays a song by url (Suno url supported only) or scrapes recent if blank.
        """
        if not ctx.voice_client:
            await ctx.invoke(self.join)

        guild_id = ctx.guild.id
        queue = self.queues[guild_id]

        # User activity cancels any pending or queued autofill
        self._cancel_autofill_task(guild_id)
        self._clear_autofill_from_queue(guild_id)

        # Capture requester
        requester_id = ctx.author.id
        requester_tag = str(ctx.author)
        requester_name = ctx.author.display_name
        requester_mention = ctx.author.mention
        requested_at = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        # Per-user cap enforcement
        remaining_user_slots = self._user_slots_remaining(guild_id, requester_id)

        try:
            if not url.strip():
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    raw_tracks = await asyncio.get_event_loop().run_in_executor(
                        executor, scrape_suno_songs, "", 5
                    )
                if not raw_tracks:
                    embed = discord.Embed(title="❌ Error", description="Failed to scrape Suno songs.", color=0xff0000)
                    await ctx.send(embed=embed)
                    return

                # enforce queue add limit (bulk)
                intended = len(raw_tracks)

                # existing per-add throttle
                allowed_by_add, notice = self._enforce_queue_add_limit(guild_id, intended)

                # NEW: per-user remaining slots
                if remaining_user_slots <= 0:
                    await ctx.send(embed=self._deny_user_cap_embed(requester_mention))
                    return

                allowed_total = min(allowed_by_add, remaining_user_slots)
                if allowed_total <= 0:
                    await ctx.send(embed=self._deny_user_cap_embed(requester_mention))
                    return
                if allowed_total < intended:
                    raw_tracks = raw_tracks[:allowed_total]

                tracks = await self._resolve_tracks(raw_tracks, max_workers=4)

                for song in tracks:
                    song["requester_id"] = requester_id
                    song["requester_tag"] = requester_tag
                    song["requester_name"] = requester_name
                    song["requester_mention"] = requester_mention
                    song["requested_at"] = requested_at
                    self.queues[guild_id].append(song)

                save_data(guild_id, self.queues, self.playlists, self.user_mappings)

                desc = f"Added {len(tracks)} songs"
                if notice:
                    desc += f"\n\n{notice}"
                embed = discord.Embed(
                    title="➕ Added",
                    description=desc,
                    color=EMBED_COLOR_ADDED
                )
                embed.add_field(
                    name="Requested by",
                    value=f"{requester_mention} at <t:{requested_at}:t>",
                    inline=True
                )
                await ctx.send(embed=embed)
            else:
                # Single song path
                song = extract_song_info(url)
                song.setdefault("artist", song.pop("author", None))  # back-compat

                # requester fields
                song["requester_id"] = requester_id
                song["requester_tag"] = requester_tag
                song["requester_name"] = requester_name
                song["requester_mention"] = requester_mention
                song["requested_at"] = requested_at

                # Single song path
                if remaining_user_slots <= 0:
                    await ctx.send(embed=self._deny_user_cap_embed(requester_mention))
                    return

                queue.append(song)
                position = len(queue)  # position after append
                save_data(guild_id, self.queues, self.playlists, self.user_mappings)

                embed = build_added_embed(song, requester_mention=requester_mention, position=position)
                await ctx.send(embed=embed)

            if not ctx.voice_client.is_playing():
                await self.play_next(ctx)

        except Exception as e:
            embed = discord.Embed(
                title="❌ Error",
                description=f"Failed to add song: {str(e)}.",
                color=0xff0000
            )
            await ctx.send(embed=embed)

    async def play_next(self, ctx):
        guild_id = ctx.guild.id
        queue = self.queues[guild_id]
        if not queue:
            return
        if not ctx.voice_client:
            embed = discord.Embed(title="❌ Connection Lost", description="Bot lost voice connection!", color=0xff0000)
            channel = self.get_radio_channel(ctx)
            await channel.send(embed=embed)
            self.current_song = None
            self.song_start_time = None
            if self.update_song_activity.is_running():
                self.update_song_activity.stop()
            await self.bot.change_presence(activity=None)
            return

        channel = self.get_radio_channel(ctx)
        song = queue.popleft()

        # ---- PREFETCH handling ---------------------------------------------
        local_to_delete = None  # ensure defined for after_playing
        try:
            lp = await maybe_prefetch(song)
            if lp and PREFETCH_MODE == "full":
                local_to_delete = lp
        except Exception as e:
            print(f"Prefetch failed for {song.get('url')}: {e}")
        # --------------------------------------------------------------------

        # FFmpeg options - improve detection for streams
        if str(song.get('url', '')).startswith(('http://', 'https://')):
            ffmpeg_options = {
                'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -fflags +nobuffer -nostdin',
                'options': '-vn -probesize 32M -analyzeduration 20M -af aresample=async=1:min_hard_comp=0.100:first_pts=0'
            }
        else:
            ffmpeg_options = {'options': '-vn -probesize 32M -analyzeduration 20M -af aresample=async=1:min_hard_comp=0.100:first_pts=0'}

        try:
            source = discord.FFmpegPCMAudio(song['url'], **ffmpeg_options)
            volume_transformer = discord.PCMVolumeTransformer(source, volume=self.volumes[guild_id])
        except Exception as e:
            print(f"Audio source error: {e} for {song.get('url')}")
            embed = discord.Embed(
                title="❌ Playback Error",
                description=f"Failed to play {song.get('title','Unknown')}: {str(e)}",
                color=0xff0000
            )
            try:
                await ctx.send(embed=embed)
            except Exception:
                pass
            if queue:
                asyncio.run_coroutine_threadsafe(self.play_next(ctx), self.bot.loop)
            return

        def after_playing(error):
            try:
                if error:
                    print(f"Player error: {error}")

                if local_to_delete:
                    try:
                        if os.path.exists(local_to_delete):
                            os.remove(local_to_delete)
                    except Exception as _e:
                        print(f"Prefetch cleanup failed: {local_to_delete}: {_e}")

                self.current_song = None
                self.song_start_time = None
                if self.update_song_activity.is_running():
                    self.update_song_activity.stop()
                asyncio.run_coroutine_threadsafe(self.bot.change_presence(activity=None), self.bot.loop)

                if queue and ctx.voice_client:
                    asyncio.run_coroutine_threadsafe(self.play_next(ctx), self.bot.loop)
                elif not queue:
                    embed2 = discord.Embed(title="⏹️ Queue Empty", description="Finished playing! 🎉", color=0x00ff00)
                    asyncio.run_coroutine_threadsafe(self.get_radio_channel(ctx).send(embed=embed2), self.bot.loop)
                    # schedule idle radio (autofill) if configured — thread-safe trigger
                    try:
                        self.bot.loop.call_soon_threadsafe(lambda: self._schedule_autofill_if_idle(ctx))
                    except Exception as _e:
                        print(f"[autofill schedule] {_e}")
            except Exception as e2:
                print(f"after_playing crashed: {e2}")

        ctx.voice_client.play(volume_transformer, after=after_playing)

        # Set bot activity
        if self.update_song_activity.is_running():
            self.update_song_activity.stop()
        self.current_song = song
        self.song_start_time = time.time()
        await self.set_song_activity(song, 0.0)
        if not self.update_song_activity.is_running():
            self.update_song_activity.start()

        # NOW PLAYING card (prefer radio control channel, fallback to ctx)
        requester = (song.get("requester_mention")
                     or song.get("requester_name")
                     or song.get("requester_tag"))
        upcoming_two = list(self.queues[guild_id])[:2]
        np_embed = build_now_playing_embed(song, requester_mention=requester, upcoming_tracks=upcoming_two)
        try:
            await self.get_radio_channel(ctx).send(embed=np_embed)
        except Exception:
            await ctx.send(embed=np_embed)

    @commands.command(name='queue')
    async def show_queue(self, ctx):
        """
        Shows the current queue
        """
        guild_id = ctx.guild.id
        queue = self.queues[guild_id]
        if not queue:
            embed = discord.Embed(
                title="📋 Queue",
                description="Queue is empty! Add songs with `!play`.",
                color=0x0099ff
            )
            await ctx.send(embed=embed)
            return

        max_lines = 15
        lines = []
        for i, song in enumerate(queue, start=1):
            title_link = _track_title_link(song) + _filler_badge(song)
            requester = (song.get("requester_mention")
                         or (f"<@{song['requester_id']}>" if song.get("requester_id") else None)
                         or song.get("requester_tag")
                         or song.get("requester_name")
                         or "someone")
            lines.append(f"{i}. {title_link} • requested by {requester}")
            if i >= max_lines:
                break

        remaining = len(queue) - max_lines
        if remaining > 0:
            lines.append(f"… and **{remaining}** more in queue")

        embed = discord.Embed(
            title="📋 Current Queue",
            description="\n".join(lines),
            color=0x0099ff
        )
        await ctx.send(embed=embed)

    @commands.command(name='skip')
    async def skip(self, ctx, target: str = ""):
        """
        Skip the currently playing track.
        Usage:
          !skip             -> skip current track (any)
          !skip filler      -> if current is filler, stop it; also purge filler from queue
        """
        gid = ctx.guild.id
        target = (target or "").strip().lower()

        # Helper to remove all filler from queue
        def _purge_filler_from_queue() -> int:
            q = self.queues[gid]
            if not q:
                return 0
            kept = []
            removed = 0
            for item in q:
                if item.get("_autofill"):
                    removed += 1
                else:
                    kept.append(item)
            q.clear()
            q.extend(kept)
            if removed:
                save_data(gid, self.queues, self.playlists, self.user_mappings)
            return removed

        # Special mode: skip & purge filler
        if target == "filler":
            removed_current = False
            if self.current_song and self.current_song.get("_autofill") and ctx.voice_client and ctx.voice_client.is_playing():
                ctx.voice_client.stop()
                removed_current = True

            removed_queued = _purge_filler_from_queue()

            desc = []
            if removed_current:
                desc.append("Skipped the **current filler** track.")
            if removed_queued:
                desc.append(f"🧹 Removed **{removed_queued}** filler track(s) from the queue.")
            if not desc:
                desc.append("No filler tracks were playing or queued.")

            await ctx.send(embed=discord.Embed(
                title="📻 Filler Skip",
                description="\n".join(desc),
                color=0x9b59b6
            ))
            return

        # Default behavior: skip whatever is playing
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.stop()
            await ctx.send(embed=discord.Embed(
                title="⏭️ Skipped",
                description="Skipped the current track! 🚀",
                color=0x0099ff
            ))


    @commands.command(name='stop')
    async def stop(self, ctx):
        """
        Stops all playback and clears the playlist queue
        """
        if ctx.voice_client:
            ctx.voice_client.stop()

        gid = ctx.guild.id
        self.queues[gid].clear()

        if CLEAR_PLAYLISTS_ON_STOP:
            self.playlists[gid].clear()

        self._cancel_autofill_task(gid)
        self._clear_autofill_from_queue(gid)

        self.current_song = None
        self.song_start_time = None
        if self.update_song_activity.is_running():
            self.update_song_activity.stop()
        await self.bot.change_presence(activity=None)

        save_data(gid, self.queues, self.playlists, self.user_mappings)

        msg = "Stopped and cleared queue! 😴"
        if CLEAR_PLAYLISTS_ON_STOP:
            msg += " (Playlists cleared)"
        embed = discord.Embed(title="⏹️ Stopped", description=msg, color=0xff0000)
        await ctx.send(embed=embed)

    @commands.command(name='shuffle')
    async def shuffle_queue(self, ctx):
        """
        Shuffles the current queue
        """
        guild_id = ctx.guild.id
        queue = self.queues[guild_id]
        if not queue:
            embed = discord.Embed(title="❌ Error", description="Queue is empty! No songs to shuffle.", color=0xff0000)
            await ctx.send(embed=embed)
            return
        items = list(queue)
        random.shuffle(items)
        queue.clear()
        queue.extend(items)
        save_data(guild_id, self.queues, self.playlists, self.user_mappings)
        embed = discord.Embed(title="🔀 Shuffled", description="Queue has been shuffled! 🎲", color=0x00ff00)
        await ctx.send(embed=embed)

    @commands.command(name='volume')
    async def volume(self, ctx, *, vol: int):
        """
        Set volume from 0 to 100 (defaults to 69)
        """
        guild_id = ctx.guild.id
        if not (0 <= vol <= 200):
            embed = discord.Embed(title="❌ Error", description="Volume must be between 0 and 200 (100 = default).", color=0xff0000)
            await ctx.send(embed=embed)
            return
        self.volumes[guild_id] = vol / 100.0
        embed = discord.Embed(title="🔊 Volume", description=f"Volume set to {vol}%! 🎙️", color=0x00ff00)
        await ctx.send(embed=embed)
        if ctx.voice_client and ctx.voice_client.source:
            if hasattr(ctx.voice_client.source, 'volume'):
                ctx.voice_client.source.volume = self.volumes[guild_id]

    @commands.command(name='playlist')
    @commands.has_permissions(administrator=True)
    async def playlist(self, ctx, url: str, max_items: int = 100):
        """
        Enqueue tracks from a Suno playlist/profile/handle in bulk (Admin only)
        Usage: !playlist https://suno.com/playlist/##"
        """
        if not ctx.voice_client:
            await ctx.invoke(self.join)

        guild_id = ctx.guild.id
        queue = self.queues[guild_id]

        # User playlist enqueue cancels any pending/queued autofill
        self._cancel_autofill_task(guild_id)
        self._clear_autofill_from_queue(guild_id)

        try:
            # run sync scraper in a thread
            loop = asyncio.get_event_loop()
            raw_tracks = await loop.run_in_executor(
                None, lambda: scrape_suno_songs(url, limit=max_items)
            )
            if not raw_tracks:
                embed = discord.Embed(
                    title="❌ No Tracks Found",
                    description="Couldn't find songs on that page.",
                    color=0xff0000
                )
                await ctx.send(embed=embed)
                return

            # enforce queue add limit (bulk)
            intended = len(raw_tracks)
            allowed, notice = self._enforce_queue_add_limit(guild_id, intended)
            if allowed <= 0:
                await ctx.send(embed=discord.Embed(
                    title="🚫 Queue Limit",
                    description=notice or "Queue limit reached for bulk adds.",
                    color=0xe74c3c
                ))
                return
            if allowed < intended:
                raw_tracks = raw_tracks[:allowed]

            tracks = await self._resolve_tracks(raw_tracks, max_workers=6)

            start_pos = len(queue) + 1
            for t in tracks:
                t["requester_id"] = ctx.author.id
                t["requester_tag"] = str(ctx.author)
                t["requester_name"] = ctx.author.display_name
                t["requester_mention"] = ctx.author.mention
                t["requested_at"] = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
                queue.append(t)

            end_pos = len(queue)
            save_data(guild_id, self.queues, self.playlists, self.user_mappings)

            desc = f"Added {len(tracks)} tracks!"
            if end_pos >= start_pos:
                desc += f" (positions #{start_pos}–#{end_pos})"
            if notice:
                desc += f"\n\n{notice}"

            embed = discord.Embed(title="➕ Added Playlist", description=desc, color=0x0099ff)
            await ctx.send(embed=embed)

            if not ctx.voice_client.is_playing():
                await self.play_next(ctx)

        except Exception as e:
            embed = discord.Embed(
                title="❌ Error",
                description=f"Failed to add playlist: {e}",
                color=0xff0000
            )
            await ctx.send(embed=embed)

    @commands.hybrid_command(name='add_user_songs', description='Add songs from a Suno user profile')
    @commands.has_permissions(administrator=False)
    async def add_user_songs(self, ctx, suno_username: str):
        """
        Add songs from a Suno user profile (Admin Only)
        """
        guild_id = ctx.guild.id
        src = suno_username.strip()
        with concurrent.futures.ThreadPoolExecutor() as executor:
            song_links = await asyncio.get_event_loop().run_in_executor(
                executor, scrape_suno_songs, src, 100
            )

        if not song_links:
            embed = discord.Embed(
                title="❌ No Songs Found",
                description=f"No songs scraped from '{suno_username}'. Page may require JS login or be private.",
                color=0xff0000
            )
            await ctx.send(embed=embed)
            return

        # enforce queue add limit (bulk)
        intended = len(song_links)
        allowed, notice = self._enforce_queue_add_limit(guild_id, intended)
        if allowed <= 0:
            await ctx.send(embed=discord.Embed(
                title="🚫 Queue Limit",
                description=notice or "Queue limit reached for bulk adds.",
                color=0xe74c3c
            ))
            return
        if allowed < intended:
            song_links = song_links[:allowed]

        for song in song_links:
            try:
                song_info = extract_song_info(song['url'])
                song.update(song_info)
            except Exception as e:
                print(f"Error extracting song info for {song.get('title','Untitled')}: {e}")
        self.playlists[guild_id]['default'].extend(song_links)
        save_data(guild_id, self.queues, self.playlists, self.user_mappings)

        desc = f"Added {len(song_links)} songs from '{suno_username}' to default playlist! Use !load_playlist default to play."
        if notice:
            desc += f"\n\n{notice}"
        embed = discord.Embed(
            title="🎵 Added User Songs",
            description=desc,
            color=0x00ff00
        )
        await ctx.send(embed=embed)

    @commands.command(name='remove')
    async def remove_from_queue(self, ctx, position: str = ""):
        """Remove a song from the queue by position."""
        try:
            position = int(position)
        except ValueError:
            embed = discord.Embed(title="❌ Error", description="Invalid position! Use a number (e.g., !remove 1).", color=0xff0000)
            await ctx.send(embed=embed)
            return

        guild_id = ctx.guild.id
        queue = self.queues[guild_id]
        if not queue:
            embed = discord.Embed(title="❌ Error", description="Queue is empty!", color=0xff0000)
            await ctx.send(embed=embed)
            return

        if position < 1 or position > len(queue):
            embed = discord.Embed(title="❌ Error", description=f"Invalid position! Must be between 1 and {len(queue)}.", color=0xff0000)
            await ctx.send(embed=embed)
            return

        idx = position - 1
        queue_list = list(queue)
        removed_song = queue_list[idx]
        queue_list.pop(idx)
        queue.clear()
        queue.extend(queue_list)
        save_data(guild_id, self.queues, self.playlists, self.user_mappings)

        embed = discord.Embed(title="🗑️ Removed", description=f"Removed: {removed_song.get('title','Untitled')} from position {position}", color=0x00ff00)
        await ctx.send(embed=embed)

    @commands.command(name='reload')
    @commands.has_permissions(administrator=True)
    async def reload(self, ctx):
        """
        Reload the song cog activity (Admin Only)
        """
        try:
            if ctx.voice_client:
                ctx.voice_client.stop()
            self.current_song = None
            self.song_start_time = None
            if self.update_song_activity.is_running():
                self.update_song_activity.stop()
            await self.bot.change_presence(activity=None)

            gid = ctx.guild.id
            self.queues[gid].clear()

            if CLEAR_PLAYLISTS_ON_RELOAD:
                self.playlists[gid].clear()

            self._cancel_autofill_task(gid)
            self._clear_autofill_from_queue(gid)

            save_data(gid, self.queues, self.playlists, self.user_mappings)

            await self.bot.unload_extension('src.cogs.music')
            await self.bot.load_extension('src.cogs.music')

            msg = "Music cog reloaded successfully!"
            if CLEAR_PLAYLISTS_ON_RELOAD:
                msg += " (Playlists cleared)"
            embed = discord.Embed(title="✅ Reloaded", description=msg, color=0x00ff00)
            await ctx.send(embed=embed)
        except Exception as e:
            embed = discord.Embed(title="❌ Reload Failed", description=f"Error: {str(e)}", color=0xff0000)
            await ctx.send(embed=embed)

    @commands.has_permissions(administrator=False)
    @commands.command(name='queue_clear')
    async def queue_clear(self, ctx):
        """
        Clears the song queue
        """
        gid = ctx.guild.id
        self.queues[gid].clear()
        save_data(gid, self.queues, self.playlists, self.user_mappings)
        await ctx.send(embed=discord.Embed(title="🧹 Queue Cleared", description="All queued tracks removed.", color=0x00ff00))

    @commands.has_permissions(administrator=False)
    @commands.command(name='playlist_clear')
    async def playlist_clear(self, ctx):
        """
        Clears the song queue of playlists (experimental)
        """
        gid = ctx.guild.id
        self.playlists[gid].clear()
        save_data(gid, self.queues, self.playlists, self.user_mappings)
        await ctx.send(embed=discord.Embed(title="🗑️ Playlists Cleared", description="All playlists removed.", color=0xff5555))

    @commands.has_permissions(administrator=True)
    @commands.command(name='reset_state')
    async def reset_state(self, ctx):
        """
        Resets the bot state for debugging or issues (Admin Only)
        """
        gid = ctx.guild.id
        self.queues[gid].clear()
        self.playlists[gid].clear()
        self.user_mappings[gid].clear()  # This will intentionally erase the saved autofill url/flag.
        self._cancel_autofill_task(gid)
        self._clear_autofill_from_queue(gid)
        save_data(gid, self.queues, self.playlists, self.user_mappings)
        await ctx.send(embed=discord.Embed(title="♻️ State Reset", description="Queues, playlists, and mappings wiped.", color=0xff9900))

#     @commands.command(name="song_info")
#     async def cmd_song_info(self, ctx, position: int | None = None):
#         ...
#             await ctx.send(embed=embed)embed


    # ========== Autofill Admin/User Commands =================================

    @commands.has_permissions(administrator=True)
    @commands.command(name="autofill_set")
    async def autofill_set(self, ctx, url: str):
        """
        Set the playlist/profile URL to use for idle autofill radio. (Admin only)
        Usage: !autofill_set https://suno.com/playlist/XXXX  or  https://suno.com/@handle  or @handle
        """
        if not self._autofill_feature_on:
            await ctx.send(embed=discord.Embed(title="Feature Disabled", description="Autofill is disabled.", color=0xe74c3c))
            return
        gid = ctx.guild.id
        the_url = url.strip()
        self.auto_playlist_urls[gid] = the_url
        # default ON (and persists)
        self.auto_play_enabled[gid] = True

        # persist in user_mappings
        amap = self.user_mappings[gid]
        if not isinstance(amap, dict):
            amap = {}
            self.user_mappings[gid] = amap
        amap["autofill"] = {"url": the_url, "enabled": True}
        save_data(gid, self.queues, self.playlists, self.user_mappings)

        await ctx.send(embed=discord.Embed(
            title="🟢 Autofill Source Set",
            description=f"Autofill will pull from:\n`{the_url}`\n(Starts **{AUTOFILL_DELAY_SEC}s** after finishing when the queue is empty.)",
            color=0x2ecc71
        ))

    @commands.has_permissions(administrator=True)
    @commands.command(name="autofill_on")
    async def autofill_on(self, ctx):
        """
        Turns on Autofill (Admin only)
        """
        if not self._autofill_feature_on:
            await ctx.send(embed=discord.Embed(title="Feature Disabled", description="Autofill is disabled.", color=0xe74c3c))
            return
        gid = ctx.guild.id
        self.auto_play_enabled[gid] = True

        # persist flag
        amap = self.user_mappings[gid]
        if not isinstance(amap, dict):
            amap = {}
            self.user_mappings[gid] = amap
        ainfo = amap.get("autofill", {})
        ainfo["enabled"] = True
        amap["autofill"] = ainfo
        save_data(gid, self.queues, self.playlists, self.user_mappings)

        await ctx.send(embed=discord.Embed(
            title="🟢 Autofill Enabled",
            description="Idle radio will resume after the queue finishes.",
            color=0x2ecc71
        ))

    @commands.has_permissions(administrator=True)
    @commands.command(name="autofill_off")
    async def autofill_off(self, ctx):
        """
        Turns off Autofill (Admin only)
        """
        gid = ctx.guild.id
        self.auto_play_enabled[gid] = False
        self._cancel_autofill_task(gid)
        self._clear_autofill_from_queue(gid)

        # persist flag
        amap = self.user_mappings[gid]
        if not isinstance(amap, dict):
            amap = {}
            self.user_mappings[gid] = amap
        ainfo = amap.get("autofill", {})
        ainfo["enabled"] = False
        amap["autofill"] = ainfo
        save_data(gid, self.queues, self.playlists, self.user_mappings)

        await ctx.send(embed=discord.Embed(
            title="🔴 Autofill Disabled",
            description="Idle radio will no longer auto-resume.",
            color=0xe74c3c
        ))

    @commands.command(name="autofill_status")
    async def autofill_status(self, ctx):
        """
        Show Autofill Status
        """
        gid = ctx.guild.id
        enabled = bool(self.auto_play_enabled.get(gid, True)) and self._autofill_feature_on

        # Prefer saved URL; else show CSV count if present; else env default; else —
        url = self.auto_playlist_urls.get(gid)
        csv_rows = self.autofill_seed_rows.get(gid)
        if url:
            src_str = url
        elif csv_rows:
            src_str = f"CSV ({len(csv_rows)} items)"
        else:
            src_str = (DEFAULT_AUTOFILL_URL or ("CSV" if DEFAULT_AUTOFILL_CSV else "—"))

        await ctx.send(embed=discord.Embed(
            title="ℹ️ Autofill Status",
            description=f"**Feature:** {'ON' if self._autofill_feature_on else 'OFF'}\n"
                        f"**State:** {'Enabled' if enabled else 'Disabled'}\n"
                        f"**Source:** {src_str}\n"
                        f"**Delay:** {AUTOFILL_DELAY_SEC}s",
            color=0x7289DA
        ))

    # ========== Queue limit commands (Admin) =================================

    @commands.has_permissions(administrator=True)
    @commands.command(name="queue_limit_on")
    async def queue_limit_on(self, ctx):
        """
        Turn the queue limit on (Admin only)
        """
        gid = ctx.guild.id
        self.queue_limit_enabled[gid] = True
        amap = self.user_mappings[gid]
        if not isinstance(amap, dict):
            amap = {}
            self.user_mappings[gid] = amap
        amap["queue_limit"] = {"enabled": True, "max": self._limit_max(gid)}
        save_data(gid, self.queues, self.playlists, self.user_mappings)
        await ctx.send(embed=discord.Embed(
            title="📦 Queue Limit",
            description=f"Queue limit is **ON** (max {self._limit_max(gid)} per add).",
            color=0x2ecc71
        ))

    @commands.has_permissions(administrator=True)
    @commands.command(name="queue_limit_off")
    async def queue_limit_off(self, ctx):
        """
        Turn the queue limit off (Admin only)
        """
        gid = ctx.guild.id
        self.queue_limit_enabled[gid] = False
        amap = self.user_mappings[gid]
        if not isinstance(amap, dict):
            amap = {}
            self.user_mappings[gid] = amap
        amap["queue_limit"] = {"enabled": False, "max": self._limit_max(gid)}
        save_data(gid, self.queues, self.playlists, self.user_mappings)
        await ctx.send(embed=discord.Embed(
            title="📦 Queue Limit",
            description="Queue limit is **OFF**.",
            color=0xe67e22
        ))

    @commands.has_permissions(administrator=True)
    @commands.command(name="queue_limit_set")
    async def queue_limit_set(self, ctx, max_per_add: int):
        """
        Set the queue limit amount (Admin only)
        """
        gid = ctx.guild.id
        max_per_add = max(1, int(max_per_add))
        self.queue_limit_max[gid] = max_per_add
        amap = self.user_mappings[gid]
        if not isinstance(amap, dict):
            amap = {}
            self.user_mappings[gid] = amap
        enabled = self._limit_is_on(gid)
        amap["queue_limit"] = {"enabled": enabled, "max": max_per_add}
        save_data(gid, self.queues, self.playlists, self.user_mappings)
        await ctx.send(embed=discord.Embed(
            title="📦 Queue Limit",
            description=f"Max songs per add set to **{max_per_add}**.",
            color=0x3498db
        ))

    @commands.command(name="queue_limit_status")
    async def queue_limit_status(self, ctx):
        """
        Queue Limit Status (Admin only)
        """
        gid = ctx.guild.id
        enabled = self._limit_is_on(gid)
        maxn = self._limit_max(gid)
        per_user_cap = int(QUEUE_MAX_PER_USER)
        await ctx.send(embed=discord.Embed(
            title="ℹ️ Queue Limit Status",
            description=f"**State:** {'ON' if enabled else 'OFF'}\n**Max per add:** {maxn}\n**Max per user:** {per_user_cap}",
            color=0x7289DA
        ))

async def setup(bot):
    await bot.add_cog(MusicCog(bot))
