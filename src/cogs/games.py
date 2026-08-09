import discord
import logging
import re
from collections import Counter
from discord.ext import commands
import asyncio
import random
from src.data.db import get_all_guild_tracks
from src.cogs.music.constants import GAME_AUTOFILL_DELAY_SEC
from src.cogs.music import build_song_info_embed

log = logging.getLogger(__name__)

class GuessButton(discord.ui.Button):
    def __init__(self, label: str, custom_id: str):
        super().__init__(style=discord.ButtonStyle.primary, label=label, custom_id=custom_id)

    async def callback(self, interaction: discord.Interaction):
        view: GuessView = self.view
        if interaction.user.id in view.guesses:
            await interaction.response.send_message("❌ You already guessed!", ephemeral=True)
            return
        
        view.guesses[interaction.user.id] = int(self.custom_id)
        
        # Acknowledge without sending a message
        await interaction.response.defer(ephemeral=True)
        
        # Update embed with total vote count
        try:
            embed = interaction.message.embeds[0]
            total_votes = len(view.guesses)
            
            # Extract base title "Round X/Y" and append vote count
            original_title = embed.title.split('•')[0].strip()
            embed.title = f"{original_title} • Votes: {total_votes}"
            
            await interaction.message.edit(embed=embed)
        except Exception:
            pass

