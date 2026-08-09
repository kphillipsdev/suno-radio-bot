"""
Visual helpers for anonymous song contests.

Provides three things the contest cog uses to make cards look nicer:

  * A configurable contest *label* (falls back to the contest name, then an env
    default) instead of a hard-coded "Anonymous Contest".
  * A 10-color palette cycled per entry so each now-playing card is a different
    color.
  * Server-hosted images: a poster for the "listening party starting" card and a
    default entry thumbnail. Images can be swapped per contest either by dropping
    a name-based file (``images/contest/<slug>_poster.png``) or by overwriting the
    env-configured default (``CONTEST_POSTER_PATH``).
  * Blurred, "?"-stamped versions of the real Suno cover art, generated with
    Pillow, so entry cards get a "mystery cover" thumbnail without revealing the
    song.

All of this is best-effort: if a file is missing or Pillow is unavailable the
helpers just return ``None`` and the cog falls back to plain embeds.
"""
from __future__ import annotations

import hashlib
import logging
import os
import random
import re
from typing import Optional

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- config

# Text shown on the contest cards when a contest has no name of its own.
CONTEST_LABEL = os.getenv("CONTEST_LABEL", "Anonymous Contest")

# Folder that holds server-hosted contest art. Drop files here and swap them per
# contest. Name-based files take precedence over the env defaults below.
CONTEST_IMAGE_DIR = os.getenv("CONTEST_IMAGE_DIR", "images/contest")

# Default images used when a contest has no name-specific file.
CONTEST_POSTER_PATH = os.getenv(
    "CONTEST_POSTER_PATH", os.path.join(CONTEST_IMAGE_DIR, "poster.png")
)
CONTEST_ENTRY_THUMB_PATH = os.getenv(
    "CONTEST_ENTRY_THUMB_PATH", os.path.join(CONTEST_IMAGE_DIR, "entry.png")
)

# Celebration / hype GIFs for the voting + winner cards. Each has a single default
# file, plus an optional pool directory: drop several GIFs in the *_GIF_DIR folder
# and one is picked at random each time (fun variety). Name-based per-contest
# overrides (<slug>_voting.* / <slug>_winner.*) take precedence over both.
CONTEST_VOTING_GIF = os.getenv(
    "CONTEST_VOTING_GIF", os.path.join(CONTEST_IMAGE_DIR, "voting.gif")
)
CONTEST_WINNER_GIF = os.getenv(
    "CONTEST_WINNER_GIF", os.path.join(CONTEST_IMAGE_DIR, "winner.gif")
)
CONTEST_VOTING_GIF_DIR = os.getenv(
    "CONTEST_VOTING_GIF_DIR", os.path.join(CONTEST_IMAGE_DIR, "voting_gifs")
)
CONTEST_WINNER_GIF_DIR = os.getenv(
    "CONTEST_WINNER_GIF_DIR", os.path.join(CONTEST_IMAGE_DIR, "winner_gifs")
)

# Force every entry card to the same width by attaching a transparent spacer image
# of this pixel width (Discord otherwise sizes each embed to its longest line).
# Set to 0 to disable.
CONTEST_CARD_WIDTH = int(os.getenv("CONTEST_CARD_WIDTH", "512"))

# Toggle the blurred "?" cover thumbnails (needs Pillow).
CONTEST_BLUR_COVERS = os.getenv("CONTEST_BLUR_COVERS", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
# Where generated blurred covers are cached (keyed by song id / url hash).
CONTEST_BLUR_CACHE_DIR = os.getenv(
    "CONTEST_BLUR_CACHE_DIR", os.path.join(CONTEST_IMAGE_DIR, "blurred")
)

_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")

# Sequential 10-color palette, cycled by entry number.
ENTRY_COLOR_PALETTE = [
    0xE74C3C,  # red
    0xE67E22,  # orange
    0xF1C40F,  # yellow
    0x2ECC71,  # green
    0x1ABC9C,  # teal
    0x3498DB,  # blue
    0x9B59B6,  # purple
    0xE84393,  # pink
    0x00CEC9,  # cyan
    0xFAB1A0,  # peach
]

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
)


# --------------------------------------------------------------------------- text / color


def contest_label(contest: Optional[dict]) -> str:
    """Name to show on cards: the contest's name if set, else the env default."""
    if contest:
        name = (contest.get("name") or "").strip()
        if name:
            return name
    return CONTEST_LABEL


def entry_color(entry_no) -> int:
    """Return a palette color for an entry number (1-based), cycling every 10."""
    try:
        n = int(entry_no)
    except (TypeError, ValueError):
        n = 1
    if n < 1:
        n = 1
    return ENTRY_COLOR_PALETTE[(n - 1) % len(ENTRY_COLOR_PALETTE)]


