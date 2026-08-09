"""
Anonymous song contests.

Lifecycle:
  1. `!contest add <suno url>`  (anyone)  -> stores an entry, never echoes the link
  2. `!contest play`            (anyone)  -> plays every entry in order, anonymously
                                              (shows only "Entry N" + title), then posts
                                              a voting card with one button per entry
  3. `!contest results`         (admin)   -> tallies votes, reveals the entries and the
                                              artist/link/submitter, and announces the winner

Modeled on the Games cog (active-state gating + Suno playback + button voting) and the
music cog's `request` command (anonymous Suno-URL submission + dedup).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.utils import escape_markdown

from src.utils import contest_images as cimg
from src.utils.extractor import extract_song_info
from src.data.db import (
    get_active_contest,
    get_contest,
    create_contest,
    set_contest_status,
    set_contest_name,
    set_contest_voting_message,
    cancel_active_contest,
    close_contest_submissions,
    set_contest_deadline,
    close_due_submissions,
    submissions_are_open,
    add_contest_entry,
    count_entries,
    get_contest_entries,
    assign_entry_numbers,
    update_entry_metadata,
    get_entry_owner,
    get_user_entry,
    get_entry_by_artist,
    replace_user_entry,
    get_user_votes,
    set_user_votes,
    tally_votes,
)

log = logging.getLogger(__name__)

# Discord allows at most 25 components (5 rows x 5) per message. Past that we
# switch the voting card from buttons to a single select menu.
MAX_VOTE_BUTTONS = 25
# A select menu can hold at most 25 options too.
MAX_SELECT_OPTIONS = 25
# How many entries each person may vote for (toggle on/off, changeable).
MAX_VOTES_PER_USER = 3

_SUNO_SONG_RE = re.compile(r"suno\.com/song/([0-9a-fA-F\-]{36})")

EMBED_COLOR_INFO = 0x5865F2
EMBED_COLOR_OK = 0x2ECC71
EMBED_COLOR_ERR = 0xE74C3C
EMBED_COLOR_GOLD = 0xFFD700
EMBED_COLOR_PLAYING = 0x580FD6

_DEADLINE_CLEAR = frozenset({"clear", "none", "off", "remove", "unset"})


def _parse_submission_deadline(when: str) -> tuple[int | None, str | None]:
    """Parse a deadline string -> unix seconds (UTC), or None to clear.

    Returns (timestamp, error_message). On success error is None.
    """
    raw = (when or "").strip()
    if not raw:
        return None, "Please provide a date/time, or `clear` to remove the deadline."
    if raw.lower() in _DEADLINE_CLEAR:
        return None, None

    if raw.isdigit() and len(raw) >= 10:
        try:
            ts = int(raw)
            if ts <= int(datetime.now(timezone.utc).timestamp()):
                return None, "That time is already in the past."
            return ts, None
        except ValueError:
            pass

    patterns = (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y",
    )
    parsed = None
    for fmt in patterns:
        try:
            parsed = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue
    if parsed is None:
        return None, (
            "Couldn't read that date. Try `YYYY-MM-DD`, `YYYY-MM-DD 18:00`, "
            "`MM/DD/YYYY`, or `clear` to remove a deadline. Times are **UTC**."
        )
    ts = int(parsed.timestamp())
    if ts <= int(datetime.now(timezone.utc).timestamp()):
        return None, "That time is already in the past."
    return ts, None


def _format_deadline(close_at: int | None) -> str:
    if not close_at:
        return "No deadline"
    return f"<t:{int(close_at)}:F> (<t:{int(close_at)}:R>)"


def _submissions_closed_message(contest: dict) -> str:
    if contest.get("status") == "submissions_closed":
        return "Submissions are closed for this contest. Ask an admin to run `!contest play` when you're ready."
    return "This contest is already playing or in voting."


def _fallback_extract_suno_song_id(url: str) -> tuple[str | None, str | None]:
    """Best-effort Suno song-id parse used only if the music cog is unavailable."""
    if not url:
        return None, None
    u = url.strip()
    if u.startswith("<") and u.endswith(">"):
        u = u[1:-1].strip()
    m = _SUNO_SONG_RE.search(u)
    if not m:
        return None, None
    song_id = m.group(1).lower()
    return f"https://suno.com/song/{song_id}", song_id


class ContestBallotSelect(discord.ui.Select):
    """The per-voter dropdown: pick up to MAX_VOTES_PER_USER entries at once.

    The voter's current picks are pre-selected (checkmarks), so they always see
    what they've chosen and can change it in one go.
    """

    def __init__(self, *, entries: list[dict], current: set[int]):
        options = []
        for e in entries[:MAX_SELECT_OPTIONS]:
            title = (e.get("title") or "Untitled")[:90]
            options.append(
                discord.SelectOption(
                    label=f"Entry {cimg.entry_label(e['entry_no']) if e.get('entry_no') else '?'}",
                    description=title,
                    value=str(e["id"]),
                    default=(e["id"] in current),
                )
            )
        max_pick = max(1, min(MAX_VOTES_PER_USER, len(options)))
        super().__init__(
            placeholder=f"Choose up to {max_pick} entr{'y' if max_pick == 1 else 'ies'}…",
            min_values=0,
            max_values=max_pick,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        view: "ContestBallotView" = self.view  # type: ignore[assignment]
        chosen = [int(v) for v in self.values]
        try:
            set_user_votes(
                contest_id=view.contest_id,
                user_id=interaction.user.id,
                entry_ids=chosen,
                max_votes=MAX_VOTES_PER_USER,
            )
        except Exception as e:
            log.warning("[contest] set votes failed: %s", e)
            await interaction.response.edit_message(
                content="Could not save your votes, please try again.", view=None
            )
            return

        if chosen:
            nums = sorted(str(view.entry_no_by_id.get(i, "?")) for i in chosen)
            txt = "✅ Your votes are saved: " + ", ".join(f"Entry {n}" for n in nums)
        else:
            txt = "Your votes have been cleared."
        await interaction.response.edit_message(content=txt, view=None)
        await view.refresh_public_footer()


class ContestBallotView(discord.ui.View):
    """Ephemeral, per-voter ballot shown when someone clicks the Vote button."""

    def __init__(self, *, contest_id: int, entries: list[dict], current: set[int], public_message, label: str | None = None):
        super().__init__(timeout=180)
        self.contest_id = contest_id
        self.public_message = public_message
        self.label = label or cimg.CONTEST_LABEL
        self.entry_no_by_id = {e["id"]: (cimg.entry_label(e["entry_no"]) if e.get("entry_no") else "?") for e in entries}
        self.add_item(ContestBallotSelect(entries=entries, current=current))

    async def refresh_public_footer(self):
        try:
            totals = tally_votes(contest_id=self.contest_id)
            m = self.public_message
            if m and m.embeds:
                embed = m.embeds[0]
                embed.set_footer(
                    text=f"{self.label} • Total votes: {totals['total']} • Up to {MAX_VOTES_PER_USER} votes per person"
                )
                # `attachments=[]` keeps the already-embedded images inside the embed
                # (their URLs are now CDN links). Without it, editing a message that
                # used attachment:// images makes Discord re-show those files as a
                # loose gallery above the embed (duplicate images).
                await m.edit(embed=embed, attachments=[])
        except Exception as e:
            log.debug("[contest] footer refresh failed: %s", e)


class OpenBallotButton(discord.ui.Button):
    def __init__(self):
        super().__init__(style=discord.ButtonStyle.primary, label="Manage your votes", custom_id="contest_open_ballot")

    async def callback(self, interaction: discord.Interaction):
        view: "ContestVoteView" = self.view  # type: ignore[assignment]
        if interaction.user.bot:
            return await interaction.response.defer(ephemeral=True)
        current = set(get_user_votes(contest_id=view.contest_id, user_id=interaction.user.id))
        ballot = ContestBallotView(
            contest_id=view.contest_id,
            entries=view.entries,
            current=current,
            public_message=interaction.message,
            label=view.label,
        )
        await interaction.response.send_message(
            content=(
                f"Pick up to **{MAX_VOTES_PER_USER}** entries below — it saves as soon as you choose. "
                "Open this again any time to change your picks."
            ),
            view=ballot,
            ephemeral=True,
        )


class ContestVoteView(discord.ui.View):
    """Public voting card: a single 'Vote' button that opens a private ballot.

    Each voter gets an ephemeral dropdown pre-filled with their current picks and
    can select up to MAX_VOTES_PER_USER entries. Votes persist to the DB so
    results survive a restart.
    """

    def __init__(self, *, contest_id: int, entries: list[dict], timeout: float | None = None, label: str | None = None):
        super().__init__(timeout=timeout)
        self.contest_id = contest_id
        self.entries = entries
        self.label = label or cimg.CONTEST_LABEL
        self.add_item(OpenBallotButton())


class Contest(commands.Cog):
    """Anonymous song contests."""

    def __init__(self, bot):
        self.bot = bot
        # guild_id -> True ONLY while entries are actively being played (blocks the
        # radio for the duration of a listening party). Cleared once the party ends
        # so the radio resumes and voting can stay open across multiple parties.
        self.active_contests: dict[int, bool] = {}
        # guild_id present while a listening party is in progress (concurrency guard).
        self._playing: set[int] = set()

    async def cog_load(self):
        self._deadline_check.start()

    async def cog_unload(self):
        self._deadline_check.cancel()

    @tasks.loop(minutes=1)
    async def _deadline_check(self):
        try:
            n = close_due_submissions()
            if n:
                log.info("[contest] auto-closed submissions on %s contest(s)", n)
        except Exception as e:
            log.warning("[contest] deadline check failed: %s", e)

    @_deadline_check.before_loop
    async def _deadline_check_before(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ helpers

    def is_contest_active(self, guild_id: int) -> bool:
        """Parallel to Games.is_game_active — used by the music cog to back off."""
        return guild_id in self.active_contests

    @staticmethod
    def _is_admin(member: discord.Member) -> bool:
        try:
            perms = member.guild_permissions
            return bool(perms.administrator or perms.manage_guild)
        except Exception:
            return False

    async def _canonicalize_suno(self, url: str) -> tuple[str | None, str | None]:
        """Validate/normalize a Suno URL -> (canonical_url, song_id)."""
        loop = asyncio.get_event_loop()
        music_cog = self.bot.get_cog("Music")
        if music_cog and hasattr(music_cog, "_extract_suno_song_id"):
            try:
                return await loop.run_in_executor(None, music_cog._extract_suno_song_id, url)
            except Exception as e:
                log.debug("[contest] music cog canonicalize failed: %s", e)
        return _fallback_extract_suno_song_id(url)

    async def _deny_if_not_admin(self, ctx: commands.Context) -> bool:
        """Return True (and reply) if the invoker is not an admin."""
        if self._is_admin(ctx.author):
            return False
        embed = discord.Embed(
            title="Admins only",
            description="Only server admins can manage contests.",
            color=EMBED_COLOR_ERR,
        )
        await self._reply(ctx, embed=embed, ephemeral=True)
        return True

    async def _reply(self, ctx: commands.Context, *, embed: discord.Embed, ephemeral: bool = False, **kwargs):
        """Reply that works for both prefix and slash invocations."""
        is_slash = ctx.interaction is not None
        if is_slash:
            inter = ctx.interaction
            if inter.response.is_done():
                return await inter.followup.send(embed=embed, ephemeral=ephemeral, **kwargs)
            return await inter.response.send_message(embed=embed, ephemeral=ephemeral, **kwargs)
        return await ctx.send(embed=embed, **kwargs)

    @staticmethod
    def _attach(embed: discord.Embed, path: str | None, files: list, *, name: str, as_thumbnail: bool = False) -> None:
        """Attach a local image/GIF to `embed`, appending the File to `files`.

        No-op if the path is missing, so callers can attach several optional
        assets to one embed and send whatever ended up in `files`.
        """
        if not path or not os.path.isfile(path):
            return
        fname = name + (os.path.splitext(path)[1].lower() or ".png")
        if as_thumbnail:
            embed.set_thumbnail(url=f"attachment://{fname}")
        else:
            embed.set_image(url=f"attachment://{fname}")
        try:
            files.append(discord.File(path, filename=fname))
        except Exception as e:
            log.debug("[contest] could not attach image %s: %s", path, e)

    @staticmethod
    def _files_kwargs(files: list) -> dict:
        """Send kwargs for an optional list of attachments (empty -> no kwarg)."""
        return {"files": files} if files else {}

    def _get_music_cog(self):
        return self.bot.get_cog("Music")

    async def _restore_radio(self, ctx: commands.Context) -> None:
        """Hand control back to the music cog once the contest is over."""
        music_cog = self._get_music_cog()
        if not music_cog:
            return
        try:
            if hasattr(music_cog, "_clear_now_playing_if_guild"):
                music_cog._clear_now_playing_if_guild(ctx.guild.id)
            if ctx.voice_client and ctx.voice_client.is_playing():
                ctx.voice_client.stop()
            if music_cog.queues.get(ctx.guild.id):
                radio_ctx = music_cog._ctx_for_autofill(ctx.guild.id, fallback_ctx=ctx)
                await music_cog.play_next(radio_ctx)
            elif hasattr(music_cog, "_schedule_autofill_if_idle"):
                music_cog._schedule_autofill_if_idle(ctx)
        except Exception as e:
            log.warning("[contest] radio restore failed: %s", e)

    # ------------------------------------------------------------------ commands

    @commands.hybrid_group(
        name="contest",
        invoke_without_command=True,
        description="Anonymous song contests. Use a subcommand: add, play, results, …",
    )
    async def contest(self, ctx: commands.Context):
        """Anonymous song contests. Run `!contest help` for the list of subcommands."""
        await ctx.invoke(self.contest_status)

    @contest.command(name="help", description="How contests work")
    async def contest_help(self, ctx: commands.Context):
        embed = discord.Embed(
            title="Anonymous Song Contests",
            description=(
                "Submit songs anonymously, play them back without revealing who made "
                "them, vote, then reveal the winner."
            ),
            color=EMBED_COLOR_INFO,
        )
        embed.add_field(
            name="Anyone",
            value=(
                "`!contest add <suno url>` — add a song (one entry per artist; "
                "regular members get one entry, admins can add several / on others' behalf; "
                "your link is never shown publicly)\n"
                "`!contest play` — host a listening party (plays all entries, opens voting); "
                "repeatable for multiple parties\n"
                "`!contest status` — show entry count + state"
            ),
            inline=False,
        )
        embed.add_field(
            name="Admins",
            value=(
                "`!contest new [name]` — start a fresh contest\n"
                "`!contest close` — stop accepting new entries (contest stays open)\n"
                "`!contest deadline <date>` — set/clear an auto-close time (UTC)\n"
                "`!contest name <name>` — name/rename the current contest\n"
                "`!contest entries` — privately list the submitted entries\n"
                "`!contest submitters` — list the entered artists (no songs/links)\n"
                "`!contest results` — reveal entries and announce the winner (run any time)\n"
                "`!contest cancel` — abort the current contest"
            ),
            inline=False,
        )
        await self._reply(ctx, embed=embed, ephemeral=True)

    @contest.command(name="add", description="Add a Suno song to the contest (anonymous)")
    @app_commands.describe(url="A Suno song URL (e.g. https://suno.com/song/...)")
    async def contest_add(self, ctx: commands.Context, *, url: str = ""):
        if ctx.guild is None:
            await self._reply(
                ctx,
                embed=discord.Embed(
                    title="Server only",
                    description="Contests can only be used in a server.",
                    color=EMBED_COLOR_ERR,
                ),
                ephemeral=True,
            )
            return

        if not (url or "").strip():
            await self._reply(
                ctx,
                embed=discord.Embed(
                    title="Missing URL",
                    description="Usage: `!contest add <suno song url>`",
                    color=EMBED_COLOR_ERR,
                ),
                ephemeral=True,
            )
            return

        gid = ctx.guild.id
        is_slash = ctx.interaction is not None

        # Defer slash early — canonicalization may make a network HEAD request.
        if is_slash and not ctx.interaction.response.is_done():
            try:
                await ctx.interaction.response.defer(ephemeral=True, thinking=True)
            except Exception as e:
                log.debug("[contest] add defer failed: %s", e)

        contest = get_active_contest(guild_id=gid)
        if contest and not submissions_are_open(contest):
            await self._reply(
                ctx,
                embed=discord.Embed(
                    title="Submissions closed",
                    description=_submissions_closed_message(contest),
                    color=EMBED_COLOR_ERR,
                ),
                ephemeral=True,
            )
            return

        canonical_url, song_id = await self._canonicalize_suno(url)
        if not canonical_url or not song_id:
            await self._reply(
                ctx,
                embed=discord.Embed(
                    title="Suno only",
                    description=(
                        "Only Suno song URLs are supported.\n"
                        "Example: `https://suno.com/song/<id>` or `https://suno.com/s/<short>`."
                    ),
                    color=EMBED_COLOR_ERR,
                ),
                ephemeral=True,
            )
            return

        # Lazily create the contest on first submission.
        if contest is None:
            contest_id = create_contest(guild_id=gid, created_by=ctx.author.id)
        else:
            contest_id = contest["id"]

        is_admin = self._is_admin(ctx.author)

        # Resolve metadata now (title/artist) so we can dedup by artist and show
        # titles before playback. Best-effort — falls back gracefully if it fails.
        title = artist = cover = None
        try:
            loop = asyncio.get_event_loop()
            song = await loop.run_in_executor(None, extract_song_info, canonical_url)
            if song:
                title = song.get("title")
                artist = song.get("artist") or song.get("author")
                cover = song.get("thumbnail") or song.get("image_url")
        except Exception as e:
            log.debug("[contest] add metadata resolve failed: %s", e)

        # Exact-song dedup (applies to everyone): the same song can't be entered twice.
        owner = get_entry_owner(contest_id=contest_id, song_id=song_id)
        if owner is not None:
            if not is_admin and str(owner) == str(ctx.author.id):
                title_txt, desc_txt = ("Already your entry", "That song is already your current entry in this contest.")
            else:
                title_txt, desc_txt = ("Already entered", "That exact song is already an entry in this contest.")
            await self._reply(
                ctx,
                embed=discord.Embed(title=title_txt, description=desc_txt, color=EMBED_COLOR_INFO),
                ephemeral=True,
            )
            return

        # Duplicate check is by ARTIST (not by submitter).
        artist_entry = get_entry_by_artist(contest_id=contest_id, artist=artist) if artist else None
        target_entry_id: int | None = None

        if is_admin:
            # Admins can add many entries (e.g. on someone's behalf or for testing).
            # Uniqueness is per-artist: another song by an already-entered artist
            # replaces that artist's entry.
            if artist_entry:
                replace_user_entry(entry_id=artist_entry["id"], url=canonical_url, song_id=song_id)
                target_entry_id = artist_entry["id"]
                action = "replaced"
            else:
                target_entry_id = add_contest_entry(
                    contest_id=contest_id,
                    url=canonical_url,
                    song_id=song_id,
                    submitted_by=ctx.author.id,
                    submitted_by_name=str(ctx.author),
                )
                action = "added"
        else:
            # Regular users: can't take an artist someone else already entered.
            if artist_entry and str(artist_entry.get("submitted_by")) != str(ctx.author.id):
                await self._reply(
                    ctx,
                    embed=discord.Embed(
                        title="Artist already entered",
                        description="A song by that artist is already in this contest.",
                        color=EMBED_COLOR_INFO,
                    ),
                    ephemeral=True,
                )
                return
            # One entry per user: a new submission replaces their previous one.
            existing = get_user_entry(contest_id=contest_id, submitted_by=ctx.author.id)
            if existing:
                replace_user_entry(entry_id=existing["id"], url=canonical_url, song_id=song_id)
                target_entry_id = existing["id"]
                action = "replaced"
            else:
                target_entry_id = add_contest_entry(
                    contest_id=contest_id,
                    url=canonical_url,
                    song_id=song_id,
                    submitted_by=ctx.author.id,
                    submitted_by_name=str(ctx.author),
                )
                action = "added"

        # Store the resolved metadata on the entry (replace_user_entry clears it).
        if target_entry_id is not None:
            update_entry_metadata(entry_id=target_entry_id, title=title, artist=artist, cover_url=cover)
            # Pre-generate the blurred "?" thumbnail now (cached on disk, keyed by
            # song id) so the first listening party plays back instantly instead of
            # rendering covers mid-party. Best-effort — never blocks the submission.
            if cover:
                try:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(
                        None,
                        lambda: cimg.make_mystery_cover(cover_url=cover, song_id=song_id),
                    )
                except Exception as e:
                    log.debug("[contest] pre-generate mystery cover failed: %s", e)

        total = count_entries(contest_id=contest_id)

        verb = "updated" if action == "replaced" else "added"

        # Never echo the link — keep submissions anonymous. Slash replies are
        # ephemeral; prefix replies are a minimal public acknowledgement.
        artist_label = f" by **{escape_markdown(artist)}**" if artist else ""
        if is_slash:
            if action == "replaced":
                if is_admin:
                    desc = f"Updated the existing entry{artist_label} with this song. **{total}** entries total."
                else:
                    desc = f"Your entry was updated to your new song. **{total}** entries total."
            else:
                if is_admin:
                    desc = f"Added the song{artist_label} to the contest. There are now **{total}** entries."
                else:
                    desc = f"Your song is in the contest. There are now **{total}** entries."
            embed = discord.Embed(
                title=f"Entry {verb}",
                description=desc,
                color=EMBED_COLOR_OK,
            )
            await self._reply(ctx, embed=embed, ephemeral=True)
        else:
            # Delete the user's message so the link (and its preview) doesn't
            # linger in the channel. This needs the "Manage Messages" permission.
            deleted = False
            try:
                await ctx.message.delete()
                deleted = True
            except Exception as e:
                log.debug("[contest] could not delete submission message: %s", e)

            if deleted:
                if action == "replaced":
                    desc = f"An entry was updated anonymously. **{total}** entries so far."
                else:
                    desc = f"A new entry was added anonymously. **{total}** entries so far."
                embed = discord.Embed(
                    title=f"Entry {verb}",
                    description=desc,
                    color=EMBED_COLOR_OK,
                )
            else:
                # Couldn't hide the link — warn and point at the private route.
                embed = discord.Embed(
                    title=f"Entry {verb} — but I couldn't hide your link",
                    description=(
                        f"Your song was {verb} (**{total}** entries so far), but I couldn't delete "
                        "your message, so the link/preview is still visible.\n\n"
                        "Use **`/contest add`** (slash) for fully private submissions, or give me the "
                        "**Manage Messages** permission in this channel so I can clean up `!contest add` posts."
                    ),
                    color=EMBED_COLOR_INFO,
                )
            await ctx.send(embed=embed, delete_after=15)

        log.info("[contest] guild=%s entry %s (song_id=%s, total=%s)", gid, action, song_id, total)

    @contest.command(name="status", description="Show contest status")
    async def contest_status(self, ctx: commands.Context):
        if ctx.guild is None:
            return
        contest = get_active_contest(guild_id=ctx.guild.id)
        if not contest:
            await self._reply(
                ctx,
                embed=discord.Embed(
                    title="No active contest",
                    description="There's no contest running. Add a song with `!contest add <url>` to start one.",
                    color=EMBED_COLOR_INFO,
                ),
                ephemeral=True,
            )
            return
        total = count_entries(contest_id=contest["id"])
        status_label = {
            "collecting": "Collecting submissions",
            "submissions_closed": "Submissions closed",
            "playing": "Playing entries",
            "voting": "Voting open",
        }.get(contest["status"], contest["status"])
        lines = [f"**State:** {status_label}", f"**Entries:** {total}"]
        if contest.get("submissions_close_at"):
            lines.append(f"**Deadline:** {_format_deadline(contest['submissions_close_at'])}")
        embed = discord.Embed(
            title="Contest status",
            description="\n".join(lines),
            color=EMBED_COLOR_INFO,
        )
        if contest.get("name"):
            embed.set_author(name=contest["name"])
        await self._reply(ctx, embed=embed, ephemeral=True)

    @contest.command(name="entries", description="List the contest entries privately (admin)")
    async def contest_entries(self, ctx: commands.Context):
        if ctx.guild is None:
            return
        # Admin-only + ephemeral so listing entries never breaks anonymity in chat.
        if await self._deny_if_not_admin(ctx):
            return
        contest = get_active_contest(guild_id=ctx.guild.id)
        if not contest:
            await self._reply(
                ctx,
                embed=discord.Embed(
                    title="No active contest",
                    description="There's no contest running.",
                    color=EMBED_COLOR_INFO,
                ),
                ephemeral=True,
            )
            return

        entries = get_contest_entries(contest_id=contest["id"], ordered=True)
        if not entries:
            await self._reply(
                ctx,
                embed=discord.Embed(
                    title="No entries yet",
                    description="Nobody has submitted a song yet.",
                    color=EMBED_COLOR_INFO,
                ),
                ephemeral=True,
            )
            return

        lines = []
        for i, e in enumerate(entries, start=1):
            label = cimg.entry_label(e["entry_no"]) if e.get("entry_no") else i
            title = e.get("title") or "(resolves when you run !contest play)"
            submitter = e.get("submitted_by_name") or (f"<@{e['submitted_by']}>" if e.get("submitted_by") else "Unknown")
            url = e.get("url") or ""
            line = f"**{label}.** {escape_markdown(title)} — by {escape_markdown(str(submitter))}"
            if url:
                line += f"\n   {url}"
            lines.append(line)

        embed = discord.Embed(
            title=f"Contest entries ({len(entries)})",
            description="\n".join(lines)[:4000],
            color=EMBED_COLOR_INFO,
        )
        embed.set_footer(text="Links shown for verification")
        await self._reply(ctx, embed=embed, ephemeral=True)

    @contest.command(name="submitters", description="List who has submitted — no song titles or links (admin)")
    async def contest_submitters(self, ctx: commands.Context):
        if ctx.guild is None:
            return
        if await self._deny_if_not_admin(ctx):
            return
        contest = get_active_contest(guild_id=ctx.guild.id)
        if not contest:
            await self._reply(
                ctx,
                embed=discord.Embed(
                    title="No active contest",
                    description="There's no contest running.",
                    color=EMBED_COLOR_INFO,
                ),
            )
            return

        entries = get_contest_entries(contest_id=contest["id"], ordered=True)
        if not entries:
            await self._reply(
                ctx,
                embed=discord.Embed(
                    title="No entries yet",
                    description="Nobody has submitted a song yet.",
                    color=EMBED_COLOR_INFO,
                ),
            )
            return

        # List the ARTISTS (not the submitting user) so admins entering songs on
        # someone's behalf still see the right names to match sign-ups against.
        artists: list[str] = []
        seen: set[str] = set()
        unresolved = 0
        for e in entries:
            artist = (e.get("artist") or "").strip()
            if not artist:
                unresolved += 1
                continue
            key = artist.lower()
            if key in seen:
                continue
            seen.add(key)
            artists.append(artist)

        artists.sort(key=str.lower)
        lines = [f"• {escape_markdown(a)}" for a in artists]
        if unresolved:
            lines.append(
                f"• *{unresolved} entr{'y' if unresolved == 1 else 'ies'} not resolved yet "
                "— run `!contest play` or re-add to resolve the artist*"
            )
        if not lines:
            lines.append("*No artists resolved yet — run `!contest play` to resolve them.*")

        embed = discord.Embed(
            title=f"Contest artists ({len(artists)})",
            description="\n".join(lines)[:4000],
            color=EMBED_COLOR_INFO,
        )
        if contest.get("name"):
            embed.set_author(name=contest["name"])
        await self._reply(ctx, embed=embed)

    @contest.command(name="new", description="Start a fresh contest (admin)")
    @app_commands.describe(name="Optional name for the contest")
    async def contest_new(self, ctx: commands.Context, *, name: str = ""):
        if ctx.guild is None:
            return
        if await self._deny_if_not_admin(ctx):
            return
        cancel_active_contest(guild_id=ctx.guild.id)
        self.active_contests.pop(ctx.guild.id, None)
        contest_id = create_contest(guild_id=ctx.guild.id, name=(name.strip() or None), created_by=ctx.author.id)
        embed = discord.Embed(
            title="New contest started",
            description="Submissions are open. Anyone can add a song with `!contest add <suno url>`.",
            color=EMBED_COLOR_OK,
        )
        if name.strip():
            embed.set_author(name=name.strip())
        await self._reply(ctx, embed=embed)
        log.info("[contest] guild=%s new contest %s", ctx.guild.id, contest_id)

    @contest.command(name="name", description="Name or rename the current contest (admin)")
    @app_commands.describe(name="The contest name (leave empty to clear it)")
    async def contest_name(self, ctx: commands.Context, *, name: str = ""):
        if ctx.guild is None:
            return
        if await self._deny_if_not_admin(ctx):
            return
        contest = get_active_contest(guild_id=ctx.guild.id)
        if not contest:
            await self._reply(
                ctx,
                embed=discord.Embed(
                    title="No active contest",
                    description="There's no contest running. Start one with `!contest new [name]` or `!contest add <url>`.",
                    color=EMBED_COLOR_INFO,
                ),
                ephemeral=True,
            )
            return
        clean = name.strip()
        set_contest_name(contest_id=contest["id"], name=clean or None)
        if clean:
            embed = discord.Embed(
                title="Contest renamed",
                description=(
                    f"This contest is now called **{escape_markdown(clean)}**. "
                    "It'll show on the voting and results cards (and any new listening parties)."
                ),
                color=EMBED_COLOR_OK,
            )
            embed.set_author(name=clean)
        else:
            embed = discord.Embed(
                title="Contest name cleared",
                description=f"The cards will show **{escape_markdown(cimg.CONTEST_LABEL)}** again.",
                color=EMBED_COLOR_OK,
            )
        await self._reply(ctx, embed=embed)
        log.info("[contest] guild=%s renamed contest %s to %r", ctx.guild.id, contest["id"], clean)

    @contest.command(name="close", description="Close submissions now (admin)")
    async def contest_close_submissions(self, ctx: commands.Context):
        if ctx.guild is None:
            return
        if await self._deny_if_not_admin(ctx):
            return
        contest = get_active_contest(guild_id=ctx.guild.id)
        if not contest:
            await self._reply(
                ctx,
                embed=discord.Embed(
                    title="No active contest",
                    description="There's no contest running.",
                    color=EMBED_COLOR_INFO,
                ),
                ephemeral=True,
            )
            return
        if not submissions_are_open(contest):
            await self._reply(
                ctx,
                embed=discord.Embed(
                    title="Already closed",
                    description="Submissions are already closed for this contest.",
                    color=EMBED_COLOR_INFO,
                ),
                ephemeral=True,
            )
            return
        close_contest_submissions(contest_id=contest["id"])
        total = count_entries(contest_id=contest["id"])
        embed = discord.Embed(
            title="Submissions closed",
            description=(
                f"Nobody can add more songs. **{total}** entries are locked in.\n"
                "Run `!contest play` when you're ready for a listening party."
            ),
            color=EMBED_COLOR_OK,
        )
        if contest.get("name"):
            embed.set_author(name=contest["name"])
        await self._reply(ctx, embed=embed)
        log.info("[contest] guild=%s closed submissions on contest %s", ctx.guild.id, contest["id"])

    @contest.command(name="deadline", description="Set or clear the submissions deadline (admin)")
    @app_commands.describe(
        when="Date/time in UTC (e.g. 2026-07-15 or 2026-07-15 18:00), or clear to remove"
    )
    async def contest_deadline(self, ctx: commands.Context, *, when: str = ""):
        if ctx.guild is None:
            return
        if await self._deny_if_not_admin(ctx):
            return
        contest = get_active_contest(guild_id=ctx.guild.id)
        if not contest:
            await self._reply(
                ctx,
                embed=discord.Embed(
                    title="No active contest",
                    description="There's no contest running.",
                    color=EMBED_COLOR_INFO,
                ),
                ephemeral=True,
            )
            return
        if contest["status"] != "collecting":
            await self._reply(
                ctx,
                embed=discord.Embed(
                    title="Submissions already closed",
                    description="You can only set a deadline while submissions are still open.",
                    color=EMBED_COLOR_ERR,
                ),
                ephemeral=True,
            )
            return
        if not (when or "").strip():
            await self._reply(
                ctx,
                embed=discord.Embed(
                    title="Missing deadline",
                    description=(
                        "Usage: `!contest deadline 2026-07-15 18:00`\n"
                        "Or `!contest deadline clear` to remove the deadline. Times are **UTC**."
                    ),
                    color=EMBED_COLOR_ERR,
                ),
                ephemeral=True,
            )
            return

        close_at, err = _parse_submission_deadline(when)
        if err:
            await self._reply(
                ctx,
                embed=discord.Embed(title="Invalid deadline", description=err, color=EMBED_COLOR_ERR),
                ephemeral=True,
            )
            return

        set_contest_deadline(contest_id=contest["id"], close_at=close_at)
        if close_at is None:
            embed = discord.Embed(
                title="Deadline cleared",
                description="Submissions stay open until you run `!contest close` or start playing.",
                color=EMBED_COLOR_OK,
            )
        else:
            embed = discord.Embed(
                title="Deadline set",
                description=(
                    f"Submissions will close automatically at {_format_deadline(close_at)}.\n"
                    "You can still run `!contest close` to shut them early."
                ),
                color=EMBED_COLOR_OK,
            )
        if contest.get("name"):
            embed.set_author(name=contest["name"])
        await self._reply(ctx, embed=embed)
        log.info("[contest] guild=%s contest %s deadline=%s", ctx.guild.id, contest["id"], close_at)

    @contest.command(name="cancel", description="Cancel the current contest (admin)")
    async def contest_cancel(self, ctx: commands.Context):
        if ctx.guild is None:
            return
        if await self._deny_if_not_admin(ctx):
            return
        contest = get_active_contest(guild_id=ctx.guild.id)
        if not contest:
            await self._reply(
                ctx,
                embed=discord.Embed(title="Nothing to cancel", description="No contest is running.", color=EMBED_COLOR_INFO),
                ephemeral=True,
            )
            return
        cancel_active_contest(guild_id=ctx.guild.id)
        self.active_contests.pop(ctx.guild.id, None)
        await self._reply(
            ctx,
            embed=discord.Embed(title="Contest cancelled", description="The contest has been cancelled.", color=EMBED_COLOR_INFO),
        )
        await self._restore_radio(ctx)
        log.info("[contest] guild=%s cancelled", ctx.guild.id)

    @contest.command(name="play", description="Host a listening party: play all entries anonymously, then open voting (repeatable)")
    async def contest_play(self, ctx: commands.Context):
        if ctx.guild is None:
            return

        gid = ctx.guild.id
        contest = get_active_contest(guild_id=gid)
        if not contest:
            await self._reply(
                ctx,
                embed=discord.Embed(title="No contest", description="There's no contest to play.", color=EMBED_COLOR_ERR),
                ephemeral=True,
            )
            return

        if gid in self._playing:
            await self._reply(
                ctx,
                embed=discord.Embed(
                    title="Party in progress",
                    description="A listening party is already playing right now. Let it finish first.",
                    color=EMBED_COLOR_ERR,
                ),
                ephemeral=True,
            )
            return

        # Need the invoker in a voice channel.
        if not getattr(ctx.author, "voice", None) or not ctx.author.voice.channel:
            await self._reply(
                ctx,
                embed=discord.Embed(title="Join voice first", description="You must be in a voice channel to play the contest.", color=EMBED_COLOR_ERR),
                ephemeral=True,
            )
            return

        contest_id = contest["id"]
        total = assign_entry_numbers(contest_id=contest_id)
        if total == 0:
            await self._reply(
                ctx,
                embed=discord.Embed(title="No entries", description="Add some songs with `!contest add <url>` first.", color=EMBED_COLOR_ERR),
                ephemeral=True,
            )
            return

        # Connect / take over voice.
        vc = ctx.voice_client
        try:
            if not vc:
                vc = await ctx.author.voice.channel.connect()
            elif vc.channel.id != ctx.author.voice.channel.id:
                await vc.move_to(ctx.author.voice.channel)
        except Exception as e:
            await self._reply(
                ctx,
                embed=discord.Embed(title="Voice error", description=f"Could not connect to voice: {e}", color=EMBED_COLOR_ERR),
                ephemeral=True,
            )
            return

        self._playing.add(gid)
        try:
            # Take control away from the radio for the duration of the party.
            # IMPORTANT: flag the contest as active BEFORE stopping the radio.
            # Stopping the voice client fires the music cog's `after_playing`
            # callback, which would otherwise immediately start the next radio
            # song — and then our `vc.play()` below fails with "Already playing
            # audio". The active flag makes that callback back off.
            self.active_contests[gid] = True
            set_contest_status(contest_id=contest_id, status="playing")

            music_cog = self._get_music_cog()
            if music_cog:
                try:
                    if hasattr(music_cog, "_cancel_autofill_task"):
                        music_cog._cancel_autofill_task(gid)
                    if hasattr(music_cog, "_clear_now_playing_if_guild"):
                        music_cog._clear_now_playing_if_guild(gid)
                except Exception as e:
                    log.debug("[contest] could not pause radio: %s", e)

            if vc.is_playing() or vc.is_paused():
                vc.stop()
            # Wait for the voice client to actually free up before we start playing.
            await self._wait_until_idle(vc)

            start_embed = discord.Embed(
                title="🎧 Listening party starting",
                description=(
                    f"Playing **{total}** entries anonymously. Voting opens at the end — "
                    "and you can run `!contest play` again later for another listening party. "
                    "Run `!contest results` whenever you're ready to reveal the winner."
                ),
                color=EMBED_COLOR_PLAYING,
            )
            start_embed.set_author(name=cimg.contest_label(contest))
            start_files: list = []
            self._attach(start_embed, cimg.resolve_poster(contest), start_files, name="contest_poster")
            await self._reply(ctx, embed=start_embed, **self._files_kwargs(start_files))

            await self._play_entries(ctx, contest_id)
        except Exception as e:
            log.exception("[contest] playback crashed: %s", e)
            try:
                await ctx.channel.send(
                    embed=discord.Embed(title="Contest error", description=f"Playback failed: {e}", color=EMBED_COLOR_ERR)
                )
            except Exception:
                pass
        finally:
            # Party over: release the radio (but keep the contest open for voting
            # and possible future parties — only `results`/`cancel` close it).
            self.active_contests.pop(gid, None)
            self._playing.discard(gid)
            await self._restore_radio(ctx)

    async def _play_entries(self, ctx: commands.Context, contest_id: int) -> None:
        gid = ctx.guild.id
        contest = get_contest(contest_id=contest_id)
        label = cimg.contest_label(contest)
        entries = get_contest_entries(contest_id=contest_id, ordered=True)
        total = len(entries)
        loop = asyncio.get_event_loop()

        for entry in entries:
            if not self.is_contest_active(gid):
                log.info("[contest] guild=%s playback aborted", gid)
                return
            vc = ctx.voice_client
            if not vc or not vc.is_connected():
                log.warning("[contest] guild=%s lost voice during playback", gid)
                self.active_contests.pop(gid, None)
                return

            # Resolve metadata + a playable URL (network call -> executor).
            song = None
            try:
                song = await loop.run_in_executor(None, extract_song_info, entry["url"])
            except Exception as e:
                log.warning("[contest] resolve failed for entry %s: %s", entry["id"], e)

            if song:
                update_entry_metadata(
                    entry_id=entry["id"],
                    title=song.get("title"),
                    artist=song.get("artist") or song.get("author"),
                    cover_url=song.get("thumbnail") or song.get("image_url"),
                )

            audio_url = (song or {}).get("url") or self._suno_cdn_url(entry["url"])
            if not audio_url:
                log.warning("[contest] no playable url for entry %s, skipping", entry["id"])
                skip_embed = discord.Embed(
                    title="Entry skipped",
                    description="(This entry could not be played.)",
                    color=cimg.entry_color(entry["entry_no"]),
                )
                skip_embed.set_author(name=f"🎵 Entry {cimg.entry_label(entry['entry_no'])}")
                skip_embed.set_footer(text=f"{entry['entry_no']} out of {total} entries")
                skip_embed.timestamp = discord.utils.utcnow()
                skip_files: list = []
                self._attach(skip_embed, cimg.get_width_spacer(), skip_files, name="spacer")
                await ctx.channel.send(embed=skip_embed, **self._files_kwargs(skip_files))
                continue

            title = (song or {}).get("title") or entry.get("title") or "Untitled"
            done = asyncio.Event()

            def _after(err, _done=done):
                if err:
                    log.error("[contest] play error: %s", err)
                try:
                    loop.call_soon_threadsafe(_done.set)
                except Exception:
                    pass

            # Make sure the voice client is free before playing (guards against a
            # lingering stream from the previous entry or the radio).
            if vc.is_playing() or vc.is_paused():
                vc.stop()
                await self._wait_until_idle(vc)

            ffmpeg_opts = self._ffmpeg_options(audio_url)
            try:
                source = discord.FFmpegPCMAudio(audio_url, **ffmpeg_opts)
                source = discord.PCMVolumeTransformer(source, volume=1.0)
                vc.play(source, after=_after)
            except Exception as e:
                log.error("[contest] failed to start entry %s: %s", entry["id"], e)
                continue

            # Anonymous now-playing card, styled like the radio's Now Playing card:
            # a big song title, an anonymized "by <alias>", Duration + Contest columns,
            # and a "# out of # entries" footer. No real artist, link, or submitter.
            letter = cimg.entry_label(entry["entry_no"])
            np_embed = discord.Embed(
                title=f"🎵 Now Playing · Entry {letter}",
                description=f"**{escape_markdown(title)}**",
                color=cimg.entry_color(entry["entry_no"]),
            )
            dur = cimg.format_duration((song or {}).get("duration"))
            np_embed.add_field(name="Duration", value=dur or "—", inline=True)
            np_embed.add_field(name="Contest", value=label, inline=True)
            np_embed.set_footer(text=f"{entry['entry_no']} out of {total} entries")
            np_embed.timestamp = discord.utils.utcnow()

            cover_url = (
                (song or {}).get("thumbnail")
                or (song or {}).get("image_url")
                or entry.get("cover_url")
            )
            thumb_path = None
            if cover_url:
                try:
                    thumb_path = await loop.run_in_executor(
                        None,
                        lambda: cimg.make_mystery_cover(
                            cover_url=cover_url, song_id=entry.get("song_id")
                        ),
                    )
                except Exception as e:
                    log.debug("[contest] mystery cover failed for entry %s: %s", entry["id"], e)
            if not thumb_path:
                thumb_path = cimg.resolve_entry_thumb(contest)

            np_files: list = []
            self._attach(np_embed, thumb_path, np_files, name=f"entry_{entry['entry_no']}", as_thumbnail=True)
            # Transparent strip so every entry card is the same width.
            self._attach(np_embed, cimg.get_width_spacer(), np_files, name="spacer")
            await ctx.channel.send(embed=np_embed, **self._files_kwargs(np_files))

            # Wait for the song to finish (with a generous safety timeout based
            # on duration so a stuck stream can't hang the whole contest).
            duration = (song or {}).get("duration")
            try:
                timeout = float(duration) + 30 if duration else None
            except (TypeError, ValueError):
                timeout = None
            try:
                await asyncio.wait_for(done.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                log.warning("[contest] entry %s exceeded timeout, advancing", entry["id"])
                try:
                    if vc.is_playing():
                        vc.stop()
                except Exception:
                    pass

        if not self.is_contest_active(gid):
            return

        set_contest_status(contest_id=contest_id, status="voting")
        await self._post_voting_card(ctx, contest_id)

    async def _post_voting_card(self, ctx: commands.Context, contest_id: int) -> None:
        contest = get_contest(contest_id=contest_id)
        label = cimg.contest_label(contest)
        entries = get_contest_entries(contest_id=contest_id, ordered=True)
        lines = []
        for e in entries:
            title = (e.get("title") or "Untitled")[:80]
            lines.append(f"- **Entry {cimg.entry_label(e['entry_no'])}**: {escape_markdown(title)}")
        embed = discord.Embed(
            title="🗳️ Voting is open!",
            description=(
                f"Press **Vote** below and pick up to **{MAX_VOTES_PER_USER}** of your favorites "
                "from the dropdown. Your current picks stay checked, so you can reopen it any "
                "time to change them before results.\n\n"
                + "\n".join(lines)
            ),
            color=EMBED_COLOR_GOLD,
        )
        embed.set_author(name=label)
        try:
            _total_now = tally_votes(contest_id=contest_id)["total"]
        except Exception:
            _total_now = 0
        embed.set_footer(text=f"{label} • Total votes: {_total_now} • Up to {MAX_VOTES_PER_USER} votes per person")
        vote_files: list = []
        self._attach(embed, cimg.resolve_poster(contest), vote_files, name="contest_poster", as_thumbnail=True)
        # No main image on the voting card — it gets stripped the moment someone
        # votes (the card edits to update the tally, which drops attachments).
        # A transparent spacer keeps the card width consistent.
        self._attach(embed, cimg.get_width_spacer(), vote_files, name="spacer")
        view = ContestVoteView(contest_id=contest_id, entries=entries, label=label)
        msg = await ctx.channel.send(embed=embed, view=view, **self._files_kwargs(vote_files))
        try:
            set_contest_voting_message(contest_id=contest_id, channel_id=msg.channel.id, message_id=msg.id)
        except Exception as e:
            log.debug("[contest] could not store voting message id: %s", e)

    @contest.command(name="vote", description="(Re)post the voting card (admin)")
    async def contest_vote(self, ctx: commands.Context):
        if ctx.guild is None:
            return
        if await self._deny_if_not_admin(ctx):
            return
        contest = get_active_contest(guild_id=ctx.guild.id)
        if not contest:
            await self._reply(
                ctx,
                embed=discord.Embed(title="No contest", description="There's no contest running.", color=EMBED_COLOR_ERR),
                ephemeral=True,
            )
            return
        if count_entries(contest_id=contest["id"]) == 0:
            await self._reply(
                ctx,
                embed=discord.Embed(title="No entries", description="There are no entries to vote on.", color=EMBED_COLOR_ERR),
                ephemeral=True,
            )
            return
        # Make sure entries are numbered even if play was skipped.
        assign_entry_numbers(contest_id=contest["id"])
        set_contest_status(contest_id=contest["id"], status="voting")
        if ctx.interaction is not None:
            await ctx.interaction.response.send_message("Voting card posted.", ephemeral=True)
        await self._post_voting_card(ctx, contest["id"])

    @contest.command(name="results", description="Reveal entries and announce the winner (admin)")
    async def contest_results(self, ctx: commands.Context):
        if ctx.guild is None:
            return
        if await self._deny_if_not_admin(ctx):
            return

        gid = ctx.guild.id
        contest = get_active_contest(guild_id=gid)
        if not contest:
            await self._reply(
                ctx,
                embed=discord.Embed(title="No contest", description="There's no contest to finish.", color=EMBED_COLOR_ERR),
                ephemeral=True,
            )
            return

        contest_id = contest["id"]
        entries = get_contest_entries(contest_id=contest_id, ordered=True)
        if not entries:
            await self._reply(
                ctx,
                embed=discord.Embed(title="No entries", description="This contest had no entries.", color=EMBED_COLOR_ERR),
                ephemeral=True,
            )
            return

        totals = tally_votes(contest_id=contest_id)
        by_entry = totals["by_entry"]
        total_votes = totals["total"]

        # Acknowledge slash so we can post the public reveal afterwards.
        if ctx.interaction is not None and not ctx.interaction.response.is_done():
            try:
                await ctx.interaction.response.send_message("Posting results…", ephemeral=True)
            except Exception:
                pass

        max_votes = max((by_entry.get(e["id"], 0) for e in entries), default=0)
        winners = [e for e in entries if by_entry.get(e["id"], 0) == max_votes and max_votes > 0]

        # Headline (winner / tie / no-winner) shown as an H2 at the top of the card.
        if not winners:
            headline_title = "🏁 Contest Over — no votes were cast"
            color = EMBED_COLOR_INFO
        elif len(winners) == 1:
            headline_title = "🏆 We have a winner!"
            color = EMBED_COLOR_GOLD
        else:
            headline_title = "🏆 It's a tie!"
            color = EMBED_COLOR_GOLD

        # Ranked list, highest votes first; top three get medals.
        ranked = sorted(entries, key=lambda e: (-by_entry.get(e["id"], 0), e.get("entry_no") or 0))
        medals = {0: "🥇", 1: "🥈", 2: "🥉"}
        result_lines = []
        for rank, e in enumerate(ranked):
            votes = by_entry.get(e["id"], 0)
            letter = cimg.entry_label(e["entry_no"])
            title = e.get("title") or "Untitled"
            artist = e.get("artist") or "Unknown"
            url = e.get("url") or ""
            link = f"[{escape_markdown(title)}]({url})" if url else f"**{escape_markdown(title)}**"
            icon = medals.get(rank, "") if votes > 0 else ""
            medal = f"{icon} " if icon else ""
            result_lines.append(
                f"- {medal}**Entry {letter}**: {link} by {escape_markdown(artist)} "
                f"({votes} vote{'s' if votes != 1 else ''})"
            )

        description = "\n".join(result_lines)
        if len(description) > 4096:
            description = description[:4093] + "…"

        results = discord.Embed(title=headline_title, description=description, color=color)
        results.set_footer(text=f"Total votes: {total_votes}")

        results_files: list = []
        # Thumbnail: the winner's real cover on a clean win, else the contest poster.
        poster_as_thumb = False
        if len(winners) == 1 and winners[0].get("cover_url"):
            results.set_thumbnail(url=winners[0]["cover_url"])
        else:
            before = len(results_files)
            self._attach(results, cimg.resolve_poster(contest), results_files, name="contest_poster", as_thumbnail=True)
            poster_as_thumb = len(results_files) > before

        # Main image: celebration GIF on a winner/tie, else the poster, else a
        # transparent spacer so the card still matches the others' width.
        if winners:
            self._attach(results, cimg.resolve_winner_gif(contest), results_files, name="winner")
        if getattr(results.image, "url", None) is None and not poster_as_thumb:
            self._attach(results, cimg.resolve_poster(contest), results_files, name="poster_img")
        if getattr(results.image, "url", None) is None:
            self._attach(results, cimg.get_width_spacer(), results_files, name="spacer")

        await ctx.channel.send(embed=results, **self._files_kwargs(results_files))

        set_contest_status(contest_id=contest_id, status="closed")
        self.active_contests.pop(gid, None)
        await self._restore_radio(ctx)
        log.info("[contest] guild=%s results posted (total_votes=%s, winners=%s)", gid, total_votes, len(winners))

    # ------------------------------------------------------------------ audio

    @staticmethod
    async def _wait_until_idle(vc, timeout: float = 3.0) -> None:
        """Wait until the voice client is no longer playing/paused (bounded)."""
        elapsed = 0.0
        step = 0.1
        while vc and (vc.is_playing() or vc.is_paused()) and elapsed < timeout:
            await asyncio.sleep(step)
            elapsed += step

    @staticmethod
    def _suno_cdn_url(source_url: str) -> str | None:
        """Fallback playable URL: convert a suno.com/song/<uuid> page to its CDN mp3."""
        if not source_url:
            return None
        m = re.search(r"suno\.com/song/([a-f0-9\-]{36})", source_url)
        if m:
            return f"https://cdn1.suno.ai/{m.group(1)}.mp3"
        return None

    def _ffmpeg_options(self, url: str) -> dict:
        """FFmpeg options for full-song playback. Reuses the music cog's tuned
        options when available; otherwise a safe default."""
        music_cog = self._get_music_cog()
        if music_cog and hasattr(music_cog, "_build_ffmpeg_options"):
            try:
                is_hls = ".m3u8" in url or "/hls" in url.lower()
                return music_cog._build_ffmpeg_options(is_hls=is_hls)
            except Exception as e:
                log.debug("[contest] could not reuse ffmpeg options: %s", e)
        return {
            "before_options": (
                "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
            ),
            "options": "-vn",
        }


async def setup(bot):
    await bot.add_cog(Contest(bot))
