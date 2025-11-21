-- tracks + plays schema
CREATE TABLE IF NOT EXISTS tracks (
  id TEXT PRIMARY KEY,
  title TEXT,
  artist TEXT,
  cover_url TEXT,
  source_url TEXT,
  duration_sec INTEGER,
  prompt TEXT,
  lyrics TEXT,
  meta_json TEXT,
  created_at INTEGER DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS plays (
  play_id INTEGER PRIMARY KEY AUTOINCREMENT,
  track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  guild_id TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  requested_by TEXT,
  context TEXT,
  started_at INTEGER NOT NULL,
  ended_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_plays_guild_started ON plays(guild_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_plays_track ON plays(track_id);
CREATE INDEX IF NOT EXISTS idx_plays_guild_track ON plays(guild_id, track_id);

CREATE VIEW IF NOT EXISTS v_recent AS
SELECT p.play_id, p.guild_id, p.started_at, p.ended_at, p.requested_by, p.context,
       t.id AS track_id, t.title, t.artist, t.source_url
FROM plays p
JOIN tracks t ON t.id = p.track_id;