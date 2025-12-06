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
import csv
from pathlib import Path
from discord.utils import escape_markdown
from src.data.persistence import load_data, save_data
from src.utils.yt_extractor import extract_song_info
from src.utils.scraper import scrape_suno_songs
from src.utils.prefetch import prefetch_to_file
from src.data.db import like_track, unlike_track, has_liked, get_like_count, top_liked_for_users
from shuffle_displacing_first import shuffle_displacing_first_inplace

# === Play history DB (safe if module not present) ===========================
try:
    from src.data.db import upsert_track_basic, log_play_start, log_play_end
except Exception:
    upsert_track_basic = lambda **kwargs: None
    def log_play_start(**kwargs): return None
    def log_play_end(**kwargs): return None

# ===== Embed + Formatting Helpers ===========================================
EMBED_COLOR_PLAYING = 0x580fd6
EMBED_COLOR_ADDED   = 0xc1d4d6

# ---- Prefetch config (env-driven) ------------------------------------------
PREFETCH_MODE    = os.getenv("PREFETCH_MODE", "full").lower()  # "none" | "warmup" | "full"
PREFETCH_BYTES   = int(os.getenv("PREFETCH_BYTES", "524288"))    # ~512 KB for warmup
PREFETCH_TIMEOUT = int(os.getenv("PREFETCH_TIMEOUT", "25"))      # seconds
PREFETCH_DIR     = os.getenv("PREFETCH_DIR", "songs") or "songs"

# ---- Startup polish & FFmpeg tuning --------------------------------------
PREBUFFER_SECONDS       = float(os.getenv("PREBUFFER_SECONDS", "0.5"))   # wait before play() to fill buffers
FADE_IN_SECONDS         = float(os.getenv("FADE_IN_SECONDS", "0.5"))     # 0 disables fade-in
FADE_IN_STEPS           = int(os.getenv("FADE_IN_STEPS", "20"))           # number of steps in the fade
FADE_OUT_SECONDS        = float(os.getenv("FADE_OUT_SECONDS", "1.0"))     # 0 disables
FADE_OUT_STEPS          = int(os.getenv("FADE_OUT_STEPS", "20"))
STARTUP_ADELAY_MS       = int(os.getenv("STARTUP_ADELAY_MS", "200"))      # adelay padding in ms for first packets
FFMPEG_PROBESIZE        = os.getenv("FFMPEG_PROBESIZE", "8M")             # probe size; lower = faster start
FFMPEG_ANALYZEDURATION  = os.getenv("FFMPEG_ANALYZEDURATION", "5M")       # analyze duration; lower = faster start
FFMPEG_THREAD_QUEUE_SIZE = int(os.getenv("FFMPEG_THREAD_QUEUE_SIZE", "1024"))
FFMPEG_RW_TIMEOUT_US     = int(os.getenv("FFMPEG_RW_TIMEOUT_US", "15000000"))  # 15s
FFMPEG_NOBUFFER          = os.getenv("FFMPEG_NOBUFFER", "0") == "1"
FFMPEG_BUFFER_SIZE       = os.getenv("FFMPEG_BUFFER_SIZE", "512k")
FFMPEG_MAX_DELAY_US      = int(os.getenv("FFMPEG_MAX_DELAY_US", "5000000"))
VOICE_BITRATE_KBPS = int(os.getenv("VOICE_BITRATE_KBPS", "128"))

# Queue/playlist clear policy toggles
CLEAR_PLAYLISTS_ON_STOP   = os.getenv("CLEAR_PLAYLISTS_ON_STOP", "0") == "1"
CLEAR_PLAYLISTS_ON_RELOAD = os.getenv("CLEAR_PLAYLISTS_ON_RELOAD", "0") == "1"

# ---- Autofill (idle radio) -------------------------------------------------
AUTOFILL_FEATURE   = os.getenv("AUTOFILL_FEATURE", "1") == "1"
AUTOFILL_DELAY_SEC = int(os.getenv("AUTOFILL_DELAY_SEC", "30"))   # wait after finishing
AUTOFILL_MAX_PULL  = int(os.getenv("AUTOFILL_MAX_PULL", "50"))    # how many to enqueue per fill
DEFAULT_AUTOFILL_URL = os.getenv("DEFAULT_AUTOFILL_URL", "").strip()
DEFAULT_AUTOFILL_CSV = os.getenv("DEFAULT_AUTOFILL_CSV", "").strip()
AUTOFILL_LIKES_PER_USER = int(os.getenv("AUTOFILL_LIKES_PER_USER", "5"))

# ---- Queue add limit (peak throttle) ---------------------------------------
QUEUE_LIMIT_DEFAULT_ENABLED = os.getenv("QUEUE_LIMIT_DEFAULT_ENABLED", "1") == "1"
QUEUE_LIMIT_MAX_PER_ADD     = int(os.getenv("QUEUE_LIMIT_MAX_PER_ADD", "25"))  # default cap
QUEUE_MAX_PER_USER          = int(os.getenv("QUEUE_MAX_PER_USER", "3"))        # hard cap per user in queue

# ---- Now Playing pruning (autofill-only) -----------------------------------
# Only prune NP cards that came from autofill tracks, once N subsequent songs have started.
REMOVE_NP_AFTER_SONGS = int(os.getenv("REMOVE_NP_AFTER_SONGS", "2"))  # default=2 songs

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

def _duration_to_seconds(d) -> int | None:
    """Return total seconds from int/float or 'HH:MM:SS'/'MM:SS' strings. None if unknown."""
    if d is None:
        return None
    if isinstance(d, (int, float)):
        return max(0, int(d))
    s = str(d).strip()
    if not s:
        return None
    parts = s.split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == 3:
        h, m, sec = parts
        return max(0, h * 3600 + m * 60 + sec)
    if len(parts) == 2:
        m, sec = parts
        return max(0, m * 60 + sec)
    try:
        return max(0, int(s))
    except ValueError:
        return None

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

def _canonical_track_id(track: dict) -> str | None:
    # 1) explicit id if you already stash one
    if track.get("id"):
        return str(track["id"])

    # 2) try the Suno page URL
    page = _derive_suno_url(track) or (track.get("url") or "")
    m = re.search(r"/song/([A-Za-z0-9\-]{8,})", page)
    if m:
        return m.group(1)

    # 3) audio filename .../{id}.mp3 (including "songs/{id}.mp3")
    url = str(track.get("url") or "")
    m = re.search(r"/([A-Fa-f0-9\-]{8,})\.mp3", url)
    if m:
        return m.group(1)
    if url.startswith("songs/") and url.endswith(".mp3"):
        return Path(url).stem

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
    return " ⟳" if track.get("_autofill") else ""

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
        lines.append(f"{i}. {title} {byline} / Requested by {requester}")
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

    embed.set_footer(text="Suno Radio")
    embed.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return embed

