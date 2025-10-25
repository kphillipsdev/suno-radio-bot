"""
Utility for generating TTS audio from text.
"""
from tempfile import NamedTemporaryFile
import asyncio
from pathlib import Path
import subprocess
import os
import logging

async def async_speak_tts(text: str):
    """
    Generate TTS audio file for the given text using native macOS TTS via PyObjC.
    Falls back to gTTS if on non-macOS or unavailable.
    Returns: (file_path, cleanup_func)
    """
    loop = asyncio.get_running_loop()
    temp_file = NamedTemporaryFile(delete=False, suffix='.aiff', prefix='tts_')
    temp_file.close()  # Close to allow NSSpeechSynthesizer to write to it

    async def generate_tts():
        try:
            from AppKit import NSSpeechSynthesizer
            import Cocoa  # For file URL
        except ImportError:
            raise ImportError("AppKit not available. Use macOS with PyObjC.")

        def _generate():
            synthesizer = NSSpeechSynthesizer.alloc().init()
            if synthesizer is None:
                raise RuntimeError("Failed to create NSSpeechSynthesizer")

            # Set voice and rate if needed
            # synthesizer.setVoice_(NSSpeechSynthesizer.availableVoices()[0])
            # synthesizer.setRate_(180.0)

            # Convert string to NSSString
            ns_string = Cocoa.NSString.stringWithString_(text)
            if ns_string is None:
                raise RuntimeError("Failed to create NSStrings")

            # Create file URL
            file_path_obj = Path(temp_file.name)
            file_url = Cocoa.NSURL.fileURLWithPath_(str(file_path_obj))

            logging.info(f"TTS: Generating AIFF file at {temp_file.name}")

            # Start speaking to file (blocking call)
            success = synthesizer.startSpeakingString_toURL_(ns_string, file_url)

            if not success:
                raise RuntimeError("NSSpeechSynthesizer failed to start speaking to file")

            # Wait for completion
            while synthesizer.isSpeaking():
                import time
                time.sleep(0.1)  # Polling to avoid busy wait issues

            # Check AIFF file
            if os.path.exists(temp_file.name):
                aiff_size = os.path.getsize(temp_file.name)
                logging.info(f"TTS: AIFF file generated, size: {aiff_size} bytes")
            else:
                logging.error("TTS: AIFF file not created")
                raise RuntimeError("TTS AIFF file not created")

            # Convert AIFF to WAV using ffmpeg
            wav_file_name = temp_file.name.replace('.aiff', '.wav')
            try:
                logging.info(f"TTS: Converting AIFF to WAV: {wav_file_name}")
                result = subprocess.run([
                    'ffmpeg', '-y', '-i', temp_file.name,
                    '-acodec', 'pcm_s16le', '-ar', '48000',
                    '-ac', '2', '-f', 'wav', wav_file_name
                ], capture_output=True, text=True, timeout=10)
                if result.returncode != 0:
                    logging.error(f"TTS: FFmpeg conversion failed: {result.stderr}")
                    raise RuntimeError(f"FFmpeg conversion failed: {result.stderr}")
                if os.path.exists(wav_file_name):
                    wav_size = os.path.getsize(wav_file_name)
                    logging.info(f"TTS: WAV file converted, size: {wav_size} bytes")
                else:
                    logging.error("TTS: WAV file not created")
                    raise RuntimeError("TTS WAV file not created")
            except subprocess.TimeoutExpired:
                logging.error("TTS: FFmpeg conversion timed out")
                raise RuntimeError("FFmpeg conversion timed out")
            except FileNotFoundError:
                logging.error("TTS: FFmpeg not found, please install ffmpeg")
                raise RuntimeError("FFmpeg not found")

            return wav_file_name

        try:
            return await loop.run_in_executor(None, _generate)
        except Exception as e:
            # Fallback to gTTS if available
            try:
                from gtts import gTTS
                file_path_obj = Path(temp_file.name)
                file_without_ext = file_path_obj.with_suffix('.mp3')
                def _gtts_fallback():
                    tts = gTTS(text=text, lang='en')
                    tts.save(str(file_without_ext))
                    return str(file_without_ext)
                return await loop.run_in_executor(None, _gtts_fallback)
            except ImportError:
                logging.error(f"TTS: AppKit generation failed: {e}, trying gTTS fallback")
                try:
                    from gtts import gTTS
                    file_path_obj = Path(temp_file.name)
                    mp3_file = file_path_obj.with_suffix('.mp3')
                    wav_file = file_path_obj.with_suffix('.wav')
                    def _gtts_fallback():
                        tts = gTTS(text=text, lang='en')
                        tts.save(str(mp3_file))
                        logging.info(f"TTS: gTTS generated MP3, size: {os.path.getsize(mp3_file)} bytes")
                        # Convert MP3 to WAV
                        result = subprocess.run([
                            'ffmpeg', '-y', '-i', str(mp3_file),
                            '-acodec', 'pcm_s16le', '-ar', '48000',
                            '-ac', '2', '-f', 'wav', str(wav_file)
                        ], capture_output=True, text=True, timeout=10)
                        if result.returncode == 0 and os.path.exists(wav_file):
                            logging.info(f"TTS: MP3 converted to WAV, size: {os.path.getsize(wav_file)} bytes")
                            return str(wav_file)
                        else:
                            logging.error(f"TTS: FFmpeg conversion failed: {result.stderr}")
                            raise RuntimeError(f"FFmpeg conversion failed: {result.stderr}")
                    return await loop.run_in_executor(None, _gtts_fallback)
                except ImportError:
                    logging.error("TTS: gTTS not available")
                    raise Exception(f"TTS generation failed: {e}. Install gTTS or run on macOS.")
                except Exception as gtts_error:
                    logging.error(f"TTS: gTTS fallback failed: {gtts_error}")
                    raise Exception(f"TTS generation failed: {e}. gTTS also failed.")

    file_path = await generate_tts()
    cleanup = lambda: os.unlink(file_path) if file_path and os.path.exists(file_path) else None
    return file_path, cleanup