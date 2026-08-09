import discord
from discord.ext import commands, tasks
from discord import ui, app_commands
from collections import deque, defaultdict
import asyncio
import logging
import random
import os
import concurrent.futures
import time
import re
import datetime
import csv
import inspect
from pathlib import Path
from discord.utils import escape_markdown

log = logging.getLogger(__name__)

from src.data.persistence import load_data, save_data
from src.utils.extractor import extract_song_info, _normalize_suno_short
from src.utils.playlist_fast_scraper import get_playlist_links_api
from src.data.db import (
    like_track, unlike_track, get_like_count, get_user_like_count,
    top_liked_for_users, top_liked_for_guild, get_user_liked_tracks_all_guilds, get_conn, recent_plays,
    record_track_failure, record_track_success, get_unhealthy_tracks,
    clear_track_health, prune_likes_and_tracks_for_urls,
)
from src.utils.shuffle_displacing_first import shuffle_displacing_first_inplace
try:
    from src.utils.image_cache import cache_song_image, get_default_image_path
except ImportError:
    from src.utils import image_cache
    cache_song_image = image_cache.cache_song_image
    get_default_image_path = image_cache.get_default_image_path
from src.ui.pagination import PaginatedView
from src.ui.liked_songs_manager import LikedSongsManagerView

try:
    from src.data.db import upsert_track_basic, log_play_start, log_play_end
except Exception:
    upsert_track_basic = lambda **kwargs: None
    def log_play_start(**kwargs): return None
    def log_play_end(**kwargs): return None

from .constants import *
from .embeds import (
    _fmt_duration, _duration_to_seconds, _truncate,
    _get_platform_color, _get_platform_name, _derive_suno_url,
    _canonical_track_id, _scrape_playlist_to_tracks,
    _track_title_link, _artist_line, _filler_badge, _prompt_text,
    _thumb, _get_thumbnail_info, _format_upcoming_list,
    _join_info_blocks, _chunk_text,
    build_now_playing_embed, build_added_embed, build_song_info_embed,
    _render_song_header, _render_prompt_lyrics_block,
)
from .views import (
    LyricsButton, LikeButton, NowPlayingView, PaginatedQueueView,
)


class BrokenSongsCleanupView(ui.View):
    """Interactive confirmation for `!autofill_health`.

    Shows a "Remove flagged" button (red) and a "Cancel" button. Restricted to
    the invoking admin. On confirm, removes flagged URLs from autofill.csv,
    prunes the matching rows from `likes` + `tracks`, and clears health rows.
    """

    def __init__(self, *, cog: "RadioBot", gid: int, invoker_id: int, flagged: list[dict], timeout: float = 60.0):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.gid = gid
        self.invoker_id = invoker_id
        self.flagged = flagged
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.invoker_id:
            return True
        await interaction.response.send_message(
            "Only the admin who ran this command can use these buttons.",
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, ui.Button):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception as e:
                log.debug("[autofill_health] timeout edit failed: %s", e)

    @ui.button(label="Remove Flagged", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def remove_flagged(self, interaction: discord.Interaction, button: ui.Button) -> None:
        await interaction.response.defer()
        urls = [str(f.get("url") or "").strip() for f in self.flagged if (f.get("url") or "").strip()]
        if not urls:
            await interaction.followup.send("Nothing to remove.", ephemeral=True)
            return

        try:
            csv_removed = await self.cog._remove_urls_from_autofill_csv(self.gid, urls)
        except Exception as e:
            log.exception("[autofill_health] CSV removal failed")
            await interaction.followup.send(f"CSV removal failed: `{type(e).__name__}: {e}`", ephemeral=True)
            return

        try:
            db_counts = prune_likes_and_tracks_for_urls(urls)
        except Exception as e:
            log.exception("[autofill_health] DB prune failed")
            db_counts = {"likes": 0, "tracks": 0, "error": f"{type(e).__name__}: {e}"}

        try:
            health_cleared = clear_track_health(urls)
        except Exception as e:
            log.debug("[autofill_health] clear_track_health failed: %s", e)
            health_cleared = 0

        for child in self.children:
            if isinstance(child, ui.Button):
                child.disabled = True

        summary = discord.Embed(
            title="🗑️ Cleanup Complete",
            description=(
                f"**{csv_removed}** row(s) removed from `autofill.csv`\n"
                f"**{db_counts.get('likes', 0)}** like(s) pruned\n"
                f"**{db_counts.get('tracks', 0)}** track row(s) deleted\n"
                f"**{health_cleared}** health record(s) cleared"
            ),
            color=0x2ecc71,
        )
        if db_counts.get("error"):
            summary.add_field(name="DB error", value=f"`{db_counts['error']}`", inline=False)

        try:
            await interaction.message.edit(embed=summary, view=self)
        except Exception as e:
            log.debug("[autofill_health] result edit failed: %s", e)
        self.stop()

        log.info(
            "[autofill_health] guild=%s admin=%s removed urls=%s csv=%s likes=%s tracks=%s health=%s",
            self.gid, self.invoker_id, len(urls),
            csv_removed, db_counts.get("likes", 0), db_counts.get("tracks", 0), health_cleared,
        )

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button) -> None:
        for child in self.children:
            if isinstance(child, ui.Button):
                child.disabled = True
        embed = discord.Embed(
            title="❎ Cleanup Cancelled",
            description="No changes were made.",
            color=0x95a5a6,
        )
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            log.debug("[autofill_health] cancel edit failed: %s", e)
        self.stop()


class ConfirmUnrequestAllView(ui.View):
    """Two-button confirmation for `!unrequest all`.

    Removes every autofill.csv row whose `requested_by` matches the invoking
    user's display name. Admins may target another user by passing
    `target_name` (set by the command when an admin uses `--user`).
    """

    def __init__(
        self,
        *,
        cog: "RadioBot",
        gid: int,
        invoker_id: int,
        target_name: str,
        urls: list[str],
        timeout: float = 30.0,
    ):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.gid = gid
        self.invoker_id = invoker_id
        self.target_name = target_name
        self.urls = urls
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.invoker_id:
            return True
        await interaction.response.send_message(
            "Only the user who ran this command can confirm.",
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, ui.Button):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception as e:
                log.debug("[unrequest_all] timeout edit failed: %s", e)

    @ui.button(label="Remove All", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: ui.Button) -> None:
        await interaction.response.defer()
        try:
            removed = await self.cog._remove_urls_from_autofill_csv(self.gid, self.urls)
        except Exception as e:
            log.exception("[unrequest_all] CSV removal failed")
            await interaction.followup.send(
                f"CSV removal failed: `{type(e).__name__}: {e}`",
                ephemeral=True,
            )
            return

        for child in self.children:
            if isinstance(child, ui.Button):
                child.disabled = True

        summary = discord.Embed(
            title="🗑️ Autofill Entries Removed",
            description=(
                f"Removed **{removed}** autofill row(s) belonging to "
                f"**{escape_markdown(self.target_name)}**."
            ),
            color=0x2ecc71,
        )
        try:
            await interaction.message.edit(embed=summary, view=self)
        except Exception as e:
            log.debug("[unrequest_all] result edit failed: %s", e)
        self.stop()

        log.info(
            "[unrequest] guild=%s invoker=%s target=%s removed=%s mode=all",
            self.gid, self.invoker_id, self.target_name, removed,
        )

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button) -> None:
        for child in self.children:
            if isinstance(child, ui.Button):
                child.disabled = True
        embed = discord.Embed(
            title="❎ Cancelled",
            description="No changes were made.",
            color=0x95a5a6,
        )
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            log.debug("[unrequest_all] cancel edit failed: %s", e)
        self.stop()


class _ResumeCtx:
    """Minimal stand-in for a commands.Context used after a voice reconnect.

    Built when the bot has rejoined a voice channel but no real ctx is cached
    (typically after a process restart, since `_reconnect_ctx` lives in-memory).
    Only exposes the attributes the play_next / autofill code path actually
    reads, and proxies `.send` to the cached text channel.
    """

    def __init__(self, *, guild: discord.Guild, text_channel: discord.abc.Messageable):
        self.guild = guild
        self.channel = text_channel
        # ctx.author is occasionally read for permission checks; the bot
        # always passes its own admin checks.
        self.author = guild.me

    @property
    def voice_client(self):
        return self.guild.voice_client

    async def send(self, *args, **kwargs):
        return await self.channel.send(*args, **kwargs)


