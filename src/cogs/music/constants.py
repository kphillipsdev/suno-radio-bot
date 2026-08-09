import os

# ===== Embed + Formatting Helpers ===========================================
EMBED_COLOR_PLAYING = 0x580fd6  # Purple (default/Suno)
EMBED_COLOR_ADDED   = 0xc1d4d6

# Platform-specific colors for Now Playing embeds
PLATFORM_COLORS = {
    "suno": 0xFD429C,       # Pink (Suno brand) - rgb(253, 66, 156)
    "soundcloud": 0xFF5500, # Orange (SoundCloud brand)
    "bandcamp": 0x1DA0C3,   # Teal/Cyan (Bandcamp brand)
    "mixcloud": 0x5000FF,   # Purple (Mixcloud brand)
    "audiomack": 0xFFA200,  # Orange/Gold (Audiomack brand)
    "direct": 0x2DD4BF,     # Teal (direct MP3/files)
    "default": 0x580fd6,    # Purple (fallback)
}

# Discord file upload limit (8MB for regular servers, using 7MB to be safe)
MAX_THUMBNAIL_SIZE_BYTES = 7 * 1024 * 1024  # 7MB

# Commands whose *text* messages should be auto-deleted after successful run
AUTO_DELETE_COMMANDS: set[str] = {"play", "p", "stop", "top", "history", "queue", "remove", "autofill_saves", "radio_cleanup", "request", "req"}

# ---- Startup polish & FFmpeg tuning --------------------------------------
PREBUFFER_SECONDS       = float(os.getenv("PREBUFFER_SECONDS", "0.5"))
FADE_IN_SECONDS         = float(os.getenv("FADE_IN_SECONDS", "0.5"))
FADE_IN_STEPS           = int(os.getenv("FADE_IN_STEPS", "20"))
FADE_OUT_SECONDS        = float(os.getenv("FADE_OUT_SECONDS", "1.0"))
FADE_OUT_STEPS          = int(os.getenv("FADE_OUT_STEPS", "20"))
STARTUP_ADELAY_MS       = int(os.getenv("STARTUP_ADELAY_MS", "500"))
OUTRO_APAD_MS           = int(os.getenv("OUTRO_APAD_MS", "750"))
FFMPEG_PROBESIZE        = os.getenv("FFMPEG_PROBESIZE", "8M")
FFMPEG_ANALYZEDURATION  = os.getenv("FFMPEG_ANALYZEDURATION", "5M")
FFMPEG_THREAD_QUEUE_SIZE = int(os.getenv("FFMPEG_THREAD_QUEUE_SIZE", "1024"))
FFMPEG_RW_TIMEOUT_US     = int(os.getenv("FFMPEG_RW_TIMEOUT_US", "30000000"))  # 30s
FFMPEG_MAX_DELAY_US      = int(os.getenv("FFMPEG_MAX_DELAY_US", "20000000"))
_vb = os.getenv("VOICE_BITRATE_KBPS", "").strip()
VOICE_BITRATE_KBPS: int | None = int(_vb) if _vb.isdigit() else None

# Queue/playlist clear policy toggles
CLEAR_PLAYLISTS_ON_STOP   = os.getenv("CLEAR_PLAYLISTS_ON_STOP", "0") == "1"
CLEAR_PLAYLISTS_ON_RELOAD = os.getenv("CLEAR_PLAYLISTS_ON_RELOAD", "0") == "1"

# ---- Autofill (idle radio) -------------------------------------------------
AUTOFILL_FEATURE   = os.getenv("AUTOFILL_FEATURE", "1") == "1"
AUTOFILL_DELAY_SEC = int(os.getenv("AUTOFILL_DELAY_SEC", "30"))
# Extra wait before autofill resumes after a guessing game ends (default 60s).
GAME_AUTOFILL_DELAY_SEC = int(os.getenv("GAME_AUTOFILL_DELAY_SEC", "60"))
AUTOFILL_MAX_PULL  = int(os.getenv("AUTOFILL_MAX_PULL", "50"))
DEFAULT_AUTOFILL_URL = os.getenv("DEFAULT_AUTOFILL_URL", "").strip()
DEFAULT_AUTOFILL_CSV = os.getenv("DEFAULT_AUTOFILL_CSV", "").strip()
AUTOFILL_LIKES_PER_USER = int(os.getenv("AUTOFILL_LIKES_PER_USER", "5"))
AUTOFILL_CSV_MIN_RATIO = float(os.getenv("AUTOFILL_CSV_MIN_RATIO", "0.6"))
# Size of the "fresh tail" — the most recently appended CSV rows that get
# priority in autofill selection. Designed to make /request entries surface
# within the next 1-2 autofill cycles. Set to 0 to disable prioritization.
AUTOFILL_CSV_FRESH_TAIL_SIZE = int(os.getenv("AUTOFILL_CSV_FRESH_TAIL_SIZE", "10"))