class GuessView(discord.ui.View):
    def __init__(self, options: list[dict], correct_url: str, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.guesses = {}  # user_id -> option_index
        self.options = options
        self.correct_url = correct_url
        
        for idx, song in enumerate(options):
            btn = GuessButton(label=str(idx + 1), custom_id=str(idx))
            self.add_item(btn)

    async def on_timeout(self):
        self.stop()

class Games(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.current_games = {}

    def is_game_active(self, guild_id: int) -> bool:
        """Check if a game is currently running in this guild."""
        return guild_id in self.current_games

    @staticmethod
    async def _wait_until_idle(vc, timeout: float = 3.0) -> None:
        """Wait until the voice client is no longer playing/paused (bounded)."""
        elapsed = 0.0
        step = 0.1
        while vc and (vc.is_playing() or vc.is_paused()) and elapsed < timeout:
            await asyncio.sleep(step)
            elapsed += step

    def _arm_game_block(self, gid: int) -> None:
        """Mark the game active and pause the radio BEFORE stopping playback."""
        self.current_games[gid] = True
        music_cog = self.bot.get_cog("Music")
        if not music_cog:
            return
        try:
            if hasattr(music_cog, "_cancel_autofill_task"):
                music_cog._cancel_autofill_task(gid)
            if hasattr(music_cog, "_clear_now_playing_if_guild"):
                music_cog._clear_now_playing_if_guild(gid)
        except Exception as e:
            log.debug("[game] arm block failed: %s", e)

    def _disarm_game_block(self, gid: int) -> None:
        self.current_games.pop(gid, None)

    def _resolve_audio_url(self, source_url: str) -> str | None:
        """Convert a source_url to a playable CDN audio URL. Returns None if not possible."""
        if not source_url:
            return None

        # Already a CDN link
        if "cdn" in source_url and source_url.endswith(".mp3"):
            return source_url

        # Convert suno.com/song/<uuid> page URL to CDN link
        if "suno.com/song/" in source_url:
            match = re.search(r"suno\.com/song/([a-f0-9\-]{36})", source_url)
            if match:
                uuid = match.group(1)
                cdn_url = f"https://cdn1.suno.ai/{uuid}.mp3"
                log.debug("Converted page URL to CDN: %s", cdn_url)
                return cdn_url

        return None

    async def _restore_music_after_game(self, ctx) -> None:
        """Reset music cog state and resume playback/autofill after a game ends."""
        music_cog = self.bot.get_cog("Music")
        if not music_cog:
            return
        try:
            if hasattr(music_cog, "_clear_now_playing_if_guild"):
                music_cog._clear_now_playing_if_guild(ctx.guild.id)
            else:
                music_cog.current_song = None
                music_cog.song_start_time = None
            if hasattr(music_cog, 'update_song_activity') and music_cog.update_song_activity.is_running():
                music_cog.update_song_activity.stop()
            await self.bot.change_presence(activity=None)
            if hasattr(music_cog, "_cancel_autofill_task"):
                music_cog._cancel_autofill_task(ctx.guild.id)
            else:
                music_cog.auto_play_tasks[ctx.guild.id] = None

            if ctx.voice_client and ctx.voice_client.is_playing():
                ctx.voice_client.stop()

            if music_cog.queues.get(ctx.guild.id):
                await music_cog.play_next(ctx)
            elif hasattr(music_cog, "_schedule_autofill_if_idle"):
                log.info(
                    "[Game End] scheduling autofill in %ss for guild %s",
                    GAME_AUTOFILL_DELAY_SEC,
                    ctx.guild.id,
                )
                music_cog._schedule_autofill_if_idle(ctx, delay=GAME_AUTOFILL_DELAY_SEC)
            else:
                log.warning("[Game End] music cog has no autofill scheduler")
        except Exception as e:
            log.error("[Game End] Failed to trigger music: %s", e)

    @commands.command(name="startgame", aliases=["guess", "quiz"])
    async def start_game(self, ctx, *args):
        """
        Start a 'Guess the Song' game!
        Usage:
          !startgame [rounds]          -> Guess the Song (default)
          !startgame artist [rounds]   -> Guess the Artist
        """
        # Defaults
        mode = "song"
        rounds = 5
        
        # Parse args
        for arg in args:
            arg_str = str(arg).lower()
            if arg_str.isdigit():
                rounds = int(arg_str)
            elif arg_str in ["artist", "author", "band"]:
                mode = "artist"
            elif arg_str in ["song", "title", "track"]:
                mode = "song"
        
        if ctx.guild.id in self.current_games:
            await ctx.send("A game is already in progress!")
            return

        if not ctx.author.voice:
            await ctx.send("❌ You must be in a voice channel to play!")
            return

        gid = ctx.guild.id
        vc = ctx.voice_client
        if not vc:
            try:
                vc = await ctx.author.voice.channel.connect()
            except Exception as e:
                await ctx.send(f"❌ Could not connect to voice: {e}")
                return

        # Block the radio BEFORE stopping playback so after_playing doesn't
        # sneak in an autofill/radio song during game setup.
        self._arm_game_block(gid)
        if vc.is_playing() or vc.is_paused():
            vc.stop()
            await self._wait_until_idle(vc)

        # Fetch songs from DB (grab from all history to avoid repetition)
        songs = get_all_guild_tracks(guild_id=gid, limit=1000)

        # Filter valid songs: must have a playable CDN URL and a title, deduplicate
        seen_urls = set()
        valid_songs = []
        for s in songs:
            url = s.get('source_url')
            title = s.get('title')
            artist = s.get('artist')
            audio_url = self._resolve_audio_url(url) if url else None

            # Basic validation
            if not (audio_url and title and audio_url not in seen_urls):
                continue

            # Mode-specific validation
            if mode == "artist":
                if not artist or artist.lower() in ["unknown", "unknown artist", ""]:
                    continue

            s['_audio_url'] = audio_url
            # Ensure thumbnail key is present for build_song_info_embed
            if 'cover_url' in s and not s.get('thumbnail'):
                s['thumbnail'] = s['cover_url']
            valid_songs.append(s)
            seen_urls.add(audio_url)

        if len(valid_songs) < 4:
            self._disarm_game_block(gid)
            await ctx.send(f"❌ Not enough unique songs with valid data to start a {mode} game! (Need at least 4)")
            return

        # Shuffle valid songs to randomize selection and prevent duplicates
        random.shuffle(valid_songs)

        scores = {}

        # Create starting embed
        game_title = "Guess the Artist!" if mode == "artist" else "Guess the Song!"
        start_embed = discord.Embed(
            title=f"🎮 Starting {game_title}",
            description=f"Get ready for **{rounds} rounds** of music trivia!",
            color=0x00FF00  # Green
        )
        
        instructions = "1. Listen to the 30-second song snippet.\n"
        if mode == "artist":
            instructions += "2. Look at the numbered list of **Artists**.\n"
        else:
            instructions += "2. Look at the numbered list of **Song Titles**.\n"
        instructions += "3. Click the matching number button to guess!"
        
        start_embed.add_field(
            name="How to Play", 
            value=instructions,
            inline=False
        )
        start_embed.set_footer(text="Game starting in 3 seconds...")
        
        await ctx.send(embed=start_embed)
        await asyncio.sleep(3)

        try:
            for round_num in range(1, rounds + 1):
                if ctx.guild.id not in self.current_games:
                    break

                # Ensure we have enough songs left for a round (1 correct + 3 distractors)
                if len(valid_songs) < 4:
                    await ctx.send("Not enough unique songs left! Ending game early.")
                    break

                # Pop the correct song so it can't be reused as a correct answer
                correct_song = valid_songs.pop(0)
                
                # Pick 3 distractors
                if mode == "artist":
                    # For artist mode, distractors must have DIFFERENT artists
                    correct_artist = correct_song.get('artist')
                    possible_distractors = [
                        s for s in valid_songs 
                        if s.get('artist') != correct_artist
                    ]
                    
                    # Ensure we have enough unique artists
                    unique_distractors = []
                    seen_artists = {correct_artist}
                    
                    random.shuffle(possible_distractors)
                    for s in possible_distractors:
                        a = s.get('artist')
                        if a not in seen_artists:
                            unique_distractors.append(s)
                            seen_artists.add(a)
                        if len(unique_distractors) >= 3:
                            break
                            
                    if len(unique_distractors) < 3:
                        await ctx.send("Not enough unique artists left for distractors! Ending game.")
                        break
                        
                    distractors = unique_distractors
                else:
                    # Song mode: any other song is a valid distractor
                    if len(valid_songs) < 3:
                        await ctx.send("Not enough songs left for distractors! Ending game.")
                        break
                    distractors = random.sample(valid_songs, 3)

                options = distractors + [correct_song]
                random.shuffle(options)

                # Calculate start time (30% into the song)
                start_time = 0
                duration = correct_song.get('duration')
                if duration:
                    try:
                        start_time = int(float(duration) * 0.3)
                    except (ValueError, TypeError):
                        pass

                # FFmpeg options with fade in/out and 30s clip starting at 30% in
                ffmpeg_opts = {
                    'before_options': (
                        '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 '
                        '-nostdin'
                    ),
                    'options': (
                        f'-vn -ss {start_time} -t 30 -probesize 10M -analyzeduration 10M '
                        '-af "aresample=async=1000:min_hard_comp=0.01:first_pts=0,'
                        'afade=t=in:ss=0:d=1,afade=t=out:st=29:d=1"'
                    )
                }

                try:
                    vc.stop()
                    await asyncio.sleep(0.5)
                    
                    audio_url = correct_song.get('_audio_url')
                    if not audio_url:
                        log.warning("Skipping song with no audio URL: %s", correct_song.get('title'))
                        continue

                    source = discord.FFmpegPCMAudio(audio_url, **ffmpeg_opts)
                    source = discord.PCMVolumeTransformer(source, volume=0.5)
                    
                    def after_play(e):
                        if e:
                            log.error("Game audio error: %s", e)
                            
                    vc.play(source, after=after_play)
                except Exception as e:
                    await ctx.send(f"Error playing snippet: {e}")
                    continue

                view = GuessView(options, correct_song['source_url'], timeout=30.0)
                
                # Build options text
                options_text = ""
                for idx, song in enumerate(options):
                    if mode == "artist":
                        label = (song.get('artist') or "Unknown Artist")[:80]
                    else:
                        label = (song.get('title') or "Unknown Title")[:80]
                    options_text += f"**{idx + 1}.** {label}\n"

                embed = discord.Embed(
                    title=f"Round {round_num}/{rounds} • Votes: 0",
                    description=f"🎵 **{game_title}**\nListen to the snippet and click the matching number below.\n\n{options_text}",
                    color=0x0099ff
                )
                embed.set_footer(text="You have 30 seconds!")
                
                msg = await ctx.send(embed=embed, view=view)

                await asyncio.sleep(30)
                
                try:
                    if vc.is_connected():
                        vc.stop()
                except Exception:
                    pass
                view.stop()
                
                # Tally results
                round_guesses = view.guesses
                round_winners = []
                
                for user_id, guess_idx in round_guesses.items():
                    guessed_song = options[guess_idx]
                    if guessed_song['source_url'] == correct_song['source_url']:
                        scores[user_id] = scores.get(user_id, 0) + 1
                        round_winners.append(user_id)

                vote_counts = Counter(round_guesses.values())
                stats_desc = "**Votes:**\n"
                for idx, song in enumerate(options):
                    count = vote_counts.get(idx, 0)
                    is_correct = song['source_url'] == correct_song['source_url']
                    marker = "✅" if is_correct else "❌"
                    
                    if mode == "artist":
                        label = song.get('artist')
                    else:
                        label = song.get('title')
                        
                    stats_desc += f"{marker} **{label}**: {count} votes\n"
                
                if round_winners:
                    winner_mentions = ", ".join([f"<@{uid}>" for uid in round_winners])
                    stats_desc += f"\n**Correct Guessers (+1 pt):** {winner_mentions}"
                else:
                    stats_desc += "\n**Correct Guessers:** None 😢"

                # Show result with song info card (no lyrics)
                try:
                    result_embed, thumb_file = build_song_info_embed(correct_song, include_lyrics=False)
                    
                    if mode == "artist":
                        result_embed.title = f"⏰ Time's Up! The artist was: {correct_song.get('artist')}"
                    else:
                        result_embed.title = "⏰ Time's Up! The song was:"
                        
                    result_embed.add_field(name="📊 Round Results", value=stats_desc, inline=False)
                    
                    if thumb_file:
                        await ctx.send(embed=result_embed, file=thumb_file)
                    else:
                        await ctx.send(embed=result_embed)
                except Exception as e:
                    log.error("Error building song info embed for game: %s", e)
                    fallback_embed = discord.Embed(
                        title="Time's Up!",
                        description=f"The song was: **{correct_song['title']}** by {correct_song.get('artist', 'Unknown')}\n\n{stats_desc}",
                        color=0x00ff00
                    )
                    await ctx.send(embed=fallback_embed)
                
                # Update buttons to show correct/incorrect
                for child in view.children:
                    if isinstance(child, discord.ui.Button):
                        child.disabled = True
                        idx = int(child.custom_id)
                        is_correct = options[idx]['source_url'] == correct_song['source_url']
                        
                        if is_correct:
                            child.style = discord.ButtonStyle.success
                        elif list(round_guesses.values()).count(idx) > 0:
                            child.style = discord.ButtonStyle.danger
                        else:
                            child.style = discord.ButtonStyle.secondary
                
                try:
                    await msg.edit(view=view)
                except Exception:
                    pass

                await asyncio.sleep(5)

            # Game Over
            if ctx.guild.id in self.current_games:
                if scores:
                    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
                    
                    # Build leaderboard string
                    lines = []
                    winner_id = sorted_scores[0][0]
                    winner_pts = sorted_scores[0][1]
                    
                    for uid, pts in sorted_scores:
                        lines.append(f"• <@{uid}> ({pts} pts)")
                    
                    leaderboard_text = "\n".join(lines)
                    
                    embed = discord.Embed(
                        title="🎮 Game Finished! Final Results",
                        description=leaderboard_text,
                        color=0xFFD700  # Gold
                    )
                    
                    embed.add_field(
                        name=f"🏆 Winner",
                        value=f"**<@{winner_id}> is the winner!**",
                        inline=False
                    )
                    
                    embed.set_footer(text="Thank you for playing!")
                    await ctx.send(embed=embed)
                else:
                    await ctx.send("❌ Game Over! No one scored any points.")

        finally:
            if vc and vc.is_connected():
                vc.stop()
                await asyncio.sleep(1)
            
            if ctx.guild.id in self.current_games:
                del self.current_games[ctx.guild.id]
            
            await self._restore_music_after_game(ctx)

    @commands.command(name="stopgame", aliases=["stopguess"])
    async def stop_game(self, ctx):
        """Stop the current guessing game."""
        if ctx.guild.id in self.current_games:
            if ctx.voice_client:
                ctx.voice_client.stop()
                await asyncio.sleep(1)
                
            if ctx.guild.id in self.current_games:
                del self.current_games[ctx.guild.id]
            
            await ctx.send("🛑 Game stopped.")
            await self._restore_music_after_game(ctx)
        else:
            await ctx.send("No game is currently running.")

async def setup(bot):
    await bot.add_cog(Games(bot))
