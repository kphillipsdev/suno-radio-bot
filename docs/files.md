# Subot Discord Music Bot - File Overview

This document provides a scoped overview of each file in the project, based on file names, directory structure, code analysis from semantic search, and Git status (noting deleted/untracked files). The project is a feature-rich Discord bot for music playback, queue/playlist management, Suno AI integration, and voice features, with v1 (cogs-based) and v2 (modular) implementations. Deleted files from recent changes are marked.

## Root Files

- **.cache_ggshield**: Cache file for GitGuardian security scanning tool; used for pre-commit hooks to detect secrets in code. Not core to bot functionality.

- **.gitignore**: Standard Git ignore file; excludes temporary files, caches, .env (secrets), data/ JSONs (sensitive), and build artifacts to keep repo clean.

- **dev.py**: Likely a development script for running the bot in dev mode, possibly with debugging or hot-reload features. Not explicitly detailed in search, but inferred from project context.

- **migrate_playlists.py**: Script to migrate playlists from one format/version to another, handling data from data/playlists/ JSONs. Useful for updating persistence between v1 and v2.

- **pyproject.toml**: Poetry or modern Python project configuration; defines dependencies, build settings, and metadata for the package. Replaces setup.py for modern Python projects.

- **README.md**: Project root documentation; describes the Discord Music Bot features (music playback, queues, playlists, Suno integration, TTS), setup instructions (pip install, .env config, bot token, FFmpeg), permissions, and key technologies (discord.py, yt-dlp, etc.). Includes features like persistent data and controls.

- **requirements.txt**: List of Python dependencies; includes discord.py[voice], yt-dlp, beautifulsoup4, requests, selenium for scraping, gTTS/PyObjC for TTS, and pytest for testing. Used for pip install.

- **run.py**: Main entry point script for running the v1 bot; loads bot.py and music cogs for standard Discord music playback.

- **runv2.py**: Entry point for v2 bot; runs the interactive version with src/v2/ modules, supporting advanced UI and controls. Modified in current uncommitted changes.

## data/ Directory (Persistence Layer)

Data stored as JSON per guild for queues, playlists, and user mappings; auto-saved across restarts.

- **data/guild_123.json**: Guild-specific data file for guild ID 123; stores queues, playlists, user Suno mappings. Example guild data.

- **data/guild_1408250432580747295.json**: Similar to above; for guild ID 1408250432580747295, containing persistent bot state.

