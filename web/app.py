"""
SubotLive Wrapped – lightweight FastAPI dashboard.

Reads the bot's SQLite DB (read-only) and serves:
  • Public stats API  (/api/stats/*)
  • Personal stats    (/api/me/*) behind Discord OAuth2
  • Static frontend   (/)
"""
from __future__ import annotations

import os
import sys
import time
import sqlite3
import secrets
from pathlib import Path
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import URLSafeTimedSerializer, BadSignature

# ── ENV ──────────────────────────────────────────────────────────────────────
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH, override=True)

DISCORD_CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_BOT_TOKEN     = os.getenv("BOT_TOKEN", "").strip()
SECRET_KEY            = os.getenv("DASHBOARD_SECRET_KEY", secrets.token_hex(32))
BASE_URL              = os.getenv("DASHBOARD_BASE_URL", "http://localhost:8100").rstrip("/")
DB_PATH               = os.getenv("SUNO_RADIO_DB", str(Path(__file__).resolve().parent.parent / "suno_radio.db"))

# When false, the app does not open the DB and returns 503 for all routes except /health (saves CPU / DB / bandwidth).
WRAPPED_DASHBOARD_ENABLED = os.getenv("WRAPPED_DASHBOARD_ENABLED", "0") == "1"

DISCORD_API      = "https://discord.com/api/v10"
DISCORD_AUTH_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
OAUTH_REDIRECT   = f"{BASE_URL}/auth/callback"
OAUTH_SCOPES     = "identify"
SESSION_COOKIE   = "subot_session"
SESSION_MAX_AGE  = 60 * 60 * 24 * 30  # 30 days

signer = URLSafeTimedSerializer(SECRET_KEY)

# ── DB ───────────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"file:{DB_PATH}?mode=ro", uri=True,
        check_same_thread=False, isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    return conn

db = get_db() if WRAPPED_DASHBOARD_ENABLED else None

# ── APP ──────────────────────────────────────────────────────────────────────

app = FastAPI(title="SubotLive Wrapped", docs_url=None, redoc_url=None)

STATIC_DIR = Path(__file__).parent / "static"
if WRAPPED_DASHBOARD_ENABLED:
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── Helpers ──────────────────────────────────────────────────────────────────

