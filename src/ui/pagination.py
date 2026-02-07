# src/ui/pagination.py
"""
Reusable pagination components for Discord bot views.
"""
from __future__ import annotations

from typing import Optional
import discord


class PaginatedView(discord.ui.View):
    """
    Base class for paginated Discord views.
    Provides Previous/Next/Refresh buttons and page management.
    """
    
    def __init__(
        self,
        *,
        total_items: int,
        items_per_page: int = 12,
        timeout: float | None = 300.0,
    ):
        super().__init__(timeout=timeout)
        self.total_items = total_items
        self.items_per_page = items_per_page
        self.current_page = 0
        self.message: Optional[discord.Message] = None
        
        # Calculate total pages
        self.total_pages = max(1, (total_items + items_per_page - 1) // items_per_page)
        # Clamp current_page to valid range
        self.current_page = min(self.current_page, max(0, self.total_pages - 1))
        
        self._update_buttons()
    
    def _update_buttons(self):
        """Enable/disable buttons based on current page."""
        self.previous_button.disabled = (self.current_page == 0)
        self.next_button.disabled = (self.current_page >= self.total_pages - 1)
    
    def _build_embed(self) -> discord.Embed:
        """
        Build the embed for the current page.
        Must be implemented by subclasses.
        """
        raise NotImplementedError("Subclasses must implement _build_embed()")
    
    @discord.ui.button(label="⬅️", style=discord.ButtonStyle.secondary, row=0)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            self._update_buttons()
            embed = self._build_embed()
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.defer()
    
    @discord.ui.button(label="➡️", style=discord.ButtonStyle.secondary, row=0)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self._update_buttons()
            embed = self._build_embed()
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.defer()
    
    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.primary, row=0)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Rebuild embed to show current state."""
        embed = self._build_embed()
        self._update_buttons()
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def send(self, channel: discord.abc.Messageable) -> discord.Message:
        """Send the initial paginated message."""
        embed = self._build_embed()
        self._update_buttons()
        self.message = await channel.send(embed=embed, view=self)
        return self.message

