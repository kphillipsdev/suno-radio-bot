# src/ui/liked_songs_manager.py
"""
UI for managing user's autofill liked songs.
"""
from __future__ import annotations

from typing import Optional, Callable, Awaitable
import discord
from discord.utils import escape_markdown
from src.ui.pagination import PaginatedView
import re

def parse_song_numbers(input_text: str, max_number: int) -> tuple[list[int], list[str]]:
    """
    Parse song numbers from user input.
    Handles comma-separated, space-separated, and mixed formats.
    
    Returns:
        tuple: (valid_numbers, invalid_entries)
    """
    if not input_text or not input_text.strip():
        return [], []
    
    # Replace commas with spaces, then split by whitespace
    # This handles: "1,2,3", "1 2 3", "1, 2, 3", "1,2 3,4"
    normalized = re.sub(r'[,]+', ' ', input_text)
    parts = normalized.split()
    
    valid_numbers = []
    invalid_entries = []
    seen = set()
    
    for part in parts:
        part = part.strip()
        if not part:
            continue
        
        try:
            num = int(part)
            # Check if within valid range (1 to max_number)
            if 1 <= num <= max_number:
                if num not in seen:
                    valid_numbers.append(num)
                    seen.add(num)
            else:
                invalid_entries.append(part)
        except ValueError:
            invalid_entries.append(part)
    
    # Sort for consistent processing
    valid_numbers.sort()
    
    return valid_numbers, invalid_entries


