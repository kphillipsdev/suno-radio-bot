# src/ui/queue_manager.py
from __future__ import annotations

from typing import Sequence, Optional, Callable, Awaitable

import discord
from discord.ext import commands
from discord.utils import escape_markdown

MAX_LINES = 15
SELECT_MAX = 25  # Discord select limit


def _fmt_duration(seconds: Optional[int | float]) -> str:
    if seconds is None:
        return "—"
    try:
        seconds = int(seconds)
    except Exception:
        return "—"
    m, s = divmod(max(0, seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def build_queue_embed(guild: discord.Guild, queue: Sequence[dict], current_page: int = 0, items_per_page: int = 12) -> discord.Embed:
    """
    Build a compact queue embed for the current guild with pagination support.
    Expects each item in queue to be a dict with (at least):
      - title / author or artist
      - requester_mention / requester_name / requester_tag / requester_id
      - optional duration (seconds or string)
    """
    if not queue:
        return discord.Embed(
            title="📋 Current Queue",
            description="Queue is empty!",
            color=0x0099FF,
        )

    # Convert to list to support slicing (deque doesn't support slicing)
    queue_list = list(queue)
    
    total_items = len(queue_list)
    total_pages = max(1, (total_items + items_per_page - 1) // items_per_page)
    current_page = min(current_page, max(0, total_pages - 1))
    
    # Calculate start and end indices for current page
    start_idx = current_page * items_per_page
    end_idx = min(start_idx + items_per_page, total_items)
    
    lines: list[str] = []
    for idx, song in enumerate(queue_list[start_idx:end_idx], start=start_idx + 1):
        title_raw = (song.get("title") or "Untitled").strip()
        title = escape_markdown(title_raw)

        artist_raw = (song.get("artist") or song.get("author") or "Unknown").strip()
        artist = escape_markdown(artist_raw)

        requester = (
            song.get("requester_mention")
            or (f"<@{song['requester_id']}>" if song.get("requester_id") else None)
            or song.get("requester_tag")
            or song.get("requester_name")
            or "someone"
        )

        dur = song.get("duration")
        if isinstance(dur, str):
            dur_str = dur
        else:
            dur_str = _fmt_duration(dur)

        lines.append(
            f"{idx}. **{title}**\n "
            f"by {artist} ({dur_str}) / Requested by {requester}"
        )

    description = "\n".join(lines) if lines else "No items on this page."
    
    embed = discord.Embed(
        title=f"📋 Queue for {guild.name}",
        description=description,
        color=0x0099FF,
    )
    
    # Add footer with page info
    if total_pages > 1:
        embed.set_footer(
            text=f"Queue Manager Panel • Page {current_page + 1} of {total_pages} • "
                 f"Showing items {start_idx + 1}-{end_idx} of {total_items}"
        )
    else:
        embed.set_footer(text=f"Queue Manager Panel • Total: {total_items} item{'s' if total_items != 1 else ''}")
    
    return embed


class QueueManagerView(discord.ui.View):
    """
    Interactive queue manager.

    - Dropdown 1: select WHICH song to act on.
    - Dropdown 2: select TARGET POSITION → immediately moves selected song.
    - Buttons:
        • ⬆ Move Up (selected)
        • ⬇ Move Down (selected)
        • 🗑 Remove Selected
        • 🔄 Refresh
    """

    def __init__(
        self,
        *,
        guild: discord.Guild,
        queue,
        invoker: discord.abc.User,
        timeout: float | None = 300.0,
        on_timeout_callback: Optional[Callable[[int], Awaitable[None]]] = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.guild = guild
        self.queue = queue          # expected: collections.deque of dicts
        self.invoker = invoker
        self.message: Optional[discord.Message] = None
        self.on_timeout_callback = on_timeout_callback

        self.selected_index: Optional[int] = None  # 0-based index in queue
        self.current_page = 0
        self.items_per_page = 12

        # dynamic selects
        self.song_select: Optional[discord.ui.Select] = None
        self.pos_select: Optional[discord.ui.Select] = None
        
        # Store references to pagination buttons
        self.previous_page_button: Optional[discord.ui.Button] = None
        self.next_page_button: Optional[discord.ui.Button] = None

        self._build_selects()
        self._update_page_buttons()

    # --- internal helpers ---------------------------------------------------

    def _is_authorized(self, user: discord.abc.User) -> bool:
        """Only allow the invoker or admins."""
        if user.id == self.invoker.id:
            return True
        if isinstance(user, discord.Member):
            perms = user.guild_permissions
            return bool(perms.administrator or perms.manage_guild)
        return False

    async def _reject(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.send_message(
                "You’re not allowed to edit this queue.", ephemeral=True
            )
        except Exception:
            pass

    def _build_selects(self) -> None:
        """Initial construction of the two selects."""
        items = list(self.queue)

        # --- Song select (which song to edit) -------------------------------
        opts_song: list[discord.SelectOption] = []
        # Get songs from current page
        start_idx = self.current_page * self.items_per_page
        end_idx = min(start_idx + self.items_per_page, len(items))
        page_items = items[start_idx:end_idx]
        
        for i, song in enumerate(page_items, start=start_idx + 1):
            title = (song.get("title") or "Untitled").strip()
            if len(title) > 80:
                title = title[:77] + "…"
            label = f"{i}. {title}"
            opts_song.append(discord.SelectOption(label=label, value=str(i - 1)))

        if opts_song:
            song_select = discord.ui.Select(
                placeholder="Select song to edit",
                min_values=1,
                max_values=1,
                options=opts_song,
                row=0,
            )

            async def song_select_cb(interaction: discord.Interaction):
                if not self._is_authorized(interaction.user):
                    return await self._reject(interaction)
                try:
                    idx0 = int(song_select.values[0])
                except Exception:
                    self.selected_index = None
                else:
                    if 0 <= idx0 < len(self.queue):
                        self.selected_index = idx0
                    else:
                        self.selected_index = None
                # Refresh the message to update button states and show selection in placeholder
                self._refresh_select_options()
                self._update_page_buttons()
                embed = build_queue_embed(self.guild, list(self.queue), self.current_page, self.items_per_page)
                await interaction.response.edit_message(embed=embed, view=self)

            song_select.callback = song_select_cb
            self.song_select = song_select
            self.add_item(song_select)

        # --- Position select (target slot, immediately moves) ---------------
        opts_pos: list[discord.SelectOption] = []
        q_len = len(items)
        for i in range(1, min(q_len, SELECT_MAX) + 1):
            opts_pos.append(
                discord.SelectOption(label=f"Move to position {i}", value=str(i - 1))
            )

        if opts_pos:
            pos_select = discord.ui.Select(
                placeholder="Select target position (moves immediately)",
                min_values=1,
                max_values=1,
                options=opts_pos,
                row=1,
            )

            async def pos_select_cb(interaction: discord.Interaction):
                # selecting a position should move the *currently selected* song
                if not self._is_authorized(interaction.user):
                    return await self._reject(interaction)

                items_local = list(self.queue)
                q_len_local = len(items_local)
                if q_len_local == 0:
                    return await interaction.response.defer(ephemeral=True)

                if self.selected_index is None:
                    try:
                        await interaction.response.send_message(
                            "Select a song to move first (top dropdown).",
                            ephemeral=True,
                        )
                    except Exception:
                        pass
                    return

                try:
                    dst = int(pos_select.values[0])
                except Exception:
                    return await interaction.response.defer(ephemeral=True)

                src = self.selected_index
                if not (0 <= src < q_len_local and 0 <= dst < q_len_local):
                    return await interaction.response.defer(ephemeral=True)

                if src == dst:
                    return await interaction.response.defer(ephemeral=True)

                track = items_local.pop(src)
                items_local.insert(dst, track)
                self.queue.clear()
                self.queue.extend(items_local)

                # update selection to the new location
                self.selected_index = dst

                await self._sync_message(interaction)

            pos_select.callback = pos_select_cb
            self.pos_select = pos_select
            self.add_item(pos_select)

    def _refresh_select_options(self) -> None:
        """Update select options to reflect current queue and size."""
        items = list(self.queue)
        q_len = len(items)

        # clamp selection if queue shrank
        if self.selected_index is not None and self.selected_index >= q_len:
            self.selected_index = None

        # --- refresh song_select -------------------------------------------
        if self.song_select is not None:
            opts_song: list[discord.SelectOption] = []
            selected_song_title = None
            
            # Get songs from current page
            start_idx = self.current_page * self.items_per_page
            end_idx = min(start_idx + self.items_per_page, len(items))
            page_items = items[start_idx:end_idx]
            
            # Check if selected song is on current page
            selected_on_page = (
                self.selected_index is not None 
                and start_idx <= self.selected_index < end_idx
            )
            
            for i, song in enumerate(page_items, start=start_idx + 1):
                title = (song.get("title") or "Untitled").strip()
                if len(title) > 80:
                    title = title[:77] + "…"
                label = f"{i}. {title}"
                opts_song.append(discord.SelectOption(label=label, value=str(i - 1)))
                
                # Check if this is the selected song
                if self.selected_index is not None and (i - 1) == self.selected_index:
                    selected_song_title = title
            
            # If selected song is not on current page, get its title for placeholder
            if not selected_on_page and self.selected_index is not None and self.selected_index < q_len:
                selected_song = items[self.selected_index]
                title_raw = (selected_song.get("title") or "Untitled").strip()
                if len(title_raw) > 100:
                    selected_song_title = title_raw[:97] + "…"
                else:
                    selected_song_title = title_raw

            if opts_song:
                self.song_select.options = opts_song
                # Update placeholder to show selected song if one is selected
                if selected_song_title:
                    self.song_select.placeholder = selected_song_title[:100]
                else:
                    self.song_select.placeholder = "Select song to edit"
            else:
                self.song_select.options = [
                    discord.SelectOption(label="(queue empty)", value="0", default=True)
                ]
                self.song_select.placeholder = "Select song to edit"

        # --- refresh pos_select --------------------------------------------
        if self.pos_select is not None:
            opts_pos: list[discord.SelectOption] = []
            for i in range(1, min(q_len, SELECT_MAX) + 1):
                opts_pos.append(
                    discord.SelectOption(
                        label=f"Move to position {i}", value=str(i - 1)
                    )
                )
            if opts_pos:
                self.pos_select.options = opts_pos
            else:
                self.pos_select.options = [
                    discord.SelectOption(label="(no slots)", value="0", default=True)
                ]

    def _update_page_buttons(self) -> None:
        """Enable/disable pagination buttons based on current page."""
        total_items = len(self.queue)
        total_pages = max(1, (total_items + self.items_per_page - 1) // self.items_per_page)
        self.current_page = min(self.current_page, max(0, total_pages - 1))
        
        # Find pagination buttons in children if not already stored
        if self.previous_page_button is None or self.next_page_button is None:
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    if item.label == "⬅️":
                        self.previous_page_button = item
                    elif item.label == "➡️":
                        self.next_page_button = item
        
        if self.previous_page_button is not None:
            self.previous_page_button.disabled = (self.current_page == 0)
        if self.next_page_button is not None:
            self.next_page_button.disabled = (self.current_page >= total_pages - 1)
    
    async def _sync_message(self, interaction: discord.Interaction) -> None:
        """Re-render the embed and refresh select options after queue changes."""
        if not self.message:
            return
        self._refresh_select_options()
        self._update_page_buttons()
        embed = build_queue_embed(self.guild, list(self.queue), self.current_page, self.items_per_page)
        await interaction.response.edit_message(embed=embed, view=self)

    # --- buttons -----------------------------------------------------------

    @discord.ui.button(label="⬆ Move Up", style=discord.ButtonStyle.primary, row=3)
    async def move_up(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not self._is_authorized(interaction.user):
            return await self._reject(interaction)

        items = list(self.queue)
        if not items:
            return await interaction.response.defer(ephemeral=True)

        # use selected song if available, otherwise last
        idx = self.selected_index if self.selected_index is not None else (len(items) - 1)
        if idx <= 0 or idx >= len(items):
            # nothing to move
            return await interaction.response.defer(ephemeral=True)

        items[idx - 1], items[idx] = items[idx], items[idx - 1]
        self.queue.clear()
        self.queue.extend(items)
        # keep selection on the moved item
        self.selected_index = idx - 1

        await self._sync_message(interaction)

    @discord.ui.button(label="⬇ Move Down", style=discord.ButtonStyle.primary, row=3)
    async def move_down(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not self._is_authorized(interaction.user):
            return await self._reject(interaction)

        items = list(self.queue)
        if not items:
            return await interaction.response.defer(ephemeral=True)

        # use selected song if available, otherwise first
        idx = self.selected_index if self.selected_index is not None else 0
        if idx < 0 or idx >= len(items) - 1:
            # nothing to move
            return await interaction.response.defer(ephemeral=True)

        items[idx], items[idx + 1] = items[idx + 1], items[idx]
        self.queue.clear()
        self.queue.extend(items)
        self.selected_index = idx + 1

        await self._sync_message(interaction)

    @discord.ui.button(label="🗑", style=discord.ButtonStyle.danger, row=3)
    async def remove_selected(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not self._is_authorized(interaction.user):
            return await self._reject(interaction)

        items = list(self.queue)
        if not items:
            return await interaction.response.defer(ephemeral=True)

        idx = self.selected_index if self.selected_index is not None else 0
        if idx < 0 or idx >= len(items):
            return await interaction.response.defer(ephemeral=True)

        try:
            items.pop(idx)
        except Exception:
            return await interaction.response.defer(ephemeral=True)

        self.queue.clear()
        self.queue.extend(items)

        # after removal, clear selection
        self.selected_index = None

        await self._sync_message(interaction)

    @discord.ui.button(label="⬅️", style=discord.ButtonStyle.secondary, row=2)
    async def previous_page_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        """Navigate to previous page."""
        if not self._is_authorized(interaction.user):
            return await self._reject(interaction)
        
        if self.current_page > 0:
            self.current_page -= 1
            await self._sync_message(interaction)
        else:
            await interaction.response.defer()
    
    @discord.ui.button(label="➡️", style=discord.ButtonStyle.secondary, row=2)
    async def next_page_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        """Navigate to next page."""
        if not self._is_authorized(interaction.user):
            return await self._reject(interaction)
        
        total_items = len(self.queue)
        total_pages = max(1, (total_items + self.items_per_page - 1) // self.items_per_page)
        if self.current_page < total_pages - 1:
            self.current_page += 1
            await self._sync_message(interaction)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, row=2)
    async def refresh(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        """Rebuild embed and dropdowns from current queue without changing it."""
        if not self._is_authorized(interaction.user):
            return await self._reject(interaction)

        await self._sync_message(interaction)

    async def on_timeout(self) -> None:
        """Called when the view times out. Clean up tracking if callback provided."""
        if self.on_timeout_callback:
            try:
                await self.on_timeout_callback(self.guild.id)
            except Exception:
                pass
        await super().on_timeout()