# ---- User song requests (writes to autofill CSV) --------------------------
REQUEST_MAX_PER_USER = int(os.getenv("REQUEST_MAX_PER_USER", "10"))
# Optional: restrict the !request prefix command to a single text channel.
# Empty string = no restriction. Slash /request is unaffected (ephemeral).
REQUEST_CHANNEL_ID = os.getenv("REQUEST_CHANNEL", "").strip()

# ---- Pinned radio voice channel -------------------------------------------
# When set, autofill will always play in this voice channel. If the bot is
# in a different VC (e.g. moved there by a contest listening-party), it will
# be silently moved back before autofill starts. Manual !play / !playlist
# still honor the invoker's current channel. Empty = no pin (legacy behavior:
# autofill plays wherever the bot currently is).
_rvc = os.getenv("RADIO_VC_ID", "").strip()
RADIO_VC_ID: int | None = int(_rvc) if _rvc.isdigit() else None

# Pinned text channel for radio now-playing / autofill messages. When set,
# autofill and reconnect resumes always post here — never in a contest or
# admin channel that happened to invoke the last command.
_rcc = os.getenv("RADIO_CONTROL_CHANNEL", "").strip()
RADIO_CONTROL_CHANNEL: int | None = int(_rcc) if _rcc.isdigit() else None

# ---- Requester VC check ---------------------------------------------------
SKIP_IF_REQUESTER_LEFT = os.getenv("SKIP_IF_REQUESTER_LEFT", "1") == "1"
SHOW_SKIP_MESSAGE = os.getenv("SHOW_SKIP_MESSAGE", "1") == "1"

# ---- Queue add limit (peak throttle) ---------------------------------------
QUEUE_LIMIT_DEFAULT_ENABLED = os.getenv("QUEUE_LIMIT_DEFAULT_ENABLED", "1") == "1"
QUEUE_LIMIT_MAX_PER_ADD     = int(os.getenv("QUEUE_LIMIT_MAX_PER_ADD", "200"))
QUEUE_MAX_PER_USER          = int(os.getenv("QUEUE_MAX_PER_USER", "3"))
PLAYLIST_MAX_NON_ADMIN      = int(os.getenv("PLAYLIST_MAX_NON_ADMIN", "15"))

# ---- Now Playing pruning ---------------------------------------------------
REMOVE_NP_AFTER_SONGS = int(os.getenv("REMOVE_NP_AFTER_SONGS", "1"))
REMOVE_NON_AUTOFILL_NP = os.getenv("REMOVE_NON_AUTOFILL_NP", "0") == "1"

# ---- Like button -----------------------------------------------------------
LIKE_EMOJI_NAME = "sunobotlike"
LIKE_EMOJI_ID   = 1437172794499534930
LIKE_FALLBACK   = "👍"

# ---- NP card cleanup: keep cards with reactions above this threshold -------
NP_REACTION_KEEP_THRESHOLD = 3  # keep NP cards with > this many reactions

# ---- Misc tuning -----------------------------------------------------------
AUTOFILL_RETRY_DELAY_SEC = 30       # delay before retrying empty autofill batch
AUTOFILL_DEEP_RETRY_DELAY_SEC = int(os.getenv("AUTOFILL_DEEP_RETRY_DELAY_SEC", "300"))  # long backoff (Suno hiccup, etc.)
AUTOFILL_DEEP_RETRY_MAX_ATTEMPTS = int(os.getenv("AUTOFILL_DEEP_RETRY_MAX_ATTEMPTS", "6"))  # 6 * 5min = ~30min self-heal window
AUTOFILL_LIKED_LIMIT = 1000         # max liked tracks fetched per autofill cycle
RESOLVER_MAX_WORKERS = 6            # thread pool size for track resolution
RESOLVER_TIMEOUT_SEC = 45           # timeout for batch track resolution
ADMIN_UNLIMITED_SLOTS = 10**9       # effectively unlimited queue slots for admins
PURGE_LIMIT_MIN = 1
PURGE_LIMIT_MAX = 500
RECONNECT_RETRIES = 3
RECONNECT_BASE_DELAY = 2.0
RECONNECT_JOIN_TIMEOUT = 15.0
AUTOFILL_RECALC_DEBOUNCE = 2.0     # seconds to debounce autofill recalculation

# ---- Broken-song cleanup ---------------------------------------------------
# A track is only considered "broken" when ALL of these hold:
#   1. It returned a confirmed HTTP 404/410 (`gone`) at least BROKEN_MIN_FAILURES times.
#   2. The first and last failures are at least BROKEN_MIN_AGE_HOURS apart.
#   3. The autofill batch that observed the failure had a success rate >= RESOLVER_HEALTH_MIN_SUCCESS_RATE
#      (circuit breaker — protects against Suno-wide outages emptying the DB).
BROKEN_MIN_FAILURES                 = int(os.getenv("BROKEN_MIN_FAILURES", "3"))
BROKEN_MIN_AGE_HOURS                = int(os.getenv("BROKEN_MIN_AGE_HOURS", "24"))
RESOLVER_HEALTH_MIN_SUCCESS_RATE    = float(os.getenv("RESOLVER_HEALTH_MIN_SUCCESS_RATE", "0.5"))