def _epoch_to_iso(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _get_session_user(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        return signer.loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None


# Short-lived cache so we do not hammer Discord on every /api/me poll.
_profile_cache: dict[str, tuple[float, dict]] = {}
_PROFILE_CACHE_TTL = 120.0


async def _fetch_discord_user_profile(user_id: str) -> dict | None:
    """
    Fetch current username / avatar from Discord (Bot REST).
    Fixes stale avatar URLs after the user changes their profile on Discord.
    """
    if not DISCORD_BOT_TOKEN:
        return None
    now = time.monotonic()
    hit = _profile_cache.get(user_id)
    if hit and (now - hit[0]) < _PROFILE_CACHE_TTL:
        return hit[1]

    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{DISCORD_API}/users/{user_id}", headers=headers, timeout=10.0)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    data = r.json()
    out = {
        "username": data.get("global_name") or data.get("username") or "",
        "avatar": data.get("avatar"),
    }
    _profile_cache[user_id] = (now, out)
    return out


async def _session_user_with_fresh_profile(session_user: dict) -> dict:
    fresh = await _fetch_discord_user_profile(session_user["id"])
    if not fresh:
        return dict(session_user)
    merged = dict(session_user)
    if fresh.get("username"):
        merged["username"] = fresh["username"]
    merged["avatar"] = fresh.get("avatar")
    return merged


_username_cache: dict[str, str] = {}

def _resolve_username(user_id: str) -> str:
    if user_id in _username_cache:
        return _username_cache[user_id]
    try:
        row = db.execute(
            "SELECT requested_by_username FROM plays WHERE requested_by = ? AND requested_by_username IS NOT NULL AND requested_by_username != '' ORDER BY started_at DESC LIMIT 1",
            [user_id],
        ).fetchone()
        if row and row["requested_by_username"]:
            name = row["requested_by_username"]
            _username_cache[user_id] = name
            return name
    except Exception:
        pass
    row = db.execute(
        "SELECT username FROM likes WHERE user_id = ? AND username IS NOT NULL LIMIT 1",
        [user_id],
    ).fetchone()
    name = row["username"] if row else f"DJ {user_id[-4:]}"
    _username_cache[user_id] = name
    return name


def _seconds_for_range(range_name: str) -> int | None:
    return {
        "day": 86400,
        "week": 604800,
        "month": 2592000,
        "year": 31536000,
        "all": None,
    }.get(range_name)

# ── OAuth2 ───────────────────────────────────────────────────────────────────

@app.get("/auth/login")
async def auth_login():
    if not DISCORD_CLIENT_ID:
        raise HTTPException(503, "Discord OAuth2 not configured")
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT,
        "response_type": "code",
        "scope": OAUTH_SCOPES,
    }
    return RedirectResponse(f"{DISCORD_AUTH_URL}?{urlencode(params)}")


@app.get("/auth/callback")
async def auth_callback(code: str = ""):
    if not code or not DISCORD_CLIENT_ID:
        raise HTTPException(400, "Missing code or OAuth2 not configured")

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(DISCORD_TOKEN_URL, data={
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": OAUTH_REDIRECT,
        })
        if token_resp.status_code != 200:
            raise HTTPException(502, "Failed to exchange token with Discord")
        access_token = token_resp.json()["access_token"]

        user_resp = await client.get(f"{DISCORD_API}/users/@me", headers={
            "Authorization": f"Bearer {access_token}",
        })
        if user_resp.status_code != 200:
            raise HTTPException(502, "Failed to fetch Discord user")
        user = user_resp.json()

    session_data = {
        "id": user["id"],
        "username": user.get("global_name") or user["username"],
        "avatar": user.get("avatar"),
    }
    token = signer.dumps(session_data)
    response = RedirectResponse("/")
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=BASE_URL.startswith("https"),
    )
    return response


@app.get("/auth/logout")
async def auth_logout():
    response = RedirectResponse("/")
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/api/me")
async def api_me(request: Request):
    user = _get_session_user(request)
    if not user:
        return JSONResponse({"logged_in": False})
    user = await _session_user_with_fresh_profile(user)
    return JSONResponse({"logged_in": True, **user})

# ── Public Stats API ─────────────────────────────────────────────────────────

@app.get("/api/stats/overview")
async def stats_overview(autofill: bool = True):
    af_filter = "" if autofill else "WHERE context != 'autofill'"
    af_join_filter = "" if autofill else "WHERE p.context != 'autofill'"
    row = db.execute(f"""
        SELECT
            COUNT(*)                         AS total_plays,
            COUNT(DISTINCT track_id)         AS unique_tracks,
            COUNT(DISTINCT requested_by)     AS unique_djs,
            MIN(started_at)                  AS first_play,
            MAX(started_at)                  AS last_play
        FROM plays
        {af_filter}
    """).fetchone()
    total_duration = db.execute(f"""
        SELECT COALESCE(SUM(t.duration_sec), 0) AS total_sec
        FROM plays p JOIN tracks t ON t.id = p.track_id
        {af_join_filter}
    """).fetchone()
    likes_count = db.execute("SELECT COUNT(*) AS c FROM likes").fetchone()
    return {
        "total_plays": row["total_plays"],
        "unique_tracks": row["unique_tracks"],
        "unique_djs": row["unique_djs"],
        "total_likes": likes_count["c"],
        "total_listening_hours": round(total_duration["total_sec"] / 3600, 1),
        "first_play": _epoch_to_iso(row["first_play"]),
        "last_play": _epoch_to_iso(row["last_play"]),
    }


@app.get("/api/stats/top-tracks")
async def stats_top_tracks(range: str = "all", limit: int = 10, autofill: bool = True):
    limit = max(1, min(50, limit))
    since = _seconds_for_range(range)
    params: list = []
    clauses: list[str] = []
    if since is not None:
        cutoff = int(time.time()) - since
        clauses.append("p.started_at >= ?")
        params.append(cutoff)
    if not autofill:
        clauses.append("p.context != 'autofill'")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = db.execute(f"""
        SELECT t.id, t.title, t.artist, t.cover_url, t.source_url, t.duration_sec,
               COUNT(*) AS play_count
        FROM plays p JOIN tracks t ON t.id = p.track_id
        {where}
        GROUP BY t.id
        ORDER BY play_count DESC, MAX(p.started_at) DESC
        LIMIT ?
    """, params).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/stats/top-djs")
