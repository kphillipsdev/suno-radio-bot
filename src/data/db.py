# ---------------------------------------------------------------------------
# FILE: src/data/db.py
# ---------------------------------------------------------------------------
from __future__ import annotations
import logging
import os
import random
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

log = logging.getLogger(__name__)

_CONN: Optional[sqlite3.Connection] = None
_DB_PATH: Optional[str] = None
_DB_LOCK = threading.Lock()


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


def close_db() -> None:
    """Close the global DB connection if open."""
    global _CONN
    if _CONN is not None:
        try:
            _CONN.close()
        except Exception:
            pass
        _CONN = None


def init_db(db_path: Optional[str] = None) -> None:
    """
    Initialize a process‑global SQLite connection and apply schema.
    """
    global _CONN, _DB_PATH
    if _CONN is not None:
        return

    _DB_PATH = db_path or os.getenv("SUNO_RADIO_DB", "./suno_radio.db")
    os.makedirs(os.path.dirname(_DB_PATH) or ".", exist_ok=True)

    conn = sqlite3.connect(_DB_PATH, isolation_level=None, check_same_thread=False)
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

    # Migration 002: requested_by_username on plays
    try:
        cursor = conn.execute("PRAGMA table_info(plays)")
        columns = [row["name"] for row in cursor.fetchall()]
        if "requested_by_username" not in columns:
            conn.execute("ALTER TABLE plays ADD COLUMN requested_by_username TEXT")
    except Exception as e:
        log.warning("Migration 002 (requested_by_username) failed: %s", e)

    # Check if likes table exists and needs migration
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='likes'")
    table_exists = cursor.fetchone() is not None
    
    if table_exists:
        # Check if the table has the old schema (composite PRIMARY KEY)
        cursor = conn.execute("PRAGMA table_info(likes)")
        rows = cursor.fetchall()
        # row factory returns dicts, so access by column name
        column_names = [row['name'] for row in rows]
        
        # Check if table has 'id' column
        has_id_column = 'id' in column_names
        
        # Check for composite PRIMARY KEY by looking at table definition
        cursor = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='likes'")
        table_sql_row = cursor.fetchone()
        table_sql = (table_sql_row.get('sql', '') if isinstance(table_sql_row, dict) else (table_sql_row[0] if table_sql_row else '')).upper()
        
        # Check if there's a composite PRIMARY KEY (old schema)
        # Look for PRIMARY KEY with multiple columns (track_id, guild_id, user_id)
        # Normalize the SQL by removing extra spaces for comparison
        normalized_sql = ' '.join(table_sql.split()) if table_sql else ""
        has_composite_pk = (
            table_sql and 
            ('PRIMARY KEY (TRACK_ID, GUILD_ID, USER_ID)' in normalized_sql or
             'PRIMARY KEY(TRACK_ID,GUILD_ID,USER_ID)' in normalized_sql.replace(' ', '') or
             'PRIMARY KEY(TRACK_ID,GUILD_ID,USER_ID)' in normalized_sql)
        )
        
        # Also check if there's a unique index on these columns (which would prevent duplicates)
        cursor = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='likes'")
        indexes = cursor.fetchall()
        has_unique_constraint = False
        for idx_row in indexes:
            idx_sql = idx_row.get('sql', '') if isinstance(idx_row, dict) else (idx_row[0] if idx_row else '')
            if idx_sql and ('UNIQUE' in idx_sql.upper() or 'PRIMARY' in idx_sql.upper()):
                idx_sql_upper = idx_sql.upper()
                if 'TRACK_ID' in idx_sql_upper and 'GUILD_ID' in idx_sql_upper and 'USER_ID' in idx_sql_upper:
                    has_unique_constraint = True
                    break
        
        if not has_id_column or has_composite_pk or has_unique_constraint:
            # Old schema detected - migrate to new schema
            try:
                print("[db migration] Migrating likes table to allow multiple likes per user...")
                conn.execute("""
                    CREATE TABLE likes_new (
                      id         INTEGER PRIMARY KEY AUTOINCREMENT,
                      track_id   TEXT NOT NULL,
                      guild_id   TEXT NOT NULL,
                      user_id    TEXT NOT NULL,
                      username   TEXT,
                      created_at INTEGER DEFAULT (strftime('%s','now')),
                      FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
                    );
                """)
                conn.execute("INSERT INTO likes_new (track_id, guild_id, user_id, username, created_at) SELECT track_id, guild_id, user_id, username, created_at FROM likes;")
                conn.execute("DROP TABLE likes;")
                conn.execute("ALTER TABLE likes_new RENAME TO likes;")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_likes_track_guild ON likes(track_id, guild_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_likes_user ON likes(user_id, guild_id);")
                log.info("[db migration] Successfully migrated likes table to allow multiple likes per user")
            except Exception as e:
                log.warning("[db migration] Failed to migrate likes table: %s", e)
    
    # Ensure likes table exists with new schema (safe to run every boot)
    # Allow multiple likes from the same user for the same track
    conn.execute("""
    CREATE TABLE IF NOT EXISTS likes (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      track_id   TEXT NOT NULL,
      guild_id   TEXT NOT NULL,
      user_id    TEXT NOT NULL,
      username   TEXT,
      created_at INTEGER DEFAULT (strftime('%s','now')),
      FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
    );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_likes_track_guild ON likes(track_id, guild_id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_likes_user ON likes(user_id, guild_id);")

    # Track health: persistent failure tracking for the autofill cleanup tool.
    # Keyed by canonical source URL so it works for tracks that have never been
    # successfully resolved (i.e. don't appear in `tracks` yet).
    conn.execute("""
    CREATE TABLE IF NOT EXISTS track_health (
      url                 TEXT PRIMARY KEY,
      failure_count       INTEGER NOT NULL DEFAULT 0,
      first_failure_at    INTEGER,
      last_failure_at     INTEGER,
      last_failure_reason TEXT,
      last_success_at     INTEGER
    );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_track_health_last_failure ON track_health(last_failure_at);")

    # ------------------------------
    # Anonymous contests
    # ------------------------------
    # A contest collects Suno song submissions, plays them back anonymously,
    # collects votes, then reveals the entries and announces a winner.
    conn.execute("""
    CREATE TABLE IF NOT EXISTS contests (
      id                INTEGER PRIMARY KEY AUTOINCREMENT,
      guild_id          TEXT NOT NULL,
      name              TEXT,
      status            TEXT NOT NULL DEFAULT 'collecting',
      created_by        TEXT,
      created_at        INTEGER DEFAULT (strftime('%s','now')),
      voting_channel_id TEXT,
      voting_message_id TEXT
    );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_contests_guild_status ON contests(guild_id, status);")

    # submissions_close_at: optional unix timestamp; when passed while status is
    # 'collecting', submissions flip to 'submissions_closed' automatically.
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(contests)").fetchall()}
        if "submissions_close_at" not in cols:
            conn.execute("ALTER TABLE contests ADD COLUMN submissions_close_at INTEGER;")
            log.info("[db migration] contests.submissions_close_at added")
    except Exception as e:
        log.warning("[db migration] contests.submissions_close_at failed: %s", e)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS contest_entries (
      id                INTEGER PRIMARY KEY AUTOINCREMENT,
      contest_id        INTEGER NOT NULL,
      entry_no          INTEGER,
      url               TEXT NOT NULL,
      song_id           TEXT,
      title             TEXT,
      artist            TEXT,
      cover_url         TEXT,
      submitted_by      TEXT,
      submitted_by_name TEXT,
      created_at        INTEGER DEFAULT (strftime('%s','now')),
      FOREIGN KEY (contest_id) REFERENCES contests(id) ON DELETE CASCADE
    );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_contest_entries_contest ON contest_entries(contest_id);")

    # Migrate contest_votes from the original one-vote-per-user schema
    # (UNIQUE(contest_id, user_id)) to the multi-vote schema
    # (UNIQUE(contest_id, user_id, entry_id)) so a user can cast several votes.
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='contest_votes'"
        ).fetchone()
        if row:
            existing_sql = (row.get("sql") if isinstance(row, dict) else row[0]) or ""
            norm = " ".join(existing_sql.split()).upper().replace(" ", "")
            if "UNIQUE(CONTEST_ID,USER_ID)" in norm and "UNIQUE(CONTEST_ID,USER_ID,ENTRY_ID)" not in norm:
                conn.execute("""
                    CREATE TABLE contest_votes_new (
                      id         INTEGER PRIMARY KEY AUTOINCREMENT,
                      contest_id INTEGER NOT NULL,
                      entry_id   INTEGER NOT NULL,
                      user_id    TEXT NOT NULL,
                      created_at INTEGER DEFAULT (strftime('%s','now')),
                      UNIQUE (contest_id, user_id, entry_id),
                      FOREIGN KEY (contest_id) REFERENCES contests(id) ON DELETE CASCADE,
                      FOREIGN KEY (entry_id)   REFERENCES contest_entries(id) ON DELETE CASCADE
                    );
                """)
                conn.execute(
                    "INSERT INTO contest_votes_new (contest_id, entry_id, user_id, created_at) "
                    "SELECT DISTINCT contest_id, entry_id, user_id, created_at FROM contest_votes;"
                )
                conn.execute("DROP TABLE contest_votes;")
                conn.execute("ALTER TABLE contest_votes_new RENAME TO contest_votes;")
                log.info("[db migration] contest_votes migrated to multi-vote schema")
    except Exception as e:
        log.warning("[db migration] contest_votes migration failed: %s", e)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS contest_votes (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      contest_id INTEGER NOT NULL,
      entry_id   INTEGER NOT NULL,
      user_id    TEXT NOT NULL,
      created_at INTEGER DEFAULT (strftime('%s','now')),
      UNIQUE (contest_id, user_id, entry_id),
      FOREIGN KEY (contest_id) REFERENCES contests(id) ON DELETE CASCADE,
      FOREIGN KEY (entry_id)   REFERENCES contest_entries(id) ON DELETE CASCADE
    );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_contest_votes_contest ON contest_votes(contest_id);")

# ------------------------------
# Track & Play helpers
# ------------------------------

def _sanitize_cover_url(url: Optional[str]) -> Optional[str]:
    if url and "image_large_large" in url:
        url = url.replace("image_large_large", "image_large")
    return url


def upsert_track_basic(*,
    track_id: str,
    title: Optional[str] = None,
    artist: Optional[str] = None,
    cover_url: Optional[str] = None,
    source_url: Optional[str] = None,
    duration_sec: Optional[int] = None,
) -> None:
    cover_url = _sanitize_cover_url(cover_url)
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
    requested_by_username: Optional[str] = None,
    context: str = "queue",
) -> int:
    """Create a plays row and return play_id."""
    conn = get_conn()
    now = int(time.time())
    cur = conn.execute(
        """
        INSERT INTO plays (track_id, guild_id, channel_id, requested_by, requested_by_username, context, started_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (track_id, str(guild_id), str(channel_id), requested_by, requested_by_username, context, now),
    )
    return int(cur.lastrowid)


def log_play_end(*, track_id: str, play_id: Optional[int] = None) -> None:
    conn = get_conn()
    now = int(time.time())
    with _DB_LOCK:
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

def recent_plays(*, guild_id: int | str, limit: int = 20, include_autofill: bool = False):
    conn = get_conn()
    where = "WHERE p.guild_id = ?"
    params = [str(guild_id)]
    if not include_autofill:
        where += " AND p.context != 'autofill'"
    params += [int(limit)]
    return conn.execute(
        f"""
        SELECT p.play_id, p.started_at, p.ended_at, p.requested_by, p.context,
               t.id AS track_id, t.title, t.artist, t.source_url, t.cover_url
        FROM plays p
        JOIN tracks t ON t.id = p.track_id
        {where}
        ORDER BY p.play_id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()

def top_tracks(*, guild_id: int | str, since_seconds: Optional[int], limit: int = 20, include_autofill: bool = False):
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
            t.cover_url,
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

def get_all_guild_tracks(*, guild_id: int | str, limit: int = 1000, include_autofill: bool = False):
    conn = get_conn()
    params: list[Any] = [str(guild_id)]
    where = "WHERE p.guild_id = ?"
    if not include_autofill:
        where += " AND p.context != 'autofill'"

    sql = f"""
        SELECT
            t.id AS track_id,
            t.title,
            t.artist,
            t.source_url,
            t.cover_url
        FROM plays p
        JOIN tracks t ON t.id = p.track_id
        {where}
        GROUP BY t.id
    """
    rows = conn.execute(sql, params).fetchall()
    import random as _rand
    _rand.shuffle(rows)
    return rows[:int(limit)]

def like_track(*, track_id: str, guild_id: int | str, user_id: int | str, username: str | None = None) -> int:
    """Add a like (allows multiple likes from same user). Returns total likes for this track in this guild."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO likes(track_id, guild_id, user_id, username) VALUES(?,?,?,?)",
        (track_id, str(guild_id), str(user_id), username),
    )
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM likes WHERE track_id=? AND guild_id=?",
        (track_id, str(guild_id)),
    ).fetchone()
    return int(row["c"])

def unlike_track(*, track_id: str, guild_id: int | str, user_id: int | str) -> int:
    """Remove one like (the most recent one). Returns new total."""
    conn = get_conn()
    # Delete the most recent like for this user/track combination
    conn.execute("""
        DELETE FROM likes 
        WHERE id = (
            SELECT id FROM likes 
            WHERE track_id=? AND guild_id=? AND user_id=?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        )
    """, (track_id, str(guild_id), str(user_id)))
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

def get_user_like_count(*, track_id: str, guild_id: int | str, user_id: int | str) -> int:
    """Get the number of times a specific user has liked a track."""
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM likes WHERE track_id=? AND guild_id=? AND user_id=?",
        (track_id, str(guild_id), str(user_id)),
    ).fetchone()
    return int(row["c"])

def top_liked_for_users(*, guild_id: int | str, user_ids: Iterable[int | str], limit: int = 50) -> list[dict]:
    """Return top liked tracks for the given users in this guild, ordered by like count.
    Includes basic track metadata (title, artist, source_url) when available.
    Excludes tracks with BOTH 'Unknown Title' and 'Unknown' artist.
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
          AND NOT (
            LOWER(COALESCE(t.title, '')) IN ('unknown title', 'untitled', '')
            AND LOWER(COALESCE(t.artist, '')) IN ('unknown', 'unknown artist', '', 'none')
          )
        GROUP BY l.track_id
        ORDER BY like_count DESC, last_liked_at DESC
        LIMIT ?
    """
    params = [str(guild_id)] + user_ids + [int(limit)]
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    return [dict(row) for row in rows]

def top_liked_for_guild(*, guild_id: int | str, limit: int = 50) -> list[dict]:
    """Return liked tracks for the whole guild (any liker), ordered by like count.

    Used as a fallback for autofill when no humans are in VC — gives the bot
    something to play that's still rooted in this server's taste, without
    biasing toward any particular member.
    """
    conn = get_conn()
    sql = """
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
          AND NOT (
            LOWER(COALESCE(t.title, '')) IN ('unknown title', 'untitled', '')
            AND LOWER(COALESCE(t.artist, '')) IN ('unknown', 'unknown artist', '', 'none')
          )
        GROUP BY l.track_id
        ORDER BY like_count DESC, last_liked_at DESC
        LIMIT ?
    """
    cur = conn.execute(sql, [str(guild_id), int(limit)])
    return [dict(row) for row in cur.fetchall()]

def get_user_liked_tracks_all_guilds(user_id: int | str) -> list[dict]:
    """Get all tracks liked by a user across all guilds, grouped by track_id.
    Returns tracks ordered by user's like count (most liked first).
    Each track includes the user's personal like count.
    Excludes tracks with 'Unknown Title' or 'Unknown' artist.
    
    Returns:
        List of dicts with: track_id, user_like_count, title, artist, source_url
    """
    conn = get_conn()
    sql = """
        SELECT
            l.track_id,
            COUNT(*) AS user_like_count,
            MAX(l.created_at) AS last_liked_at,
            t.title,
            t.artist,
            t.source_url
        FROM likes l
        JOIN tracks t ON t.id = l.track_id
        WHERE l.user_id = ?
          AND NOT (
            LOWER(COALESCE(t.title, '')) IN ('unknown title', 'untitled', '')
            AND LOWER(COALESCE(t.artist, '')) IN ('unknown', 'unknown artist', '', 'none')
          )
        GROUP BY l.track_id
        ORDER BY user_like_count DESC, last_liked_at DESC
    """
    cur = conn.execute(sql, (str(user_id),))
    rows = cur.fetchall()
    return [dict(row) for row in rows]


# ------------------------------
# Track health (broken-song cleanup)
# ------------------------------

def record_track_failure(*, url: str, reason: str) -> None:
    """Increment the failure counter for a URL.

    Only call this when the resolver returns a *confirmed* permanent failure
    (e.g. HTTP 404/410). Transient failures must NOT be recorded here, and
    callers should always run their circuit breaker first.
    """
    if not url:
        return
    conn = get_conn()
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO track_health (url, failure_count, first_failure_at, last_failure_at, last_failure_reason)
        VALUES (?, 1, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            failure_count       = track_health.failure_count + 1,
            first_failure_at    = COALESCE(track_health.first_failure_at, ?),
            last_failure_at     = ?,
            last_failure_reason = ?
        """,
        (url, now, now, reason, now, now, reason),
    )


def record_track_success(*, url: str) -> None:
    """Reset the failure counter for a URL after a successful resolve.

    A success means whatever was wrong with the URL is no longer wrong, so we
    don't want stale counters causing accidental future deletes.
    """
    if not url:
        return
    conn = get_conn()
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO track_health (url, failure_count, last_success_at)
        VALUES (?, 0, ?)
        ON CONFLICT(url) DO UPDATE SET
            failure_count    = 0,
            first_failure_at = NULL,
            last_success_at  = ?
        """,
        (url, now, now),
    )


def get_unhealthy_tracks(
    *,
    min_failures: int = 3,
    min_age_hours: int = 24,
    reason: str = "gone",
) -> list[dict]:
    """List URLs eligible for cleanup.

    A track is eligible when:
      - failure_count >= min_failures
      - last_failure_reason == reason
      - (last_failure_at - first_failure_at) >= min_age_hours
      - last_success_at is older than first_failure_at (or null)
    """
    conn = get_conn()
    min_age_sec = int(min_age_hours) * 3600
    rows = conn.execute(
        """
        SELECT url, failure_count, first_failure_at, last_failure_at,
               last_failure_reason, last_success_at
        FROM track_health
        WHERE failure_count >= ?
          AND last_failure_reason = ?
          AND first_failure_at IS NOT NULL
          AND last_failure_at IS NOT NULL
          AND (last_failure_at - first_failure_at) >= ?
          AND (last_success_at IS NULL OR last_success_at < first_failure_at)
        ORDER BY failure_count DESC, last_failure_at DESC
        """,
        (int(min_failures), reason, min_age_sec),
    ).fetchall()
    return [dict(r) for r in rows]


def clear_track_health(urls: Iterable[str]) -> int:
    """Forget health tracking for the given URLs (e.g. after manual cleanup)."""
    urls = [u for u in urls if u]
    if not urls:
        return 0
    conn = get_conn()
    placeholders = ",".join(["?"] * len(urls))
    cur = conn.execute(f"DELETE FROM track_health WHERE url IN ({placeholders})", urls)
    return cur.rowcount or 0


def prune_likes_and_tracks_for_urls(urls: Iterable[str]) -> dict:
    """Hard-delete likes and track rows whose source_url is in `urls`.

    Returns a dict with row counts: {'likes': N, 'tracks': M}. Intended for
    cleanup of permanently-deleted Suno songs only — call sites must already
    have established that these tracks are truly gone (see get_unhealthy_tracks).
    """
    urls = [u for u in urls if u]
    if not urls:
        return {"likes": 0, "tracks": 0}
    conn = get_conn()
    placeholders = ",".join(["?"] * len(urls))
    # Likes are CASCADE-deleted on tracks delete, but we count them explicitly
    # so the caller can show an accurate summary.
    likes_cur = conn.execute(
        f"""
        DELETE FROM likes
        WHERE track_id IN (
            SELECT id FROM tracks WHERE source_url IN ({placeholders})
        )
        """,
        urls,
    )
    tracks_cur = conn.execute(
        f"DELETE FROM tracks WHERE source_url IN ({placeholders})",
        urls,
    )
    return {"likes": likes_cur.rowcount or 0, "tracks": tracks_cur.rowcount or 0}


# ------------------------------
# Anonymous contests
# ------------------------------

# A contest is "active" until it reaches the 'closed' status. There should be
# at most one active contest per guild at any time.
_CONTEST_ACTIVE_STATUSES = ("collecting", "submissions_closed", "playing", "voting")


def _refresh_submissions_state(contest: dict) -> dict:
    """If a collecting contest is past its deadline, close submissions."""
    if contest.get("status") != "collecting":
        return contest
    close_at = contest.get("submissions_close_at")
    if close_at is not None and int(close_at) <= int(time.time()):
        set_contest_status(contest_id=int(contest["id"]), status="submissions_closed")
        updated = dict(contest)
        updated["status"] = "submissions_closed"
        return updated
    return contest


def submissions_are_open(contest: dict) -> bool:
    """True while new entries are accepted."""
    return _refresh_submissions_state(contest).get("status") == "collecting"


def get_active_contest(*, guild_id: int | str) -> Optional[dict]:
    """Return the most recent non-closed contest for a guild, or None."""
    conn = get_conn()
    placeholders = ",".join(["?"] * len(_CONTEST_ACTIVE_STATUSES))
    row = conn.execute(
        f"""
        SELECT * FROM contests
        WHERE guild_id = ? AND status IN ({placeholders})
        ORDER BY id DESC
        LIMIT 1
        """,
        [str(guild_id), *_CONTEST_ACTIVE_STATUSES],
    ).fetchone()
    if not row:
        return None
    return _refresh_submissions_state(dict(row))


def close_due_submissions() -> int:
    """Auto-close collecting contests whose deadline has passed. Returns rows updated."""
    conn = get_conn()
    now = int(time.time())
    cur = conn.execute(
        """
        UPDATE contests
        SET status = 'submissions_closed'
        WHERE status = 'collecting'
          AND submissions_close_at IS NOT NULL
          AND submissions_close_at <= ?
        """,
        (now,),
    )
    return int(cur.rowcount or 0)


def get_contest(*, contest_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM contests WHERE id = ?", (int(contest_id),)).fetchone()
    return _refresh_submissions_state(dict(row)) if row else None


def create_contest(
    *,
    guild_id: int | str,
    name: Optional[str] = None,
    created_by: Optional[str] = None,
    submissions_close_at: Optional[int] = None,
) -> int:
    """Create a new contest in the 'collecting' state and return its id."""
    conn = get_conn()
    now = int(time.time())
    cur = conn.execute(
        """
        INSERT INTO contests (guild_id, name, status, created_by, created_at, submissions_close_at)
        VALUES (?, ?, 'collecting', ?, ?, ?)
        """,
        (
            str(guild_id),
            name,
            str(created_by) if created_by is not None else None,
            now,
            int(submissions_close_at) if submissions_close_at is not None else None,
        ),
    )
    return int(cur.lastrowid)


def close_contest_submissions(*, contest_id: int) -> None:
    """Stop accepting new entries; the contest stays active for play/vote/results."""
    set_contest_status(contest_id=contest_id, status="submissions_closed")


def set_contest_deadline(*, contest_id: int, close_at: Optional[int]) -> None:
    """Set or clear the submissions deadline (unix seconds, UTC)."""
    conn = get_conn()
    conn.execute(
        "UPDATE contests SET submissions_close_at = ? WHERE id = ?",
        (int(close_at) if close_at is not None else None, int(contest_id)),
    )


def set_contest_status(*, contest_id: int, status: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE contests SET status = ? WHERE id = ?", (status, int(contest_id)))


def set_contest_name(*, contest_id: int, name: Optional[str]) -> None:
    """Set (or clear) the name of an existing contest."""
    conn = get_conn()
    conn.execute(
        "UPDATE contests SET name = ? WHERE id = ?",
        ((name.strip() if name and name.strip() else None), int(contest_id)),
    )


def set_contest_voting_message(*, contest_id: int, channel_id: int | str, message_id: int | str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE contests SET voting_channel_id = ?, voting_message_id = ? WHERE id = ?",
        (str(channel_id), str(message_id), int(contest_id)),
    )


def cancel_active_contest(*, guild_id: int | str) -> int:
    """Mark all active contests for a guild as 'closed'. Returns rows affected."""
    conn = get_conn()
    placeholders = ",".join(["?"] * len(_CONTEST_ACTIVE_STATUSES))
    cur = conn.execute(
        f"""
        UPDATE contests SET status = 'closed'
        WHERE guild_id = ? AND status IN ({placeholders})
        """,
        [str(guild_id), *_CONTEST_ACTIVE_STATUSES],
    )
    return cur.rowcount or 0


def entry_exists(*, contest_id: int, song_id: str) -> bool:
    if not song_id:
        return False
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM contest_entries WHERE contest_id = ? AND song_id = ?",
        (int(contest_id), song_id),
    ).fetchone()
    return bool(row)


def add_contest_entry(
    *,
    contest_id: int,
    url: str,
    song_id: Optional[str],
    submitted_by: Optional[str] = None,
    submitted_by_name: Optional[str] = None,
) -> int:
    """Insert a contest entry and return its id."""
    conn = get_conn()
    now = int(time.time())
    cur = conn.execute(
        """
        INSERT INTO contest_entries
            (contest_id, url, song_id, submitted_by, submitted_by_name, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            int(contest_id),
            url,
            song_id,
            str(submitted_by) if submitted_by is not None else None,
            submitted_by_name,
            now,
        ),
    )
    return int(cur.lastrowid)


def count_entries(*, contest_id: int) -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM contest_entries WHERE contest_id = ?",
        (int(contest_id),),
    ).fetchone()
    return int(row["c"])


def get_contest_entries(*, contest_id: int, ordered: bool = True) -> list[dict]:
    """Return entries for a contest. Ordered by entry_no (then id) when assigned."""
    conn = get_conn()
    order = "ORDER BY (entry_no IS NULL), entry_no ASC, id ASC" if ordered else "ORDER BY id ASC"
    rows = conn.execute(
        f"SELECT * FROM contest_entries WHERE contest_id = ? {order}",
        (int(contest_id),),
    ).fetchall()
    return [dict(r) for r in rows]


def assign_entry_numbers(*, contest_id: int, shuffle: bool = True) -> int:
    """Assign entry_no to entries that don't have one yet, then return total count.

    Order is randomized once (shuffle=True) so the play/vote order isn't just the
    submission order. Existing entry_no values are preserved, so the order stays
    stable across repeat listening parties and voting. Any brand-new entries added
    later are appended after the current max number.
    """
    conn = get_conn()
    existing = conn.execute(
        "SELECT COALESCE(MAX(entry_no), 0) AS mx FROM contest_entries WHERE contest_id = ?",
        (int(contest_id),),
    ).fetchone()
    next_no = int(existing["mx"]) + 1

    unnumbered = conn.execute(
        "SELECT id FROM contest_entries WHERE contest_id = ? AND entry_no IS NULL ORDER BY id ASC",
        (int(contest_id),),
    ).fetchall()
    ids = [r["id"] for r in unnumbered]
    if shuffle:
        random.shuffle(ids)

    for offset, entry_id in enumerate(ids):
        conn.execute(
            "UPDATE contest_entries SET entry_no = ? WHERE id = ?",
            (next_no + offset, entry_id),
        )

    total = conn.execute(
        "SELECT COUNT(*) AS c FROM contest_entries WHERE contest_id = ?",
        (int(contest_id),),
    ).fetchone()
    return int(total["c"])


def update_entry_metadata(
    *,
    entry_id: int,
    title: Optional[str] = None,
    artist: Optional[str] = None,
    cover_url: Optional[str] = None,
) -> None:
    conn = get_conn()
    conn.execute(
        """
        UPDATE contest_entries SET
            title     = COALESCE(?, title),
            artist    = COALESCE(?, artist),
            cover_url = COALESCE(?, cover_url)
        WHERE id = ?
        """,
        (title, artist, cover_url, int(entry_id)),
    )


def get_entry_owner(*, contest_id: int, song_id: str) -> Optional[str]:
    """Return the submitter (user id) of the entry with this song, or None."""
    if not song_id:
        return None
    conn = get_conn()
    row = conn.execute(
        "SELECT submitted_by FROM contest_entries WHERE contest_id = ? AND song_id = ? LIMIT 1",
        (int(contest_id), song_id),
    ).fetchone()
    return row["submitted_by"] if row else None


def get_user_entry(*, contest_id: int, submitted_by: int | str) -> Optional[dict]:
    """Return a user's existing entry in this contest, or None."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM contest_entries WHERE contest_id = ? AND submitted_by = ? ORDER BY id ASC LIMIT 1",
        (int(contest_id), str(submitted_by)),
    ).fetchone()
    return dict(row) if row else None


def get_entry_by_artist(*, contest_id: int, artist: str) -> Optional[dict]:
    """Return an existing entry with the same artist (case-insensitive), or None."""
    if not artist or not artist.strip():
        return None
    conn = get_conn()
    row = conn.execute(
        """
        SELECT * FROM contest_entries
        WHERE contest_id = ? AND LOWER(TRIM(COALESCE(artist, ''))) = ?
        ORDER BY id ASC LIMIT 1
        """,
        (int(contest_id), artist.strip().lower()),
    ).fetchone()
    return dict(row) if row else None


def replace_user_entry(*, entry_id: int, url: str, song_id: Optional[str]) -> None:
    """Swap a user's entry to a new song, clearing resolved metadata + number."""
    conn = get_conn()
    now = int(time.time())
    conn.execute(
        """
        UPDATE contest_entries
        SET url = ?, song_id = ?, title = NULL, artist = NULL, cover_url = NULL,
            entry_no = NULL, created_at = ?
        WHERE id = ?
        """,
        (url, song_id, now, int(entry_id)),
    )


def get_user_votes(*, contest_id: int, user_id: int | str) -> list[int]:
    """Return the list of entry_ids this user has voted for."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT entry_id FROM contest_votes WHERE contest_id = ? AND user_id = ?",
        (int(contest_id), str(user_id)),
    ).fetchall()
    return [int(r["entry_id"]) for r in rows]


def set_user_votes(*, contest_id: int, user_id: int | str, entry_ids: list[int], max_votes: int = 3) -> list[int]:
    """Replace a user's votes with exactly `entry_ids` (capped at `max_votes`).

    Returns the final list of entry_ids stored for the user.
    """
    conn = get_conn()
    cid, uid = int(contest_id), str(user_id)
    # De-dupe while preserving order, then cap.
    seen: list[int] = []
    for e in entry_ids:
        ei = int(e)
        if ei not in seen:
            seen.append(ei)
    seen = seen[: int(max_votes)]

    now = int(time.time())
    conn.execute("DELETE FROM contest_votes WHERE contest_id = ? AND user_id = ?", (cid, uid))
    for ei in seen:
        conn.execute(
            "INSERT INTO contest_votes (contest_id, entry_id, user_id, created_at) VALUES (?, ?, ?, ?)",
            (cid, ei, uid, now),
        )
    return seen


def toggle_vote(*, contest_id: int, entry_id: int, user_id: int | str, max_votes: int = 3) -> dict:
    """Toggle a user's vote for an entry, capped at `max_votes` distinct entries.

    Returns {'status': 'added'|'removed'|'at_limit', 'count': <user's vote count>}.
    """
    conn = get_conn()
    cid, eid, uid = int(contest_id), int(entry_id), str(user_id)
    existing = conn.execute(
        "SELECT id FROM contest_votes WHERE contest_id = ? AND user_id = ? AND entry_id = ?",
        (cid, uid, eid),
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM contest_votes WHERE id = ?", (existing["id"],))
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM contest_votes WHERE contest_id = ? AND user_id = ?",
            (cid, uid),
        ).fetchone()["c"]
        return {"status": "removed", "count": int(count)}

    count = conn.execute(
        "SELECT COUNT(*) AS c FROM contest_votes WHERE contest_id = ? AND user_id = ?",
        (cid, uid),
    ).fetchone()["c"]
    if int(count) >= int(max_votes):
        return {"status": "at_limit", "count": int(count)}

    conn.execute(
        "INSERT INTO contest_votes (contest_id, entry_id, user_id, created_at) VALUES (?, ?, ?, ?)",
        (cid, eid, uid, int(time.time())),
    )
    return {"status": "added", "count": int(count) + 1}


def tally_votes(*, contest_id: int) -> dict:
    """Return vote tallies for a contest.

    Returns a dict: {
        'total': total votes cast,
        'by_entry': { entry_id: count, ... },
    }
    """
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT entry_id, COUNT(*) AS votes
        FROM contest_votes
        WHERE contest_id = ?
        GROUP BY entry_id
        """,
        (int(contest_id),),
    ).fetchall()
    by_entry = {int(r["entry_id"]): int(r["votes"]) for r in rows}
    total = sum(by_entry.values())
    return {"total": total, "by_entry": by_entry}