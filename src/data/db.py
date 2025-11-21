# ---------------------------------------------------------------------------
# FILE: src/data/db.py
# ---------------------------------------------------------------------------
from __future__ import annotations
import os
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

_CONN: Optional[sqlite3.Connection] = None
_DB_PATH: Optional[str] = None


def _dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d


def get_conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        raise RuntimeError("DB not initialized. Call init_db(path) first.")
    return _CONN


def init_db(db_path: Optional[str] = None) -> None:
    """
    Initialize a process‑global SQLite connection and apply schema.
    """
    global _CONN, _DB_PATH
    if _CONN is not None:
        return

    _DB_PATH = db_path or os.getenv("SUNO_RADIO_DB", "./suno_radio.db")
    os.makedirs(os.path.dirname(_DB_PATH) or ".", exist_ok=True)

    conn = sqlite3.connect(_DB_PATH, isolation_level=None, check_same_thread=True)
    conn.row_factory = _dict_factory
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA synchronous = NORMAL;")

    _CONN = conn

    # apply schema if needed
    schema_path = os.path.join(os.path.dirname(__file__), "..", "migrations", "001_init.sql")
    schema_path = os.path.normpath(schema_path)
    with open(schema_path, "r", encoding="utf-8") as f:
        sql = f.read()
    conn.executescript(sql)

    # Ensure likes table exists (safe to run every boot)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS likes (
      track_id   TEXT NOT NULL,
      guild_id   TEXT NOT NULL,
      user_id    TEXT NOT NULL,
      username   TEXT,
      created_at INTEGER DEFAULT (strftime('%s','now')),
      PRIMARY KEY (track_id, guild_id, user_id),
      FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
    );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_likes_track_guild ON likes(track_id, guild_id);")

# ------------------------------
# Track & Play helpers
# ------------------------------

def upsert_track_basic(*,
    track_id: str,
    title: Optional[str] = None,
    artist: Optional[str] = None,
    cover_url: Optional[str] = None,
    source_url: Optional[str] = None,
    duration_sec: Optional[int] = None,
) -> None:
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO tracks (id, title, artist, cover_url, source_url, duration_sec)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title=COALESCE(excluded.title, tracks.title),
            artist=COALESCE(excluded.artist, tracks.artist),
            cover_url=COALESCE(excluded.cover_url, tracks.cover_url),
            source_url=COALESCE(excluded.source_url, tracks.source_url),
            duration_sec=COALESCE(excluded.duration_sec, tracks.duration_sec)
        """,
        (track_id, title, artist, cover_url, source_url, duration_sec),
    )


def log_play_start(*,
    track_id: str,
    guild_id: int | str,
    channel_id: int | str,
    requested_by: Optional[str] = None,
    context: str = "queue",
) -> int:
    """Create a plays row and return play_id."""
    conn = get_conn()
    now = int(time.time())
    cur = conn.execute(
        """
        INSERT INTO plays (track_id, guild_id, channel_id, requested_by, context, started_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (track_id, str(guild_id), str(channel_id), requested_by, context, now),
    )
    return int(cur.lastrowid)


def log_play_end(*, track_id: str, play_id: Optional[int] = None) -> None:
    conn = get_conn()
    now = int(time.time())
    if play_id is not None:
        conn.execute(
            "UPDATE plays SET ended_at=? WHERE play_id=? AND ended_at IS NULL",
            (now, play_id),
        )
    else:
        conn.execute(
            "UPDATE plays SET ended_at=? WHERE track_id=? AND ended_at IS NULL ORDER BY play_id DESC LIMIT 1",
            (now, track_id),
        )


# ------------------------------
# Queries for commands
# ------------------------------

def recent_plays(*, guild_id: int | str, limit: int = 10, include_autofill: bool = False):
    conn = get_conn()
    where = "WHERE p.guild_id = ?"
    params = [str(guild_id)]
    if not include_autofill:
        where += " AND p.context != 'autofill'"
    params += [int(limit)]
    return conn.execute(
        f"""
        SELECT p.play_id, p.started_at, p.ended_at, p.requested_by, p.context,
               t.id AS track_id, t.title, t.artist, t.source_url
        FROM plays p
        JOIN tracks t ON t.id = p.track_id
        {where}
        ORDER BY p.play_id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()

def top_tracks(*, guild_id: int | str, since_seconds: Optional[int], limit: int = 10, include_autofill: bool = False):
    conn = get_conn()
    params: list[Any] = [str(guild_id)]
    where = "WHERE p.guild_id = ?"
    if since_seconds is not None:
        cutoff = int(time.time()) - since_seconds
        where += " AND p.started_at >= ?"
        params.append(cutoff)
    if not include_autofill:
        where += " AND p.context != 'autofill'"

    sql = f"""
        SELECT
            t.id AS track_id,
            t.title,
            t.artist,
            t.source_url,
            COUNT(DISTINCT COALESCE(p.requested_by, 'anon')) AS plays
        FROM plays p
        JOIN tracks t ON t.id = p.track_id
        {where}
        GROUP BY t.id
        ORDER BY plays DESC, MAX(p.started_at) DESC
        LIMIT ?
    """
    params.append(int(limit))
    return conn.execute(sql, params).fetchall()

def like_track(*, track_id: str, guild_id: int | str, user_id: int | str, username: str | None = None) -> int:
    """Set a like (idempotent). Returns total likes for this track in this guild."""
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO likes(track_id, guild_id, user_id, username) VALUES(?,?,?,?)",
        (track_id, str(guild_id), str(user_id), username),
    )
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM likes WHERE track_id=? AND guild_id=?",
        (track_id, str(guild_id)),
    ).fetchone()
    return int(row["c"])

def unlike_track(*, track_id: str, guild_id: int | str, user_id: int | str) -> int:
    """Remove a like. Returns new total."""
    conn = get_conn()
    conn.execute(
        "DELETE FROM likes WHERE track_id=? AND guild_id=? AND user_id=?",
        (track_id, str(guild_id), str(user_id)),
    )
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM likes WHERE track_id=? AND guild_id=?",
        (track_id, str(guild_id)),
    ).fetchone()
    return int(row["c"])

def has_liked(*, track_id: str, guild_id: int | str, user_id: int | str) -> bool:
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM likes WHERE track_id=? AND guild_id=? AND user_id=?",
        (track_id, str(guild_id), str(user_id)),
    ).fetchone()
    return bool(row)

def get_like_count(*, track_id: str, guild_id: int | str) -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM likes WHERE track_id=? AND guild_id=?",
        (track_id, str(guild_id)),
    ).fetchone()
    return int(row["c"])

def top_liked_for_users(*, guild_id: int | str, user_ids: Iterable[int | str], limit: int = 50) -> list[dict]:
    """Return top liked tracks for the given users in this guild, ordered by like count.
    Includes basic track metadata (title, artist, source_url) when available.
    """
    user_ids = [str(u) for u in user_ids if u is not None]
    if not user_ids:
        return []

    conn = get_conn()
    placeholders = ",".join(["?"] * len(user_ids))
    sql = f"""
        SELECT
            l.track_id,
            COUNT(*) AS like_count,
            MAX(l.created_at) AS last_liked_at,
            t.title,
            t.artist,
            t.source_url
        FROM likes l
        JOIN tracks t ON t.id = l.track_id
        WHERE l.guild_id = ?
          AND l.user_id IN ({placeholders})
        GROUP BY l.track_id
        ORDER BY like_count DESC, last_liked_at DESC
        LIMIT ?
    """
    params = [str(guild_id)] + user_ids + [int(limit)]
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    return [dict(row) for row in rows]
