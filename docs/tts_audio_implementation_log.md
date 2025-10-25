# TTS Audio Implementation Log

## Overview
This document outlines the final implementation for the `!test_speak` command in the Discord music bot, focusing on generating and playing TTS audio in voice channels without hangs or errors.

## Key Components
- **TTS Engine**: Native macOS NSSpeechSynthesizer (via PyObjC) for offline synthesis, with gTTS fallback for cross-platform compatibility.
- **Audio Format Handling**: Generates AIFF files, converts to WAV using FFmpeg for Discord compatibility.
- **Playback Mechanism**: Uses `discord.FFmpegPCMAudio` with raw PCM (Opus disabled).
- **Error Handling & Logging**: Comprehensive try-except blocks and logging for file metrics, playback status, and errors.
- **Cleanup Logic**: Async delayed cleanup (10s post-playback) to prevent FFmpeg hangs.

## Final Code Structure
- `src/utils/test_speak.py`: Core TTS generation function (`generate_tts`) with synthesis, conversion, and fallback logic.
- `src/cogs/music.py`: Bot command handler (`test_speak`) with voice client management, error embedding, and cleanup scheduling.
- `src/bot.py`: Modified to handle opus loading/fallback and updated help descriptions.

## Dependencies
- PyObjC (for macOS TTS)
- gTTS (fallback)
- discord.py[voice]
- asyncio (for non-blocking operations)
- ffmpeg (externally installed for conversion)

## Troubleshooting Resolved
- PyObjC objc import issues (order, macOS compatibility)
- FFmpeg process termination (delayed cleanup)
- OpusNotLoaded (fallback to raw PCM)
- Temp file management (async unlink)
- pyttsx3 compatibility (replaced with native TTS)

## Usage
1. Ensure the bot is in a voice channel.
2. Type `!test_speak`.
3. Bot generates "Hello, this is a test message" and plays it.
4. Command succeeds without hangs.

## Notes
- Opus is disabled if not loaded; panel still functions via raw PCM.
- Logs provide diagnostics for file size, existence, and playback errors.
- Tested on macOS with Homebrew-installed dependencies.
- Fallback ensures cross-platform TTS availability.

## Cleanup Steps
- Monitor for dependency version conflicts impacting unrelated packages.
- Implement FFmpeg error handling (via subprocess.check_call for conversion).
- Optimize file deletion for concurrent TTS events (if expanded).
- Add unit tests for TTS functions (currently isolated).

## References
- macOS NSSpeechSynthesizer documentation
- Discord.py voice client guides
- gTTS library for offline audio synthesis