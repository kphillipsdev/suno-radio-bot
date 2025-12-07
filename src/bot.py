import os
from dotenv import load_dotenv
import discord
import discord.opus  # For voice support
from discord.ext import commands
from discord import app_commands
from collections import defaultdict
import json
import asyncio
from src.data.persistence import load_data, save_data
from src.data.db import init_db


class MusicHelpCommand(commands.HelpCommand):
    """
    A compact, auto-chunking help command that respects Discord's embed limits:
      • ≤1024 chars per field
      • ≤25 fields per embed
      • Splits long cog sections and slash sections into multiple fields
      • Truncates overly long lines defensively

    Extended:
      • Hides admin-only commands from normal !help
      • Adds !help admin for admins to see admin-only commands
    """
    def __init__(self):
        super().__init__(command_attrs={
            "help": "Show help",
            "brief": "Show help",
            "usage": "help [command|category]",
            "description": "Shows available commands. Use `!help <command>` for details."
        })

        # Tunables to keep things tidy
        self._FIELD_CHAR_LIMIT = 1024
        self._MAX_FIELDS_PER_EMBED = 25
        self._LINE_CHAR_LIMIT = 140  # clamp per-line length (signature + brief)
        self._EMBED_COLOR = 0x0099FF

    # ---------- Small helpers ----------

    def _is_admin_command(self, command: commands.Command) -> bool:
        """
        Heuristic: detect commands decorated with @commands.has_permissions(administrator=True)
        by inspecting their checks' closures.
        """
        for check in getattr(command, "checks", []):
            closure = getattr(check, "__closure__", None)
            if not closure:
                continue
            for cell in closure:
                try:
                    val = cell.cell_contents
                except ValueError:
                    continue
                if isinstance(val, dict) and val.get("administrator"):
                    return True
        return False

    async def command_callback(self, ctx, *, command: str | None = None):
        """
        Special-case: `!help admin` shows admin-only commands.
        Everything else falls back to the normal HelpCommand routing.
        """
        self.context = ctx

        if command and command.lower() == "admin":
            # Only admins can see this view
            if not (ctx.guild and ctx.author.guild_permissions.administrator):
                embed = discord.Embed(
                    title="🔒 Admin Help",
                    description="You need administrator permissions to view admin-only commands.",
                    color=0xe74c3c,
                )
                await self.get_destination().send(embed=embed)
                return

            await self.send_admin_help()
            return

        # Default behaviour: !help, !help foo, !help RadioBot, etc.
        return await super().command_callback(ctx, command=command)

    def _fmt_sig(self, command: commands.Command) -> str:
        # Prefer the context's cleaned prefix; fall back to raw prefix or "!"
        ctx = getattr(self, "context", None)
        prefix = "!"
        if ctx is not None:
            prefix = getattr(ctx, "clean_prefix", None) or getattr(ctx, "prefix", None) or "!"
        signature = f"{prefix}{command.qualified_name} {command.signature}".strip()
        return f"`{signature}`"

    def _shorten(self, s: str, limit: int) -> str:
        s = (s or "—").strip()
        return s if len(s) <= limit else (s[: limit - 1] + "…")

    def _chunk_lines(self, lines, max_chars) -> list[list[str]]:
        """
        Greedy-pack lines into chunks where each chunk's joined length <= max_chars.
        Adds '\n' between lines when measuring.
        """
        chunks = []
        cur = []
        cur_len = 0
        for ln in lines:
            ln = ln.rstrip()
            add_len = len(ln) + (1 if cur else 0)  # + newline if not first
            if cur and (cur_len + add_len) > max_chars:
                chunks.append(cur)
                cur = [ln]
                cur_len = len(ln)
            else:
                if cur:
                    cur_len += 1 + len(ln)
                    cur.append(ln)
                else:
                    cur = [ln]
                    cur_len = len(ln)
        if cur:
            chunks.append(cur)
        return chunks

    async def _send_embeds_paginated(self, embeds: list[discord.Embed]):
        """
        Sends a sequence of embeds. (Minimal wrapper, but keeps callsites clean.)
        """
        dest = self.get_destination()
        for e in embeds:
            await dest.send(embed=e)

    async def send_admin_help(self):
        """
        Show only commands that require administrator permissions.
        Auto-updates based on actual registered commands.
        """
        ctx = self.context
        EMBED_COLOR = getattr(self, "_EMBED_COLOR", 0x0099FF)
        LINE_CHAR_LIMIT = getattr(self, "_LINE_CHAR_LIMIT", 140)
        FIELD_CHAR_LIMIT = getattr(self, "_FIELD_CHAR_LIMIT", 1024)

        # All prefix commands, filtered to ones the *invoker* can run
        all_commands = [c for c in ctx.bot.commands if not c.hidden and c.enabled]
        accessible = await self.filter_commands(all_commands, sort=True)

        # Only commands that actually require administrator permission
        admin_cmds = [c for c in accessible if self._is_admin_command(c)]

        if not admin_cmds:
            embed = discord.Embed(
                title="🛠 Admin Commands",
                description="No admin-only commands are currently registered.",
                color=EMBED_COLOR,
            )
            await self.get_destination().send(embed=embed)
            return

        lines: list[str] = []
        for cmd in admin_cmds:
            brief = cmd.brief or (cmd.help.splitlines()[0] if cmd.help else "—")
            line = f"• {self._fmt_sig(cmd)} — {self._shorten(brief, LINE_CHAR_LIMIT)}"
            lines.append(self._shorten(line, LINE_CHAR_LIMIT + 20))

        chunks = self._chunk_lines(lines, FIELD_CHAR_LIMIT) or [["—"]]

        embeds: list[discord.Embed] = []
        title = "🛠 Admin Commands"

        for i, chunk in enumerate(chunks, start=1):
            e = discord.Embed(
                title=title if i == 1 else f"{title} (cont. {i})",
                color=EMBED_COLOR,
                description="Commands that require administrator permissions." if i == 1 else None,
            )
            e.add_field(name="Commands", value="\n".join(chunk), inline=False)
            embeds.append(e)

        await self._send_embeds_paginated(embeds)

    async def send_bot_help(self, mapping):
        """
        Bot-level help:
          - Skips uncategorized (cog == None) so no "General" section
          - Chunks fields to <=1024 chars
          - Paginates if >25 fields
          - Includes slash commands
          - Hides admin-only commands from normal view
        """
        # ---- Local limits / styling (safe defaults if class attrs not present) ----
        FIELD_CHAR_LIMIT = getattr(self, "_FIELD_CHAR_LIMIT", 1024)
        MAX_FIELDS_PER_EMBED = getattr(self, "_MAX_FIELDS_PER_EMBED", 25)
        LINE_CHAR_LIMIT = getattr(self, "_LINE_CHAR_LIMIT", 140)
        EMBED_COLOR = getattr(self, "_EMBED_COLOR", 0x0099FF)

        # ---- Fallback helpers if the class doesn't define them ----
        def _shorten(s: str, limit: int) -> str:
            s = (s or "—").strip()
            return s if len(s) <= limit else (s[: limit - 1] + "…")

        def _fmt_sig_local(command: commands.Command) -> str:
            ctx = getattr(self, "context", None)
            prefix = "!"
            if ctx is not None:
                prefix = getattr(ctx, "clean_prefix", None) or getattr(ctx, "prefix", None) or "!"
            signature = f"{prefix}{command.qualified_name} {command.signature}".strip()
            return f"`{signature}`"

        fmt_sig = getattr(self, "_fmt_sig", _fmt_sig_local)

        def _chunk_lines(lines, max_chars) -> list[list[str]]:
            chunks = []
            cur = []
            cur_len = 0
            for ln in lines:
                ln = ln.rstrip()
                add_len = len(ln) + (1 if cur else 0)  # newline if not first in chunk
                if cur and (cur_len + add_len) > max_chars:
                    chunks.append(cur)
                    cur = [ln]
                    cur_len = len(ln)
                else:
                    if cur:
                        cur.append(ln)
                        cur_len += add_len
                    else:
                        cur = [ln]
                        cur_len = len(ln)
            if cur:
                chunks.append(cur)
            return chunks

        # ---- Embed pagination helpers ----
        embeds = []
        cur_embed = discord.Embed(
            title="🎵 Bot Help",
            description="Here’s what I can do right now. Use `!help <command>` for more details.",
            color=EMBED_COLOR,
        )
        cur_fields = 0

        def _flush_embed():
            nonlocal cur_embed, cur_fields
            if cur_fields > 0:
                embeds.append(cur_embed)
            cur_embed = discord.Embed(color=EMBED_COLOR)
            cur_fields = 0

        # ---- Prefix commands grouped by Cog (skip 'General'/None) ----
        for cog, commands_list in mapping.items():
            if cog is None:
                continue  # skip uncategorized
            cog_name = getattr(cog, "qualified_name", None)
            if not cog_name:
                continue

            # Use built-in filter_commands so users only see what they can run
            visible = await self.filter_commands(commands_list, sort=True)
            # Extra safety: skip hidden/disabled and admin-only commands here
            visible = [
                c for c in visible
                if not c.hidden and c.enabled and not self._is_admin_command(c)
            ]
            if not visible:
                continue

            lines = []
            for cmd in sorted(visible, key=lambda x: x.qualified_name):
                short = cmd.brief or (cmd.help.splitlines()[0] if cmd.help else "—")
                line = f"• {fmt_sig(cmd)} — {_shorten(short, LINE_CHAR_LIMIT)}"
                lines.append(_shorten(line, LINE_CHAR_LIMIT + 20))
                if len(lines) >= 60:
                    lines.append("…")
                    break

            chunks = _chunk_lines(lines, FIELD_CHAR_LIMIT) or [["—"]]
            for i, chunk in enumerate(chunks, start=1):
                if cur_fields >= MAX_FIELDS_PER_EMBED:
                    _flush_embed()
                field_name = cog_name if i == 1 else f"{cog_name} (cont. {i})"
                cur_embed.add_field(name=field_name, value="\n".join(chunk), inline=False)
                cur_fields += 1

        # ---- Slash (app) commands ----
        try:
            tree = self.context.bot.tree  # type: ignore[attr-defined]
            app_cmds = [c for c in tree.get_commands() if c.enabled]
        except Exception:
            app_cmds = []

        if app_cmds:
            lines = []
            for ac in sorted(app_cmds, key=lambda c: c.name):
                desc = (ac.description or "—").splitlines()[0]
                lines.append(_shorten(f"• `/{ac.name}` — {desc}", LINE_CHAR_LIMIT))
                if len(lines) >= 60:
                    lines.append("…")
                    break

            chunks = _chunk_lines(lines, FIELD_CHAR_LIMIT) or [["—"]]
            for i, chunk in enumerate(chunks, start=1):
                if cur_fields >= MAX_FIELDS_PER_EMBED:
                    _flush_embed()
                name = "Slash Commands" if i == 1 else f"Slash Commands (cont. {i})"
                cur_embed.add_field(name=name, value="\n".join(chunk), inline=False)
                cur_fields += 1

        # ---- Finalize & send ----
        if cur_fields == 0:
            cur_embed.description = "No visible commands."
            embeds.append(cur_embed)
        else:
            embeds.append(cur_embed)

        dest = self.get_destination()
        for e in embeds:
            await dest.send(embed=e)

    async def send_cog_help(self, cog: commands.Cog):
        """
        Show commands for a single cog, chunked into fields by length.
        Admin-only commands are still hidden unless you explicitly do !help admin.
        """
        all_lines = []
        for command in sorted(cog.get_commands(), key=lambda x: x.qualified_name):
            if command.hidden or not command.enabled:
                continue
            # Also hide admin-only commands from cog help, to keep consistent
            if self._is_admin_command(command):
                continue
            brief = command.brief or (command.help.splitlines()[0] if command.help else "—")
            line = f"• {self._fmt_sig(command)} — {self._shorten(brief, self._LINE_CHAR_LIMIT)}"
            all_lines.append(self._shorten(line, self._LINE_CHAR_LIMIT + 20))

        chunks = self._chunk_lines(all_lines, self._FIELD_CHAR_LIMIT) or [["—"]]

        embeds = []
        title = f"{cog.qualified_name} Commands"
        desc = (cog.__doc__.strip() if cog.__doc__ else None)
        # paginate by 25 fields if somehow huge
        batch = []
        fields_in_batch = 0

        def _flush(title_text: str):
            nonlocal batch, fields_in_batch
            if fields_in_batch == 0:
                # still send one empty embed with description
                e = discord.Embed(title=title_text, color=self._EMBED_COLOR, description=desc)
                embeds.append(e)
                return
            e = discord.Embed(title=title_text, color=self._EMBED_COLOR, description=(desc if len(embeds) == 0 else None))
            for (name, value) in batch:
                e.add_field(name=name, value=value, inline=False)
            embeds.append(e)
            batch = []
            fields_in_batch = 0

        for i, chunk in enumerate(chunks, start=1):
            name = title if i == 1 else f"{title} (cont. {i})"
            value = "\n".join(chunk)
            if fields_in_batch >= self._MAX_FIELDS_PER_EMBED:
                _flush(title)
            batch.append((name, value))
            fields_in_batch += 1
        _flush(title)

        await self._send_embeds_paginated(embeds)

    async def send_command_help(self, command: commands.Command):
        """
        Single command help; chunk subcommands list if needed.
        """
        embed = discord.Embed(
            title=f"Help: {command.qualified_name}",
            color=self._EMBED_COLOR,
            description=command.help or ""
        )
        embed.add_field(name="Usage", value=self._fmt_sig(command), inline=False)

        # Subcommands
        if isinstance(command, commands.Group) and command.commands:
            subs = []
            for sc in sorted(command.commands, key=lambda x: x.qualified_name):
                if sc.hidden or not sc.enabled:
                    continue
                desc = sc.brief or (sc.help.splitlines()[0] if sc.help else "—")
                line = f"• `{sc.qualified_name} {sc.signature}`: {self._shorten(desc, self._LINE_CHAR_LIMIT)}".strip()
                subs.append(self._shorten(line, self._LINE_CHAR_LIMIT + 20))

            # chunk subcommands into multiple fields if needed
            chunks = self._chunk_lines(subs, self._FIELD_CHAR_LIMIT) or [["—"]]
            for i, chunk in enumerate(chunks, start=1):
                name = "Subcommands" if i == 1 else f"Subcommands (cont. {i})"
                embed.add_field(name=name, value="\n".join(chunk), inline=False)

        await self.get_destination().send(embed=embed)