async def stats_top_djs(range: str = "all", limit: int = 10, autofill: bool = True):
    limit = max(1, min(50, limit))
    since = _seconds_for_range(range)
    params: list = []
    where = "WHERE p.requested_by IS NOT NULL"
    if since is not None:
        cutoff = int(time.time()) - since
        where += " AND p.started_at >= ?"
        params.append(cutoff)
    if not autofill:
        where += " AND p.context != 'autofill'"
    params.append(limit)
    rows = db.execute(f"""
        SELECT p.requested_by AS user_id,
               COUNT(*) AS play_count,
               COUNT(DISTINCT p.track_id) AS unique_tracks
        FROM plays p
        {where}
        GROUP BY p.requested_by
        ORDER BY play_count DESC
        LIMIT ?
    """, params).fetchall()
    results = [dict(r) for r in rows]
    for r in results:
        r["username"] = _resolve_username(r["user_id"])
    return results


@app.get("/api/stats/heatmap")
async def stats_heatmap(autofill: bool = True):
    """Hour-of-day × day-of-week play counts for the activity heatmap."""
    af_filter = "" if autofill else "WHERE context != 'autofill'"
    rows = db.execute(f"""
        SELECT
            CAST(strftime('%w', started_at, 'unixepoch') AS INTEGER) AS dow,
            CAST(strftime('%H', started_at, 'unixepoch') AS INTEGER) AS hour,
            COUNT(*) AS plays
        FROM plays
        {af_filter}
        GROUP BY dow, hour
    """).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/stats/now-playing")
async def stats_now_playing():
    """Return the currently playing track (if any) based on plays with no ended_at.

    A play is considered stale if it started more than 2x its duration ago
    (or more than 15 minutes for tracks with unknown duration).  Stale rows
    are auto-closed so they stop polluting future queries.
    """
    MAX_FALLBACK_SEC = 900  # 15 min cap for tracks with unknown duration
    now = int(time.time())

    row = db.execute("""
        SELECT t.id, t.title, t.artist, t.cover_url, t.source_url, t.duration_sec,
               p.play_id, p.started_at, p.guild_id, p.channel_id,
               p.requested_by, p.context
        FROM plays p JOIN tracks t ON t.id = p.track_id
        WHERE p.ended_at IS NULL
        ORDER BY p.started_at DESC
        LIMIT 1
    """).fetchone()
    if not row:
        return {"playing": False}

    duration = row["duration_sec"] or 0
    max_age = max(duration * 2, MAX_FALLBACK_SEC)
    age = now - (row["started_at"] or 0)

    if age > max_age:
        return {"playing": False}

    result = dict(row)
    result["playing"] = True
    if result.get("requested_by"):
        result["requested_by_name"] = (
            result.get("requested_by_username")
            or _resolve_username(result["requested_by"])
        )
    return result


@app.get("/api/stats/top-liked")
async def stats_top_liked(limit: int = 10):
    limit = max(1, min(50, limit))
    rows = db.execute("""
        SELECT t.id, t.title, t.artist, t.cover_url, t.source_url,
               COUNT(*) AS like_count
        FROM likes l JOIN tracks t ON t.id = l.track_id
        GROUP BY t.id
        ORDER BY like_count DESC
        LIMIT ?
    """, [limit]).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/stats/all-djs")