- **data/playlists/**: Subdirectory for playlist JSONs.

  - **data/playlists/guild_123.json**: Playlists for guild 123; JSON array of song objects (title, URL) for named playlists like 'default', 'party'.

  - **data/playlists/guild_1408250432580747295.json**: Playlists for the other guild.

## docs/ Directory (Planning and Notes)

Documentation files for development, features, testing, and logs.

- **docs/features.md**: Outlines bot features like music playback (YouTube/SoundCloud/Suno), queue management, shuffle, volume, persistence, and suggestions for collaborative enhancements.

- **docs/note_from_grok.md**: Original monolithic bot code proposal from Grok AI; kept for reference but deprecated. Includes full example code for MusicCog, commands (!join, !play, !playlist), Suno mapping, and setup notes. Notes limitations like yt-dlp for Suno.

- **docs/plan.md**: High-level project plan; overview, status (fully implemented), timeline (3 weeks), feature summary (voice management, playback, queues, Suno scraper, persistence, testing), modular architecture, achievements, and next steps (hot reload, UI buttons).

- **docs/planv2.md**: Plan for v2 implementation; focuses on interactive bot with UI manager, playlist manager, and modular v2 components.

- **docs/tdd_testing.md**: TDD strategy for bot commands; guides test-driven development for music features.

- **docs/testing.md**: General testing documentation; covers unit/integration tests for core commands.

- **docs/todo.md**: Todo list for implementation; includes feasibility checks, modularization, scraper testing, and completed items like exporting to docs/.

- **docs/tts_audio_implementation_log.md**: Log for TTS (!test_speak) implementation; details generating/playing TTS audio in voice channels without errors.

- **docs/update_plan.md**: Updates to the project plan; likely tracks changes from v1 to v2.

## songs/ Directory

- Empty or placeholder for storing downloaded songs/audio files; not actively used in current modular setup.

## src/ Directory (Core Source Code - v1 Structure)

v1 uses cogs for modularity; some files deleted in current changes.

- **src/test_scraper.py**: Test script for Suno scraper; uses BeautifulSoup/Selenium to extract songs from user pages (e.g., https://suno.com/@huzzy).

- **src/bot.py**: Main bot setup file for v1; initializes discord.py bot, loads cogs (e.g., music), sets intents, and handles on_ready with embed. Includes help commands for Suno integration.

- **Deleted: src/bot.py** (from git status; was the entry for v1, now untracked or replaced by v2/bot.py?).

- **src/data/**:

  - **src/data/persistence.py**: Data layer for saving/loading guild data (queues, playlists, mappings) to/from JSON in data/. Handles versioning and per-guild storage.

- **Deleted: src/cogs/music/** (entire cog for music commands; modular extension for bot).

  - **Deleted: src/cogs/music/__init__.py**: Initializes the music cog package.

  - **Deleted: src/cogs/music/controls.py**: Handles playback controls (skip, stop, volume, shuffle).

  - **Deleted: src/cogs/music/main.py**: Core music commands (!play, !queue, !load_playlist, !list_playlists); manages queues, yt-dlp extraction, and UI embeds.

  - **Deleted: src/cogs/music/utils.py**: Utility functions for music (progress bars, activity updates, time formatting, Suno link generation).

- **src/utils/**:

  - **src/utils/scraper.py**: Suno web scraper; uses BeautifulSoup/Selenium to fetch user songs and extract URLs/titles for adding to playlists.

  - **src/utils/test_speak.py**: Test script for TTS (text-to-speech); implements !test_speak command using gTTS or macOS PyObjC for voice announcements.

  - **src/utils/yt_extractor.py**: YouTube/SoundCloud/Suno URL extractor; uses yt-dlp to get song info (title, URL, duration) for queues/playlists.

## src/v2/ Directory (v2 Modular Implementation)

Updated, interactive version with separate managers; untracked src/v2/bot.py.

- **src/v2/bot.py** (Untracked): v2 main bot setup; similar to src/bot.py but loads v2 modules (music_player, ui_manager, etc.). Includes help embeds and on_ready.

- **src/v2/controls.py**: v2 controls module; handles voice controls, volume, skip, etc., for interactive bot.

- **src/v2/interactive_bot.py**: Core interactive bot logic; manages user interactions, commands, and integration with v2 components. Currently visible in VSCode.

- **src/v2/music.py**: v2 music handling; extracts and plays songs, integrates with yt_extractor.

- **src/v2/music_player.py**: Dedicated music player; manages playback, queues, after-play callbacks, and activity updates (progress bars).

- **src/v2/playlist_manager.py**: Manages playlists in v2; create, add, load, list with controls (e.g., buttons for mobile).

- **src/v2/ui_manager.py**: UI components for v2; Discord embeds, buttons, views for commands like queue display, playlist selection.

## tests/ Directory

- **tests/test_music.py**: Unit/integration tests for music features; covers commands (play, queue, shuffle), mocking yt-dlp, and persistence. 3+ passing tests.

## Notes on Deleted/Untracked Files
- Deleted files (src/bot.py, src/cogs/music/*) are from v1; restoring via Git revert would recover the cog-based music system.
- Untracked: src/v2/bot.py – new v2 entry point.
- Modified files (e.g., runv2.py, src/v2/interactive_bot.py) indicate ongoing v2 development.
- This overview helps decide on Git revert: v1 cogs provide stable music core, v2 adds interactivity but may be WIP.

For more details, refer to code snippets in docs/plan.md or run tests.