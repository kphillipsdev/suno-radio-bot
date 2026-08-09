from __future__ import annotations
from collections import deque

import discord
from discord.utils import escape_markdown

from src.data.db import like_track, get_like_count, get_user_like_count
from src.ui.pagination import PaginatedView

from .constants import LIKE_EMOJI_NAME, LIKE_EMOJI_ID, LIKE_FALLBACK
from .embeds import (
    _get_platform_name,
    _track_title_link,
    _filler_badge,
    _fmt_duration,
    build_song_info_embed,
)


class LyricsButton(discord.ui.Button):
    def __init__(self, song: dict):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="View Lyrics/Prompt"
        )
        self.song = song.copy()

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.bot:
            return await interaction.response.defer(ephemeral=True)

        try:
            embed, thumb_file = build_song_info_embed(self.song)
            if thumb_file:
                await interaction.response.send_message(embed=embed, file=thumb_file, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            try:
                await interaction.response.send_message(
                    f"❌ Failed to load song information: {e}",
                    ephemeral=True
                )
            except Exception:
                pass


class LikeButton(discord.ui.Button):
    def __init__(
        self,
        track_id: str,
        guild_id: int,
        song_title: str,
        song_url: str,
        view_instance
    ):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Save for Autofill",
            custom_id="suno_like_btn"
        )
        self.track_id = track_id
        self.guild_id = guild_id
        self.song_title = song_title
        self.song_url = song_url
        self.view_instance = view_instance

        try:
            self.emoji = discord.PartialEmoji(name=LIKE_EMOJI_NAME, id=LIKE_EMOJI_ID)
        except Exception:
            self.emoji = LIKE_FALLBACK

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.bot:
            return await interaction.response.defer(ephemeral=True)

        try:
            user_id = interaction.user.id
            has_clicked_before = user_id in self.view_instance.user_clicked

            existing_user_likes = get_user_like_count(
                track_id=self.track_id, guild_id=self.guild_id, user_id=user_id
            )

            if has_clicked_before:
                if existing_user_likes > 0:
                    user_count = existing_user_likes
                    msg = f"You have already saved **{self.song_title}**! (You've added this {user_count} times)"
                    total = get_like_count(track_id=self.track_id, guild_id=self.guild_id)
                else:
                    total = like_track(
                        track_id=self.track_id, guild_id=self.guild_id, user_id=user_id,
                        username=str(interaction.user),
                    )
                    user_count = get_user_like_count(
                        track_id=self.track_id, guild_id=self.guild_id, user_id=user_id
                    )
                    msg = f"Saved to Autofill **{self.song_title}**!"
            else:
                if existing_user_likes > 0:
                    total = like_track(
                        track_id=self.track_id, guild_id=self.guild_id, user_id=user_id,
                        username=str(interaction.user),
                    )
                    user_count = get_user_like_count(
                        track_id=self.track_id, guild_id=self.guild_id, user_id=user_id
                    )
                    msg = f"Saved to Autofill **{self.song_title}** again! (You've added this {user_count} times)"
                else:
                    total = like_track(
                        track_id=self.track_id, guild_id=self.guild_id, user_id=user_id,
                        username=str(interaction.user),
                    )
                    user_count = get_user_like_count(
                        track_id=self.track_id, guild_id=self.guild_id, user_id=user_id
                    )
                    if user_count > 1:
                        msg = f"Saved to Autofill **{self.song_title}**! (You've added this {user_count} times)"
                    else:
                        msg = f"Saved to Autofill **{self.song_title}**!"

                self.view_instance.user_clicked.add(user_id)

            self.label = "Save for Autofill"

            await interaction.response.edit_message(view=self.view_instance)

            if self.song_url.startswith("http"):
                link_view = discord.ui.View()
                platform_name = _get_platform_name(self.song_url)
                link_view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, url=self.song_url, label=f"Open on {platform_name}"))
                await interaction.followup.send(msg, view=link_view, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)

        except Exception as e:
            try:
                await interaction.response.send_message(f"❌ Failed: {e}", ephemeral=True)
            except Exception:
                pass


class NowPlayingView(discord.ui.View):
    def __init__(
        self,
        *,
        song: dict,
        track_id: str | None = None,
        guild_id: int | None = None,
        bot_user_id: int | None = None,
        song_title: str | None = None,
        song_url: str | None = None,
        timeout: float | None = 3600,
    ):
        super().__init__(timeout=timeout)
        self.song = song.copy()
        self.track_id = track_id
        self.guild_id = guild_id
        self.bot_user_id = bot_user_id
        self.song_title = song_title or (song.get("title") or "Untitled")
        self.song_url = (song_url or "").strip()
        self.user_clicked = set()

        is_external_source = bool(song.get("_source"))

        if track_id and guild_id and bot_user_id and not is_external_source:
            like_btn = LikeButton(
                track_id=track_id,
                guild_id=guild_id,
                song_title=self.song_title,
                song_url=self.song_url,
                view_instance=self
            )
            self.add_item(like_btn)

        if not is_external_source:
            lyrics_btn = LyricsButton(song=self.song)
            self.add_item(lyrics_btn)


class PaginatedQueueView(PaginatedView):
    def __init__(
        self,
        *,
        queue: deque,
        eta_list: list[int | None],
        timeout: float | None = 300.0,
    ):
        self.queue = queue
        self.eta_list = eta_list
        super().__init__(
            total_items=len(queue),
            items_per_page=12,
            timeout=timeout,
        )

    def _build_embed(self) -> discord.Embed:
        queue_list = list(self.queue)
        total_items = len(queue_list)

        if total_items == 0:
            return discord.Embed(
                title="📋 Queue",
                description="Queue is empty! Add songs with `!play`.",
                color=0x0099ff
            )

        start_idx = self.current_page * self.items_per_page
        end_idx = min(start_idx + self.items_per_page, total_items)

        lines = []
        for i in range(start_idx, end_idx):
            song = queue_list[i]
            song_num = i + 1
            eta_sec = self.eta_list[i] if i < len(self.eta_list) else None

            title_link = _track_title_link(song) + _filler_badge(song)
            artist_raw = (song.get("artist") or song.get("author") or "Unknown Artist").strip()
            artist = escape_markdown(artist_raw)
            requester = (
                song.get("requester_mention")
                or (f"<@{song['requester_id']}>" if song.get("requester_id") else None)
                or song.get("requester_tag")
                or song.get("requester_name")
                or ""
            )

            if eta_sec is None:
                eta_str = "≈unknown"
            else:
                eta_str = _fmt_duration(max(0, int(eta_sec)))

            lines.append(
                f"**{song_num}.** {title_link} by {artist}\n"
                f"   Up in ~{eta_str} / Requested by {requester}"
            )

        description = "\n".join(lines) if lines else "No items on this page."

        embed = discord.Embed(
            title="📋 Current Queue",
            description=description,
            color=0x0099ff
        )

        if self.total_pages > 1:
            embed.set_footer(
                text=f"Page {self.current_page + 1} of {self.total_pages} • "
                     f"Showing items {start_idx + 1}-{end_idx} of {total_items}"
            )
        else:
            embed.set_footer(text=f"Total: {total_items} item{'s' if total_items != 1 else ''}")

        return embed