_ANON_ADJECTIVES = [
    "Velvet", "Crimson", "Midnight", "Golden", "Silent", "Neon", "Cosmic", "Electric",
    "Frosted", "Shadow", "Lunar", "Solar", "Emerald", "Scarlet", "Obsidian", "Silver",
    "Wandering", "Hidden", "Radiant", "Feral", "Gilded", "Static", "Molten", "Phantom",
]
_ANON_NOUNS = [
    "Fox", "Comet", "Raven", "Echo", "Wolf", "Nova", "Tiger", "Falcon",
    "Cipher", "Ghost", "Drifter", "Oracle", "Panther", "Sparrow", "Nomad", "Specter",
    "Lynx", "Viper", "Heron", "Jackal", "Griffin", "Otter", "Mantis", "Wren",
]


def anon_artist(name: Optional[str]) -> str:
    """A neat, deterministic pseudonym for an artist name (hides who made it).

    The same name always maps to the same alias (e.g. "Velvet Fox"), so it's stable
    within a contest but reveals nothing about the real artist.
    """
    if not name or not str(name).strip():
        return "Mystery Artist"
    h = hashlib.sha1(str(name).strip().lower().encode("utf-8", "ignore")).hexdigest()
    adj = _ANON_ADJECTIVES[int(h[0:4], 16) % len(_ANON_ADJECTIVES)]
    noun = _ANON_NOUNS[int(h[4:8], 16) % len(_ANON_NOUNS)]
    return f"{adj} {noun}"


def anon_tag(name: Optional[str]) -> str:
    """A masked, deterministic hash tag for an artist (e.g. '#A4F2C1').

    Hides the real name while staying consistent per artist. Returns '#######'
    when the artist is unknown.
    """
    if not name or not str(name).strip():
        return "#######"
    h = hashlib.sha1(str(name).strip().lower().encode("utf-8", "ignore")).hexdigest()
    return "#" + h[:6].upper()


def entry_label(entry_no) -> str:
    """Convert a 1-based entry number to a letter label: 1->A, 26->Z, 27->AA."""
    try:
        n = int(entry_no)
    except (TypeError, ValueError):
        return str(entry_no)
    if n < 1:
        return str(entry_no)
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def progress_bar(pos, total, *, width: int = 0) -> str:
    """A ▰/▱ bar showing position within the lineup (1-based)."""
    try:
        total = max(1, int(total))
        pos = max(1, min(int(pos), total))
    except (TypeError, ValueError):
        return ""
    w = width or min(total, 12)
    filled = max(1, round(w * pos / total))
    filled = min(filled, w)
    return "▰" * filled + "▱" * (w - filled)


def format_duration(seconds) -> Optional[str]:
    """Format seconds as M:SS (or H:MM:SS), or None if unknown/invalid."""
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return None
    if s <= 0:
        return None
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


# --------------------------------------------------------------------------- server images


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")


def _first_existing(paths) -> Optional[str]:
    for p in paths:
        if p and os.path.isfile(p):
            return p
    return None


def _name_based_candidates(contest: Optional[dict], *suffixes: str) -> list[str]:
    out: list[str] = []
    name = (contest or {}).get("name") if contest else None
    if name:
        slug = _slug(name)
        if slug:
            for suffix in suffixes:
                for ext in _IMG_EXTS:
                    out.append(os.path.join(CONTEST_IMAGE_DIR, f"{slug}{suffix}{ext}"))
    return out


def resolve_poster(contest: Optional[dict]) -> Optional[str]:
    """Path to the poster for this contest, or None.

    Order: ``<slug>_poster.*`` / ``<slug>.*`` in CONTEST_IMAGE_DIR, then the
    CONTEST_POSTER_PATH default.
    """
    candidates = _name_based_candidates(contest, "_poster", "")
    candidates.append(CONTEST_POSTER_PATH)
    return _first_existing(candidates)


def resolve_entry_thumb(contest: Optional[dict]) -> Optional[str]:
    """Path to the default per-entry thumbnail for this contest, or None."""
    candidates = _name_based_candidates(contest, "_entry", "_thumb")
    candidates.append(CONTEST_ENTRY_THUMB_PATH)
    return _first_existing(candidates)


def _random_from_dir(path: Optional[str]) -> Optional[str]:
    """Pick a random image/gif file from a directory, or None if empty/missing."""
    try:
        if path and os.path.isdir(path):
            files = [
                os.path.join(path, f)
                for f in os.listdir(path)
                if f.lower().endswith((".gif", ".png", ".jpg", ".jpeg", ".webp"))
            ]
            files = [f for f in files if os.path.isfile(f)]
            if files:
                return random.choice(files)
    except Exception as e:
        log.debug("[contest_images] random pick from %s failed: %s", path, e)
    return None


def resolve_voting_gif(contest: Optional[dict]) -> Optional[str]:
    """GIF for the voting card: <slug>_voting.*, then a random pool pick, then default."""
    hit = _first_existing(_name_based_candidates(contest, "_voting"))
    if hit:
        return hit
    return _random_from_dir(CONTEST_VOTING_GIF_DIR) or _first_existing([CONTEST_VOTING_GIF])