async def stats_all_djs(page: int = 1, per_page: int = 20, autofill: bool = True):
    """Paginated list of all DJs (users who have requested at least one play)."""
    per_page = max(1, min(50, per_page))
    page = max(1, page)
    offset = (page - 1) * per_page
    where = "WHERE p.requested_by IS NOT NULL"
    if not autofill:
        where += " AND p.context != 'autofill'"
    count_row = db.execute(
        f"SELECT COUNT(DISTINCT p.requested_by) AS total FROM plays p {where}"
    ).fetchone()
    total = count_row["total"]
    rows = db.execute(f"""
        SELECT p.requested_by AS user_id,
               COUNT(*) AS play_count,
               COUNT(DISTINCT p.track_id) AS unique_tracks
        FROM plays p
        {where}
        GROUP BY p.requested_by
        ORDER BY play_count DESC
        LIMIT ? OFFSET ?
    """, [per_page, offset]).fetchall()
    results = [dict(r) for r in rows]
    for r in results:
        r["username"] = _resolve_username(r["user_id"])
    return {"items": results, "total": total, "page": page, "per_page": per_page}


@app.get("/api/stats/all-liked")
async def stats_all_liked(page: int = 1, per_page: int = 20):
    """Paginated list of all liked tracks (by total like count)."""
    per_page = max(1, min(50, per_page))
    page = max(1, page)
    offset = (page - 1) * per_page
    count_row = db.execute(
        "SELECT COUNT(DISTINCT l.track_id) AS total FROM likes l"
    ).fetchone()
    total = count_row["total"]
    rows = db.execute("""
        SELECT t.id, t.title, t.artist, t.cover_url, t.source_url,
               COUNT(*) AS like_count
        FROM likes l JOIN tracks t ON t.id = l.track_id
        GROUP BY t.id
        ORDER BY like_count DESC
        LIMIT ? OFFSET ?
    """, [per_page, offset]).fetchall()
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "per_page": per_page}

# ── Personal Stats API ───────────────────────────────────────────────────────

@app.get("/api/me/wrapped")
async def me_wrapped(request: Request):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(401, "Not logged in")
    user = await _session_user_with_fresh_profile(user)
    uid = user["id"]

    total = db.execute("""
        SELECT COUNT(*) AS plays,
               COUNT(DISTINCT track_id) AS unique_tracks
        FROM plays WHERE requested_by = ?
    """, [uid]).fetchone()

    listening = db.execute("""
        SELECT COALESCE(SUM(t.duration_sec), 0) AS total_sec
        FROM plays p JOIN tracks t ON t.id = p.track_id
        WHERE p.requested_by = ?
    """, [uid]).fetchone()

    top_tracks = db.execute("""
        SELECT t.id, t.title, t.artist, t.cover_url, t.source_url,
               COUNT(*) AS play_count
        FROM plays p JOIN tracks t ON t.id = p.track_id
        WHERE p.requested_by = ?
        GROUP BY t.id
        ORDER BY play_count DESC
        LIMIT 10
    """, [uid]).fetchall()

    top_artists = db.execute("""
        SELECT t.artist, COUNT(*) AS play_count
        FROM plays p JOIN tracks t ON t.id = p.track_id
        WHERE p.requested_by = ? AND t.artist IS NOT NULL
              AND LOWER(t.artist) NOT IN ('unknown', 'unknown artist', '')
        GROUP BY LOWER(t.artist)
        ORDER BY play_count DESC
        LIMIT 5
    """, [uid]).fetchall()

    like_count = db.execute(
        "SELECT COUNT(*) AS c FROM likes WHERE user_id = ?", [uid]
    ).fetchone()

    favorite_liked = db.execute("""
        SELECT t.id, t.title, t.artist, t.cover_url, t.source_url,
               COUNT(*) AS like_count
        FROM likes l JOIN tracks t ON t.id = l.track_id
        WHERE l.user_id = ?
        GROUP BY t.id
        ORDER BY like_count DESC
        LIMIT 5
    """, [uid]).fetchall()

    # Fun facts
    busiest_day = db.execute("""
        SELECT date(started_at, 'unixepoch') AS day, COUNT(*) AS plays
        FROM plays WHERE requested_by = ?
        GROUP BY day ORDER BY plays DESC LIMIT 1
    """, [uid]).fetchone()

    rarest = db.execute("""
        SELECT t.id, t.title, t.artist, t.source_url, global_plays
        FROM (
            SELECT track_id, COUNT(*) AS global_plays
            FROM plays GROUP BY track_id HAVING global_plays = 1
        ) rare
        JOIN plays p ON p.track_id = rare.track_id AND p.requested_by = ?
        JOIN tracks t ON t.id = rare.track_id
        LIMIT 5
    """, [uid]).fetchall()

    # Listening streak: consecutive days with at least one play
    day_rows = db.execute("""
        SELECT DISTINCT date(started_at, 'unixepoch') AS day
        FROM plays WHERE requested_by = ?
        ORDER BY day DESC
    """, [uid]).fetchall()
    streak = 0
    if day_rows:
        from datetime import date, timedelta
        days = [date.fromisoformat(r["day"]) for r in day_rows]
        streak = 1
        for i in range(1, len(days)):
            if days[i - 1] - days[i] == timedelta(days=1):
                streak += 1
            else:
                break

    # Resolve the user's own Suno artist name (most-played artist by them)
    # so we can provide a "top artist excluding self" option
    own_artist = top_artists[0]["artist"].lower() if top_artists else None
    other_artists = [dict(r) for r in top_artists if r["artist"].lower() != own_artist] if own_artist else []
    top_artist_display = other_artists[0] if other_artists else (dict(top_artists[0]) if top_artists else None)

    return {
        "user": user,
        "total_plays": total["plays"],
        "unique_tracks": total["unique_tracks"],
        "listening_hours": round(listening["total_sec"] / 3600, 1),
        "total_likes": like_count["c"],
        "top_tracks": [dict(r) for r in top_tracks],
        "top_artists": [dict(r) for r in top_artists],
        "top_artist_display": top_artist_display,
        "favorite_liked": [dict(r) for r in favorite_liked],
        "busiest_day": dict(busiest_day) if busiest_day else None,
        "rarest_finds": [dict(r) for r in rarest],
        "listening_streak": streak,
    }