class RadioBot(commands.Cog, name="Music"):
    def __init__(self, bot):
        self.bot = bot
        self.queues = defaultdict(deque)
        self.playlists = defaultdict(lambda: defaultdict(deque))
        self.user_mappings = defaultdict(dict)
        self.volumes = defaultdict(lambda: float(os.getenv("DEFAULT_VOLUME", "1.0")))
        self.current_song = None
        self.song_start_time = None
        # Which guild `current_song` belongs to (avoids multi-guild / race bugs with autofill)
        self._now_playing_guild_id: int | None = None
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

        # --- Now Playing tracking for pruning ----------------------------------
        self._song_index = defaultdict(int)
        self._np_track = defaultdict(list)
        self._np_retention_n = REMOVE_NP_AFTER_SONGS

        # --- Autofill saves DM message tracking (for cleanup) -------------------
        self._autofill_dm_messages = {}  # user_id -> discord.Message

        self.np_clean_non_autofill = {} # guild_id -> bool

        # --- Autofill queue recalculation debouncing ----------------------------
        self._autofill_recalc_timers = {}  # guild_id -> asyncio.Task for debouncing

        # --- Per-guild lock for serializing autofill CSV read-modify-write ------
        self._autofill_csv_locks: dict[int, asyncio.Lock] = {}

        # --- Voice reconnect tracking -------------------------------------------
        self._last_vc_channel: dict[int, int] = {}   # guild_id -> channel_id (to rejoin on drop)
        self._reconnect_ctx: dict[int, object] = {}   # guild_id -> ctx (for play_next after rejoin)
        # Last text channel id we've ever seen for this guild. Used to rebuild
        # a synthetic ctx when a reconnect happens before any command/play has
        # cached one (e.g. after a process restart).
        self._reconnect_text_channel: dict[int, int] = {}
        self._intentional_disconnect: set[int] = set()  # guilds where !leave was used
        self._played_csv_urls: dict[int, set] = defaultdict(set)  # per-guild: CSV URLs already played once
        self._reconnecting: dict[int, bool] = {}        # per-guild reconnect guard
        self._reconnect_task: dict[int, asyncio.Task] = {}  # per-guild active reconnect task
        self._reconnect_cooldown: dict[int, float] = {}  # guild_id -> monotonic timestamp of last failure

    def _is_admin(self, member: discord.Member) -> bool:
        """Admins bypass queue limitations."""
        try:
            perms = member.guild_permissions
            return bool(perms.administrator or perms.manage_guild)
        except Exception:
            return False

    @staticmethod
    def _set_encoder_bitrate(vc) -> None:
        """Apply custom encoder bitrate if configured."""
        if vc and getattr(vc, "encoder", None) and VOICE_BITRATE_KBPS:
            vc.encoder.bitrate = VOICE_BITRATE_KBPS * 1000

    async def _try_voice_reconnect(self, gid: int, retries: int = RECONNECT_RETRIES, delay: float = RECONNECT_BASE_DELAY) -> bool:
        """
        Attempt to rejoin the last voice channel for this guild.
        Returns True if reconnected successfully.
        Guarded: only one reconnect attempt per guild at a time.
        """
        if self._reconnecting.get(gid):
            log.debug("[reconnect] guild %s: already reconnecting, skipping", gid)
            return False

        cooldown_until = self._reconnect_cooldown.get(gid, 0)
        if time.monotonic() < cooldown_until:
            log.debug("[reconnect] guild %s: in cooldown, skipping", gid)
            return False

        self._reconnecting[gid] = True
        try:
            return await self._do_voice_reconnect(gid, retries, delay)
        finally:
            self._reconnecting[gid] = False

    def _get_radio_text_channel(self, guild: discord.Guild) -> discord.abc.Messageable | None:
        """Resolve the pinned radio text channel for now-playing / autofill posts."""
        if not RADIO_CONTROL_CHANNEL:
            return None
        ch = guild.get_channel(RADIO_CONTROL_CHANNEL)
        if ch is None:
            return None
        try:
            if ch.permissions_for(guild.me).send_messages:
                return ch
        except Exception as e:
            log.debug("[radio_pin] text channel perms check failed: %s", e)
        return None

    def _ctx_for_autofill(self, gid: int, fallback_ctx=None):
        """Return a ctx whose text channel is the pinned radio channel.

        Autofill and reconnect resumes should never post now-playing cards into
        whatever channel last ran a contest/admin command.
        """
        guild = self.bot.get_guild(gid)
        if guild is None:
            return fallback_ctx
        radio_ch = self._get_radio_text_channel(guild)
        if radio_ch is None:
            return fallback_ctx
        if (
            fallback_ctx is not None
            and getattr(getattr(fallback_ctx, "channel", None), "id", None) == radio_ch.id
        ):
            return fallback_ctx
        return _ResumeCtx(guild=guild, text_channel=radio_ch)

    def _remember_ctx(self, ctx) -> None:
        """Cache ctx + its text channel id for later reconnect-driven resumes."""
        try:
            gid = ctx.guild.id
        except Exception:
            return
        self._reconnect_ctx[gid] = ctx
        radio_ch = self._get_radio_text_channel(ctx.guild)
        if radio_ch is not None:
            # Never let contest/admin channels poison the reconnect text cache.
            self._reconnect_text_channel[gid] = radio_ch.id
            return
        try:
            ch = getattr(ctx, "channel", None)
            ch_id = getattr(ch, "id", None)
            if ch_id is not None:
                self._reconnect_text_channel[gid] = int(ch_id)
        except Exception as e:
            log.debug("[reconnect] remember_ctx channel cache failed: %s", e)

    def _build_resume_ctx(self, gid: int):
        """Synthesize a minimal ctx for reconnect-driven autoplay/autofill.

        Tries, in order:
          1. `RADIO_CONTROL_CHANNEL` env var, if set and present in this guild.
          2. Cached text channel id from `_reconnect_text_channel[gid]`.
          3. The guild's system channel, if the bot can send there.
          4. The first text channel where the bot has send permission.
        Returns None only when we genuinely have nowhere to talk.
        """
        guild = self.bot.get_guild(gid)
        if guild is None:
            return None

        me = guild.me
        text_ch = None

        radio_ch = self._get_radio_text_channel(guild)
        if radio_ch is not None:
            text_ch = radio_ch

        if text_ch is None:
            ch_id = self._reconnect_text_channel.get(gid)
            if ch_id:
                cand = guild.get_channel(int(ch_id))
                if cand is not None and getattr(cand, "permissions_for", None):
                    try:
                        if cand.permissions_for(me).send_messages:
                            text_ch = cand
                    except Exception as e:
                        log.debug("[reconnect] cached channel perms check failed: %s", e)

        if text_ch is None:
            sys_ch = guild.system_channel
            try:
                if sys_ch is not None and sys_ch.permissions_for(me).send_messages:
                    text_ch = sys_ch
            except Exception as e:
                log.debug("[reconnect] system channel perms check failed: %s", e)

        if text_ch is None:
            for cand in guild.text_channels:
                try:
                    if cand.permissions_for(me).send_messages:
                        text_ch = cand
                        break
                except Exception as e:
                    log.debug("[reconnect] text channel perms scan failed on %s: %s", cand.id, e)

        if text_ch is None:
            return None

        return _ResumeCtx(guild=guild, text_channel=text_ch)

    def _resume_after_reconnect(self, gid: int) -> None:
        """Re-arm playback or autofill after the bot is back in VC.

        Called from any reconnect path. Idempotent — safe to call even if
        the bot is already actively playing (in which case it's a no-op).
        This is the fix for "bot rejoined but stays silent": discord.py's
        built-in voice reconnect often succeeds before our manual reconnect
        kicks in, and the old code's early-return on `is_connected()` meant
        autofill never got rescheduled.
        """
        guild = self.bot.get_guild(gid)
        vc = getattr(guild, "voice_client", None) if guild else None
        if not (vc and vc.is_connected()):
            log.debug("[reconnect] guild %s: resume skipped (no live vc)", gid)
            return

        if self.current_song and self._now_playing_guild_id == gid and vc.is_playing():
            log.debug("[reconnect] guild %s: already playing, nothing to resume", gid)
            return

        ctx = self._reconnect_ctx.get(gid)
        if ctx is None:
            ctx = self._build_resume_ctx(gid)
            if ctx is None:
                log.warning(
                    "[reconnect] guild %s: no stored ctx and could not synthesize one "
                    "(no writable text channel) — cannot auto-resume", gid,
                )
                return
            log.info("[reconnect] guild %s: synthesized ctx for resume (text channel %s)", gid, getattr(ctx.channel, "id", "?"))
            # Cache so subsequent reconnects don't have to rebuild.
            self._reconnect_ctx[gid] = ctx
            try:
                ch_id = getattr(ctx.channel, "id", None)
                if ch_id is not None:
                    self._reconnect_text_channel[gid] = int(ch_id)
            except Exception as e:
                log.debug("[reconnect] cache synthesized channel failed: %s", e)

        if self.queues.get(gid):
            log.info("[reconnect] guild %s: resuming queue (%s songs)", gid, len(self.queues[gid]))
            try:
                self.bot.loop.create_task(self.play_next(ctx))
            except Exception as e:
                log.warning("[reconnect] guild %s: play_next schedule failed: %s", gid, e)
        elif self._is_autofill_enabled(gid):
            log.info("[reconnect] guild %s: scheduling autofill", gid)
            try:
                ctx = self._ctx_for_autofill(gid, fallback_ctx=ctx)
                self._schedule_autofill_if_idle(ctx)
            except Exception as e:
                log.warning("[reconnect] guild %s: autofill schedule failed: %s", gid, e)
        else:
            log.debug("[reconnect] guild %s: connected but no queue and autofill disabled", gid)

    async def _do_voice_reconnect(self, gid: int, retries: int, delay: float) -> bool:
        guild = self.bot.get_guild(gid)
        if not guild:
            return False

        # When a pinned radio VC is configured, always rejoin there — not
        # wherever a contest listening-party last moved the bot.
        pinned = self._get_pinned_radio_channel(gid)
        if pinned is not None:
            channel = pinned
        else:
            channel_id = self._last_vc_channel.get(gid)
            if not channel_id:
                return False
            channel = guild.get_channel(channel_id)
            if not channel:
                return False

        for attempt in range(1, retries + 1):
            try:
                existing = guild.voice_client
                if existing and existing.is_connected():
                    log.info("[reconnect] guild %s: already connected, nothing to do", gid)
                    return True
                if existing:
                    try:
                        await existing.disconnect(force=True)
                    except Exception as e:
                        log.debug("suppressed: %s", e)
                    await asyncio.sleep(1.0)

                await channel.connect(timeout=RECONNECT_JOIN_TIMEOUT, reconnect=True)
                self._set_encoder_bitrate(guild.voice_client)
                log.info("[reconnect] guild %s: rejoined %s (attempt %s)", gid, channel.name, attempt)
                return True
            except Exception as e:
                log.warning("[reconnect] guild %s: attempt %s/%s failed: %s", gid, attempt, retries, e)
                if attempt < retries:
                    await asyncio.sleep(delay * attempt)

        log.error("[reconnect] guild %s: all %s attempts failed, cooling down 30s", gid, retries)
        self._reconnect_cooldown[gid] = time.monotonic() + 30
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
            except Exception as e:
                log.debug("suppressed: %s", e)
            return

        # prevent double fades
        if self._fadeout_active[gid]:
            try:
                vc.stop()
            except Exception as e:
                log.debug("suppressed: %s", e)
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
            except Exception as e:
                log.debug("suppressed: %s", e)

            vc.stop()

        except Exception as e:
            # log it to console but don't break playback system
            log.warning("[fadeout] error: %s", e)
            try:
                vc.stop()
            except Exception as e:
                log.debug("suppressed: %s", e)

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

    async def _check_requester_in_vc(self, ctx, song: dict) -> bool:
        """
        Check if the requester of a song is still in the voice channel.
        Returns True if requester is in VC (or if check should be skipped), False otherwise.
        
        Skips check for:
        - Autofill tracks (they have _autofill flag)
        - Tracks without a requester_id
        - If SKIP_IF_REQUESTER_LEFT is disabled
        """
        # Skip check if feature is disabled
        if not SKIP_IF_REQUESTER_LEFT:
            return True
        
        # Skip check for autofill tracks
        if song.get("_autofill"):
            return True
        
        # Skip check if no requester_id
        requester_id = song.get("requester_id")
        if not requester_id:
            return True
        
        # Skip check if no voice client or channel
        if not ctx.voice_client or not ctx.voice_client.channel:
            return True
        
        # Check if requester is in the voice channel
        vc_members = ctx.voice_client.channel.members
        requester_in_vc = any(member.id == requester_id for member in vc_members)
        
        return requester_in_vc

    def _deny_user_cap_embed(self, requester_mention: str | None = None, gid: int | None = None) -> discord.Embed:
        cap = self._per_user_max(gid) if gid is not None else QUEUE_MAX_PER_USER
        who = requester_mention or "You"
        # Always log when the cap fires so we can trace which command path
        # produced it — pure-text embeds are otherwise invisible in logs.
        try:
            stack = inspect.stack()
            caller = next(
                (f.function for f in stack[1:6] if f.function not in ("_deny_user_cap_embed",)),
                "?",
            )
        except Exception:
            caller = "?"
        log.info(
            "[per_user_cap] denied: gid=%s cap=%s caller=%s requester=%s",
            gid, cap, caller, requester_mention or "<none>",
        )
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

    def _clear_now_playing_if_guild(self, gid: int) -> None:
        """Clear now-playing state only if it belongs to this guild (multi-guild safe)."""
        if getattr(self, "_now_playing_guild_id", None) == gid:
            self.current_song = None
            self.song_start_time = None
            self._now_playing_guild_id = None

    def _set_now_playing(self, gid: int, song) -> None:
        self.current_song = song
        self._now_playing_guild_id = gid

    def _embed_queue_empty_notice(self, gid: int) -> discord.Embed:
        """Embed when playback queue becomes empty and autofill is off."""
        return discord.Embed(
            title="⏹️ Queue Empty",
            description="Finished playing! 🎉",
            color=0x00FF00,
        )

    def _clear_autofill_from_queue(self, gid: int):
        dq = self.queues[gid]
        if not dq:
            return
        kept = [t for t in dq if not t.get("_autofill")]
        dq.clear()
        dq.extend(kept)

    @staticmethod
    def _is_unknown_track(song: dict) -> bool:
        title = (song.get("title") or "").strip().lower()
        artist = (song.get("artist") or song.get("author") or "").strip().lower()
        return (
            title in ("unknown title", "untitled", "")
            and artist in ("unknown", "unknown artist", "", "none")
        )

    def _is_contest_active(self, gid: int) -> bool:
        """True while an anonymous contest is playing/voting in this guild."""
        contest_cog = self.bot.get_cog("Contest")
        return bool(contest_cog and contest_cog.is_contest_active(gid))

    async def _check_game_active(self, ctx) -> bool:
        """Return True (and send a message) if a game/contest is active and the user isn't admin."""
        games_cog = self.bot.get_cog("Games")
        if games_cog and games_cog.is_game_active(ctx.guild.id):
            if not ctx.author.guild_permissions.administrator:
                await ctx.send("🚫 **Game in progress!** Please finish the game or run `!stopgame` before using this command.")
                return True
        if self._is_contest_active(ctx.guild.id):
            if not ctx.author.guild_permissions.administrator:
                await ctx.send("🚫 **Contest in progress!** Please wait for the contest to finish before using this command.")
                return True
        return False

    @staticmethod
    def _build_ffmpeg_options(*, is_hls: bool = False, is_ytdlp: bool = False) -> dict:
        """Build FFmpeg before_options/options dict for the given stream type.

        The filter chain pads silence on both ends so Discord's voice client
        warm-up doesn't clip the head of the track and so the tail doesn't
        cut off abruptly before `after_playing` fires. Tunable via the
        STARTUP_ADELAY_MS and OUTRO_APAD_MS env vars.
        """
        base_before = (
            f"-probesize {FFMPEG_PROBESIZE} "
            f"-analyzeduration {FFMPEG_ANALYZEDURATION} "
        )
        reconnect = (
            "-reconnect 1 "
            "-reconnect_streamed 1 "
            "-reconnect_delay_max 10 "
        )

        # Build the audio filter chain. Skip each padding stage cleanly if
        # the corresponding env var is set to 0 so users can opt out.
        af_parts: list[str] = []
        if STARTUP_ADELAY_MS > 0:
            af_parts.append(f"adelay={STARTUP_ADELAY_MS}|{STARTUP_ADELAY_MS}")
        if OUTRO_APAD_MS > 0:
            af_parts.append(f"apad=pad_dur={OUTRO_APAD_MS}ms")

        if is_hls:
            # HLS sources benefit from async resample to fix PTS jitter; it
            # must come first in the chain.
            af_parts.insert(0, "aresample=async=1:first_pts=0")
            before = (
                base_before
                + "-protocol_whitelist file,http,https,tcp,tls,crypto "
                + f"-rw_timeout {FFMPEG_RW_TIMEOUT_US} "
                + "-nostdin"
            )
        elif is_ytdlp:
            before = (
                base_before + reconnect
                + f"-rw_timeout {FFMPEG_RW_TIMEOUT_US} "
                + "-nostdin"
            )
        else:
            before = (
                base_before
                + f"-thread_queue_size {FFMPEG_THREAD_QUEUE_SIZE} "
                + f"-max_delay {FFMPEG_MAX_DELAY_US} "
                + reconnect
                + f"-rw_timeout {FFMPEG_RW_TIMEOUT_US} "
                + "-nostdin"
            )

        opts = "-vn"
        if af_parts:
            opts += f" -af {','.join(af_parts)}"
        return {"before_options": before, "options": opts}

    def _resolve_autofill_csv_path(self, gid: int) -> str:
        csv_path = ""
        try:
            amap = self.user_mappings.get(gid) or {}
            ainfo = amap.get("autofill") or {}
            csv_path = (ainfo.get("csv") or "").strip()
        except Exception as e:
            log.debug("suppressed: %s", e)
        if not csv_path:
            csv_path = (
                os.getenv("AUTOFILL_CSV_PATH", "").strip()
                or os.getenv("DEFAULT_AUTOFILL_CSV", "").strip()
                or "autofill.csv"
            )
        return os.path.abspath(os.path.expanduser(csv_path))

    async def _full_playback_teardown(self, gid: int):
        self._cancel_autofill_task(gid)
        self._clear_autofill_from_queue(gid)
        self._clear_now_playing_if_guild(gid)
        if self.update_song_activity.is_running():
            self.update_song_activity.stop()
        await self.bot.change_presence(activity=None)
        await self._clear_all_np_messages_for_guild(gid)

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
                except Exception as e:
                    log.debug("suppressed: %s", e)

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
                    rb_id = (r[2] if len(r) >= 3 else "").strip()
                    if url:
                        rows.append({"url": url, "requested_by": rb, "requested_by_id": rb_id})
        except Exception as e:
            log.error("[autofill CSV] Failed to load %s: %s", path, e)
        return rows

    # ---- /request command helpers ---------------------------------------
    def _get_autofill_csv_lock(self, gid: int) -> asyncio.Lock:
        """Per-guild lock to serialize concurrent autofill CSV writes."""
        lock = self._autofill_csv_locks.get(gid)
        if lock is None:
            lock = asyncio.Lock()
            self._autofill_csv_locks[gid] = lock
        return lock

    _SUNO_SONG_RE = re.compile(r"suno\.com/song/([a-f0-9\-]{8,})", re.IGNORECASE)

    def _extract_suno_song_id(self, url: str) -> tuple[str | None, str | None]:
        """
        Validate and normalize a Suno song URL.

        Returns (canonical_url, song_id) if the URL is a valid Suno song link
        (either /song/<id> or /s/<short> which is resolved via HEAD), or
        (None, None) otherwise. May make a network HEAD request for short links
        - call this from an executor.
        """
        if not url:
            return None, None
        u = url.strip()
        # Strip wrapping angle brackets that Discord uses to suppress previews
        if u.startswith("<") and u.endswith(">"):
            u = u[1:-1].strip()
        if not u:
            return None, None
        # Resolve /s/<short> -> /song/<id> via HEAD (network call)
        if "suno.com/s/" in u and "suno.com/song/" not in u:
            try:
                u = _normalize_suno_short(u)
            except Exception as e:
                log.debug("[request] suno short normalize failed: %s", e)
        m = self._SUNO_SONG_RE.search(u)
        if not m:
            return None, None
        song_id = m.group(1).lower()
        return f"https://suno.com/song/{song_id}", song_id

    def _atomic_write_autofill_csv(self, path: str, rows: list[dict]) -> None:
        """Atomically write the autofill CSV (header + rows) to `path`."""
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Song URL", "Requested by", "Requested by ID"])
            for row in rows:
                writer.writerow([
                    (row.get("url") or "").strip(),
                    (row.get("requested_by") or "").strip(),
                    (row.get("requested_by_id") or "").strip(),
                ])
        os.replace(tmp_path, path)

    async def _remove_urls_from_autofill_csv(self, gid: int, urls: list[str]) -> int:
        """Remove every row whose URL is in `urls` from this guild's autofill CSV.

        Uses the same per-guild lock as /request to avoid races. Refreshes the
        in-memory seed cache. Returns the number of rows removed.
        """
        if not urls:
            return 0
        target = {(u or "").strip().lower() for u in urls if (u or "").strip()}
        if not target:
            return 0

        path = self._resolve_autofill_csv_path(gid)
        async with self._get_autofill_csv_lock(gid):
            rows = self._load_autofill_csv(path)
            kept = [r for r in rows if (r.get("url") or "").strip().lower() not in target]
            removed = len(rows) - len(kept)
            if removed > 0:
                self._atomic_write_autofill_csv(path, kept)
                self.autofill_seed_rows[gid] = kept[:]
        return removed

    @staticmethod
    def _row_matches_member(row: dict, member: "discord.Member | discord.User") -> bool:
        """Does `row` belong to `member`?

        Rows written since the `requested_by_id` column was added carry the
        requester's stable Discord user id, so we match on that first. Older
        rows only have the requester's *display name* at the time they typed
        the command; since display names/nicknames can change, we fall back
        to a case-insensitive name match for those legacy rows only.
        """
        if member is None:
            return False
        rid = (row.get("requested_by_id") or "").strip()
        if rid:
            return rid == str(member.id)
        rb = (row.get("requested_by") or "").strip().lower()
        return bool(rb) and rb == (getattr(member, "display_name", None) or member.name or "").strip().lower()

    @staticmethod
    def _count_requests_for(rows: list[dict], member: "discord.Member | discord.User") -> int:
        """Count rows belonging to `member` (see `_row_matches_member`)."""
        return sum(1 for r in rows if RadioBot._row_matches_member(r, member))

    def _csv_seed_with_fresh_priority(self, gid: int) -> list[dict]:
        """
        Return the CSV seed reordered so the most recently appended rows (the
        "fresh tail", controlled by AUTOFILL_CSV_FRESH_TAIL_SIZE) come first,
        with played-this-session songs pushed to the back of each group.

        Rationale: /request appends to the end of autofill.csv, so the fresh
        tail is roughly "the last N user requests". Putting them first in the
        candidate pool makes them very likely to land in the next autofill
        cycle's CSV slot bucket.

        Output is a list of dicts shaped like the raw seed
        ({"url", "requested_by"}), already deduplicated by URL.
        """
        seed = self.autofill_seed_rows.get(gid) or []
        if not seed:
            return []
        played = self._played_csv_urls[gid]

        n = AUTOFILL_CSV_FRESH_TAIL_SIZE
        if n > 0 and len(seed) > n:
            fresh_tail = seed[-n:]
            fresh_urls = {(r.get("url") or "").strip() for r in fresh_tail if r.get("url")}
            older = [r for r in seed if (r.get("url") or "").strip() not in fresh_urls]
        else:
            fresh_tail = seed[:]
            older = []

        def _split_shuffle(group: list[dict]) -> list[dict]:
            new_g = [r for r in group if (r.get("url") or "").strip() not in played]
            old_g = [r for r in group if (r.get("url") or "").strip() in played]
            random.shuffle(new_g)
            random.shuffle(old_g)
            return new_g + old_g

        combined = _split_shuffle(fresh_tail) + _split_shuffle(older)

        seen: set[str] = set()
        deduped: list[dict] = []
        for r in combined:
            u = (r.get("url") or "").strip()
            if u and u not in seen:
                seen.add(u)
                deduped.append(r)
        return deduped

    async def _get_autofill_liked_raw(self, ctx, gid: int) -> list[dict]:
        try:
            vc = ctx.voice_client
        except AttributeError:
            vc = None

        if not vc or not getattr(vc, "channel", None):
            return []

        members = [m for m in vc.channel.members if not getattr(m, "bot", False)]
        user_ids = [m.id for m in members]

        empty_vc = not user_ids
        try:
            if empty_vc:
                # No humans in VC: fall back to the guild-wide liked pool with
                # uniform weights, so autofill still gets variety from the DB
                # instead of cycling exclusively through autofill.csv.
                all_songs = top_liked_for_guild(
                    guild_id=gid,
                    limit=AUTOFILL_LIKED_LIMIT,
                )
            else:
                all_songs = top_liked_for_users(
                    guild_id=gid,
                    user_ids=user_ids,
                    limit=AUTOFILL_LIKED_LIMIT,
                )
        except Exception as e:
            log.warning("[autofill likes] failed to fetch liked tracks: %s", e)
            return []

        if not all_songs:
            return []

        if empty_vc:
            # Uniform weights → every guild-liked song equally likely.
            weights = [1] * len(all_songs)
        else:
            # Weight by like count so popular tracks surface more often.
            weights = [max(1, song.get("like_count", 1)) for song in all_songs]

        # Weighted random selection - select up to AUTOFILL_MAX_PULL
        selected_count = min(AUTOFILL_MAX_PULL, len(all_songs))
        selected = random.choices(all_songs, weights=weights, k=selected_count)

        # Convert to expected format and deduplicate by URL
        seen_urls = set()
        raw: list[dict] = []
        for song in selected:
            url = (song.get("source_url") or "").strip()
            if not url:
                continue
            if url in seen_urls:
                continue  # Skip duplicates
            seen_urls.add(url)
            raw.append(
                {
                    "id": song.get("track_id"),
                    "url": url,
                    "suno_url": url,
                    "_liked_weight": song.get("like_count", 0),
                }
            )

        return raw

    def _apply_recently_played_guard(self, gid: int, tracks: list[dict]) -> list[dict]:
        """
        Reorder tracks so recently played songs (last 10) are moved to the end.
        The recently played portion is shuffled before appending.
        """
        try:
            recent = recent_plays(guild_id=gid, limit=10, include_autofill=True)
            recent_track_ids = {str(r["track_id"]) for r in recent if r.get("track_id")}
        except Exception:
            return tracks  # If query fails, return unchanged
        
        if not recent_track_ids:
            return tracks
        
        not_recent = []
        recently_played = []
        
        for t in tracks:
            track_id = _canonical_track_id(t)
            if track_id and track_id in recent_track_ids:
                recently_played.append(t)
            else:
                not_recent.append(t)
        
        # Shuffle the recently played list before appending
        random.shuffle(recently_played)
        
        return not_recent + recently_played

    async def _enqueue_autofill_batch(self, ctx, gid: int):
        liked_raw = await self._get_autofill_liked_raw(ctx, gid)
        random.shuffle(liked_raw)

        url = (self.auto_playlist_urls.get(gid) or "").strip()
        csv_raw: list[dict] = []

        if url:
            raw_from_url = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _scrape_playlist_to_tracks(url, limit=AUTOFILL_MAX_PULL)
            )
            if raw_from_url:
                random.shuffle(raw_from_url)
                csv_raw = raw_from_url
        else:
            deduped = self._csv_seed_with_fresh_priority(gid)
            if deduped:
                csv_raw = [
                    {"url": r["url"], "requested_by_note": r.get("requested_by", "")}
                    for r in deduped
                ]

        csv_slots = max(1, int(AUTOFILL_MAX_PULL * AUTOFILL_CSV_MIN_RATIO))
        liked_slots = AUTOFILL_MAX_PULL - csv_slots

        csv_pick = csv_raw[:csv_slots]
        liked_pick = liked_raw[:liked_slots]

        seen_urls: set[str] = set()
        combined_raw: list[dict] = []

        for it in csv_pick:
            u = str(it.get("url") or it.get("suno_url") or "").strip()
            if u and u not in seen_urls:
                seen_urls.add(u)
                combined_raw.append(it)

        for it in liked_pick:
            u = str(it.get("url") or it.get("suno_url") or "").strip()
            if u and u not in seen_urls:
                seen_urls.add(u)
                combined_raw.append(it)

        remaining = AUTOFILL_MAX_PULL - len(combined_raw)
        if remaining > 0:
            for it in csv_raw[csv_slots:] + liked_raw[liked_slots:]:
                u = str(it.get("url") or it.get("suno_url") or "").strip()
                if u and u not in seen_urls:
                    seen_urls.add(u)
                    combined_raw.append(it)
                    if len(combined_raw) >= AUTOFILL_MAX_PULL:
                        break

        random.shuffle(combined_raw)
        
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

        tracks = await self._resolve_tracks(cleaned_raw, max_workers=RESOLVER_MAX_WORKERS)
        # Don't shuffle here - we've already shuffled separately and want to preserve order (user songs first, then CSV)

        valid_tracks = [
            t for t in tracks
            if not self._is_unknown_track(t) and not t.get("_resolve_failed")
        ]

        # Apply recently played guard - move recently played songs to the end
        valid_tracks = self._apply_recently_played_guard(gid, valid_tracks)

        now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        for t in valid_tracks:
            t["_autofill"] = True
            t.setdefault("tags", []).append("filler")

            t["requester_id"] = self.bot.user.id if self.bot.user else None
            t["requester_tag"] = "Autofill"
            t["requester_name"] = "Autofill"
            t["requester_mention"] = None
            t["requested_at"] = now_ts

            self.queues[gid].append(t)
            track_url = str(t.get("url") or t.get("suno_url") or "").strip()
            if track_url:
                self._played_csv_urls[gid].add(track_url)

        save_data(gid, self.queues, self.playlists, self.user_mappings)
        return len(valid_tracks)

    def _get_pinned_radio_channel(self, gid: int) -> discord.VoiceChannel | None:
        """Resolve `RADIO_VC_ID` to a VoiceChannel in the given guild.

        Returns None if unset, the channel doesn't exist, isn't in this guild,
        or isn't actually a voice channel. Safe to call from any context.
        """
        if not RADIO_VC_ID:
            return None
        guild = self.bot.get_guild(gid)
        if guild is None:
            return None
        ch = guild.get_channel(RADIO_VC_ID)
        if not isinstance(ch, discord.VoiceChannel):
            log.debug(
                "[radio_pin] guild %s: RADIO_VC_ID=%s did not resolve to a voice channel (got %r)",
                gid, RADIO_VC_ID, type(ch).__name__ if ch else None,
            )
            return None
        return ch

    async def _ensure_in_pinned_radio_channel(self, gid: int) -> bool:
        """If a pinned radio channel is configured and the bot isn't in it,
        silently move (or connect). Returns True if the bot is in the pinned
        channel after this call (or if no pin is configured).

        Returns False only when a pin IS set but we couldn't get the bot into
        it — the caller can decide whether to proceed or bail.
        """
        pinned = self._get_pinned_radio_channel(gid)
        if pinned is None:
            return True  # no pin -> nothing to enforce

        guild = self.bot.get_guild(gid)
        if guild is None:
            return False

        vc = guild.voice_client
        try:
            if vc is None or not vc.is_connected():
                await pinned.connect(timeout=RECONNECT_JOIN_TIMEOUT, reconnect=True)
                self._set_encoder_bitrate(guild.voice_client)
                self._last_vc_channel[gid] = pinned.id
                log.info("[radio_pin] guild %s: connected to pinned channel %s", gid, pinned.name)
                return True
            if vc.channel and vc.channel.id != pinned.id:
                log.info(
                    "[radio_pin] guild %s: moving bot from %s -> pinned %s",
                    gid, vc.channel.name, pinned.name,
                )
                await vc.move_to(pinned)
                self._last_vc_channel[gid] = pinned.id
                return True
            return True
        except Exception as e:
            log.warning("[radio_pin] guild %s: could not enforce pin (%s): %s", gid, type(e).__name__, e)
            return False

    async def _autofill_after_delay(self, ctx, gid: int, delay: int, deep_attempt: int = 0):
        my_task = asyncio.current_task()
        try:
            await asyncio.sleep(max(0, delay))
            if self.queues[gid]:
                log.warning("[autofill] guild %s: queue non-empty after delay, skipping", gid)
                return
            if self.current_song and getattr(self, "_now_playing_guild_id", None) == gid:
                log.warning("[autofill] guild %s: song already playing, skipping", gid)
                return
            if not self._is_autofill_enabled(gid):
                log.warning("[autofill] guild %s: autofill disabled, skipping", gid)
                return

            # If a pinned radio VC is configured, enforce it BEFORE any batch
            # work. Handles: (a) bot was moved to a non-radio VC by a contest
            # and !stop rescheduled autofill in the wrong channel; (b) bot
            # rejoined via `!join` in an unrelated VC after a restart. Runs
            # only for autofill — manual !play/!playlist still honor the
            # invoker's VC.
            if not await self._ensure_in_pinned_radio_channel(gid):
                log.warning(
                    "[autofill] guild %s: pinned radio channel enforcement failed; skipping this cycle",
                    gid,
                )
                return

            vc = ctx.voice_client
            if vc is None:
                vc = ctx.guild.voice_client
            if not vc:
                log.warning("[autofill] guild %s: no voice_client after delay, skipping", gid)
                return

            log.debug("[autofill] guild %s: pulling batch…", gid)
            try:
                await self._clear_all_np_messages_for_guild(gid)
            except Exception as e:
                log.debug("[autofill] NP cleanup failed: %s", e)
            added = await self._enqueue_autofill_batch(ctx, gid)
            if added > 0 and (ctx.voice_client or ctx.guild.voice_client):
                log.info("[autofill] guild %s: enqueued %s songs, starting playback", gid, added)
                await self.play_next(ctx)
                return

            # added == 0: short retry first
            log.warning("[autofill] guild %s: batch returned 0 songs (resolve/filter issue), retry in %ss", gid, AUTOFILL_RETRY_DELAY_SEC)
            await asyncio.sleep(AUTOFILL_RETRY_DELAY_SEC)
            if not self.queues[gid] and self._is_autofill_enabled(gid) and (ctx.voice_client or ctx.guild.voice_client):
                added2 = await self._enqueue_autofill_batch(ctx, gid)
                if added2 > 0 and (ctx.voice_client or ctx.guild.voice_client):
                    log.info("[autofill] guild %s: retry enqueued %s songs", gid, added2)
                    await self.play_next(ctx)
                    return

            # Both quick attempts returned 0 → schedule a deep retry (covers
            # transient outages like Suno hiccups). Bounded by
            # AUTOFILL_DEEP_RETRY_MAX_ATTEMPTS so we don't loop forever.
            if (
                deep_attempt + 1 < AUTOFILL_DEEP_RETRY_MAX_ATTEMPTS
                and self._is_autofill_enabled(gid)
                and (ctx.voice_client or ctx.guild.voice_client)
            ):
                next_attempt = deep_attempt + 1
                log.warning(
                    "[autofill] guild %s: scheduling deep retry %s/%s in %ss",
                    gid, next_attempt, AUTOFILL_DEEP_RETRY_MAX_ATTEMPTS, AUTOFILL_DEEP_RETRY_DELAY_SEC,
                )
                self.auto_play_tasks[gid] = self.bot.loop.create_task(
                    self._autofill_after_delay(ctx, gid, AUTOFILL_DEEP_RETRY_DELAY_SEC, deep_attempt=next_attempt)
                )
            else:
                log.warning(
                    "[autofill] guild %s: exhausted %s deep retries — manual intervention needed",
                    gid, AUTOFILL_DEEP_RETRY_MAX_ATTEMPTS,
                )

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[autofill] guild %s FAILED", gid)
        finally:
            if self.auto_play_tasks.get(gid) is my_task:
                self.auto_play_tasks[gid] = None

    def _schedule_autofill_if_idle(self, ctx, delay: int | None = None):
        gid = ctx.guild.id
        ctx = self._ctx_for_autofill(gid, fallback_ctx=ctx)

        # Check if a game or contest is active
        games_cog = self.bot.get_cog("Games")
        if games_cog and games_cog.is_game_active(gid):
            return
        if self._is_contest_active(gid):
            return

        if not self._is_autofill_enabled(gid):
            return
        use_delay = AUTOFILL_DELAY_SEC if (delay is None) else max(0, int(delay))

        # New idle event: cancel any pending wait so we always re-arm (fixes missed autofill)
        prev = self.auto_play_tasks.get(gid)
        if prev and not prev.done():
            prev.cancel()

        self.auto_play_tasks[gid] = self.bot.loop.create_task(
            self._autofill_after_delay(ctx, gid, use_delay)
        )

    async def _recalculate_autofill_queue(self, guild: discord.Guild):
        """
        Completely rebuild the autofill queue based on current listeners in VC.
        
        When users join or leave while autofill is playing:
        1. Keep manual songs as-is
        2. Completely regenerate the autofill portion using the new user list
        3. Use weighted selection based on combined likes from current VC users
        4. Shuffle and deduplicate the new autofill list
        5. Replace the autofill portion of the queue
        """
        gid = guild.id
        
        # Check if autofill is enabled
        if not self._is_autofill_enabled(gid):
            return
        
        # Check if there are any autofill songs in queue
        queue = self.queues.get(gid, deque())
        has_autofill = any(song.get("_autofill") for song in queue)
        if not has_autofill:
            return  # Only manual songs, no need to recalculate
        
        # Check if bot is in VC
        try:
            vc = guild.voice_client
            if not vc or not getattr(vc, "channel", None):
                return
        except Exception:
            return
        
        # Split queue into manual and autofill songs (keep manual songs unchanged)
        manual_songs = [s for s in queue if not s.get("_autofill")]
        
        # Create a mock context for methods that need it
        class MockContext:
            def __init__(self, guild, voice_client):
                self.guild = guild
                self.voice_client = voice_client
        
        ctx = MockContext(guild, vc)
        
        # --- Completely regenerate autofill from scratch ---
        
        # Step 1: Get weighted liked songs for current VC users
        liked_raw = await self._get_autofill_liked_raw(ctx, gid)
        random.shuffle(liked_raw)
        liked_raw = liked_raw[:AUTOFILL_MAX_PULL]
        remaining = max(0, AUTOFILL_MAX_PULL - len(liked_raw))
        
        # Step 2: Fill remaining slots with CSV/URL fallback
        fallback_raw: list[dict] = []
        if remaining > 0:
            url = (self.auto_playlist_urls.get(gid) or "").strip()
            if url:
                raw_from_url = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: _scrape_playlist_to_tracks(url, limit=AUTOFILL_MAX_PULL)
                )
                if raw_from_url:
                    random.shuffle(raw_from_url)
                    fallback_raw = raw_from_url[:remaining]
            else:
                pick = self._csv_seed_with_fresh_priority(gid)[:remaining]
                if pick:
                    fallback_raw = [
                        {"url": r["url"], "requested_by_note": r.get("requested_by", "")}
                        for r in pick
                    ]
        
        # Step 3: Combine and deduplicate by URL
        seen_urls = set()
        combined_raw = []
        
        for it in liked_raw:
            url = str(it.get("url") or it.get("suno_url") or "").strip()
            if url and url not in seen_urls:
                seen_urls.add(url)
                combined_raw.append(it)
        
        for it in fallback_raw:
            url = str(it.get("url") or it.get("suno_url") or "").strip()
            if url and url not in seen_urls:
                seen_urls.add(url)
                combined_raw.append(it)
        
        if not combined_raw:
            # No autofill songs available - just keep manual songs
            # Re-read manual songs to capture any added during async operations
            manual_songs = [s for s in queue if not s.get("_autofill")]
            queue.clear()
            queue.extend(manual_songs)
            save_data(gid, self.queues, self.playlists, self.user_mappings)
            return
        
        # Step 4: Clean URLs
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
            # Re-read manual songs to capture any added during async operations
            manual_songs = [s for s in queue if not s.get("_autofill")]
            queue.clear()
            queue.extend(manual_songs)
            save_data(gid, self.queues, self.playlists, self.user_mappings)
            return
        
        # Step 5: Resolve tracks
        tracks = await self._resolve_tracks(cleaned_raw, max_workers=RESOLVER_MAX_WORKERS)
        
        valid_tracks = [
            t for t in tracks
            if not self._is_unknown_track(t) and not t.get("_resolve_failed")
        ]

        # Step 7: Mark as autofill and set metadata
        now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        for t in valid_tracks:
            t["_autofill"] = True
            t.setdefault("tags", []).append("filler")
            t["requester_id"] = self.bot.user.id if self.bot.user else None
            t["requester_tag"] = "Autofill"
            t["requester_name"] = "Autofill"
            t["requester_mention"] = None
            t["requested_at"] = now_ts
        
        # Step 8: Final shuffle
        random.shuffle(valid_tracks)
        
        # Step 9: Final deduplication check by track_id and URL (safety net)
        final_autofill = []
        seen_track_ids = set()
        seen_final_urls = set()
        
        for t in valid_tracks:
            track_id = _canonical_track_id(t)
            track_url = str(t.get("url") or t.get("suno_url") or "").strip()
            
            # Skip if we've seen this track_id before
            if track_id and track_id in seen_track_ids:
                continue
            
            # Skip if we've seen this URL before
            if track_url and track_url in seen_final_urls:
                continue
            
            # Add to seen sets
            if track_id:
                seen_track_ids.add(track_id)
            if track_url:
                seen_final_urls.add(track_url)
            
            final_autofill.append(t)
        
        # Apply recently played guard - move recently played songs to the end
        final_autofill = self._apply_recently_played_guard(gid, final_autofill)
        
        # Step 10: Reconstruct queue - manual songs first, then new autofill songs
        # Re-read manual songs to capture any added during async operations above
        manual_songs = [s for s in queue if not s.get("_autofill")]
        queue.clear()
        queue.extend(manual_songs)
        queue.extend(final_autofill)
        
        # Save queue state
        save_data(gid, self.queues, self.playlists, self.user_mappings)

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
        if RADIO_CONTROL_CHANNEL:
            try:
                radio_channel = ctx.guild.get_channel(RADIO_CONTROL_CHANNEL)
                return radio_channel if radio_channel else ctx.channel
            except (ValueError, TypeError):
                pass
        return ctx.channel

    def format_time(self, seconds):
        mins, secs = divmod(int(seconds), 60)
        return f"{mins:02d}:{secs:02d}"

    @staticmethod
    def _classify_resolve_failure(exc: BaseException) -> str:
        """Map a resolver exception to one of: 'gone', 'transient', 'parse'.

        Only 'gone' (confirmed HTTP 404/410) is allowed to count against a
        track's persistent failure record. Everything else is treated as
        recoverable noise to avoid false-positive deletions during outages.
        """
        try:
            import requests as _rq
        except Exception:
            _rq = None

        if _rq is not None and isinstance(exc, _rq.exceptions.HTTPError):
            resp = getattr(exc, "response", None)
            status = getattr(resp, "status_code", None)
            if status in (404, 410):
                return "gone"
            return "transient"
        if _rq is not None and isinstance(exc, (_rq.exceptions.ConnectionError, _rq.exceptions.Timeout)):
            return "transient"
        if isinstance(exc, ValueError):
            # e.g. "Could not extract Suno audio URL" — page loaded but markup
            # changed or song is hidden. Ambiguous; never delete on this alone.
            return "parse"
        return "transient"

    async def _resolve_tracks(self, items: list[dict], max_workers: int = 6, timeout: int = 90) -> list[dict]:
        loop = asyncio.get_event_loop()

        def _resolve_one(item: dict) -> dict:
            try:
                info = extract_song_info(item.get("url") or item.get("suno_url") or "")
                if info:
                    item.update(info)
            except Exception as e:
                log.error("[resolver] failed on %s: %s", item.get("url"), e)
                item["_resolve_failed"] = True
                item["_resolve_failed_reason"] = self._classify_resolve_failure(e)
            item.setdefault("title", "Unknown Title")
            item.setdefault("artist", "Unknown")
            item.setdefault("duration", None)
            item.setdefault("thumbnail", None)
            return item

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [loop.run_in_executor(ex, _resolve_one, it) for it in items]
            gather_future = asyncio.gather(*futures, return_exceptions=True)
            try:
                resolved = list(await asyncio.wait_for(gather_future, timeout=timeout))
            except asyncio.TimeoutError:
                log.error("[resolver] timed out after %ss resolving %s tracks", timeout, len(items))
                gather_future.cancel()
                resolved = []
                for f in futures:
                    if f.done() and not f.cancelled():
                        r = f.result()
                        if isinstance(r, dict):
                            resolved.append(r)
            except asyncio.CancelledError:
                gather_future.cancel()
                for f in futures:
                    f.cancel()
                raise

            try:
                self._record_batch_health(resolved)
            except Exception as e:
                log.debug("[track_health] record failed: %s", e)

            return resolved

    def _record_batch_health(self, items: list[dict]) -> None:
        """Persist per-URL success/failure stats from a resolver batch.

        Circuit breaker: if the batch success rate is below
        RESOLVER_HEALTH_MIN_SUCCESS_RATE we assume Suno (or our resolver) is
        unhealthy and skip recording failures entirely — successes are still
        recorded because they're unambiguous good news. This is what protects
        us from a Suno outage marking every song as broken.
        """
        if not items:
            return

        considered = [it for it in items if (it.get("url") or it.get("suno_url"))]
        if not considered:
            return

        succeeded = [it for it in considered if not it.get("_resolve_failed")]
        success_rate = len(succeeded) / len(considered)

        circuit_ok = success_rate >= RESOLVER_HEALTH_MIN_SUCCESS_RATE
        if not circuit_ok:
            log.warning(
                "[track_health] circuit breaker tripped (success rate %.0f%% < %.0f%%); "
                "recording successes only, ignoring %s failures",
                success_rate * 100,
                RESOLVER_HEALTH_MIN_SUCCESS_RATE * 100,
                sum(1 for it in considered if it.get("_resolve_failed")),
            )

        for it in considered:
            url = str(it.get("url") or it.get("suno_url") or "").strip()
            if not url:
                continue
            if not it.get("_resolve_failed"):
                try:
                    record_track_success(url=url)
                except Exception as e:
                    log.debug("[track_health] success record failed for %s: %s", url, e)
                continue

            if not circuit_ok:
                continue
            reason = it.get("_resolve_failed_reason") or "transient"
            if reason != "gone":
                # Only persist confirmed-gone failures; transient/parse are
                # too noisy to count toward deletion.
                continue
            try:
                record_track_failure(url=url, reason=reason)
            except Exception as e:
                log.debug("[track_health] failure record failed for %s: %s", url, e)

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
            log.warning("Error setting song activity: %s", e)

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
            except Exception as e:
                log.debug("suppressed: %s", e)

    @tasks.loop(seconds=30)
    async def update_song_activity(self):
        gid = self._now_playing_guild_id
        if self.current_song and self.song_start_time and gid:
            guild = self.bot.get_guild(gid)
            if guild and guild.voice_client and guild.voice_client.is_playing():
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
        except Exception as e:
            log.debug("suppressed: %s", e)

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
                    if vc.permissions_for(ctx.guild.me).connect:
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

            try:
                self._set_encoder_bitrate(ctx.voice_client)
            except Exception as e:
                log.warning("[bitrate] couldn't set encoder bitrate: %s", e)

            gid = ctx.guild.id
            self._intentional_disconnect.discard(gid)
            self._last_vc_channel[gid] = channel.id
            self._remember_ctx(ctx)
            if not self.queues[gid] and not (ctx.voice_client and ctx.voice_client.is_playing()):
                self._cancel_autofill_task(gid)
                self._schedule_autofill_if_idle(ctx, delay=AUTOFILL_DELAY_SEC)
        except Exception as e:
            embed = discord.Embed(title="❌ Voice Connection Error", description=f"Failed to join {channel.name}: {str(e)}.", color=0xff0000)
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
        gid = ctx.guild.id
        channel_name = ctx.voice_client.channel.name
        log.info("[leave] Leaving voice channel %r (user requested !leave)", channel_name)
        self._intentional_disconnect.add(gid)
        await ctx.voice_client.disconnect()

        await self._full_playback_teardown(gid)

    @commands.command(name='play', aliases=['Play', 'p'])
    async def play(self, ctx, url: str = ""):
        """
        Plays a song by url (Suno song or playlist URL supported).
        """
        if await self._check_game_active(ctx):
            return

        if not ctx.voice_client:
            await ctx.invoke(self.join)
            if not ctx.voice_client:
                return

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
            remaining_user_slots = ADMIN_UNLIMITED_SLOTS

        try:
            if not url.strip():
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    raw_tracks = await asyncio.get_event_loop().run_in_executor(
                        executor, _scrape_playlist_to_tracks, "", 5
                    )
                if not raw_tracks:
                    embed = discord.Embed(title="❌ Missing URL", description="Please provide a Suno song or playlist URL.\n\nUsage: `!play <url>`", color=0xff0000)
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

                tracks = await self._resolve_tracks(raw_tracks, max_workers=RESOLVER_MAX_WORKERS)
                tracks = [t for t in tracks if not t.get("_resolve_failed")]

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
                song = await asyncio.get_event_loop().run_in_executor(
                    None, extract_song_info, url
                )
                if song is None:
                    raise ValueError("Failed to extract song information: extract_song_info returned None")
                
                # Guard against SoundCloud PRO previews (30 seconds or shorter)
                if "soundcloud.com" in url.lower():
                    duration = song.get("duration")
                    if duration is not None and duration <= 30:
                        raise ValueError(
                            "This appears to be a SoundCloud PRO preview (30 seconds or shorter). "
                            "The full track is only available to SoundCloud Go+ subscribers"
                        )
                
                song.setdefault("artist", song.pop("author", None))

                song["requester_id"] = requester_id
                song["requester_tag"] = requester_tag
                song["requester_name"] = requester_name
                song["requester_mention"] = requester_mention
                song["requested_at"] = requested_at

                if remaining_user_slots <= 0:
                    await ctx.send(embed=self._deny_user_cap_embed(requester_mention, gid=guild_id))
                    return

                insert_idx = next(
                    (i for i, t in enumerate(queue)
                     if t.get("_from_playlist") or t.get("_autofill")),
                    len(queue),
                )
                queue.insert(insert_idx, song)
                position = insert_idx + 1
                save_data(guild_id, self.queues, self.playlists, self.user_mappings)

                eta_sec, eta_unknown = self._estimate_eta_seconds(guild_id, position)
                embed, thumb_file = build_added_embed(
                    song,
                    requester_mention=requester_mention,
                    position=position,
                    eta_seconds=eta_sec,
                    eta_unknown=eta_unknown
                )
                await ctx.send(embed=embed, file=thumb_file) if thumb_file else await ctx.send(embed=embed)

            if not ctx.voice_client.is_playing():
                await self.play_next(ctx)

        except Exception as e:
            embed = discord.Embed(
                title="❌ Error",
                description=f"Failed to add song: {str(e)}.",
                color=0xff0000
            )
            await ctx.send(embed=embed)

    def _prune_non_autofill(self, gid: int) -> bool:
        """Check if non-autofill NP cards should be pruned for this guild."""
        if gid in self.np_clean_non_autofill:
            return self.np_clean_non_autofill[gid]
        
        # Fallback to user_mappings if loaded
        amap = self.user_mappings.get(gid) or {}
        if isinstance(amap, dict) and "np_clean_non_autofill" in amap:
            self.np_clean_non_autofill[gid] = bool(amap["np_clean_non_autofill"])
            return self.np_clean_non_autofill[gid]
        
        return REMOVE_NON_AUTOFILL_NP

    async def _cleanup_now_playing_messages(self, gid: int):
        """
        Delete Now Playing messages that are older than the last N songs.
        Autofill messages are always cleaned up; non-autofill messages
        are only cleaned up if REMOVE_NON_AUTOFILL_NP is True or toggled ON.
        """
        if self._np_retention_n <= 0:
            return
        entries = self._np_track.get(gid) or []
        if not entries:
            return

        current_idx = self._song_index.get(gid, 0)
        keep = []
        
        prune_non_autofill = self._prune_non_autofill(gid)

        for e in entries:
            is_autofill = e.get("is_autofill", False)
            # Delete if the gap is >= retention_n AND (it's autofill OR toggle is ON)
            if (current_idx - e.get("song_index", current_idx)) >= self._np_retention_n:
                if is_autofill or prune_non_autofill:
                    try:
                        ch = self.bot.get_channel(e["channel_id"])
                        if ch:
                            # Fetch the message to check reactions
                            msg = await ch.fetch_message(e["message_id"])
                            reaction_count = sum(reaction.count for reaction in msg.reactions)
                            if reaction_count > NP_REACTION_KEEP_THRESHOLD:
                                keep.append(e)
                                continue
                            await msg.delete()
                    except Exception as e:
                        log.debug("suppressed: %s", e)
                # Once it's outside the retention window, we stop tracking it regardless
                continue
            
            keep.append(e)
        self._np_track[gid] = keep

    async def _clear_all_np_messages_for_guild(self, gid: int) -> None:
        """
        Delete tracked Now Playing messages for autofill only (e.g. when stopping or leaving mid-song).
        Stops the last autofill NP card from staying visible; keeps NP cards for songs added with !play.
        Autofill cards with reactions above NP_REACTION_KEEP_THRESHOLD are kept.
        """
        entries = self._np_track.get(gid) or []
        keep = []
        for e in entries:
            if e.get("is_autofill"):
                try:
                    ch = self.bot.get_channel(e["channel_id"])
                    if ch:
                        msg = await ch.fetch_message(e["message_id"])
                        reaction_count = sum(r.count for r in msg.reactions)
                        if reaction_count <= NP_REACTION_KEEP_THRESHOLD:
                            await msg.delete()
                        else:
                            keep.append(e)
                except Exception as e:
                    log.debug("suppressed: %s", e)
            else:
                keep.append(e)
        self._np_track[gid] = keep

    def _cleanup_local_file(self, local_path: str | None) -> None:
        if not local_path:
            return
        try:
            if os.path.exists(local_path):
                os.remove(local_path)
        except Exception as e:
            log.warning("[cleanup] failed to remove %s: %s", local_path, e)

    async def play_next(self, ctx):
        gid = ctx.guild.id
        lock = self._play_locks[gid]

        async with lock:
            queue = self.queues[gid]
            if not queue:
                # Safety net: something invoked play_next with an empty queue
                if (
                    ctx.voice_client
                    and self._is_autofill_enabled(gid)
                    and not (self.current_song and self._now_playing_guild_id == gid)
                ):
                    try:
                        self._schedule_autofill_if_idle(ctx)
                    except Exception as e:
                        log.debug("suppressed: %s", e)
                return
            if not ctx.voice_client:
                embed = discord.Embed(title="❌ Connection Lost", description="Bot lost voice connection!", color=0xff0000)
                channel = self.get_radio_channel(ctx)
                await channel.send(embed=embed)
                self._clear_now_playing_if_guild(gid)
                if self.update_song_activity.is_running():
                    self.update_song_activity.stop()
                await self.bot.change_presence(activity=None)
                return

            channel = self.get_radio_channel(ctx)
            song = queue.popleft()

            # Check if requester is still in VC before playing
            requester_in_vc = await self._check_requester_in_vc(ctx, song)
            if not requester_in_vc:
                # Requester left, skip this song
                requester_mention = (
                    song.get("requester_mention")
                    or (f"<@{song['requester_id']}>" if song.get("requester_id") else None)
                    or song.get("requester_tag")
                    or song.get("requester_name")
                    or "someone"
                )
                song_title = song.get("title") or "Unknown"
                
                if SHOW_SKIP_MESSAGE:
                    skip_embed = discord.Embed(
                        title="⏭️ Skipped",
                        description=f"Skipped **{_truncate(song_title, 200)}** - {requester_mention} is no longer in the voice channel.",
                        color=0xff9900
                    )
                    try:
                        await channel.send(embed=skip_embed)
                    except Exception as e:
                        log.debug("suppressed: %s", e)
                
                # If there are other songs in queue, continue to next song
                if queue and ctx.voice_client:
                    self.bot.loop.create_task(self.play_next(ctx))
                else:
                    # No other songs follow - trigger queue empty behavior to resume autofill
                    self._clear_now_playing_if_guild(gid)
                    if self.update_song_activity.is_running():
                        self.update_song_activity.stop()
                    await self.bot.change_presence(activity=None)
                    
                    if not self._is_autofill_enabled(ctx.guild.id):
                        embed2 = self._embed_queue_empty_notice(ctx.guild.id)
                        try:
                            await channel.send(embed=embed2)
                        except Exception as e:
                            log.debug("suppressed: %s", e)
                    try:
                        self._schedule_autofill_if_idle(ctx)
                    except Exception as e:
                        log.debug("suppressed: %s", e)
                return

            track_id = _canonical_track_id(song)
            if track_id:
                try:
                    title = song.get("title")
                    artist = song.get("artist") or song.get("author")

                    if self._is_unknown_track(song):
                        song["_track_id"] = None
                        song["_play_id"] = None
                    else:
                        upsert_track_basic(
                            track_id=track_id,
                            title=title,
                            artist=artist,
                            cover_url=song.get("thumbnail") or song.get("thumb") or song.get("image"),
                            source_url=_derive_suno_url(song),
                            duration_sec=_duration_to_seconds(song.get("duration")),
                        )
                        requester_id = str(song.get("requester_id") or getattr(ctx.author, "id", ""))
                        requester_name = song.get("requester_tag")
                        if not requester_name and getattr(ctx, "author", None):
                            requester_name = getattr(ctx.author, "display_name", None) or getattr(ctx.author, "name", None)
                        play_id = log_play_start(
                            track_id=track_id,
                            guild_id=ctx.guild.id,
                            channel_id=ctx.channel.id,
                            requested_by=requester_id,
                            requested_by_username=requester_name,
                            context="autofill" if song.get("_autofill") else "queue",
                        )
                        song["_track_id"] = track_id
                        song["_play_id"] = play_id
                except Exception as e:
                    log.debug("[history] start log failed: %s", e)
                    song.setdefault("_track_id", track_id)
                    song.setdefault("_play_id", None)
            else:
                song["_track_id"] = None
                song["_play_id"] = None

            local_to_delete = None
            url_val = str(song.get("url", "")).strip()

            if not url_val or song.get("_resolve_failed"):
                log.warning("[play_next] Skipping unplayable track: %s", song.get("title", "Unknown"))
                if queue and ctx.voice_client:
                    self.bot.loop.create_task(self.play_next(ctx))
                elif ctx.voice_client and self._is_autofill_enabled(gid):
                    try:
                        self._schedule_autofill_if_idle(ctx)
                    except Exception as e:
                        log.debug("suppressed: %s", e)
                return

            # For yt-dlp sources, re-extract fresh URL at playback time
            # because these platforms use signed URLs that expire quickly
            if song.get("_source") and song.get("_original_url"):
                try:
                    from src.utils.ytdlp_extractor import extract_with_ytdlp
                    fresh_info = extract_with_ytdlp(song["_original_url"], download_thumbnail=False)
                    if fresh_info and fresh_info.get("url"):
                        url_val = fresh_info["url"]
                        song["url"] = url_val
                        if fresh_info.get("_downloaded_file"):
                            local_to_delete = fresh_info["_downloaded_file"]
                        log.info("[play_next] Refreshed URL for %s", song.get("title", "Unknown")[:50])
                    else:
                        log.warning("[play_next] Failed to refresh URL for %s, using cached URL", song.get("_original_url"))
                except Exception as e:
                    log.warning("[play_next] URL refresh failed for %s: %s", song.get("_original_url"), e)

            # ---- FFmpeg options (latency + stability tuned) ------------------
            is_hls = ".m3u8" in url_val or "/hls" in url_val.lower()
            is_ytdlp_source = bool(song.get("_source"))
            ffmpeg_options = self._build_ffmpeg_options(
                is_hls=is_hls, is_ytdlp=is_ytdlp_source
            )

            try:
                source = discord.FFmpegPCMAudio(url_val, **ffmpeg_options)
                volume_transformer = discord.PCMVolumeTransformer(source, volume=self.volumes[gid])
            except Exception as e:
                log.error("Audio source error: %s for %s", e, song.get("url"))
                self._cleanup_local_file(local_to_delete)
                embed = discord.Embed(
                    title="❌ Playback Error",
                    description=f"Failed to play {song.get('title','Unknown')}: {str(e)}",
                    color=0xff0000
                )
                try:
                    await ctx.send(embed=embed)
                except Exception as e:
                    log.debug("suppressed: %s", e)
                if queue and ctx.voice_client:
                    self.bot.loop.create_task(self.play_next(ctx))
                elif ctx.voice_client and self._is_autofill_enabled(gid):
                    try:
                        self._schedule_autofill_if_idle(ctx)
                    except Exception as e:
                        log.debug("suppressed: %s", e)
                return

            def after_playing(error):
                _gid = ctx.guild.id
                try:
                    if error:
                        log.error("[after_playing] Player error (guild %s): %s", _gid, error)

                    try:
                        if song.get("_track_id") and song.get("_play_id"):
                            log_play_end(track_id=song["_track_id"], play_id=song["_play_id"])
                    except Exception as e_end:
                        log.debug("[history] end log failed: %s", e_end)

                    self._cleanup_local_file(local_to_delete)

                    try:
                        self._clear_now_playing_if_guild(_gid)
                    except Exception as e:
                        log.debug("suppressed: %s", e)
                    try:
                        if self.update_song_activity.is_running():
                            self.update_song_activity.stop()
                        asyncio.run_coroutine_threadsafe(self.bot.change_presence(activity=None), self.bot.loop)
                    except Exception as e:
                        log.debug("suppressed: %s", e)

                    games_cog = self.bot.get_cog("Games")
                    if games_cog and games_cog.is_game_active(_gid):
                        return
                    if self._is_contest_active(_gid):
                        return

                    vc = ctx.voice_client
                    vc_valid = vc is not None and getattr(vc, "channel", None) is not None

                    if queue and vc_valid:
                        try:
                            self.bot.loop.call_soon_threadsafe(lambda: self.bot.loop.create_task(self.play_next(ctx)))
                            log.debug("[after_playing] guild %s: scheduled play_next (%s in queue)", _gid, len(queue))
                        except Exception as e_vc:
                            log.error("[after_playing] Failed to schedule next song: %s", e_vc)
                    elif not queue:
                        if vc_valid and not self._is_autofill_enabled(_gid):
                            embed2 = self._embed_queue_empty_notice(_gid)
                            try:
                                asyncio.run_coroutine_threadsafe(
                                    self.get_radio_channel(ctx).send(embed=embed2), self.bot.loop
                                )
                            except Exception as e:
                                log.debug("suppressed: %s", e)

                        try:
                            self.bot.loop.call_soon_threadsafe(lambda: self._schedule_autofill_if_idle(ctx))
                            log.debug("[after_playing] guild %s: queue empty, scheduled autofill check", _gid)
                        except Exception as _e:
                            log.warning("[after_playing] autofill schedule failed: %s", _e)
                    else:
                        if _gid in self._intentional_disconnect:
                            log.debug("[after_playing] guild %s: intentional leave, skipping reconnect", _gid)
                        elif self._reconnecting.get(_gid):
                            log.debug("[after_playing] guild %s: reconnect already in progress, skipping", _gid)
                        else:
                            log.debug("[after_playing] guild %s: vc invalid (%s), attempting reconnect…", _gid, vc)
                            async def _reconnect_and_continue():
                                try:
                                    await asyncio.sleep(2.0)
                                    guild_obj = self.bot.get_guild(_gid)
                                    already_back = bool(
                                        guild_obj
                                        and guild_obj.voice_client
                                        and guild_obj.voice_client.is_connected()
                                    )
                                    if already_back:
                                        log.info("[after_playing] guild %s: already reconnected by discord.py", _gid)
                                    else:
                                        ok = await self._try_voice_reconnect(_gid)
                                        if not ok:
                                            log.error("[after_playing] guild %s: reconnect failed, giving up", _gid)
                                            return
                                    # Always re-arm — whether discord.py auto-reconnected
                                    # or we did, the player needs to know to resume.
                                    if self._reconnect_ctx.get(_gid) is None:
                                        self._remember_ctx(ctx)
                                    self._resume_after_reconnect(_gid)
                                except Exception as _re:
                                    log.error("[after_playing] guild %s: reconnect error: %s", _gid, _re)
                            try:
                                asyncio.run_coroutine_threadsafe(_reconnect_and_continue(), self.bot.loop)
                            except Exception:
                                log.error("[after_playing] guild %s: could not schedule reconnect", _gid)
                except Exception:
                    log.exception("[after_playing] CRASHED (guild %s)", _gid)
                    try:
                        self._clear_now_playing_if_guild(_gid)
                        if queue:
                            self.bot.loop.call_soon_threadsafe(lambda: self.bot.loop.create_task(self.play_next(ctx)))
                        else:
                            self.bot.loop.call_soon_threadsafe(lambda: self._schedule_autofill_if_idle(ctx))
                    except Exception:
                        log.error("[after_playing] recovery also failed")

            target_vol = self.volumes[gid]
            start_muted = (PREBUFFER_SECONDS > 0) or (FADE_IN_SECONDS > 0)
            if start_muted:
                try:
                    volume_transformer.volume = 0.0001
                except Exception as e:
                    log.debug("suppressed: %s", e)

            # Track which channel / ctx so reconnect knows where to go
            try:
                if ctx.voice_client and ctx.voice_client.channel:
                    if RADIO_VC_ID:
                        self._last_vc_channel[gid] = RADIO_VC_ID
                    else:
                        self._last_vc_channel[gid] = ctx.voice_client.channel.id
                    self._remember_ctx(ctx)
            except Exception as e:
                log.debug("suppressed: %s", e)

            try:
                ctx.voice_client.play(volume_transformer, after=after_playing)
            except Exception as play_err:
                log.error("[play_next] play() failed for %s: %s", song.get("title", "?")[:60], play_err)
                self._cleanup_local_file(local_to_delete)
                if "already playing" in str(play_err).lower():
                    queue.appendleft(song)
                    log.debug("[play_next] guild %s: re-queued song, bailing — current track's after_playing will advance", gid)
                    return
                self._clear_now_playing_if_guild(gid)
                if queue and ctx.voice_client:
                    self.bot.loop.create_task(self.play_next(ctx))
                elif ctx.voice_client and self._is_autofill_enabled(gid):
                    try:
                        self._schedule_autofill_if_idle(ctx)
                    except Exception as e:
                        log.debug("suppressed: %s", e)
                return

            if PREBUFFER_SECONDS > 0:
                try:
                    await asyncio.sleep(PREBUFFER_SECONDS)
                except Exception as e:
                    log.debug("suppressed: %s", e)

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
                except Exception as e:
                    log.debug("suppressed: %s", e)

            self._set_now_playing(gid, song)
            self.song_start_time = time.time()
            log.info("[play_next] guild %s: playing %s", gid, song.get("title", "?")[:60])

            self._song_index[gid] += 1
            current_song_index = self._song_index[gid]

            try:
                if self.update_song_activity.is_running():
                    self.update_song_activity.stop()
                await self.set_song_activity(song, 0.0)
                if not self.update_song_activity.is_running():
                    self.update_song_activity.start()
            except Exception as activity_err:
                log.warning("[play_next] guild %s: activity update failed (non-critical): %s", gid, activity_err)

            ch = self.get_radio_channel(ctx)
            sent_message = None
            try:
                requester = (song.get("requester_mention")
                             or song.get("requester_name")
                             or song.get("requester_tag"))
                upcoming_two = list(self.queues[gid])[:2]
                np_embed, thumb_file = build_now_playing_embed(song, requester_mention=requester, upcoming_tracks=upcoming_two)

                song_url = (
                    song.get("_original_url") or
                    song.get("video_url") or
                    _derive_suno_url(song) or
                    (song.get("url") or "")
                )
                song_title = song.get("title") or song.get("track_id") or "Untitled"

                view = NowPlayingView(
                    song=song,
                    track_id=song.get("_track_id"),
                    guild_id=ctx.guild.id,
                    bot_user_id=(self.bot.user.id if self.bot.user else 0),
                    song_title=song_title,
                    song_url=song_url,
                )

                try:
                    sent_message = await ch.send(embed=np_embed, view=view, file=thumb_file) if thumb_file else await ch.send(embed=np_embed, view=view)
                except Exception:
                    if thumb_file:
                        sent_message = await ch.send(embed=np_embed, view=view)
                    else:
                        raise
            except Exception as embed_err:
                log.debug("[play_next] guild %s: NP card failed: %s", gid, embed_err)
                try:
                    fallback = discord.Embed(
                        title="🎵 Now Playing",
                        description=f"**{(song.get('title') or 'Unknown')[:200]}**",
                        color=0xff9900,
                    )
                    fallback.set_footer(text=f"⚠️ Card error: {type(embed_err).__name__}: {embed_err}"[:200])
                    sent_message = await ch.send(embed=fallback)
                except Exception as fallback_err:
                    log.debug("[play_next] guild %s: fallback NP also failed: %s", gid, fallback_err)

            if sent_message:
                try:
                    self._np_track[gid].append({
                        "message_id": sent_message.id,
                        "channel_id": sent_message.channel.id,
                        "song_index": current_song_index,
                        "is_autofill": bool(song.get("_autofill")),
                    })
                except Exception as e:
                    log.debug("suppressed: %s", e)

            try:
                await self._cleanup_now_playing_messages(gid)
            except Exception as e:
                log.debug("suppressed: %s", e)

    @commands.command(name='queue', aliases=['q'])
    async def show_queue(self, ctx):
        """
            Shows the current queue with estimated time to start for each item.
            Supports pagination for large queues.
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
        
        view = PaginatedQueueView(
            queue=queue,
            eta_list=eta_list,
        )
        
        # Delete the command message if it should be auto-deleted
        try:
            if ctx.message and ctx.command and ctx.command.name in AUTO_DELETE_COMMANDS:
                await ctx.message.delete()
        except Exception as e:
            log.debug("suppressed: %s", e)
        
        await view.send(ctx.channel)

    @commands.command(name='skip', aliases=['s'])
    async def skip(self, ctx, target: str = ""):
        """
        Skip the currently playing track.
        Usage:
          !skip             -> skip current track (any)
          !skip autofill    -> if current is filler, stop it; also purge filler from queue
        """
        if await self._check_game_active(ctx):
            return

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
            if (
                self.current_song
                and self._now_playing_guild_id == ctx.guild.id
                and self.current_song.get("_autofill")
                and ctx.voice_client
                and ctx.voice_client.is_playing()
            ):
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

    @commands.command(name='stop', aliases=['shh'])
    async def stop(self, ctx):
        """
        Stops all playback and clears the playlist queue
        """
        if await self._check_game_active(ctx):
            return

        if ctx.voice_client:
            await self._fade_out_and_stop(ctx)

        gid = ctx.guild.id
        self.queues[gid].clear()

        if CLEAR_PLAYLISTS_ON_STOP:
            self.playlists[gid].clear()

        await self._full_playback_teardown(gid)

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
            log.warning("[autofill after stop] %s", _e)

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

    @commands.command(name='song_info', aliases=['si'])
    async def song_info(self, ctx):
        """
        Display detailed information about the currently playing song, including lyrics and prompt.
        """
        if not self.current_song or self._now_playing_guild_id != ctx.guild.id:
            embed = discord.Embed(
                title="❌ No Song Playing",
                description="There's no song currently playing. Use `!play` to start playing music!",
                color=0xff0000
            )
            await ctx.send(embed=embed)
            return

        embed, thumb_file = build_song_info_embed(self.current_song)
        await ctx.send(embed=embed, file=thumb_file) if thumb_file else await ctx.send(embed=embed)

    @commands.command(name='playlist', aliases=['pl'])
    async def playlist(self, ctx, url: str, max_items: int = 200):
        """
        Enqueue tracks from a Suno playlist/profile/handle in bulk
        Usage: !playlist https://suno.com/playlist/##"
        """
        if await self._check_game_active(ctx):
            return

        is_admin = self._is_admin(ctx.author)

        if not is_admin:
            max_items = min(max_items, PLAYLIST_MAX_NON_ADMIN)

        if not ctx.voice_client:
            await ctx.invoke(self.join)

        guild_id = ctx.guild.id
        queue = self.queues[guild_id]

        self._cancel_autofill_task(guild_id)
        self._clear_autofill_from_queue(guild_id)

        status_msg = await ctx.send(embed=discord.Embed(
            title="⏳ Processing Playlist",
            description="Fetching tracks from Suno, please wait...",
            color=0xf1c40f
        ))

        try:
            async with ctx.typing():
                loop = asyncio.get_event_loop()
                raw_tracks = await loop.run_in_executor(
                    None, lambda: _scrape_playlist_to_tracks(url, limit=max_items)
                )
                if not raw_tracks:
                    await status_msg.delete()
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
                    await status_msg.delete()
                    await ctx.send(embed=discord.Embed(
                        title="🚫 Queue Limit",
                        description=notice or "Queue limit reached for bulk adds.",
                        color=0xe74c3c
                    ))
                    return
                if allowed < intended:
                    raw_tracks = raw_tracks[:allowed]

                tracks = await self._resolve_tracks(raw_tracks, max_workers=RESOLVER_MAX_WORKERS)
                tracks = [t for t in tracks if not t.get("_resolve_failed")]

                now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())

                # Build set of existing track IDs in queue to check for duplicates
                existing_track_ids = set()
                for existing_track in queue:
                    track_id = _canonical_track_id(existing_track)
                    if track_id:
                        existing_track_ids.add(track_id)

                start_pos = len(queue) + 1
                added_count = 0
                duplicate_count = 0
                
                for t in tracks:
                    # Check for duplicates
                    track_id = _canonical_track_id(t)
                    if track_id and track_id in existing_track_ids:
                        duplicate_count += 1
                        continue
                    
                    # Add track to queue
                    t["requester_id"] = ctx.author.id
                    t["requester_tag"] = str(ctx.author)
                    t["requester_name"] = ctx.author.display_name
                    t["requester_mention"] = ctx.author.mention
                    t["requested_at"] = now_ts
                    t["_from_playlist"] = True  # optional but nice if you want later filtering
                    queue.append(t)
                    added_count += 1
                    
                    # Add to existing set to prevent duplicates within the same batch
                    if track_id:
                        existing_track_ids.add(track_id)

                end_pos = len(queue)
                save_data(guild_id, self.queues, self.playlists, self.user_mappings)

                await status_msg.delete()

                desc = f"Added {added_count} tracks!"
                if duplicate_count > 0:
                    desc += f" ({duplicate_count} duplicate{'s' if duplicate_count != 1 else ''} skipped)"
                if end_pos >= start_pos:
                    desc += f" (positions #{start_pos}–#{end_pos})"
                if notice:
                    desc += f"\n\n{notice}"

                embed = discord.Embed(title="➕ Added Playlist", description=desc, color=0x0099ff)
                await ctx.send(embed=embed)

                if not ctx.voice_client.is_playing():
                    await self.play_next(ctx)

        except Exception as e:
            try:
                await status_msg.delete()
            except (discord.NotFound, discord.HTTPException):
                pass
            embed = discord.Embed(
                title="❌ Error",
                description=f"Failed to add playlist: {e}",
                color=0xff0000
            )
            await ctx.send(embed=embed)

    @commands.command(name='remove', aliases=['rm'])
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
                except Exception as e:
                    log.debug("suppressed: %s", e)
            gid = ctx.guild.id
            self._clear_now_playing_if_guild(gid)
            if self.update_song_activity.is_running():
                self.update_song_activity.stop()
            await self.bot.change_presence(activity=None)

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
    @commands.command(name="autofill")
    async def autofill(self, ctx, action: str = "status", *, arg: str = ""):
        """
        Unified autofill management. Subcommands:
          !autofill on          – enable autofill
          !autofill off         – disable autofill
          !autofill set <url>   – set playlist/profile URL
          !autofill unset       – clear the URL override
          !autofill reload      – reload the CSV seed list
          !autofill status      – show current settings
        """
        action = action.strip().lower()

        if action == "on":
            await self._autofill_on(ctx)
        elif action == "off":
            await self._autofill_off(ctx)
        elif action == "set":
            await self._autofill_set(ctx, arg)
        elif action == "unset":
            await self._autofill_unset(ctx)
        elif action == "reload":
            await self._autofill_reload(ctx)
        else:
            await self._autofill_status(ctx)

    async def _autofill_set(self, ctx, url: str):
        if not self._autofill_feature_on:
            await ctx.send(embed=discord.Embed(title="Feature Disabled", description="Autofill is disabled.", color=0xe74c3c))
            return
        gid = ctx.guild.id
        the_url = url.strip()
        if not the_url:
            await ctx.send(embed=discord.Embed(title="❌ Missing URL", description="Usage: `!autofill set <url>`", color=0xe74c3c))
            return
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

    async def _autofill_on(self, ctx):
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

    async def _autofill_off(self, ctx):
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

    async def _autofill_status(self, ctx):
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

    # Title substrings for bot messages we remove in radio_cleanup (queue empty, join/left, connection errors, prior cleanup replies)
    _RADIO_CLEANUP_TITLES = (
        "Queue Empty",
        "Joined",
        "👋 Left",
        "Voice Connection Error",
        "Connection Lost",
        "Playback Error",
        "Radio Cleanup",
        "Queue Empty Cleanup",
    )

    @commands.has_permissions(administrator=True)
    @commands.command(name="radio_cleanup", aliases=["queue_empty_cleanup"])
    async def radio_cleanup(self, ctx, limit: int = 100):
        """
        Delete noisy bot messages from the radio channel (Admin only).
        Removes: Queue Empty, Joined, Left, Voice Connection Error, Connection Lost, Playback Error.
        Optional: !radio_cleanup [limit] (default 100, scans recent messages).
        Note: Discord only allows bulk delete for messages under 14 days old.
        """
        channel = self.get_radio_channel(ctx)
        if not channel or not hasattr(channel, "purge"):
            await ctx.send(embed=discord.Embed(title="Error", description="Could not get the radio channel.", color=0xe74c3c))
            return
        limit = max(PURGE_LIMIT_MIN, min(PURGE_LIMIT_MAX, limit))

        def is_cleanup_msg(msg):
            if msg.author != self.bot.user or not msg.embeds:
                return False
            for e in msg.embeds:
                title = getattr(e, "title", None) or ""
                if any(phrase in title for phrase in self._RADIO_CLEANUP_TITLES):
                    return True
            return False

        try:
            deleted = await channel.purge(limit=limit, check=is_cleanup_msg)
        except discord.Forbidden:
            await ctx.send(embed=discord.Embed(
                title="Missing Permission",
                description="I need **Manage Messages** in the radio channel to delete those messages.",
                color=0xe74c3c
            ))
            return
        except Exception as e:
            log.error("[radio_cleanup] %s", e)
            await ctx.send(embed=discord.Embed(title="Error", description=str(e), color=0xe74c3c))
            return

        await ctx.send(embed=discord.Embed(
            title="🧹 Radio Cleanup",
            description=f"Removed **{len(deleted)}** message(s) (queue empty, join/left, connection errors) from the radio channel.",
            color=0x2ecc71
        ))

    async def _autofill_reload(self, ctx):
        """
        Reload the Autofill CSV (Admin only) and report the total number of
        usable song URLs found. Resolves the *active* CSV path first.
        """
        gid = ctx.guild.id

        path = self._resolve_autofill_csv_path(gid)

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

        self.autofill_seed_rows[gid] = rows[:]

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

    async def _autofill_unset(self, ctx):
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
            self.user_mappings[gid] = amap
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

    @commands.hybrid_command(
        name="request",
        aliases=["req"],
        description=f"Add a Suno song to the autofill list (max {REQUEST_MAX_PER_USER} per user, unlimited for admins)"
    )
    @app_commands.describe(url="A Suno song URL (e.g. https://suno.com/song/...)")
    async def request(self, ctx: commands.Context, *, url: str = "") -> None:
        """
        Add a Suno song URL to this server's autofill.csv.

        - Limit: REQUEST_MAX_PER_USER per user. Adding past the limit drops
          the oldest of *that user's* entries. Server admins (administrator
          or manage_guild perms) bypass the cap entirely.
        - Duplicates (by Suno song id) don't count toward the limit; the user
          is shown a "already in autofill" message instead.
        - Slash invocation -> ephemeral list of the user's current slots.
        - Prefix invocation -> compact public reply (optional channel gate via
          REQUEST_CHANNEL env var).
        """
        is_slash = ctx.interaction is not None

        # Guild check
        if ctx.guild is None:
            embed = discord.Embed(
                title="❌ Server Only",
                description="`/request` can only be used in a server.",
                color=0xe74c3c,
            )
            if is_slash:
                await ctx.interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await ctx.send(embed=embed)
            return

        # Channel gate (prefix only)
        if (
            not is_slash
            and REQUEST_CHANNEL_ID
            and str(ctx.channel.id) != REQUEST_CHANNEL_ID
        ):
            try:
                hint = discord.Embed(
                    title="Wrong Channel",
                    description=f"Please use `!request` in <#{REQUEST_CHANNEL_ID}>.",
                    color=0xe67e22,
                )
                await ctx.send(embed=hint, delete_after=10)
            except Exception as e:
                log.debug("[request] failed to send channel hint: %s", e)
            return

        gid = ctx.guild.id
        requester_name = ctx.author.display_name
        is_admin = self._is_admin(ctx.author)
        cap = ADMIN_UNLIMITED_SLOTS if is_admin else REQUEST_MAX_PER_USER
        cap_display = "∞" if is_admin else str(REQUEST_MAX_PER_USER)

        if not (url or "").strip():
            embed = discord.Embed(
                title="❌ Missing URL",
                description="Usage: `!request <suno song url>`",
                color=0xe74c3c,
            )
            if is_slash:
                await ctx.interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await ctx.send(embed=embed)
            return

        # Suno URL validation (may HEAD-request for /s/ short links - run in executor)
        loop = asyncio.get_event_loop()
        canonical_url, song_id = await loop.run_in_executor(
            None, self._extract_suno_song_id, url
        )
        if not canonical_url or not song_id:
            embed = discord.Embed(
                title="❌ Suno Only",
                description=(
                    "Only Suno song URLs are supported.\n"
                    "Example: `https://suno.com/song/<id>` or `https://suno.com/s/<short>`."
                ),
                color=0xe74c3c,
            )
            if is_slash:
                await ctx.interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await ctx.send(embed=embed)
            return

        # Defer the slash response while we touch disk - keeps Discord happy
        # if anything (lock, IO) takes a beat.
        if is_slash and not ctx.interaction.response.is_done():
            try:
                await ctx.interaction.response.defer(ephemeral=True, thinking=True)
            except Exception as e:
                log.debug("[request] defer failed: %s", e)

        path = self._resolve_autofill_csv_path(gid)
        dropped_oldest_url: str | None = None
        already_owner: str | None = None
        user_rows_after: list[dict] = []
        added = False

        async with self._get_autofill_csv_lock(gid):
            # Read fresh from disk so we never act on a stale in-memory copy.
            rows = self._load_autofill_csv(path)

            # Duplicate check by canonical Suno song id
            for r in rows:
                existing_id = None
                m = self._SUNO_SONG_RE.search(r.get("url") or "")
                if m:
                    existing_id = m.group(1).lower()
                if existing_id == song_id:
                    already_owner = (r.get("requested_by") or "").strip() or "someone"
                    break

            if already_owner is None:
                # Cap enforcement: drop oldest of THIS user if at limit.
                user_indices = [
                    i for i, r in enumerate(rows)
                    if self._row_matches_member(r, ctx.author)
                ]
                if len(user_indices) >= cap:
                    drop_idx = user_indices[0]
                    dropped_oldest_url = (rows[drop_idx].get("url") or "").strip()
                    rows.pop(drop_idx)

                rows.append({
                    "url": canonical_url,
                    "requested_by": requester_name,
                    "requested_by_id": str(ctx.author.id),
                })
                added = True

                try:
                    self._atomic_write_autofill_csv(path, rows)
                except Exception as e:
                    log.exception("[request] failed to write autofill CSV: %s", e)
                    embed = discord.Embed(
                        title="❌ Save Failed",
                        description=f"Could not write to autofill CSV: `{type(e).__name__}`.",
                        color=0xe74c3c,
                    )
                    if is_slash:
                        await ctx.interaction.followup.send(embed=embed, ephemeral=True)
                    else:
                        await ctx.send(embed=embed)
                    return

                # Refresh in-memory cache so next autofill cycle sees it immediately.
                self.autofill_seed_rows[gid] = rows[:]

            # Collect THIS user's slots for the response (always re-read final rows)
            user_rows_after = [
                r for r in rows
                if self._row_matches_member(r, ctx.author)
            ]

        count = len(user_rows_after)

        # Admins have unlimited slots — skip the noisy count in responses.
        slots_line = "" if is_admin else f"Your slots: **{count}/{cap_display}**"

        # ---- Respond ---------------------------------------------------------
        if already_owner is not None:
            already_desc = (
                f"That Suno song is already in the autofill list "
                f"(added by **{escape_markdown(already_owner)}**)."
            )
            if slots_line:
                already_desc += f"\n\n{slots_line}"
            embed = discord.Embed(
                title="ℹ️ Already in Autofill",
                description=already_desc,
                color=0x3498db,
            )
            if is_slash:
                await ctx.interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await ctx.send(embed=embed)
            return

        if is_slash:
            # Ephemeral list of the user's current slots, oldest -> newest,
            # with the just-added row marked.
            lines = []
            for idx, r in enumerate(user_rows_after, start=1):
                u = (r.get("url") or "").strip()
                marker = " ⬅️ **just added**" if u == canonical_url and idx == len(user_rows_after) else ""
                lines.append(f"`{idx}.` {u}{marker}")
            desc_parts = ["\n".join(lines) if lines else "_(none yet)_"]
            if dropped_oldest_url:
                desc_parts.append(
                    f"\n_Removed your oldest entry to stay at the {cap_display}-song limit:_\n`{dropped_oldest_url}`"
                )
            title = "✅ Added to Autofill" if is_admin else f"✅ Added to Autofill ({count}/{cap_display})"
            embed = discord.Embed(
                title=title,
                description="\n".join(desc_parts),
                color=0x2ecc71,
            )
            await ctx.interaction.followup.send(embed=embed, ephemeral=True)
        else:
            desc_lines = [f"Added [`song/{song_id[:8]}…`]({canonical_url})"]
            if slots_line:
                desc_lines.append(slots_line)
            if dropped_oldest_url:
                desc_lines.append(f"_Dropped your oldest to stay under the cap: `{dropped_oldest_url}`_")
            embed = discord.Embed(
                title="✅ Request Added",
                description="\n".join(desc_lines),
                color=0x2ecc71,
            )
            embed.set_footer(text=f"Requested by {requester_name}")
            await ctx.send(embed=embed)

        log.info(
            "[request] guild=%s user=%s admin=%s added song_id=%s (slots=%s/%s, dropped_oldest=%s)",
            gid, requester_name, is_admin, song_id, count, cap_display, bool(dropped_oldest_url),
        )

    # ---------- helpers for !myrequests / !unrequest ----------

    @staticmethod
    def _user_rows_for(rows: list[dict], member: "discord.Member | discord.User") -> list[tuple[int, dict]]:
        """Return [(index, row), ...] for rows belonging to `member` (see
        `_row_matches_member`). `index` is the position inside *the user's*
        list (1-based), not the CSV index.
        """
        if member is None:
            return []
        out = []
        n = 0
        for r in rows:
            if RadioBot._row_matches_member(r, member):
                n += 1
                out.append((n, r))
        return out

    async def _resolve_unrequest_target_member(
        self, ctx: commands.Context, hint: str
    ) -> discord.Member | None:
        """Parse `@mention` / `display name` / `user id` -> Member.
        Returns None if nothing matched."""
        hint = (hint or "").strip()
        if not hint or ctx.guild is None:
            return None
        try:
            return await commands.MemberConverter().convert(ctx, hint)
        except Exception:
            target = hint.lower()
            for m in ctx.guild.members:
                if (
                    m.display_name.lower() == target
                    or m.name.lower() == target
                ):
                    return m
        return None

    async def _send_private_reply(
        self, ctx: commands.Context, embed: discord.Embed, *, is_slash: bool
    ) -> None:
        """Send `embed` so only the invoking user can see it.

        Slash invocations already get Discord's built-in ephemeral flag.
        Prefix invocations have no such mechanism, so we DM the invoker
        instead and delete their trigger message to keep the channel clean.
        If DMs are closed, we fall back to a channel reply (auto-deleted)
        with a heads-up so the list doesn't linger publicly forever.
        """
        if is_slash:
            if ctx.interaction.response.is_done():
                await ctx.interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await ctx.interaction.response.send_message(embed=embed, ephemeral=True)
            return

        try:
            await ctx.author.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.debug("[myrequests] DM failed, falling back to channel: %s", e)
            fallback = discord.Embed(
                title="📪 Couldn't DM You",
                description=(
                    "Your DMs are closed, so I can't send this privately. "
                    "Here it is in-channel instead (it'll auto-delete shortly) — "
                    "enable DMs or use `/myrequests` for a private reply next time."
                ),
                color=0xe67e22,
            )
            await ctx.send(embed=fallback, delete_after=20)
            await ctx.send(embed=embed, delete_after=20)
        else:
            try:
                await ctx.message.delete()
            except Exception as e:
                log.debug("[myrequests] could not delete trigger message: %s", e)
            ack = discord.Embed(description="📬 Sent to your DMs.", color=0x3498db)
            await ctx.send(embed=ack, delete_after=6)

    @commands.hybrid_command(
        name="myrequests",
        aliases=["myreq", "mylist"],
        description="List your current autofill songs (admins can target another user with @user)"
    )
    @app_commands.describe(user="(Admins only) show another user's autofill entries")
    async def myrequests(self, ctx: commands.Context, *, user: discord.Member | None = None) -> None:
        """Show the caller's current autofill.csv entries with indices."""
        is_slash = ctx.interaction is not None

        if ctx.guild is None:
            embed = discord.Embed(
                title="❌ Server Only",
                description="`/myrequests` can only be used in a server.",
                color=0xe74c3c,
            )
            await self._send_private_reply(ctx, embed, is_slash=is_slash)
            return

        is_admin = self._is_admin(ctx.author)
        if user is not None and user.id != ctx.author.id and not is_admin:
            embed = discord.Embed(
                title="🚫 Admin Only",
                description="Only admins can view another user's autofill entries.",
                color=0xe74c3c,
            )
            await self._send_private_reply(ctx, embed, is_slash=is_slash)
            return

        target_member = user or ctx.author
        target_name = target_member.display_name
        gid = ctx.guild.id
        path = self._resolve_autofill_csv_path(gid)

        async with self._get_autofill_csv_lock(gid):
            rows = self._load_autofill_csv(path)
            user_rows = self._user_rows_for(rows, target_member)

        cap_display = "∞" if self._is_admin(target_member) else str(REQUEST_MAX_PER_USER)
        title_suffix = f" ({len(user_rows)}/{cap_display})"
        title = f"🎵 Autofill — {target_name}{title_suffix}"

        if not user_rows:
            embed = discord.Embed(
                title=title,
                description=(
                    f"_(no autofill entries yet)_\n\n"
                    f"Add some with `!request <suno url>`."
                ),
                color=0x95a5a6,
            )
            await self._send_private_reply(ctx, embed, is_slash=is_slash)
            return

        # Build the listing. Keep each line compact so we don't blow the 4096
        # char description limit; the per-user cap is small (10) so we should
        # never hit it, but defend against admin lists growing big.
        lines = []
        for idx, r in user_rows:
            u = (r.get("url") or "").strip()
            m = self._SUNO_SONG_RE.search(u)
            label = f"song/{m.group(1)[:8]}…" if m else u
            lines.append(f"`{idx:>2}.` [{label}]({u})")

        # Soft cap to avoid embed overflow (~4096 char description limit).
        MAX_LINES = 40
        truncated = False
        if len(lines) > MAX_LINES:
            lines = lines[:MAX_LINES]
            truncated = True

        desc = "\n".join(lines)
        if truncated:
            desc += f"\n\n_…showing first {MAX_LINES} of {len(user_rows)}._"
        desc += (
            "\n\n_Remove one with_ `!unrequest <#>` _or_ `!unrequest <url>`."
            "\n_Wipe all of yours with_ `!unrequest all`."
        )

        embed = discord.Embed(title=title, description=desc, color=0x3498db)
        await self._send_private_reply(ctx, embed, is_slash=is_slash)

    @commands.hybrid_command(
        name="unrequest",
        aliases=["unreq"],
        description="Remove one of your autofill songs (by # from /myrequests, by URL, or 'all')"
    )
    @app_commands.describe(
        target="Index from /myrequests, a Suno song URL, or 'all' to remove every one of yours"
    )
    async def unrequest(self, ctx: commands.Context, *, target: str = "") -> None:
        """Remove a song from the autofill list.

        Forms:
          !unrequest <#>     – remove the Nth entry from your /myrequests list
          !unrequest <url>   – remove that specific Suno URL (must be yours unless admin)
          !unrequest all     – remove every one of your autofill entries (button-confirmed)
          !unrequest         – (no args) show your list with indices

        Admins can remove any URL regardless of owner.
        """
        is_slash = ctx.interaction is not None

        if ctx.guild is None:
            embed = discord.Embed(
                title="❌ Server Only",
                description="`/unrequest` can only be used in a server.",
                color=0xe74c3c,
            )
            if is_slash:
                await ctx.interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await ctx.send(embed=embed)
            return

        # Optional channel gate (prefix only) — same as /request.
        if (
            not is_slash
            and REQUEST_CHANNEL_ID
            and str(ctx.channel.id) != REQUEST_CHANNEL_ID
        ):
            try:
                hint = discord.Embed(
                    title="Wrong Channel",
                    description=f"Please use `!unrequest` in <#{REQUEST_CHANNEL_ID}>.",
                    color=0xe67e22,
                )
                await ctx.send(embed=hint, delete_after=10)
            except Exception as e:
                log.debug("[unrequest] failed to send channel hint: %s", e)
            return

        gid = ctx.guild.id
        requester_name = ctx.author.display_name
        is_admin = self._is_admin(ctx.author)
        target = (target or "").strip()

        # No arg -> show their list
        if not target:
            await ctx.invoke(self.myrequests)
            return

        # Defer slash response while we touch disk.
        if is_slash and not ctx.interaction.response.is_done():
            try:
                await ctx.interaction.response.defer(ephemeral=True, thinking=True)
            except Exception as e:
                log.debug("[unrequest] defer failed: %s", e)

        async def _reply(embed: discord.Embed, *, view: ui.View | None = None) -> discord.Message | None:
            if is_slash:
                msg = await ctx.interaction.followup.send(embed=embed, view=view or discord.utils.MISSING, ephemeral=True)
                return msg
            return await ctx.send(embed=embed, view=view or discord.utils.MISSING)

        path = self._resolve_autofill_csv_path(gid)

        # ----- "all" branch ---------------------------------------------------
        if target.lower() == "all":
            async with self._get_autofill_csv_lock(gid):
                rows = self._load_autofill_csv(path)
                user_rows = self._user_rows_for(rows, ctx.author)
                urls = [(r.get("url") or "").strip() for _, r in user_rows if (r.get("url") or "").strip()]

            if not urls:
                embed = discord.Embed(
                    title="ℹ️ Nothing to Remove",
                    description="You don't have any autofill entries to remove.",
                    color=0x3498db,
                )
                await _reply(embed)
                return

            preview_lines = []
            for idx, r in user_rows[:5]:
                u = (r.get("url") or "").strip()
                m = self._SUNO_SONG_RE.search(u)
                label = f"song/{m.group(1)[:8]}…" if m else u
                preview_lines.append(f"`{idx:>2}.` [{label}]({u})")
            if len(user_rows) > 5:
                preview_lines.append(f"_…and {len(user_rows) - 5} more._")

            embed = discord.Embed(
                title="⚠️ Confirm: Remove ALL of Your Autofill Entries",
                description=(
                    f"This will remove **{len(urls)}** autofill row(s) "
                    f"belonging to **{escape_markdown(requester_name)}**.\n\n"
                    + "\n".join(preview_lines)
                    + "\n\nClick **Remove All** to confirm or **Cancel** to abort."
                ),
                color=0xe67e22,
            )
            view = ConfirmUnrequestAllView(
                cog=self,
                gid=gid,
                invoker_id=ctx.author.id,
                target_name=requester_name,
                urls=urls,
            )
            msg = await _reply(embed, view=view)
            view.message = msg
            return

        # ----- Index branch ---------------------------------------------------
        if target.isdigit():
            idx = int(target)
            async with self._get_autofill_csv_lock(gid):
                rows = self._load_autofill_csv(path)
                user_rows = self._user_rows_for(rows, ctx.author)

                if not user_rows:
                    embed = discord.Embed(
                        title="ℹ️ Nothing to Remove",
                        description="You don't have any autofill entries.",
                        color=0x3498db,
                    )
                    await _reply(embed)
                    return

                if idx < 1 or idx > len(user_rows):
                    embed = discord.Embed(
                        title="❌ Out of Range",
                        description=(
                            f"You only have **{len(user_rows)}** autofill entries. "
                            f"Use `!myrequests` to see the numbered list."
                        ),
                        color=0xe74c3c,
                    )
                    await _reply(embed)
                    return

                target_url = (user_rows[idx - 1][1].get("url") or "").strip()
                kept = [r for r in rows if (r.get("url") or "").strip().lower() != target_url.lower()]
                removed_count = len(rows) - len(kept)
                if removed_count > 0:
                    self._atomic_write_autofill_csv(path, kept)
                    self.autofill_seed_rows[gid] = kept[:]
                remaining = self._user_rows_for(kept, ctx.author)

            cap_display = "∞" if is_admin else str(REQUEST_MAX_PER_USER)
            embed = discord.Embed(
                title="🗑️ Removed from Autofill",
                description=(
                    f"Removed entry `#{idx}`: [{target_url}]({target_url})\n\n"
                    f"You now have **{len(remaining)}/{cap_display}** autofill entries."
                ),
                color=0x2ecc71,
            )
            embed.set_footer(text=f"By {requester_name}")
            await _reply(embed)
            log.info(
                "[unrequest] guild=%s user=%s admin=%s removed=1 mode=index target=%s",
                gid, requester_name, is_admin, target_url,
            )
            return

        # ----- URL branch -----------------------------------------------------
        loop = asyncio.get_event_loop()
        canonical_url, song_id = await loop.run_in_executor(
            None, self._extract_suno_song_id, target
        )
        if not canonical_url or not song_id:
            embed = discord.Embed(
                title="❌ Suno Only",
                description=(
                    "That doesn't look like a Suno song URL.\n"
                    "Use `!myrequests` to see your numbered list, then "
                    "`!unrequest <#>`."
                ),
                color=0xe74c3c,
            )
            await _reply(embed)
            return

        async with self._get_autofill_csv_lock(gid):
            rows = self._load_autofill_csv(path)
            match_idx = -1
            owner = None
            owner_row = None
            for i, r in enumerate(rows):
                m = self._SUNO_SONG_RE.search(r.get("url") or "")
                if m and m.group(1).lower() == song_id:
                    match_idx = i
                    owner_row = r
                    owner = (r.get("requested_by") or "").strip() or "someone"
                    break

            if match_idx < 0:
                embed = discord.Embed(
                    title="ℹ️ Not in Autofill",
                    description="That Suno song isn't currently in the autofill list.",
                    color=0x3498db,
                )
                await _reply(embed)
                return

            if not is_admin and not self._row_matches_member(owner_row, ctx.author):
                embed = discord.Embed(
                    title="🚫 Not Yours",
                    description=(
                        f"That entry was added by **{escape_markdown(owner or '?')}**. "
                        f"Only the original requester (or an admin) can remove it."
                    ),
                    color=0xe74c3c,
                )
                await _reply(embed)
                return

            rows.pop(match_idx)
            self._atomic_write_autofill_csv(path, rows)
            self.autofill_seed_rows[gid] = rows[:]
            remaining = self._user_rows_for(rows, ctx.author)

        cap_display = "∞" if is_admin else str(REQUEST_MAX_PER_USER)
        embed = discord.Embed(
            title="🗑️ Removed from Autofill",
            description=(
                f"Removed [`song/{song_id[:8]}…`]({canonical_url})"
                + (f" (was added by **{escape_markdown(owner or '?')}**)" if is_admin and not self._row_matches_member(owner_row, ctx.author) else "")
                + f"\n\nYou now have **{len(remaining)}/{cap_display}** autofill entries."
            ),
            color=0x2ecc71,
        )
        embed.set_footer(text=f"By {requester_name}")
        await _reply(embed)
        log.info(
            "[unrequest] guild=%s user=%s admin=%s removed=1 mode=url song_id=%s owner=%s",
            gid, requester_name, is_admin, song_id, owner,
        )

    @commands.command(name="twss", hidden=True)
    async def twss(self, ctx):
        """
        Post a random GIF URL from twss.csv located in the same directory as the autofill CSV.
        If the command is used as a reply, the bot will reply to the same message.
        """
        # Get the reference from the command message (if it was a reply)
        reference = ctx.message.reference
        
        # Get the autofill CSV path to determine the directory
        gid = ctx.guild.id
        autofill_csv = self._resolve_autofill_csv_path(gid)
        twss_path = os.path.join(os.path.dirname(autofill_csv), "twss.csv")
        
        # Load URLs from twss.csv
        urls = []
        try:
            if not os.path.exists(twss_path):
                await ctx.send(embed=discord.Embed(
                    title="❌ TWSS CSV Not Found",
                    description=f"Could not find `twss.csv` at `{twss_path}`.",
                    color=0xe74c3c
                ), reference=reference)
                return
            
            with open(twss_path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.reader(f)
                for row in reader:
                    if not row:
                        continue
                    # Get first column as URL
                    url = (row[0] or "").strip()
                    # Skip empty rows and header rows
                    if url and not url.lower().startswith("url") and not url.startswith("#"):
                        urls.append(url)
        except Exception as e:
            await ctx.send(embed=discord.Embed(
                title="❌ Error Reading TWSS CSV",
                description=f"Failed to read `twss.csv`: {type(e).__name__}: {e}",
                color=0xe74c3c
            ), reference=reference)
            return
        
        if not urls:
            await ctx.send(embed=discord.Embed(
                title="❌ No URLs Found",
                description=f"No valid URLs found in `twss.csv` at `{twss_path}`.",
                color=0xe74c3c
            ), reference=reference)
            return
        
        # Pick a random URL and send it
        random_url = random.choice(urls)
        await ctx.send(random_url, reference=reference)

    @commands.command(name="scioned", hidden=True)
    async def scion(self, ctx):
        """
        Take the replied-to message (or the most recent message in the channel)
        and echo its text with a random number (2-20) of "(remix)(cover)" appended.
        """
        reference = ctx.message.reference

        # Figure out which message to remix.
        target_message = None
        if reference is not None:
            # Used as a reply -> remix the referenced message.
            resolved = getattr(reference, "resolved", None)
            if isinstance(resolved, discord.Message):
                target_message = resolved
            elif reference.message_id:
                try:
                    target_message = await ctx.channel.fetch_message(reference.message_id)
                except Exception:
                    target_message = None
        else:
            # No reply -> grab the most recent message in the channel (skip this command).
            try:
                async for msg in ctx.channel.history(limit=10):
                    if msg.id == ctx.message.id:
                        continue
                    target_message = msg
                    break
            except Exception:
                target_message = None

        if target_message is None:
            await ctx.send(embed=discord.Embed(
                title="❌ Nothing to Remix",
                description="Reply to a message or make sure there's a recent message in the channel.",
                color=0xe74c3c
            ), reference=reference)
            return

        base_text = (target_message.content or "").strip()
        if not base_text:
            await ctx.send(embed=discord.Embed(
                title="❌ Nothing to Remix",
                description="That message has no text content to remix.",
                color=0xe74c3c
            ), reference=reference)
            return

        # Append a random 2-50 "(remix)"/"(cover)" tags in random order.
        count = random.randint(2, 50)
        tags = ["(remix)"] * count + ["(cover)"] * count
        random.shuffle(tags)
        remixed = f"{base_text} {''.join(tags)}"[:2000]

        # Plain message.
        await ctx.send(remixed, reference=reference)

    @commands.has_permissions(administrator=True)
    @commands.command(name="queue_limit")
    async def queue_limit(self, ctx, action: str = "status", *, args: str = ""):
        """
        Unified queue limit management. Subcommands:
          !queue_limit on                – enable queue limit
          !queue_limit off [per_user]    – disable (optionally set per-user cap)
          !queue_limit set <max> [per_user] – set max per add
          !queue_limit status            – show settings
        """
        action = action.strip().lower()
        parts = args.split() if args else []
        gid = ctx.guild.id

        if action == "on":
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

        elif action == "off":
            self.queue_limit_enabled[gid] = False
            if parts:
                self.queue_per_user_max[gid] = max(1, int(parts[0]))
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

        elif action == "set":
            if not parts:
                await ctx.send(embed=discord.Embed(title="❌ Missing args", description="Usage: `!queue_limit set <max> [per_user]`", color=0xe74c3c))
                return
            max_per_add = max(1, int(parts[0]))
            self.queue_limit_max[gid] = max_per_add
            if len(parts) > 1:
                self.queue_per_user_max[gid] = max(1, int(parts[1]))
            amap = self.user_mappings[gid]
            if not isinstance(amap, dict):
                amap = {}
                self.user_mappings[gid] = amap
            amap["queue_limit"] = {
                "enabled": self._limit_is_on(gid),
                "max": max_per_add,
                "per_user_max": self._per_user_max(gid),
            }
            save_data(gid, self.queues, self.playlists, self.user_mappings)
            await ctx.send(embed=discord.Embed(
                title="📦 Queue Limit",
                description=f"Max songs per add set to **{max_per_add}**.\nPer-user cap: **{self._per_user_max(gid)}**",
                color=0x3498db
            ))

        else:
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
    @commands.command(name="np_clean")
    async def np_clean(self, ctx, action: str = ""):
        """
        Toggle Now Playing card cleanup. Subcommands:
          !np_clean on   – clean up ALL NP cards (manual + autofill)
          !np_clean off  – only clean autofill NP cards
        """
        action = action.strip().lower()
        gid = ctx.guild.id

        if action == "on":
            self.np_clean_non_autofill[gid] = True
        elif action == "off":
            self.np_clean_non_autofill[gid] = False
        else:
            await ctx.send(embed=discord.Embed(title="❌ Usage", description="`!np_clean on` or `!np_clean off`", color=0xe74c3c))
            return

        amap = self.user_mappings[gid]
        if not isinstance(amap, dict):
            amap = {}
            self.user_mappings[gid] = amap
        amap["np_clean_non_autofill"] = self.np_clean_non_autofill[gid]

        save_data(gid, self.queues, self.playlists, self.user_mappings)

        if action == "on":
            await ctx.send(embed=discord.Embed(
                title="🧹 Now Playing Cleanup",
                description="Cleanup is now **ON** for all tracks (manual and autofill).",
                color=0x2ecc71
            ))
        else:
            await ctx.send(embed=discord.Embed(
                title="🧹 Now Playing Cleanup",
                description="Cleanup is now **OFF** for manual tracks. Autofill tracks will still be cleaned up.",
                color=0xe67e22
            ))

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

    def _create_autofill_saves_view(self, user: discord.User, is_dm: bool = False) -> LikedSongsManagerView:
        """Helper method to create the autofill saves view."""
        user_id = user.id
        
        # Get all liked tracks for this user
        tracks = get_user_liked_tracks_all_guilds(user_id)
        
        # Filter out deleted tracks (those with Unknown Title/Unknown) but keep them in DB
        def is_deleted_track(track: dict) -> bool:
            """Check if a track appears to be deleted (has Unknown Title/Unknown).
            
            Tracks are considered deleted if both title and artist are missing/unknown.
            This filters them from display while keeping them in the database.
            """
            return self._is_unknown_track(track)
        
        visible_tracks = [t for t in tracks if not is_deleted_track(t)]
        
        # Delete callback for removing likes
        async def delete_likes_callback(user_id_str: str, track_id: str) -> None:
            """Remove all likes from user for a specific track across all guilds."""
            conn = get_conn()
            conn.execute(
                "DELETE FROM likes WHERE user_id = ? AND track_id = ?",
                (user_id_str, track_id)
            )
        
        # Timeout callback for cleaning up DM message tracking (only for DMs)
        on_timeout_callback = None
        if is_dm:
            async def timeout_cleanup_callback(uid: int) -> None:
                """Clean up DM message tracking when view times out."""
                if uid in self._autofill_dm_messages:
                    del self._autofill_dm_messages[uid]
            on_timeout_callback = timeout_cleanup_callback
        
        # Create and return the view with filtered tracks
        return LikedSongsManagerView(
            user=user,
            tracks=visible_tracks,
            delete_callback=delete_likes_callback,
            on_timeout_callback=on_timeout_callback,
        )

    @commands.hybrid_command(
        name="autofill_saves",
        description="Manage your autofill liked songs (private message in channel)",
        aliases=["mylikes"]
    )
    async def autofill_saves(self, ctx: commands.Context) -> None:
        """Show and manage user's autofill liked songs as an ephemeral message."""
        user = ctx.author
        
        # Check if we have an interaction (available for slash command invocations)
        # Hybrid commands provide ctx.interaction when invoked as slash commands
        interaction = getattr(ctx, 'interaction', None)
        
        if interaction is not None:
            # Slash command invocation - send ephemeral response
            view = self._create_autofill_saves_view(user, is_dm=False)
            await interaction.response.send_message(
                embed=view._build_embed(), 
                view=view, 
                ephemeral=True
            )
            view.message = await interaction.original_response()
        else:
            # Prefix command invocation - send DM to user (no public response)
            try:
                # Delete previous DM message if it exists
                user_id = user.id
                if user_id in self._autofill_dm_messages:
                    old_msg = self._autofill_dm_messages[user_id]
                    try:
                        await old_msg.delete()
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        # Message already deleted or we don't have permission - ignore
                        pass
                    finally:
                        # Remove from tracking even if deletion failed
                        del self._autofill_dm_messages[user_id]
                
                # Create view with timeout callback for DM cleanup
                view = self._create_autofill_saves_view(user, is_dm=True)
                
                # Send new DM message
                msg = await user.send(embed=view._build_embed(), view=view)
                view.message = msg
                # Track the new message
                self._autofill_dm_messages[user_id] = msg
            except (discord.Forbidden, Exception):
                # User has DMs disabled or other error - silently fail (no public messages)
                pass

    @commands.has_permissions(administrator=True)
    @commands.command(name="autofill_health")
    async def autofill_health(self, ctx: commands.Context) -> None:
        """List broken-looking songs (and offer one-click cleanup).

        Only flags tracks that have failed with confirmed HTTP 404/410 at least
        BROKEN_MIN_FAILURES times across BROKEN_MIN_AGE_HOURS+ hours, and only
        when the autofill batches that observed those failures had a healthy
        success rate (>= RESOLVER_HEALTH_MIN_SUCCESS_RATE). Designed to make
        Suno-outage-style mass deletions impossible.
        """
        if ctx.guild is None:
            await ctx.send("Server only.")
            return
        gid = ctx.guild.id

        try:
            flagged_raw = get_unhealthy_tracks(
                min_failures=BROKEN_MIN_FAILURES,
                min_age_hours=BROKEN_MIN_AGE_HOURS,
                reason="gone",
            )
        except Exception as e:
            log.exception("[autofill_health] get_unhealthy_tracks failed")
            await ctx.send(f"Failed to query track health: `{type(e).__name__}: {e}`")
            return

        if not flagged_raw:
            await ctx.send(embed=discord.Embed(
                title="✅ Nothing flagged",
                description=(
                    f"No tracks meet the broken-song criteria "
                    f"(≥{BROKEN_MIN_FAILURES} confirmed-gone failures spanning ≥{BROKEN_MIN_AGE_HOURS}h)."
                ),
                color=0x2ecc71,
            ))
            return

        path = self._resolve_autofill_csv_path(gid)
        async with self._get_autofill_csv_lock(gid):
            csv_rows = self._load_autofill_csv(path)
        csv_by_url = {(r.get("url") or "").strip().lower(): r for r in csv_rows}

        flagged: list[dict] = []
        for f in flagged_raw:
            url = (f.get("url") or "").strip()
            if not url:
                continue
            csv_row = csv_by_url.get(url.lower())
            f["requested_by"] = (csv_row.get("requested_by") if csv_row else "") or "—"
            f["in_csv"] = csv_row is not None
            flagged.append(f)

        if not flagged:
            await ctx.send(embed=discord.Embed(
                title="✅ Nothing flagged",
                description="Tracks were eligible by health stats but had no matching URLs to act on.",
                color=0x2ecc71,
            ))
            return

        def _fmt_ts(ts: int | None) -> str:
            if not ts:
                return "—"
            return f"<t:{int(ts)}:R>"

        show = flagged[:20]
        lines = []
        for f in show:
            url = f["url"]
            short = url
            m = self._SUNO_SONG_RE.search(url)
            if m:
                short = f"song/{m.group(1)[:8]}…"
            in_csv = "📄" if f["in_csv"] else "  "
            lines.append(
                f"{in_csv} [`{short}`]({url}) — **{f['failure_count']}** fails, "
                f"first {_fmt_ts(f.get('first_failure_at'))}, "
                f"last {_fmt_ts(f.get('last_failure_at'))} "
                f"(by **{escape_markdown(str(f['requested_by']))}**)"
            )
        overflow = len(flagged) - len(show)
        if overflow > 0:
            lines.append(f"\n_…and {overflow} more (the button will remove all {len(flagged)} URLs)._ ")

        embed = discord.Embed(
            title=f"⚠️ {len(flagged)} broken-looking song(s)",
            description="\n".join(lines),
            color=0xe67e22,
        )
        embed.set_footer(text=(
            f"Threshold: ≥{BROKEN_MIN_FAILURES} confirmed-gone failures, ≥{BROKEN_MIN_AGE_HOURS}h span, "
            f"healthy batches (≥{int(RESOLVER_HEALTH_MIN_SUCCESS_RATE*100)}% success). "
            "📄 = present in autofill.csv"
        ))

        view = BrokenSongsCleanupView(
            cog=self, gid=gid, invoker_id=ctx.author.id, flagged=flagged, timeout=60.0,
        )
        view.message = await ctx.send(embed=embed, view=view)

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context) -> None:
        """
        Auto-delete certain prefix command messages (e.g. !play, !skip)
        after they successfully complete.

        - Only runs for prefix commands (not slash commands).
        - Only affects commands on this cog.
        - Only deletes if the bot has Manage Messages in that channel.
        """
        # Safety: sometimes this event can fire with non-Context or no command
        if not isinstance(ctx, commands.Context):
            return
        if ctx.command is None:
            return

        # Only care about commands on this cog
        if ctx.cog is not self:
            return

        # Only delete selected commands (play, skip, etc.)
        cmd_name = ctx.command.name
        if cmd_name not in AUTO_DELETE_COMMANDS:
            return

        # Grab the original message (prefix command)
        msg = getattr(ctx, "message", None)
        if not msg or not msg.guild:
            return

        # Ensure we actually have perms to delete
        me = msg.guild.me
        if me is None:
            return

        perms = msg.channel.permissions_for(me)
        if not perms.manage_messages:
            return

        try:
            await msg.delete()
        except (discord.Forbidden, discord.HTTPException):
            # Silently ignore if we can't delete for some reason
            return

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """
        Listen for voice state updates (users joining/leaving VC) and recalculate
        autofill queue if autofill is active. Includes debouncing to prevent rapid recalculations.
        Also handles the bot being disconnected (kicked/moved) — tries to rejoin and continue.
        """
        # Handle *our own* disconnection (bot was kicked / server-side voice drop)
        if member.id == self.bot.user.id:
            gid = member.guild.id
            was_connected = before.channel is not None
            now_connected = after.channel is not None

            if was_connected and not now_connected:
                if gid in self._intentional_disconnect:
                    self._intentional_disconnect.discard(gid)
                    log.warning("[voice_state] guild %s: intentional leave, skipping reconnect", gid)
                    return
                log.warning("[voice_state] guild %s: bot disconnected from %s", gid, before.channel.name)
                if RADIO_VC_ID:
                    self._last_vc_channel.setdefault(gid, RADIO_VC_ID)
                else:
                    self._last_vc_channel.setdefault(gid, before.channel.id)

                has_work = bool(self.queues.get(gid)) or self._is_autofill_enabled(gid)
                if has_work and not self._reconnecting.get(gid):
                    prev_task = self._reconnect_task.get(gid)
                    if prev_task and not prev_task.done():
                        prev_task.cancel()

                    async def _auto_rejoin(_gid=gid):
                        await asyncio.sleep(3.0)
                        guild = self.bot.get_guild(_gid)
                        already_back = bool(
                            guild and guild.voice_client and guild.voice_client.is_connected()
                        )
                        if already_back:
                            log.info("[reconnect] guild %s: already reconnected by discord.py", _gid)
                        else:
                            ok = await self._try_voice_reconnect(_gid)
                            if not ok:
                                return
                        # Whether discord.py beat us to it or we did it manually,
                        # we still need to re-arm playback/autofill.
                        self._resume_after_reconnect(_gid)
                    try:
                        self._reconnect_task[gid] = self.bot.loop.create_task(_auto_rejoin())
                    except Exception as e:
                        log.debug("suppressed: %s", e)
            elif after.channel and after.channel != before.channel:
                if RADIO_VC_ID:
                    self._last_vc_channel[gid] = RADIO_VC_ID
                else:
                    self._last_vc_channel[gid] = after.channel.id
            return

        # --- Below: other users joining/leaving ---
        
        # Check if the change affects a VC where the bot is connected
        guild = member.guild
        gid = guild.id
        
        # Get the voice channel from before or after
        vc_channel = None
        if before.channel:
            vc_channel = before.channel
        elif after.channel:
            vc_channel = after.channel
        
        if not vc_channel:
            return
        
        # Check if bot is in this voice channel
        try:
            bot_vc = guild.voice_client
            if not bot_vc or bot_vc.channel != vc_channel:
                return
        except Exception:
            return
        
        # Check if autofill is active (enabled AND has autofill songs)
        if not self._is_autofill_enabled(gid):
            return
        
        queue = self.queues.get(gid, deque())
        has_autofill = any(song.get("_autofill") for song in queue)
        if not has_autofill:
            return  # Only manual songs, no need to recalculate
        
        # Debounce: cancel existing timer if any
        existing_timer = self._autofill_recalc_timers.get(gid)
        if existing_timer and not existing_timer.done():
            existing_timer.cancel()
        
        # Create new debounced task (2 second delay)
        async def debounced_recalc():
            try:
                await asyncio.sleep(AUTOFILL_RECALC_DEBOUNCE)
                await self._recalculate_autofill_queue(guild)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.error("[autofill recalc] failed: %s", e)
            finally:
                self._autofill_recalc_timers[gid] = None
        
        self._autofill_recalc_timers[gid] = self.bot.loop.create_task(debounced_recalc())

async def setup(bot):
    await bot.add_cog(RadioBot(bot))