def build_added_embed(
    track: dict,
    requester_mention: str | None,
    position: int | None = None,
    eta_seconds: int | None = None,
    eta_unknown: bool = False
):
    """
    Added card: heading = song title (clickable), body = artist,
    fields = Duration, Requested by (with original request time), Position (+ ETA).
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
    embed.add_field(name="Duration", value=_fmt_duration(track.get("duration")), inline=True)

    ts = int(track.get("requested_at") or datetime.datetime.now(datetime.timezone.utc).timestamp())
    req_val = (requester_mention or "—") + f" at <t:{ts}:t>"
    embed.add_field(name="Requested by", value=req_val, inline=True)

    if isinstance(position, int) and position >= 1:
        eta_label = None
        if eta_seconds is not None:
            eta_label = _fmt_duration(max(0, int(eta_seconds)))
        elif eta_unknown:
            eta_label = "≈unknown"

        pos_val = f"#{position}" + (f" (Up in ~{eta_label})" if eta_label else "")
        embed.add_field(name="Position", value=pos_val, inline=False)

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

LIKE_EMOJI_NAME = "sunobotlike"
LIKE_EMOJI_ID   = 1437172794499534930
LIKE_FALLBACK   = "👍"

class LikeView(discord.ui.View):
    def __init__(
        self,
        *,
        track_id: str,
        guild_id: int,
        bot_user_id: int,
        song_title: str | None = None,
        song_url: str | None = None,
        timeout: float | None = 3600,
        show_count: bool = False,  # toggle here
    ):
        super().__init__(timeout=timeout)
        self.track_id = track_id
        self.guild_id = guild_id
        self.bot_user_id = bot_user_id
        self.song_title = song_title or "Untitled"
        self.song_url = (song_url or "").strip()
        self.show_count = show_count

        try:
            count = get_like_count(track_id=track_id, guild_id=guild_id)
        except Exception:
            count = 0

        # Set emoji (separate from label)
        try:
            self.like_btn.emoji = discord.PartialEmoji(name=LIKE_EMOJI_NAME, id=LIKE_EMOJI_ID)
        except Exception:
            self.like_btn.emoji = LIKE_FALLBACK

        # Default: hide the count
        self.like_btn.label = "Like"
        # For testing, show the count at init:
        # if self.show_count:
        #     self.like_btn.label = str(count)

    @discord.ui.button(
        style=discord.ButtonStyle.primary,
        label="Like",
        custom_id="suno_like_btn"
    )
    async def like_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.bot:
            return await interaction.response.defer(ephemeral=True)

        try:
            if has_liked(track_id=self.track_id, guild_id=self.guild_id, user_id=interaction.user.id):
                total = unlike_track(
                    track_id=self.track_id, guild_id=self.guild_id, user_id=interaction.user.id
                )
                msg = f"Removed your like for **{self.song_title}**."
                button.style = discord.ButtonStyle.primary
                button.label = str(total) if self.show_count else "Like"
            else:
                total = like_track(
                    track_id=self.track_id, guild_id=self.guild_id, user_id=interaction.user.id,
                    username=str(interaction.user),
                )
                msg = f"Thanks for liking **{self.song_title}**!"
                button.label = str(total) if self.show_count else "Like"

            await interaction.response.edit_message(view=self)

            if self.song_url.startswith("http"):
                link_view = discord.ui.View()
                link_view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, url=self.song_url, label="Open on Suno"))
                await interaction.followup.send(msg, view=link_view, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)

        except Exception as e:
            try:
                await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)
            except Exception:
                pass

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
        self._autofill_feature_on = AUTOFILL_FEATURE
        self.autofill_seed_rows = {}
        self._autofill_row_cursor = {}
        self.queue_limit_enabled = {}
        self.queue_limit_max = {}
        self.queue_per_user_max = {}
        self._fadeout_active = defaultdict(bool)

        # --- Playback overlap guard (fixes double-play jitter) -----------------
        self._play_locks = defaultdict(asyncio.Lock)

        # --- Now Playing tracking for pruning (autofill only) -----------------
        self._song_index = defaultdict(int)
        self._np_track = defaultdict(list)
        self._np_retention_n = REMOVE_NP_AFTER_SONGS

    def _is_admin(self, member: discord.Member) -> bool:
        """Admins bypass queue limitations."""
        try:
            perms = member.guild_permissions
            return bool(perms.administrator or perms.manage_guild)
        except Exception:
            return False

    async def _fade_out_and_stop(self, ctx, *, duration=None, steps=None):
        gid = ctx.guild.id
        vc = ctx.voice_client

        # no voice client at all → nothing to fade
        if not vc:
            return

        # allow fading if playing OR paused
        is_active = vc.is_playing() or vc.is_paused()
        if not is_active:
            try:
                vc.stop()
            except Exception:
                pass
            return

        # prevent double fades
        if self._fadeout_active[gid]:
            try:
                vc.stop()
            except Exception:
                pass
            return

        self._fadeout_active[gid] = True
        try:
            d = FADE_OUT_SECONDS if duration is None else float(duration)
            if d <= 0:
                vc.stop()
                return

            s = FADE_OUT_STEPS if steps is None else int(steps)
            s = max(1, s)

            # get the actual PCMVolumeTransformer
            transformer = getattr(vc, "source", None)
            if not transformer or not hasattr(transformer, "volume"):
                # fallback — cannot fade, just stop
                vc.stop()
                return

            try:
                start_vol = float(transformer.volume or 1.0)
            except Exception:
                start_vol = 1.0

            delay = d / s
            for i in range(s):
                await asyncio.sleep(delay)
                try:
                    transformer.volume = max(0.0, start_vol * (1.0 - (i + 1) / s))
                except Exception:
                    break

            try:
                transformer.volume = 0.0
            except Exception:
                pass

            vc.stop()

        except Exception as e:
            # log it to console but don't break playback system
            print(f"[fadeout] error: {e}")
            try:
                vc.stop()
            except Exception:
                pass

        finally:
            self._fadeout_active[gid] = False

    def _pick_song_from_context(self, ctx, position: int | None):
        gid = ctx.guild.id
        if position is None:
            if self.current_song:
                return self.current_song, "Now Playing"
            q = self.queues[gid]
            if q:
                return q[0], "Next Up"
            return None, "No song is playing and the queue is empty."

        try:
            idx = int(position) - 1
        except Exception:
            return None, f"Invalid position."
        q = self.queues[gid]
        if 0 <= idx < len(q):
            return list(q)[idx], f"Queued song #{idx+1}"
        return None, f"Invalid position. Must be between 1 and {len(q)}."

    async def _ensure_song_metadata(self, song: dict) -> dict:
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

    def _estimate_eta_seconds(self, gid: int, position: int) -> tuple[int | None, bool]:
        eta = 0
        had_unknown = False
        had_known = False

        if self.current_song and self.song_start_time:
            cur_dur = _duration_to_seconds(self.current_song.get("duration"))
            if cur_dur is None:
                had_unknown = True
            else:
                elapsed = int(max(0, time.time() - self.song_start_time))
                eta += max(0, cur_dur - elapsed)
                had_known = True

        q = self.queues.get(gid, deque())
        ahead = list(q)[:max(0, position - 1)]
        for t in ahead:
            td = _duration_to_seconds(t.get("duration"))
            if td is None:
                had_unknown = True
            else:
                eta += td
                had_known = True

        if not had_known and had_unknown:
            return None, True
        return eta, had_unknown

    def _count_user_queued(self, gid: int, user_id: int, include_filler: bool = False) -> int:
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
        have = self._count_user_queued(gid, user_id, include_filler=False)
        return max(0, self._per_user_max(gid) - have)

    def _deny_user_cap_embed(self, requester_mention: str | None = None, gid: int | None = None) -> discord.Embed:
        cap = self._per_user_max(gid) if gid is not None else QUEUE_MAX_PER_USER
        who = requester_mention or "You"
        return discord.Embed(
            title="🚫 Per-User Queue Limit",
            description=f"{who} already {'have' if requester_mention else 'has'} **{cap}** song(s) in the queue. "
                        f"Please wait until one finishes before adding more.",
            color=0xe74c3c
        )

    def _queue_eta_list(self, gid: int) -> list[int | None]:
        etas: list[int | None] = []
        base = 0
        if self.current_song and self.song_start_time:
            cur = _duration_to_seconds(self.current_song.get("duration"))
            if cur is not None:
                elapsed = int(max(0, time.time() - self.song_start_time))
                base = max(0, cur - elapsed)
            else:
                return [None for _ in range(len(self.queues.get(gid, [])))]

        acc = base
        q = list(self.queues.get(gid, []))
        for t in q:
            etas.append(acc if acc is not None else None)
            d = _duration_to_seconds(t.get("duration"))
            if d is None:
                acc = None
            else:
                if acc is not None:
                    acc += d
        return etas

    # ===== AUTOFILL (Idle Radio) ============================================
    def _is_autofill_enabled(self, gid: int) -> bool:
        return (
            self._autofill_feature_on
            and bool(self.auto_play_enabled.get(gid))
            and (
                bool(self.auto_playlist_urls.get(gid)) or
                bool(self.autofill_seed_rows.get(gid))
            )
        )

    def _cancel_autofill_task(self, gid: int):
        task = self.auto_play_tasks.get(gid)
        if task and not task.done():
            task.cancel()
        self.auto_play_tasks[gid] = None

    def _clear_autofill_from_queue(self, gid: int):
        dq = self.queues[gid]
        if not dq:
            return
        kept = [t for t in dq if not t.get("_autofill")]
        dq.clear()
        dq.extend(kept)

    def _load_autofill_csv(self, path: str) -> list[dict]:
        rows = []
        if not path or not os.path.exists(path):
            return rows
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                sniffer = csv.Sniffer()
                sample = f.read(2048)
                f.seek(0)
                has_header = False
                try:
                    has_header = sniffer.has_header(sample)
                except Exception:
                    pass

                reader = csv.reader(f)
                for r in reader:
                    if not r or all(not (c or "").strip() for c in r):
                        continue
                    if has_header and reader.line_num == 1:
                        headers = [h.strip().lower() for h in r]
                        try:
                            url_idx = headers.index("url")
                        except ValueError:
                            url_idx = 0
                        requested_by_idx = None
                        for cand in ("requested by", "requested_by", "requestedby", "by"):
                            if cand in headers:
                                requested_by_idx = headers.index(cand)
                                break
                        continue

                    url = (r[0] if len(r) >= 1 else "").strip()
                    rb = (r[1] if len(r) >= 2 else "").strip()
                    if url:
                        rows.append({"url": url, "requested_by": rb})
        except Exception as e:
            print(f"[autofill CSV] Failed to load {path}: {e}")
        return rows

    async def _get_autofill_liked_raw(self, ctx, gid: int) -> list[dict]:
        try:
            vc = ctx.voice_client
        except AttributeError:
            vc = None

        if not vc or not getattr(vc, "channel", None):
            return []

        members = [m for m in vc.channel.members if not getattr(m, "bot", False)]
        user_ids = [m.id for m in members]
        if not user_ids:
            return []

        try:
            rows = top_liked_for_users(
                guild_id=gid,
                user_ids=user_ids,
                limit=AUTOFILL_MAX_PULL * max(1, AUTOFILL_LIKES_PER_USER),
            )
        except Exception as e:
            print(f"[autofill likes] failed to fetch liked tracks: {e}")
            return []

        by_user: dict[int, list[dict]] = defaultdict(list)
        for r in rows:
            uid = r.get("user_id") or r.get("liked_by") or r.get("user")
            if uid is None:
                continue
            try:
                uid_int = int(uid)
            except (TypeError, ValueError):
                continue
            by_user[uid_int].append(r)

        raw: list[dict] = []
        per_user_cap = max(1, AUTOFILL_LIKES_PER_USER)
        for uid in user_ids:
            user_rows = by_user.get(uid, [])
            if not user_rows:
                continue

            random.shuffle(user_rows)
            pick = user_rows[:per_user_cap]

            for r in pick:
                url = (r.get("source_url") or "").strip()
                if not url:
                    continue
                raw.append(
                    {
                        "id": r.get("track_id"),
                        "url": url,
                        "suno_url": url,
                        "_liked_weight": r.get("like_count", 0),
                    }
                )

        if len(raw) > AUTOFILL_MAX_PULL:
            random.shuffle(raw)
            raw = raw[:AUTOFILL_MAX_PULL]

        return raw

    async def _enqueue_autofill_batch(self, ctx, gid: int):
        liked_raw = await self._get_autofill_liked_raw(ctx, gid)
        liked_raw = liked_raw[:AUTOFILL_MAX_PULL]
        remaining = max(0, AUTOFILL_MAX_PULL - len(liked_raw))

        url = (self.auto_playlist_urls.get(gid) or "").strip()
        fallback_raw: list[dict] = []

        if remaining > 0:
            if url:
                raw_from_url = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: scrape_suno_songs(url, limit=AUTOFILL_MAX_PULL)
                )
                if raw_from_url:
                    random.shuffle(raw_from_url)
                    fallback_raw = raw_from_url[:remaining]
            else:
                seed = self.autofill_seed_rows.get(gid) or []
                if seed:
                    pick = seed[:]
                    random.shuffle(pick)
                    pick = pick[:remaining]
                    fallback_raw = [
                        {"url": r["url"], "requested_by_note": r.get("requested_by", "")}
                        for r in pick
                    ]

        combined_raw = liked_raw + fallback_raw
        if not combined_raw:
            return 0

        cleaned_raw = []
        for it in combined_raw:
            u = str(it.get("url") or it.get("suno_url") or "").strip()
            if not u:
                continue
            if not (u.startswith("http://") or u.startswith("https://") or u.startswith("songs/")):
                continue
            it["url"] = u
            cleaned_raw.append(it)

        if not cleaned_raw:
            return 0

        tracks = await self._resolve_tracks(cleaned_raw, max_workers=6)
        random.shuffle(tracks)

        now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        for t in tracks:
            t["_autofill"] = True
            t.setdefault("tags", []).append("filler")

            t["requester_id"] = self.bot.user.id if self.bot.user else None
            t["requester_tag"] = "Autofill"
            t["requester_name"] = "Autofill"
            t["requester_mention"] = None
            t["requested_at"] = now_ts

            self.queues[gid].append(t)

        save_data(gid, self.queues, self.playlists, self.user_mappings)
        return len(tracks)

    async def _autofill_after_delay(self, ctx, gid: int, delay: int):
        try:
            await asyncio.sleep(max(0, delay))
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
            self.auto_play_tasks[gid] = None

    def _schedule_autofill_if_idle(self, ctx, delay: int | None = None):
        gid = ctx.guild.id
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
        return bool(self.queue_limit_enabled.get(gid, QUEUE_LIMIT_DEFAULT_ENABLED))

    def _limit_max(self, gid: int) -> int:
        return int(self.queue_limit_max.get(gid, QUEUE_LIMIT_MAX_PER_ADD))

    def _per_user_max(self, gid: int) -> int:
        return int(self.queue_per_user_max.get(gid, QUEUE_MAX_PER_USER))

    def _enforce_queue_add_limit(self, gid: int, intended_count: int, *, bypass: bool = False) -> tuple[int, str | None]:
        if bypass or (not self._limit_is_on(gid)):
            return intended_count, None
        cap = self._limit_max(gid)
        if intended_count <= cap:
            return intended_count, None
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
        mins, secs = divmod(int(seconds), 60)
        return f"{mins:02d}:{secs:02d}"

    async def _resolve_tracks(self, items: list[dict], max_workers: int = 6) -> list[dict]:
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
        try:
            title = song.get('title', 'Unknown Song')
            duration = song.get('duration', 0) or 0
            current_time = self.format_time(elapsed_seconds)
            total_time = self.format_time(duration)

            activity_name = f"🎶 {title} - {current_time} / {total_time}"
            activity = discord.Activity(
                type=discord.ActivityType.listening,
                name=activity_name[:128]
            )
            await self.bot.change_presence(activity=activity)
        except Exception as e:
            print(f"Error setting song activity: {e}")

    async def _fade_in_volume(self, transformer, target, duration, steps):
        try:
            if duration <= 0 or transformer is None:
                if transformer is not None:
                    transformer.volume = target
                return
            initial = 0.0001
            transformer.volume = initial
            steps = max(1, int(steps))
            delay = max(0.001, float(duration) / steps)
            delta = (target - initial) / steps
            for i in range(steps):
                await asyncio.sleep(delay)
                transformer.volume = max(0.0, initial + delta * (i + 1))
        except Exception:
            try:
                transformer.volume = target
            except Exception:
                pass

    @tasks.loop(seconds=30)
    async def update_song_activity(self):
        if self.current_song and self.song_start_time:
            elapsed = time.time() - self.song_start_time
            await self.set_song_activity(self.current_song, elapsed)

    async def cog_load(self):
        for guild in self.bot.guilds:
            loaded_queues, loaded_playlists, loaded_user_mappings = load_data(guild.id)
            if guild.id in loaded_queues:
                self.queues[guild.id] = loaded_queues[guild.id]
            if guild.id in loaded_playlists:
                self.playlists[guild.id] = loaded_playlists[guild.id]
            if guild.id in loaded_user_mappings:
                self.user_mappings[guild.id] = loaded_user_mappings[guild.id]

            gid = guild.id
            amap = self.user_mappings[gid]
            ainfo = amap.get("autofill") if isinstance(amap, dict) else None

            enabled_default = True

            if isinstance(ainfo, dict):
                url = (ainfo.get("url") or "").strip()
                enabled = bool(ainfo.get("enabled", enabled_default))
                csv_path = (ainfo.get("csv") or "").strip() if isinstance(ainfo, dict) else ""

                if url:
                    self.auto_playlist_urls[gid] = url
                self.auto_play_enabled[gid] = enabled

                if not url and csv_path:
                    self.autofill_seed_rows[gid] = self._load_autofill_csv(csv_path)
            else:
                if not isinstance(amap, dict):
                    amap = {}
                    self.user_mappings[gid] = amap
                self.auto_play_enabled[gid] = enabled_default

            if not self.auto_playlist_urls.get(gid):
                if DEFAULT_AUTOFILL_URL:
                    self.auto_playlist_urls[gid] = DEFAULT_AUTOFILL_URL
                    amap = self.user_mappings[gid]
                    amap["autofill"] = {
                        "url": DEFAULT_AUTOFILL_URL,
                        "enabled": self.auto_play_enabled.get(gid, enabled_default),
                    }
                    save_data(gid, self.queues, self.playlists, self.user_mappings)
                elif DEFAULT_AUTOFILL_CSV:
                    rows = self._load_autofill_csv(DEFAULT_AUTOFILL_CSV)
                    if rows:
                        self.autofill_seed_rows[gid] = rows
                        amap = self.user_mappings[gid]
                        amap["autofill"] = {
                            "csv": DEFAULT_AUTOFILL_CSV,
                            "enabled": self.auto_play_enabled.get(gid, enabled_default),
                        }
                        save_data(gid, self.queues, self.playlists, self.user_mappings)

    async def cog_unload(self):
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

            # ✅ bump Opus bitrate a bit for cleaner highs
            try:
                vc = ctx.voice_client
                if vc and getattr(vc, "encoder", None):
                    vc.encoder.bitrate = VOICE_BITRATE_KBPS * 1000  # discord expects bps
            except Exception as e:
                print(f"[bitrate] couldn't set encoder bitrate: {e}")

            embed = discord.Embed(title="✅ Joined", description=f"Joined {channel.name} 🎧", color=0x00ff00)
            await ctx.send(embed=embed)

            gid = ctx.guild.id
            if not self.queues[gid] and not (ctx.voice_client and ctx.voice_client.is_playing()):
                self._cancel_autofill_task(gid)
                self._schedule_autofill_if_idle(ctx, delay=AUTOFILL_DELAY_SEC)
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

        self.current_song = None
        self.song_start_time = None
        if self.update_song_activity.is_running():
            self.update_song_activity.stop()
        await self.bot.change_presence(activity=None)
        embed = discord.Embed(title="👋 Left", description=f"Left {channel_name} 🎧", color=0xff0000)
        await ctx.send(embed=embed)

    @commands.command(name='play')
    @app_commands.describe(channel='Play a song using !play [url]')
    async def play(self, ctx, url: str = ""):
        """
        Plays a song by url (Suno url supported only) or scrapes recent if blank.
        """
        if not ctx.voice_client:
            await ctx.invoke(self.join)

        guild_id = ctx.guild.id
        queue = self.queues[guild_id]

        self._cancel_autofill_task(guild_id)
        self._clear_autofill_from_queue(guild_id)

        requester_id = ctx.author.id
        requester_tag = str(ctx.author)
        requester_name = ctx.author.display_name
        requester_mention = ctx.author.mention
        requested_at = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        remaining_user_slots = self._user_slots_remaining(guild_id, requester_id)
        is_admin = self._is_admin(ctx.author)
        if is_admin:
            remaining_user_slots = 10**9  # effectively unlimited

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

                intended = len(raw_tracks)
                allowed_by_add, notice = self._enforce_queue_add_limit(
                    guild_id, intended, bypass=is_admin
                )

                if remaining_user_slots <= 0:
                    await ctx.send(embed=self._deny_user_cap_embed(requester_mention, gid=guild_id))
                    return

                allowed_total = min(allowed_by_add, remaining_user_slots)
                if allowed_total <= 0:
                    await ctx.send(embed=self._deny_user_cap_embed(requester_mention, gid=guild_id))
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
                song = extract_song_info(url)
                song.setdefault("artist", song.pop("author", None))

                song["requester_id"] = requester_id
                song["requester_tag"] = requester_tag
                song["requester_name"] = requester_name
                song["requester_mention"] = requester_mention
                song["requested_at"] = requested_at

                if remaining_user_slots <= 0:
                    await ctx.send(embed=self._deny_user_cap_embed(requester_mention))
                    return

                queue.append(song)
                position = len(queue)
                save_data(guild_id, self.queues, self.playlists, self.user_mappings)

                eta_sec, eta_unknown = self._estimate_eta_seconds(guild_id, position)
                embed = build_added_embed(
                    song,
                    requester_mention=requester_mention,
                    position=position,
                    eta_seconds=eta_sec,
                    eta_unknown=eta_unknown
                )
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

    async def _cleanup_np_autofill(self, gid: int):
        """
        Delete autofill Now Playing messages that are older than the last N songs.
        Only touches messages that correspond to autofill tracks.
        """
        if self._np_retention_n <= 0:
            return
        entries = self._np_track.get(gid) or []
        if not entries:
            return

        current_idx = self._song_index.get(gid, 0)
        keep = []
        for e in entries:
            if e.get("is_autofill") and (current_idx - e.get("song_index", current_idx)) >= self._np_retention_n:
                try:
                    ch = self.bot.get_channel(e["channel_id"])
                    if ch:
                        msg = await ch.fetch_message(e["message_id"])
                        await msg.delete()
                except Exception:
                    pass
            else:
                keep.append(e)
        self._np_track[gid] = keep

    async def play_next(self, ctx):
        gid = ctx.guild.id
        lock = self._play_locks[gid]

        async with lock:
            queue = self.queues[gid]
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

            track_id = _canonical_track_id(song)
            if track_id:
                try:
                    upsert_track_basic(
                        track_id=track_id,
                        title=song.get("title"),
                        artist=song.get("artist") or song.get("author"),
                        cover_url=song.get("thumbnail") or song.get("thumb") or song.get("image"),
                        source_url=_derive_suno_url(song),
                        duration_sec=_duration_to_seconds(song.get("duration")),
                    )
                    play_id = log_play_start(
                        track_id=track_id,
                        guild_id=ctx.guild.id,
                        channel_id=ctx.channel.id,
                        requested_by=str(song.get("requester_id") or getattr(ctx.author, "id", "")),
                        context="autofill" if song.get("_autofill") else "queue",
                    )
                    song["_track_id"] = track_id
                    song["_play_id"] = play_id
                except Exception as e:
                    print(f"[history] start log failed: {e}")
            else:
                song["_track_id"] = None
                song["_play_id"] = None

            local_to_delete = None
            try:
                lp = await maybe_prefetch(song)
                if lp and PREFETCH_MODE == "full":
                    local_to_delete = lp
            except Exception as e:
                print(f"Prefetch failed for {song.get('url')}: {e}")

            # ---- FFmpeg options (latency + stability tuned) ------------------
            af_filter = (
                "aresample=async=1:min_hard_comp=0.10:first_pts=0,"
                f"adelay={STARTUP_ADELAY_MS}|{STARTUP_ADELAY_MS}"
            )
            base_opts = (
                f"-vn "
                f"-probesize {FFMPEG_PROBESIZE} "
                f"-analyzeduration {FFMPEG_ANALYZEDURATION} "
                f"-thread_queue_size {FFMPEG_THREAD_QUEUE_SIZE} "
                f"-buffer_size {FFMPEG_BUFFER_SIZE} "
                f"-max_delay {FFMPEG_MAX_DELAY_US} "
                f"-af {af_filter}"
            )
            if FFMPEG_NOBUFFER:
                base_opts += " -fflags +nobuffer"

            url_val = str(song.get("url", "")).strip()
            is_http = url_val.startswith(("http://", "https://"))

            if is_http:
                before_opts = (
                    "-reconnect 1 "
                    "-reconnect_streamed 1 "
                    "-reconnect_at_eof 1 "
                    "-reconnect_delay_max 5 "
                    f"-rw_timeout {FFMPEG_RW_TIMEOUT_US} "
                    "-nostdin"
                )
                ffmpeg_options = {
                    "before_options": before_opts,
                    "options": base_opts,
                }
            else:
                ffmpeg_options = {
                    "options": base_opts,
                }

            try:
                source = discord.FFmpegPCMAudio(url_val, **ffmpeg_options)
                volume_transformer = discord.PCMVolumeTransformer(source, volume=self.volumes[gid])
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
                if queue and ctx.voice_client:
                    self.bot.loop.create_task(self.play_next(ctx))
                return

            def after_playing(error):
                try:
                    if error:
                        print(f"Player error: {error}")

                    try:
                        if song.get("_track_id") and song.get("_play_id"):
                            log_play_end(track_id=song["_track_id"], play_id=song["_play_id"])
                    except Exception as e_end:
                        print(f"[history] end log failed: {e_end}")

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
                        self.bot.loop.call_soon_threadsafe(lambda: self.bot.loop.create_task(self.play_next(ctx)))
                    elif not queue:
                        embed2 = discord.Embed(title="⏹️ Queue Empty", description="Finished playing! 🎉", color=0x00ff00)
                        asyncio.run_coroutine_threadsafe(self.get_radio_channel(ctx).send(embed=embed2), self.bot.loop)
                        try:
                            self.bot.loop.call_soon_threadsafe(lambda: self._schedule_autofill_if_idle(ctx))
                        except Exception as _e:
                            print(f"[autofill schedule] {_e}")
                except Exception as e2:
                    print(f"after_playing crashed: {e2}")

            target_vol = self.volumes[gid]
            start_muted = (PREBUFFER_SECONDS > 0) or (FADE_IN_SECONDS > 0)
            if start_muted:
                try:
                    volume_transformer.volume = 0.0001
                except Exception:
                    pass

            ctx.voice_client.play(volume_transformer, after=after_playing)

            if PREBUFFER_SECONDS > 0:
                try:
                    await asyncio.sleep(PREBUFFER_SECONDS)
                except Exception:
                    pass

            if FADE_IN_SECONDS > 0:
                asyncio.create_task(
                    self._fade_in_volume(
                        volume_transformer,
                        target_vol,
                        FADE_IN_SECONDS,
                        FADE_IN_STEPS
                    )
                )
            else:
                try:
                    volume_transformer.volume = target_vol
                except Exception:
                    pass

            self._song_index[gid] += 1
            current_song_index = self._song_index[gid]

            if self.update_song_activity.is_running():
                self.update_song_activity.stop()
            self.current_song = song
            self.song_start_time = time.time()
            await self.set_song_activity(song, 0.0)
            if not self.update_song_activity.is_running():
                self.update_song_activity.start()

            requester = (song.get("requester_mention")
                         or song.get("requester_name")
                         or song.get("requester_tag"))
            upcoming_two = list(self.queues[gid])[:2]
            np_embed = build_now_playing_embed(song, requester_mention=requester, upcoming_tracks=upcoming_two)

            song_url = _derive_suno_url(song) or (song.get("url") or "")
            song_title = song.get("title") or song.get("track_id") or "Untitled"

            view = None
            if song.get("_track_id"):
                view = LikeView(
                    track_id=song["_track_id"],
                    guild_id=ctx.guild.id,
                    bot_user_id=(self.bot.user.id if self.bot.user else 0),
                    song_title=song_title,
                    song_url=song_url,
                )

            ch = self.get_radio_channel(ctx)
            sent_message = await ch.send(embed=np_embed, view=view)

            try:
                if sent_message:
                    entry = {
                        "message_id": sent_message.id,
                        "channel_id": sent_message.channel.id,
                        "song_index": current_song_index,
                        "is_autofill": bool(song.get("_autofill")),
                    }
                    self._np_track[gid].append(entry)
            except Exception:
                pass

            await self._cleanup_np_autofill(gid)

    @commands.command(name='queue')
    async def show_queue(self, ctx):
        """
            Shows the current queue with estimated time to start for each item.
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

        eta_list = self._queue_eta_list(guild_id)

        max_lines = 15
        lines = []
        for i, (song, eta_sec) in enumerate(zip(queue, eta_list), start=1):
            title_link = _track_title_link(song) + _filler_badge(song)
            artist_raw = (song.get("artist") or song.get("author") or "Unknown Artist").strip()
            artist = escape_markdown(artist_raw)
            requester = (song.get("requester_mention")
                         or (f"<@{song['requester_id']}>" if song.get("requester_id") else None)
                         or song.get("requester_tag")
                         or song.get("requester_name")
                         or "someone")
            if eta_sec is None:
                eta_str = "≈unknown"
            else:
                eta_str = _fmt_duration(max(0, int(eta_sec)))

            lines.append(f"{i}. {title_link} by {artist}\n Up in ~{eta_str} / Requested by {requester}")
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
          !skip autofill    -> if current is filler, stop it; also purge filler from queue
        """
        gid = ctx.guild.id
        target = (target or "").strip().lower()

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

        if target == "autofill":
            removed_current = False
            if self.current_song and self.current_song.get("_autofill") and ctx.voice_client and ctx.voice_client.is_playing():
                await self._fade_out_and_stop(ctx)
                removed_current = True

            removed_queued = _purge_filler_from_queue()

            desc = []
            if removed_current:
                desc.append("Skipped the **current autofill** track.")
            if removed_queued:
                desc.append(f"🧹 Removed **{removed_queued}** autofill track(s) from the queue.")
            if not desc:
                desc.append("No autofill tracks were playing or queued.")

            await ctx.send(embed=discord.Embed(
                title="📻 Autofill Skip",
                description="\n".join(desc),
                color=0x9b59b6
            ))
            return

        if ctx.voice_client and ctx.voice_client.is_playing():
            await self._fade_out_and_stop(ctx)
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
            await self._fade_out_and_stop(ctx)

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

        try:
            if self._is_autofill_enabled(gid):
                self._schedule_autofill_if_idle(ctx, delay=AUTOFILL_DELAY_SEC)
        except Exception as _e:
            print(f"[autofill after stop] {_e}")

    @commands.command(name="stahp", hidden=True)
    async def stahp(self, ctx):
        """STAHP"""
        await self.stop(ctx)

    @commands.command(name="hush", hidden=True)
    async def hush(self, ctx):
        """Shh"""
        await self.stop(ctx)

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
        
        # random.shuffle(items) vvv changed by Paul Schirf
        shuffle_displacing_first_inplace(items)

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
    async def playlist(self, ctx, url: str, max_items: int = 100):
        """
        Enqueue tracks from a Suno playlist/profile/handle in bulk
        Usage: !playlist https://suno.com/playlist/##"
        """
        is_admin = self._is_admin(ctx.author)

        if not ctx.voice_client:
            await ctx.invoke(self.join)

        guild_id = ctx.guild.id
        queue = self.queues[guild_id]

        self._cancel_autofill_task(guild_id)
        self._clear_autofill_from_queue(guild_id)

        try:
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

            intended = len(raw_tracks)
            allowed, notice = self._enforce_queue_add_limit(
                guild_id, intended, bypass=is_admin
            )

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

            # ✅ define timestamp once
            now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())

            start_pos = len(queue) + 1
            for t in tracks:
                t["requester_id"] = ctx.author.id
                t["requester_tag"] = str(ctx.author)
                t["requester_name"] = ctx.author.display_name
                t["requester_mention"] = ctx.author.mention
                t["requested_at"] = now_ts
                t["_from_playlist"] = True  # optional but nice if you want later filtering
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
            if ctx.voice_client and ctx.voice_client.is_playing():
                await self._fade_out_and_stop(ctx)
            else:
                try:
                    if ctx.voice_client:
                        ctx.voice_client.stop()
                except Exception:
                    pass
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

    @commands.command(name='queue_clear')
    async def queue_clear(self, ctx):
        """
        Clears the song queue
        """
        gid = ctx.guild.id
        self.queues[gid].clear()
        save_data(gid, self.queues, self.playlists, self.user_mappings)
        await ctx.send(embed=discord.Embed(title="🧹 Queue Cleared", description="All queued tracks removed.", color=0x00ff00))

    @commands.command(name='playlist_clear')
    async def playlist_clear(self, ctx):
        """
        Clears the song queue of playlists (experimental)
        """
        gid = ctx.guild.id
        q = self.queues[gid]

        if not q:
            await ctx.send(embed=discord.Embed(
                title="🧹 Playlist Tracks Cleared",
                description="Queue is empty — nothing to clear.",
                color=0x00ff00
            ))
            return

        before = len(q)

        kept = [t for t in q if not t.get("_from_playlist")]
        removed = before - len(kept)

        q.clear()
        q.extend(kept)

        save_data(gid, self.queues, self.playlists, self.user_mappings)

        desc = (
            f"Removed **{removed}** playlist-added track(s) from the queue."
            if removed else
            "No playlist-added tracks were in the queue."
        )

        await ctx.send(embed=discord.Embed(
            title="🧹 Playlist Tracks Cleared",
            description=desc,
            color=0x00ff00
        ))

    @commands.has_permissions(administrator=True)
    @commands.command(name='reset_state')
    async def reset_state(self, ctx):
        """
        Resets the bot state for debugging or issues (Admin Only)
        """
        gid = ctx.guild.id
        self.queues[gid].clear()
        self.playlists[gid].clear()
        self.user_mappings[gid].clear()
        self._cancel_autofill_task(gid)
        self._clear_autofill_from_queue(gid)
        save_data(gid, self.queues, self.playlists, self.user_mappings)
        await ctx.send(embed=discord.Embed(title="♻️ State Reset", description="Queues, playlists, and mappings wiped.", color=0xff9900))

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
        self.auto_play_enabled[gid] = True

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

    @commands.has_permissions(administrator=True)
    @commands.command(name="autofill_reload")
    async def autofill_reload(self, ctx):
        """
        Reload the Autofill CSV (Admin only) and report the total number of
        usable song URLs found. Resolves the *active* CSV path first.
        """
        gid = ctx.guild.id

        csv_path = None
        try:
            amap = self.user_mappings.get(gid) or {}
            ainfo = amap.get("autofill") or {}
            csv_path = (ainfo.get("csv") or "").strip()
        except Exception:
            csv_path = ""

        if not csv_path:
            csv_path = (
                os.getenv("AUTOFILL_CSV_PATH", "").strip()
                or os.getenv("DEFAULT_AUTOFILL_CSV", "").strip()
                or getattr(self, "_autofill_csv_path", "").strip()
                or "autofill.csv"
            )

        path = os.path.abspath(os.path.expanduser(csv_path))

        try:
            rows = []
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                rdr = csv.reader(f)
                first_row = True
                for r in rdr:
                    if not r:
                        continue
                    cell0 = (r[0] or "").strip()
                    cell0_norm = cell0.lower().replace(" ", "")

                    if first_row and cell0_norm in ("url", "songurl", "trackurl"):
                        first_row = False
                        continue
                    first_row = False

                    if not cell0 or cell0.startswith("#"):
                        continue
                    rows.append({"url": cell0})
        except FileNotFoundError:
            await ctx.send(embed=discord.Embed(
                title="❌ Autofill CSV Reload Failed",
                description=f"CSV not found at `{path}`.",
                color=0xe74c3c
            ))
            return
        except Exception as e:
            await ctx.send(embed=discord.Embed(
                title="❌ Autofill CSV Reload Failed",
                description=f"{type(e).__name__}: {e}",
                color=0xe74c3c
            ))
            return

        total = len(rows)

        self._autofill_csv_cache = rows
        self.autofill_seed_rows[gid] = rows[:]
        try:
            self._autofill_csv_last_mtime = os.path.getmtime(path)
        except Exception:
            pass

        try:
            size = os.path.getsize(path)
            mtime = int(os.path.getmtime(path))
            diag = f"Size: {size} bytes • Updated: <t:{mtime}:t>"
        except Exception:
            diag = "Size/mtime unavailable"

        await ctx.send(embed=discord.Embed(
            title="✅ Autofill CSV Reloaded",
            description=(
                f"Path: `{path}`\n"
                f"Found **{total}** song URL(s).\n"
                f"{diag}"
            ),
            color=0x2ecc71
        ))

    @commands.has_permissions(administrator=True)
    @commands.command(name="autofill_unset")
    async def autofill_unset(self, ctx):
        """
        Unset the saved playlist/profile URL so CSV becomes the source again (Admin only).
        Keeps the 'enabled' flag; just removes the URL override.
        """
        gid = ctx.guild.id

        if gid in self.auto_playlist_urls:
            try:
                self.auto_playlist_urls.pop(gid, None)
            except Exception:
                self.auto_playlist_urls[gid] = ""

        amap = self.user_mappings.get(gid)
        if not isinstance(amap, dict):
            amap = {}
            self.user_mappings[guild_id] = amap
        ainfo = amap.get("autofill", {})
        enabled_state = bool(self.auto_play_enabled.get(gid, ainfo.get("enabled", True)))
        ainfo["enabled"] = enabled_state
        ainfo["url"] = ""
        amap["autofill"] = ainfo

        save_data(gid, self.queues, self.playlists, self.user_mappings)

        desc_lines = [
            "Cleared the **autofill URL override**.",
            f"**Enabled:** {'Yes' if enabled_state else 'No'}",
            "Source will now come from the **CSV** (or `DEFAULT_AUTOFILL_URL` if CSV is not set)."
        ]
        await ctx.send(embed=discord.Embed(
            title="🔄 Autofill Source Unset",
            description="\n".join(desc_lines),
            color=0x3498db
        ))

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
    async def queue_limit_off(self, ctx, per_user_max: int | None = None):
        """
        Turn the queue limit off (Admin only).
        Optionally also set the per-user cap while limits are off, e.g. !queue_limit_off 5
        """
        gid = ctx.guild.id
        self.queue_limit_enabled[gid] = False

        if per_user_max is not None:
            per_user_max = max(1, int(per_user_max))
            self.queue_per_user_max[gid] = per_user_max

        amap = self.user_mappings[gid]
        if not isinstance(amap, dict):
            amap = {}
            self.user_mappings[gid] = amap

        amap["queue_limit"] = {
            "enabled": False,
            "max": self._limit_max(gid),
            "per_user_max": self._per_user_max(gid),
        }
        save_data(gid, self.queues, self.playlists, self.user_mappings)
        await ctx.send(embed=discord.Embed(
            title="📦 Queue Limit",
            description=f"Queue limit is **OFF**.\nPer-user cap: **{self._per_user_max(gid)}**",
            color=0xe67e22
        ))

    @commands.has_permissions(administrator=True)
    @commands.command(name="queue_limit_set")
    async def queue_limit_set(self, ctx, max_per_add: int, per_user_max: int | None = None):
        """
        Set the queue limit amount (Admin only).
        Usage:
          !queue_limit_set 5 -> sets max per add to 5, leaves per-user cap as-is
          !queue_limit_set 5 3 -> sets max per add to 5 and per-user cap to 3
        """
        gid = ctx.guild.id
        max_per_add = max(1, int(max_per_add))
        self.queue_limit_max[gid] = max_per_add

        if per_user_max is not None:
            per_user_max = max(1, int(per_user_max))
            self.queue_per_user_max[gid] = per_user_max

        amap = self.user_mappings[gid]
        if not isinstance(amap, dict):
            amap = {}
            self.user_mappings[gid] = amap
        enabled = self._limit_is_on(gid)
        amap["queue_limit"] = {
            "enabled": enabled,
            "max": max_per_add,
            "per_user_max": self._per_user_max(gid),
        }
        save_data(gid, self.queues, self.playlists, self.user_mappings)

        desc = [f"Max songs per add set to **{max_per_add}**."]
        desc.append(f"Per-user cap: **{self._per_user_max(gid)}**")
        await ctx.send(embed=discord.Embed(
            title="📦 Queue Limit",
            description="\n".join(desc),
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
        per_user_cap = self._per_user_max(gid)
        await ctx.send(embed=discord.Embed(
            title="ℹ️ Queue Limit Status",
            description=f"**State:** {'ON' if enabled else 'OFF'}\n"
                        f"**Max per add:** {maxn}\n"
                        f"**Max per user:** {per_user_cap}",
            color=0x7289DA
        ))

    @commands.has_permissions(administrator=True)
    @commands.command(name="ping")
    async def ping(self, ctx):
        """
            Shows Server Ping information (Admin only)
        """
        gid = ctx.guild.id
        vc = ctx.voice_client

        # --- Core latency ---
        ws_ms = round(self.bot.latency * 1000)

        # --- Shard info (safe if not sharded) ---
        shard_id = getattr(ctx.guild, "shard_id", None)
        shard_str = f"{shard_id}" if shard_id is not None else "—"

        # --- Voice / playback state ---
        if vc and vc.channel:
            voice_state = f"Connected to **{vc.channel.name}**"
            is_playing = vc.is_playing()
        else:
            voice_state = "Not connected"
            is_playing = False

        current_title = None
        if self.current_song:
            current_title = self.current_song.get("title") or "Untitled"

        q_len = len(self.queues.get(gid, []))

        # --- Feature toggles ---
        autofill_enabled = self._is_autofill_enabled(gid)
        queue_limit_on = self._limit_is_on(gid)
        max_per_add = self._limit_max(gid)
        per_user_cap = self._per_user_max(gid)

        desc_lines = [
            f"**WebSocket:** `{ws_ms} ms`",
            f"**Shard:** `{shard_str}`",
            f"**Voice:** {voice_state}",
            f"**Playing:** `{'yes' if is_playing else 'no'}`",
        ]

        if current_title:
            desc_lines.append(f"**Now playing:** {_truncate(current_title, 80)}")

        desc_lines += [
            f"**Queue size:** `{q_len}`",
            f"**Autofill:** `{'on' if autofill_enabled else 'off'}`",
            f"**Queue limit:** `{'on' if queue_limit_on else 'off'}` (max/add `{max_per_add}`, per-user `{per_user_cap}`)",
        ]

        embed = discord.Embed(
            title="🏓 Pong",
            description="\n".join(desc_lines),
            color=0x2ecc71
        )
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(MusicCog(bot))