@app.get("/api/me/tracks")
async def me_tracks(request: Request, page: int = 1, per_page: int = 20):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(401, "Not logged in")
    uid = user["id"]
    per_page = max(1, min(50, per_page))
    page = max(1, page)
    offset = (page - 1) * per_page
    total = db.execute(
        "SELECT COUNT(DISTINCT track_id) AS c FROM plays WHERE requested_by = ?", [uid]
    ).fetchone()["c"]
    rows = db.execute("""
        SELECT t.id, t.title, t.artist, t.cover_url, t.source_url,
               COUNT(*) AS play_count
        FROM plays p JOIN tracks t ON t.id = p.track_id
        WHERE p.requested_by = ?
        GROUP BY t.id
        ORDER BY play_count DESC
        LIMIT ? OFFSET ?
    """, [uid, per_page, offset]).fetchall()
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "per_page": per_page}


@app.get("/api/me/liked")
async def me_liked(request: Request, page: int = 1, per_page: int = 20):
    user = _get_session_user(request)
    if not user:
        raise HTTPException(401, "Not logged in")
    uid = user["id"]
    per_page = max(1, min(50, per_page))
    page = max(1, page)
    offset = (page - 1) * per_page
    total = db.execute(
        "SELECT COUNT(DISTINCT track_id) AS c FROM likes WHERE user_id = ?", [uid]
    ).fetchone()["c"]
    rows = db.execute("""
        SELECT t.id, t.title, t.artist, t.cover_url, t.source_url,
               COUNT(*) AS like_count
        FROM likes l JOIN tracks t ON t.id = l.track_id
        WHERE l.user_id = ?
        GROUP BY t.id
        ORDER BY like_count DESC
        LIMIT ? OFFSET ?
    """, [uid, per_page, offset]).fetchall()
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "per_page": per_page}


# ── Frontend ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text()


@app.get("/health")
async def health():
    return {"status": "ok", "wrapped_enabled": WRAPPED_DASHBOARD_ENABLED}


if not WRAPPED_DASHBOARD_ENABLED:

    @app.middleware("http")
    async def _wrapped_dashboard_disabled(request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        return PlainTextResponse(
            "SubotLive Wrapped dashboard is disabled. "
            "Set WRAPPED_DASHBOARD_ENABLED=1 in the environment to re-enable.",
            status_code=503,
        )
