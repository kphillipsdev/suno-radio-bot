# ---------------------------------------------------------------------------
# FILE: src/cogs/stats.py
# ---------------------------------------------------------------------------
from __future__ import annotations
import discord
from discord import app_commands
from discord.ext import commands
from typing import Literal
import datetime
from discord.utils import escape_markdown
from src.data.db import recent_plays, top_tracks
from src.data.db import get_conn

# ===== Embed + Formatting Helpers ===========================================
EMBED_COLOR_STATS = 0xfeb236
EMBED_COLOR_ERROR   = 0x8d9db6
RANGE_TO_SECONDS = {
    "day": 24 * 3600,
    "week": 7 * 24 * 3600,
    "month": 30 * 24 * 3600,
    "all": None,
}

def _dt_from_epoch(sec: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(sec, tz=datetime.timezone.utc)

def _title_link(title: str | None, url: str | None) -> str:
    t = escape_markdown((title or "Untitled").strip())
    u = (url or "").strip()
    if u.startswith("http"):
        return f"**[{t}]({u})**"
    return f"**{t}**"

def _embed_recent(rows):
    import discord, datetime
    if not rows:
        return discord.Embed(title="⏯️ Recent Plays", description="No history yet.", color=EMBED_COLOR_ERROR)
    lines = []
    for r in rows:
        started = datetime.datetime.fromtimestamp(int(r["started_at"]), tz=datetime.timezone.utc)
        when = discord.utils.format_dt(started, style="R")
        artist = f"by {r['artist']}" if r.get("artist") else ""
        link = _title_link(r.get("title") or r.get("track_id"), r.get("source_url"))
        lines.append(f"- {link} {artist} at {when}")
    return discord.Embed(title="⏯️ Recent Plays", description="\n".join(lines), color=EMBED_COLOR_STATS)

def _embed_top(range_label, rows):
    import discord
    if not rows:
        return discord.Embed(title=f"✨ Top Tracks ({range_label})", description="No plays in that range.", color=EMBED_COLOR_ERROR)
    lines = []
    for i, r in enumerate(rows, start=1):
        artist = f"{r['artist']}" if r.get("artist") else ""
        link = _title_link(r.get("title") or r.get("track_id"), r.get("source_url"))
        plays = r["plays"]
        lines.append(f"{i}. **{link}** by {artist} *({plays} unique plays)*")
    return discord.Embed(title=f"✨ Top Tracks ({range_label})", description="\n".join(lines), color=EMBED_COLOR_STATS)

class Stats(commands.Cog):
    """History and Top commands for Suno Radio."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _prune_orphan_tracks(self) -> int:
        """Delete tracks that have no remaining plays. Returns rows deleted."""
        conn = get_conn()
        cur = conn.execute("""
            DELETE FROM tracks
            WHERE id NOT IN (SELECT DISTINCT track_id FROM plays)
        """)
        return cur.rowcount or 0

    @app_commands.command(name="history", description="Show recent radio plays for this server")
    @app_commands.describe(limit="How many rows (default 10, max 50)")
    async def history(self, interaction: discord.Interaction, limit: int = 10):
        limit = max(1, min(50, limit))
        rows = recent_plays(guild_id=interaction.guild_id, limit=limit)
        if not rows:
            await interaction.response.send_message("No history yet.")
            return

        lines = []
        for r in rows:
            started = _dt_from_epoch(int(r["started_at"]))
            when = discord.utils.format_dt(started, style="R")
            artist = f"by {r['artist']}" if r.get("artist") else ""
            link = _title_link(r.get("title") or r.get("track_id"), r.get("source_url"))
            lines.append(f"- {link} {artist} at {when}")


        embed = discord.Embed(title="⏯️ Recent Plays", description="\n".join(lines), color = EMBED_COLOR_STATS)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="top", description="Top played tracks for this server")
    @app_commands.describe(range="Time window", limit="How many rows (default 10, max 25)")
    async def top(self, interaction: discord.Interaction, range: Literal["day", "week", "month", "all"] = "week", limit: int = 10):
        limit = max(1, min(25, limit))
        secs = RANGE_TO_SECONDS[range]
        rows = top_tracks(guild_id=interaction.guild_id, since_seconds=secs, limit=limit)
        if not rows:
            await interaction.response.send_message("No plays in that range.")
            return

        lines = []
        for i, r in enumerate(rows, start=1):
            artist = f"by {r['artist']}" if r.get("artist") else ""
            link = _title_link(r.get("title") or r.get("track_id"), r.get("source_url"))
            lines.append(f"{i}. **{link}** {artist} *({plays} plays)*")

        title = f"✨ Top Tracks ({range})"
        embed = discord.Embed(title=title, description="\n".join(lines), color = EMBED_COLOR_STATS)
        await interaction.response.send_message(embed=embed)

    @commands.command(name="history", help="Show recent radio plays for this server")
    async def history_bang(self, ctx: commands.Context, limit: int = 10):
        limit = max(1, min(50, int(limit)))
        rows = recent_plays(guild_id=ctx.guild.id, limit=limit)
        await ctx.send(embed=_embed_recent(rows))

    @commands.command(name="top", help="Top played tracks for this server")
    async def top_bang(self, ctx: commands.Context, range: str = "week", limit: int = 10):
        # accept: day/week/month/all (case-insensitive)
        rng = str(range).lower()
        if rng not in ("day", "week", "month", "all"):
            rng = "week"
        limit = max(1, min(25, int(limit)))
        secs = {
            "day": 24 * 3600,
            "week": 7 * 24 * 3600,
            "month": 30 * 24 * 3600,
            "all": None,
        }[rng]
        rows = top_tracks(guild_id=ctx.guild.id, since_seconds=secs, limit=limit)
        await ctx.send(embed=_embed_top(rng, rows))

    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.command(name="history_clear", description="Admin: clear play history")
    @app_commands.describe(scope="What to clear: 'guild' (this server) or 'all' (everything)")
    async def history_clear(self, interaction: discord.Interaction, scope: str = "guild"):
        scope = (scope or "guild").lower()
        conn = get_conn()
        if scope not in ("guild", "all"):
            await interaction.response.send_message(
                "Invalid scope. Use `guild` or `all`.", ephemeral=True
            )
            return

        try:
            if scope == "guild":
                cur = conn.execute("DELETE FROM plays WHERE guild_id = ?", (str(interaction.guild_id),))
                pruned = self._prune_orphan_tracks()
                await interaction.response.send_message(
                    f"✅ Cleared **{cur.rowcount or 0}** play(s) for this server. "
                    f"Pruned **{pruned}** orphan track(s).",
                    ephemeral=True
                )
            else:  # all
                conn.execute("DELETE FROM plays")
                cur2 = conn.execute("DELETE FROM tracks")
                await interaction.response.send_message(
                    f"🧨 Cleared ALL history: **plays** and **tracks** (deleted {cur2.rowcount or 0} track rows).",
                    ephemeral=True
                )
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed to clear history: {e}", ephemeral=True)

    # --- Prefix twin ---
    @commands.has_permissions(administrator=True)
    @commands.command(name="history_clear", help="Admin: clear play history. Usage: !history_clear [guild|all]")
    async def history_clear_bang(self, ctx: commands.Context, scope: str = "guild"):
        scope = (scope or "guild").lower()
        conn = get_conn()
        try:
            if scope == "guild":
                cur = conn.execute("DELETE FROM plays WHERE guild_id = ?", (str(ctx.guild.id),))
                pruned = self._prune_orphan_tracks()
                await ctx.send(
                    embed=discord.Embed(
                        title="✅ History Cleared",
                        description=f"Removed **{cur.rowcount or 0}** play(s) for this server.\n"
                                    f"Pruned **{pruned}** orphan track(s).",
                        color=0x2ecc71
                    )
                )
            elif scope == "all":
                conn.execute("DELETE FROM plays")
                cur2 = conn.execute("DELETE FROM tracks")
                await ctx.send(
                    embed=discord.Embed(
                        title="🧨 All History Cleared",
                        description=f"Removed ALL plays and **{cur2.rowcount or 0}** track row(s).",
                        color=0xe74c3c
                    )
                )
            else:
                await ctx.send("Invalid scope. Use `guild` or `all`.")
        except Exception as e:
            await ctx.send(f"❌ Failed to clear history: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(Stats(bot))