load_dotenv()

# NEW: initialize the SQLite DB (uses SUNO_RADIO_DB or ./suno_radio.db)
init_db(os.getenv("SUNO_RADIO_DB"))

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=MusicHelpCommand())

import logging, discord
logging.basicConfig(level=logging.INFO)                 # or DEBUG for deeper
discord.utils.setup_logging(level=logging.INFO, root=False)

# Optional: make asyncio cancellations less noisy
logging.getLogger("aiohttp.client").setLevel(logging.WARNING)
logging.getLogger("discord.gateway").setLevel(logging.INFO)


@bot.event
async def on_ready():
    embed = discord.Embed(
        title="🌟 Connection Established",
        description=f'{bot.user} is now online and ready to rock! 🎸',
        color=0x00ff00,
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="Status", value="Ready to play music! 🎵", inline=False)
    print(embed)
    # Load data for all guilds on startup
    for guild in bot.guilds:
        load_data(guild.id)

    # Ensure cog is loaded
    try:
        await bot.load_extension('src.cogs.music')
    except Exception as e:
        print(f"Failed to load music cog: {e}")

    # Ensure stat cog is loaded
    try:
        await bot.load_extension('src.cogs.stats')
    except Exception as e:
        print(f"Failed to load stats cog: {e}")

    # Voice will use Opus if available
    try:
        discord.opus.load_opus("libopus.so.0")
        if discord.opus.is_loaded():
            print("Opus loaded successfully")
        else:
            raise Exception("Opus not loaded")
    except Exception as e:
        print(f"Failed to load Opus: {e}, disabling Opus for raw PCM")
        discord.opus.load_opus(None)

    # Sync slash commands
    try:
        tree = bot.tree
        synced = await tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")


if __name__ == '__main__':
    async def main():
        await bot.start(os.getenv('BOT_TOKEN'))

    asyncio.run(main())