class RemoveSongsModal(discord.ui.Modal):
    """Modal for entering song numbers to remove."""
    
    def __init__(self, view: 'LikedSongsManagerView'):
        super().__init__(title="Remove Songs from Autofill")
        self.view = view
        
        # Add text input field
        self.numbers_input = discord.ui.TextInput(
            label="Enter song numbers to remove",
            placeholder="e.g., 1,2,3 or 1 2 3",
            required=True,
            max_length=100,
        )
        self.add_item(self.numbers_input)
    
    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Handle modal submission."""
        if interaction.user.id != self.view.user.id:
            await interaction.response.send_message(
                "You can only manage your own liked songs.", ephemeral=True
            )
            return
        
        input_text = self.numbers_input.value.strip()
        max_number = len(self.view.tracks)
        
        if not input_text:
            await interaction.response.send_message(
                "❌ Please enter at least one song number.", ephemeral=True
            )
            return
        
        # Parse input
        valid_numbers, invalid_entries = parse_song_numbers(input_text, max_number)
        
        if not valid_numbers:
            if invalid_entries:
                await interaction.response.send_message(
                    f"❌ No valid song numbers found. Invalid entries: {', '.join(invalid_entries[:10])}",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"❌ No valid song numbers found. Please enter numbers between 1 and {max_number}.",
                    ephemeral=True
                )
            return
        
        # Convert global numbers to track indices (1-based to 0-based)
        track_indices = [num - 1 for num in valid_numbers]
        
        # Get track IDs to remove (in reverse order to maintain indices)
        track_ids_to_remove = []
        for idx in sorted(track_indices, reverse=True):
            if 0 <= idx < len(self.view.tracks):
                track_ids_to_remove.append(self.view.tracks[idx]["track_id"])
        
        # Remove tracks
        removed_count = 0
        failed_tracks = []
        
        for track_id in track_ids_to_remove:
            try:
                await self.view.delete_callback(str(self.view.user.id), track_id)
                removed_count += 1
            except Exception as e:
                failed_tracks.append(track_id)
        
        # Remove from local list (in reverse order to maintain indices)
        for idx in sorted(track_indices, reverse=True):
            if 0 <= idx < len(self.view.tracks):
                self.view.tracks.pop(idx)
        
        # Recalculate pagination
        self.view.total_items = len(self.view.tracks)
        self.view.total_pages = max(1, (self.view.total_items + self.view.items_per_page - 1) // self.view.items_per_page)
        
        # Adjust current page if necessary
        if self.view.current_page >= self.view.total_pages and self.view.total_pages > 0:
            self.view.current_page = self.view.total_pages - 1
        
        # Build response message
        if removed_count > 0:
            msg_parts = [f"✅ Removed {removed_count} song{'s' if removed_count != 1 else ''}!"]
            if invalid_entries:
                msg_parts.append(f"\n⚠️ Invalid entries ignored: {', '.join(invalid_entries[:10])}")
            if failed_tracks:
                msg_parts.append(f"\n❌ Failed to remove {len(failed_tracks)} song(s).")
            
            await interaction.response.send_message("\n".join(msg_parts), ephemeral=True)
            
            # Refresh the view if we have access to the message
            if hasattr(self.view, 'message') and self.view.message:
                try:
                    embed = self.view._build_embed()
                    self.view._update_delete_button()
                    self.view._update_buttons()
                    await self.view.message.edit(embed=embed, view=self.view)
                except Exception:
                    # If we can't edit the message, that's okay - user can refresh manually
                    pass
        else:
            await interaction.response.send_message(
                "❌ Failed to remove any songs.", ephemeral=True
            )


class LikedSongsManagerView(PaginatedView):
    """
    Paginated view for managing user's liked songs (autofill saves).
    
    - Numbered songs: each song is numbered globally (1, 2, 3...)
    - Remove Songs button: opens modal to remove songs by entering numbers
    - Supports bulk removal: enter multiple numbers (e.g., "1,2,3" or "1 2 3")
    - Pagination: navigate through all liked tracks
    """
    
    def __init__(
        self,
        *,
        user: discord.User,
        tracks: list[dict],  # List of track dicts from get_user_liked_tracks_all_guilds
        delete_callback: Callable[[str, str], Awaitable[None]],  # (user_id, track_id) -> None
        timeout: float | None = 300.0,
        on_timeout_callback: Optional[Callable[[int], Awaitable[None]]] = None,  # (user_id) -> None
    ):
        self.user = user
        self.tracks = tracks
        self.delete_callback = delete_callback
        self.on_timeout_callback = on_timeout_callback
        
        super().__init__(
            total_items=len(tracks),
            items_per_page=12,
            timeout=timeout,
        )
        
        self._update_delete_button()
    
    async def on_timeout(self) -> None:
        """Called when the view times out. Clean up tracking if callback provided."""
        if self.on_timeout_callback:
            try:
                await self.on_timeout_callback(self.user.id)
            except Exception:
                pass
        await super().on_timeout()
    
    def _update_delete_button(self) -> None:
        """Enable/disable remove songs button based on whether there are tracks."""
        if hasattr(self, 'remove_songs_button'):
            self.remove_songs_button.disabled = (len(self.tracks) == 0)
    
    def _build_embed(self) -> discord.Embed:
        """Build the embed for the current page."""
        if not self.tracks:
            return discord.Embed(
                title="💾 Your Autofill Saves",
                description="You haven't liked any songs yet! Use the ❤️ button on tracks to add them to your autofill.",
                color=0x0099FF,
            )
        
        # Calculate start and end indices for current page
        start_idx = self.current_page * self.items_per_page
        end_idx = min(start_idx + self.items_per_page, len(self.tracks))
        
        lines = []
        for idx, track in enumerate(self.tracks[start_idx:end_idx], start=start_idx):
            track_id = track["track_id"]
            title = (track.get("title") or "Untitled").strip()
            artist = (track.get("artist") or "Unknown Artist").strip()
            source_url = track.get("source_url") or ""
            like_count = track.get("user_like_count", 0)
            
            # Global numbering (1-based, so idx + 1)
            global_number = idx + 1
            
            # Create link if URL available
            title_escaped = escape_markdown(title)
            if source_url and source_url.startswith("http"):
                title_display = f"[**{title_escaped}**]({source_url})"
            else:
                title_display = f"**{title_escaped}**"
            
            artist_escaped = escape_markdown(artist)
            lines.append(
                f"**{global_number}.** {title_display} by {artist_escaped} ({like_count} like{'s' if like_count != 1 else ''})"
            )
        
        description = "\n".join(lines) if lines else "No items on this page."
        
        embed = discord.Embed(
            title="💾 Your Autofill Saves",
            description=description,
            color=0x0099FF,
        )
        
        # Add footer with page info
        if self.total_pages > 1:
            embed.set_footer(
                text=f"Page {self.current_page + 1} of {self.total_pages} • "
                     f"Showing items {start_idx + 1}-{end_idx} of {len(self.tracks)} • "
                     f"Total: {len(self.tracks)} track{'s' if len(self.tracks) != 1 else ''}"
            )
        else:
            embed.set_footer(
                text=f"Total: {len(self.tracks)} track{'s' if len(self.tracks) != 1 else ''}"
            )
        
        return embed
    
    async def _sync_message(self, interaction: discord.Interaction) -> None:
        """Update the message with current state."""
        # Update button states
        self._update_delete_button()
        self._update_buttons()
        # Build embed for current page
        embed = self._build_embed()
        # Update the message with refreshed view
        await interaction.response.edit_message(embed=embed, view=self)
    
    @discord.ui.button(label="🗑️ Remove Songs (Enter Numbers)", style=discord.ButtonStyle.danger, row=2)
    async def remove_songs_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        """Open modal to remove songs by entering their numbers."""
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "You can only manage your own liked songs.", ephemeral=True
            )
            return
        
        if not self.tracks:
            await interaction.response.send_message(
                "No songs to remove.", ephemeral=True
            )
            return
        
        # Open the modal
        modal = RemoveSongsModal(self)
        await interaction.response.send_modal(modal)
    
    # Override pagination buttons
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Override to refresh on page change."""
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "You can only manage your own liked songs.", ephemeral=True
            )
            return
        
        if self.current_page > 0:
            self.current_page -= 1
            await self._sync_message(interaction)
        else:
            await interaction.response.defer()
    
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Override to refresh on page change."""
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "You can only manage your own liked songs.", ephemeral=True
            )
            return
        
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            await self._sync_message(interaction)
        else:
            await interaction.response.defer()
    
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Override to refresh on refresh."""
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "You can only manage your own liked songs.", ephemeral=True
            )
            return
        
        # Update button states
        self._update_delete_button()
        self._update_buttons()
        # Build embed for current page
        embed = self._build_embed()
        # Update the message with refreshed view
        await interaction.response.edit_message(embed=embed, view=self)