def resolve_winner_gif(contest: Optional[dict]) -> Optional[str]:
    """GIF for the winner card: <slug>_winner.*, then a random pool pick, then default."""
    hit = _first_existing(_name_based_candidates(contest, "_winner"))
    if hit:
        return hit
    return _random_from_dir(CONTEST_WINNER_GIF_DIR) or _first_existing([CONTEST_WINNER_GIF])


# --------------------------------------------------------------------------- blurred covers


def get_width_spacer(width: Optional[int] = None) -> Optional[str]:
    """Path to a transparent, fixed-width PNG strip used to equalize card widths.

    Discord sizes an embed to its widest element; dropping the same wide (but only
    a few px tall) transparent image into every entry card makes them all render at
    the same width. Generated once and cached; regenerated if the width changes.
    Returns None when disabled (width <= 0) or Pillow is unavailable.
    """
    w = int(width if width is not None else CONTEST_CARD_WIDTH)
    if w <= 0:
        return None
    try:
        from PIL import Image
    except Exception as e:
        log.debug("[contest_images] Pillow unavailable for spacer: %s", e)
        return None

    path = os.path.join(CONTEST_IMAGE_DIR, "_spacer.png")
    need = True
    if os.path.isfile(path):
        try:
            with Image.open(path) as im:
                need = im.size[0] != w
        except Exception:
            need = True
    if need:
        try:
            os.makedirs(CONTEST_IMAGE_DIR, exist_ok=True)
            Image.new("RGBA", (w, 4), (0, 0, 0, 0)).save(path, "PNG")
        except Exception as e:
            log.debug("[contest_images] could not create spacer: %s", e)
            return None
    return path


def width_line(width: Optional[int] = None) -> str:
    """An invisible, unbreakable full-width text line to equalize card widths.

    Uses Braille-blank characters (which have width but no visible glyph). A long
    unbroken run forces the embed to a consistent width even on cards that already
    have an image (where the transparent spacer attachment can't be used).
    """
    w = int(width if width is not None else CONTEST_CARD_WIDTH)
    if w <= 0:
        return ""
    return "\u2800" * max(1, w // 8)


def _load_font(size: int):
    from PIL import ImageFont

    for path in _FONT_CANDIDATES:
        try:
            if os.path.isfile(path):
                return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def make_mystery_cover(
    *, cover_url: Optional[str], song_id: Optional[str] = None
) -> Optional[str]:
    """Produce a blurred, "?"-stamped version of a Suno cover for anonymous cards.

    Downloads the real cover (reusing the image cache), blurs it, darkens it and
    stamps a large "?" in the middle, then caches the result keyed by song id /
    url hash. Returns the local PNG path, or None if disabled/unavailable.
    """
    if not CONTEST_BLUR_COVERS or not cover_url:
        return None

    try:
        from PIL import Image, ImageDraw, ImageFilter
    except Exception as e:
        log.debug("[contest_images] Pillow unavailable, skipping blur: %s", e)
        return None

    key = song_id or hashlib.md5(cover_url.encode()).hexdigest()[:16]
    key = re.sub(r"[^a-zA-Z0-9\-_]", "_", str(key))
    try:
        os.makedirs(CONTEST_BLUR_CACHE_DIR, exist_ok=True)
    except Exception as e:
        log.debug("[contest_images] could not create blur cache dir: %s", e)
        return None
    out_path = os.path.join(CONTEST_BLUR_CACHE_DIR, f"{key}.png")
    if os.path.isfile(out_path):
        return out_path

    # Get the source cover locally (cached download).
    src_path = None
    try:
        from src.utils.image_cache import download_image

        src_path = download_image(
            cover_url,
            song_id=song_id,
            referer="https://suno.com/",
            use_default_on_fail=False,
        )
    except Exception as e:
        log.debug("[contest_images] cover download failed: %s", e)
    if not src_path or not os.path.isfile(src_path):
        return None

    try:
        size = 512
        img = Image.open(src_path).convert("RGB")
        # Center-crop to a square, then scale to a fixed size.
        w, h = img.size
        s = min(w, h)
        left, top = (w - s) // 2, (h - s) // 2
        img = img.crop((left, top, left + s, top + s)).resize((size, size))

        # Heavy blur + darkening so the cover is unrecognizable.
        img = img.filter(ImageFilter.GaussianBlur(radius=28))
        img = img.convert("RGBA")
        img = Image.alpha_composite(img, Image.new("RGBA", img.size, (0, 0, 0, 120)))

        draw = ImageDraw.Draw(img)
        font = _load_font(int(size * 0.6))
        text = "?"
        if font is not None:
            try:
                bbox = draw.textbbox((0, 0), text, font=font)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                pos = ((size - tw) // 2 - bbox[0], (size - th) // 2 - bbox[1])
            except Exception:
                pos = (size // 3, size // 6)
            draw.text((pos[0] + 6, pos[1] + 6), text, font=font, fill=(0, 0, 0, 160))
            draw.text(pos, text, font=font, fill=(255, 255, 255, 235))

        img.convert("RGB").save(out_path, "PNG")
        return out_path
    except Exception as e:
        log.debug("[contest_images] blur render failed: %s", e)
        return None
