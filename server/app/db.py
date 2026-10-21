import datetime
import os
import re
import secrets
import sqlite3

from . import config
from .security import hash_password

SCHEMA = """
CREATE TABLE IF NOT EXISTS login_attempts (
  client TEXT PRIMARY KEY,
  started_at INTEGER NOT NULL,
  attempts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
  weekday        INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6), -- 0=Monday
  channel_id     TEXT NOT NULL,
  title          TEXT,
  thumbnail_url  TEXT,
  uploads_playlist_id TEXT,
  updated_at     TEXT,
  PRIMARY KEY (weekday, channel_id)                -- multiple channels per day
);

CREATE TABLE IF NOT EXISTS settings (
  id             INTEGER PRIMARY KEY CHECK (id = 1),
  min_seconds    INTEGER NOT NULL DEFAULT 420,
  max_seconds    INTEGER NOT NULL DEFAULT 780,
  sync_interval_minutes INTEGER NOT NULL DEFAULT 360,
  timezone       TEXT NOT NULL DEFAULT 'UTC',
  parent_password_hash TEXT NOT NULL,
  device_token   TEXT NOT NULL,
  require_device_token INTEGER NOT NULL DEFAULT 1,
  last_sync_at   TEXT,
  pushover_user  TEXT NOT NULL DEFAULT '',
  pushover_token TEXT NOT NULL DEFAULT '',
  pushover_enabled INTEGER NOT NULL DEFAULT 1
);

-- remembers which alerts have already fired (one push per problem, not per request)
CREATE TABLE IF NOT EXISTS notify_state (
  key        TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
  device_id      TEXT PRIMARY KEY,
  label          TEXT,
  first_seen_at  TEXT NOT NULL,
  last_seen_at   TEXT NOT NULL,
  app_version    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS videos (
  video_id       TEXT PRIMARY KEY,
  channel_id     TEXT NOT NULL,
  title          TEXT NOT NULL,
  thumbnail_url  TEXT,
  duration_seconds INTEGER NOT NULL,
  published_at   TEXT NOT NULL,
  available      INTEGER NOT NULL DEFAULT 1,
  blacklisted    INTEGER NOT NULL DEFAULT 0,
  first_seen_at  TEXT NOT NULL,
  embed_blocked  INTEGER NOT NULL DEFAULT 0,     -- set when a device reports playback refused
  view_count     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id, published_at DESC);

CREATE TABLE IF NOT EXISTS blocked_keywords (
  keyword    TEXT PRIMARY KEY COLLATE NOCASE,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT NOT NULL,
  actor      TEXT NOT NULL,        -- 'parent' or a device_id
  action     TEXT NOT NULL,
  detail     TEXT,
  ip         TEXT,
  user_agent TEXT
);

-- shared across all devices
CREATE TABLE IF NOT EXISTS watch_progress (
  video_id       TEXT PRIMARY KEY REFERENCES videos(video_id),
  position_seconds INTEGER NOT NULL DEFAULT 0,
  max_position_seconds INTEGER NOT NULL DEFAULT 0,
  completed      INTEGER NOT NULL DEFAULT 0,
  play_count     INTEGER NOT NULL DEFAULT 0,
  first_played_at TEXT,
  last_played_at TEXT,
  last_device_id TEXT
);
"""


def utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_conn() -> sqlite3.Connection:
    from .platform_store import library_path
    conn = sqlite3.connect(library_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA temp_store=MEMORY")  # systemd sandbox: /tmp is read-only
    return conn


def get_settings(conn) -> sqlite3.Row:
    return conn.execute("SELECT * FROM settings WHERE id=1").fetchone()


def _migrate(conn) -> None:
    """v1 -> v2: channels had PRIMARY KEY weekday (one channel/day)."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='channels'").fetchone()
    if row and "INTEGER PRIMARY KEY" in row["sql"]:
        conn.executescript("""
            ALTER TABLE channels RENAME TO channels_old;
            CREATE TABLE channels (
              weekday        INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
              channel_id     TEXT NOT NULL,
              title          TEXT,
              thumbnail_url  TEXT,
              uploads_playlist_id TEXT,
              updated_at     TEXT,
              PRIMARY KEY (weekday, channel_id)
            );
            INSERT INTO channels SELECT weekday, channel_id, title, thumbnail_url,
                uploads_playlist_id, updated_at FROM channels_old;
            DROP TABLE channels_old;
        """)
        print("[dailytelly] migrated channels table to multi-channel-per-day schema")
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(videos)")]
    if cols and "embed_blocked" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN embed_blocked INTEGER NOT NULL DEFAULT 0")
        print("[dailytelly] added videos.embed_blocked column")
    if cols and "view_count" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN view_count INTEGER NOT NULL DEFAULT 0")
        print("[dailytelly] added videos.view_count column")
    dcols = [r["name"] for r in conn.execute("PRAGMA table_info(devices)")]
    if dcols and "app_version" not in dcols:
        conn.execute("ALTER TABLE devices ADD COLUMN app_version INTEGER NOT NULL DEFAULT 0")
        print("[dailytelly] added devices.app_version column")
    scols = [r["name"] for r in conn.execute("PRAGMA table_info(settings)")]
    if scols and "require_device_token" not in scols:
        conn.execute("ALTER TABLE settings ADD COLUMN require_device_token INTEGER NOT NULL DEFAULT 1")
        print("[dailytelly] added settings.require_device_token column")
    for col, ddl in (("pushover_user", "TEXT NOT NULL DEFAULT ''"),
                     ("pushover_token", "TEXT NOT NULL DEFAULT ''"),
                     ("pushover_enabled", "INTEGER NOT NULL DEFAULT 1")):
        if scols and col not in scols:
            conn.execute(f"ALTER TABLE settings ADD COLUMN {col} {ddl}")
            print(f"[dailytelly] added settings.{col} column")


def init_db() -> None:
    from .platform_store import library_path
    library_path().parent.mkdir(parents=True, exist_ok=True)
    config.secret_key()
    conn = get_conn()
    try:
        _migrate(conn)
        conn.executescript(SCHEMA)
        if not conn.execute("SELECT 1 FROM settings WHERE id=1").fetchone():
            password = secrets.token_urlsafe(48)  # legacy column; platform accounts have no password
            token = secrets.token_hex(32)
            conn.execute(
                """INSERT INTO settings
                   (id, min_seconds, max_seconds, sync_interval_minutes, timezone,
                    parent_password_hash, device_token)
                   VALUES (1, 420, 780, 360, 'UTC', ?, ?)""",
                (hash_password(password), token),
            )
            conn.commit()

    finally:
        conn.close()


# ---------------- core business logic (stdlib-only, unit-testable) ----------------

COMPLETION_THRESHOLD = 0.9


def watched_pct(maxpos: int, duration: int) -> int:
    return min(100, round(100 * maxpos / duration)) if duration else 0


def _keyword_regex(keyword: str) -> re.Pattern:
    """Whole-word, case-insensitive; a trailing/inner * matches any word chars."""
    body = re.escape(keyword.strip()).replace(r"\*", r"\w*")
    return re.compile(r"(?<!\w)" + body + r"(?!\w)", re.IGNORECASE)


def keyword_matcher(conn):
    """Returns f(title) -> matching keyword or None."""
    pats = [(r["keyword"], _keyword_regex(r["keyword"]))
            for r in conn.execute("SELECT keyword FROM blocked_keywords")]

    def match(title: str):
        for kw, pat in pats:
            if pat.search(title or ""):
                return kw
        return None
    return match


def interleave_by_channel(videos: list[dict]) -> list[dict]:
    """Round-robin across channels: round 1 = each channel's newest video,
    round 2 = each channel's next-newest, etc. Each round is ordered newest
    first. Input must already be sorted newest first."""
    per_channel: dict[str, list[dict]] = {}
    for v in videos:
        per_channel.setdefault(v["channel_id"], []).append(v)
    out, rnd = [], 0
    while True:
        batch = [vs[rnd] for vs in per_channel.values() if rnd < len(vs)]
        if not batch:
            return out
        batch.sort(key=lambda v: v["published_at"], reverse=True)
        out.extend(batch)
        rnd += 1


def eligible_videos(conn, channel_ids: list[str], min_seconds: int, max_seconds: int) -> list[dict]:
    """Videos the child app may see across the given channels: available, not
    blacklisted, in duration range. Unwatched videos come first, interleaved
    round-robin by channel; fully watched videos (completed or >=95%) follow,
    newest first. Shared progress."""
    if not channel_ids:
        return []
    qmarks = ",".join("?" * len(channel_ids))
    rows = conn.execute(
        f"""SELECT v.video_id, v.channel_id, v.title, v.thumbnail_url, v.duration_seconds,
                  v.published_at, v.view_count,
                  COALESCE(p.position_seconds,0) AS position_seconds,
                  COALESCE(p.max_position_seconds,0) AS max_position_seconds,
                  COALESCE(p.completed,0) AS completed, p.last_played_at
           FROM videos v LEFT JOIN watch_progress p ON p.video_id = v.video_id
           WHERE v.channel_id IN ({qmarks}) AND v.available = 1 AND v.blacklisted = 0
             AND v.embed_blocked = 0
             AND v.duration_seconds BETWEEN ? AND ?
           ORDER BY v.published_at DESC""",
        (*channel_ids, min_seconds, max_seconds),
    ).fetchall()
    blocked = keyword_matcher(conn)
    vids = [
        {
            "video_id": r["video_id"],
            "channel_id": r["channel_id"],
            "title": r["title"],
            "thumbnail_url": r["thumbnail_url"],
            "duration_seconds": r["duration_seconds"],
            "published_at": r["published_at"],
            "position_seconds": r["position_seconds"],
            "view_count": r["view_count"],
            "completed": bool(r["completed"]),
            "watched_pct": 100 if r["completed"]
                else watched_pct(r["max_position_seconds"], r["duration_seconds"]),
            "last_played_at": r["last_played_at"],
        }
        for r in rows if not blocked(r["title"])
    ]
    # same "watched" rule the clients use to hide videos from the main grid
    watched = [v for v in vids if v["completed"] or v["watched_pct"] >= 95]
    unwatched = [v for v in vids if not (v["completed"] or v["watched_pct"] >= 95)]
    return interleave_by_channel(unwatched) + watched


def history_videos(conn, today_channel_ids: list[str], min_seconds: int,
                   max_seconds: int, limit: int = 300) -> list[dict]:
    """Fully-watched videos (completed or >=95%) across ALL configured channels,
    most recently watched first. 'playable' is true only for videos the child
    may play today, so history can't bypass the per-day channel rules."""
    playable_ids = {v["video_id"] for v in
                    eligible_videos(conn, today_channel_ids, min_seconds, max_seconds)}
    rows = conn.execute(
        """SELECT v.video_id, v.title, v.thumbnail_url, v.duration_seconds, v.published_at,
                  v.view_count, p.position_seconds, p.max_position_seconds, p.completed,
                  p.last_played_at
           FROM watch_progress p JOIN videos v ON v.video_id = p.video_id
           WHERE v.channel_id IN (SELECT channel_id FROM channels)
             AND v.available = 1 AND v.blacklisted = 0 AND v.embed_blocked = 0
             AND p.last_played_at IS NOT NULL
             AND (p.completed = 1 OR
                  (v.duration_seconds > 0 AND p.max_position_seconds >= 0.95 * v.duration_seconds))
           ORDER BY p.last_played_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    blocked = keyword_matcher(conn)
    rows = [r for r in rows if not blocked(r["title"])]
    return [
        {
            "video_id": r["video_id"],
            "title": r["title"],
            "thumbnail_url": r["thumbnail_url"],
            "duration_seconds": r["duration_seconds"],
            "published_at": r["published_at"],
            "position_seconds": r["position_seconds"],
            "view_count": r["view_count"],
            "completed": bool(r["completed"]),
            "watched_pct": 100 if r["completed"]
                else watched_pct(r["max_position_seconds"], r["duration_seconds"]),
            "last_played_at": r["last_played_at"],
            "playable": r["video_id"] in playable_ids,
        }
        for r in rows
    ]


def apply_progress(conn, video_id: str, position_seconds: int, event: str,
                   device_id: str) -> dict | None:
    """Upsert shared watch progress. Returns result dict, or None if video unknown.
    event: start | heartbeat | pause | ended."""
    # Serialize the read/modify/write across simultaneously playing household screens.
    started_transaction = not conn.in_transaction
    if started_transaction:
        conn.execute("BEGIN IMMEDIATE")
    v = conn.execute("SELECT duration_seconds FROM videos WHERE video_id=?", (video_id,)).fetchone()
    if not v:
        if started_transaction:
            conn.rollback()
        return None
    duration = v["duration_seconds"]
    now = utcnow()
    p = conn.execute("SELECT * FROM watch_progress WHERE video_id=?", (video_id,)).fetchone()

    pos = max(0, min(position_seconds, duration))
    maxpos = max(pos, p["max_position_seconds"] if p else 0)
    completed = 1 if (
        event == "ended"
        or (duration and maxpos >= COMPLETION_THRESHOLD * duration)
        or (p and p["completed"])
    ) else 0
    play_inc = 1 if event == "start" else 0

    if p:
        conn.execute(
            """UPDATE watch_progress SET position_seconds=?, max_position_seconds=?,
               completed=?, play_count=play_count+?, last_played_at=?, last_device_id=?
               WHERE video_id=?""",
            (pos, maxpos, completed, play_inc, now, device_id, video_id),
        )
    else:
        conn.execute(
            """INSERT INTO watch_progress (video_id, position_seconds, max_position_seconds,
               completed, play_count, first_played_at, last_played_at, last_device_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (video_id, pos, maxpos, completed, play_inc, now, now, device_id),
        )
    conn.commit()
    return {"ok": True, "completed": bool(completed), "watched_pct": watched_pct(maxpos, duration)}


def log_activity(conn, actor: str, action: str, detail: str = "",
                 ip: str = "", user_agent: str = "") -> None:
    # Retain configuration changes only, for 30 days. Never retain IP/UA or child viewing events here.
    if action.startswith("video_") or action == "app_open":
        return
    conn.execute(
        "INSERT INTO activity_log (ts, actor, action, detail, ip, user_agent) VALUES (?,?,?,?,?,?)",
        (utcnow(), actor[:64], action[:64], "", "", ""),
    )
    conn.execute("DELETE FROM activity_log WHERE ts < ?", ((datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),))
    row = conn.execute("SELECT MAX(id) AS m FROM activity_log").fetchone()
    if row["m"] and row["m"] % 500 == 0:  # occasional prune, keep last ~10k
        conn.execute("DELETE FROM activity_log WHERE id <= ?", (row["m"] - 10_000,))


def delete_orphan_videos(conn) -> int:
    """Remove videos (and their progress) whose channel is no longer configured."""
    conn.execute(
        """DELETE FROM watch_progress WHERE video_id IN
           (SELECT video_id FROM videos WHERE channel_id NOT IN
             (SELECT channel_id FROM channels))"""
    )
    cur = conn.execute(
        "DELETE FROM videos WHERE channel_id NOT IN (SELECT channel_id FROM channels)"
    )
    conn.commit()
    return cur.rowcount


def channel_stats(conn) -> dict:
    """Per configured channel:
       total     - synced videos
       eligible  - pass the rules (available, not blacklisted/keyword-blocked/embed-blocked,
                   within the length range)
       played    - fully watched (completed or >=95%)
       remaining - eligible and not yet fully watched (what the kid app can show)"""
    s = get_settings(conn)
    kw = keyword_matcher(conn)
    stats = {r["channel_id"]: {"total": 0, "eligible": 0, "played": 0, "remaining": 0}
             for r in conn.execute("SELECT DISTINCT channel_id FROM channels")}
    rows = conn.execute(
        """SELECT v.channel_id, v.title, v.duration_seconds, v.available, v.blacklisted,
                  v.embed_blocked, COALESCE(p.completed,0) AS completed,
                  COALESCE(p.max_position_seconds,0) AS maxpos
           FROM videos v LEFT JOIN watch_progress p ON p.video_id = v.video_id""")
    for r in rows:
        st = stats.get(r["channel_id"])
        if st is None:
            continue
        st["total"] += 1
        dur = r["duration_seconds"] or 0
        watched = bool(r["completed"]) or (dur > 0 and r["maxpos"] >= 0.95 * dur)
        allowed = (r["available"] and not r["blacklisted"] and not r["embed_blocked"]
                   and s["min_seconds"] <= dur <= s["max_seconds"] and not kw(r["title"]))
        if watched:
            st["played"] += 1
        if allowed:
            st["eligible"] += 1
            if not watched:
                st["remaining"] += 1
    return stats


def exhausted_channels(conn) -> list[dict]:
    """Configured channels with no eligible unwatched videos left."""
    stats = channel_stats(conn)
    by_channel: dict[str, dict] = {}
    for r in conn.execute("SELECT weekday, channel_id, title FROM channels ORDER BY weekday"):
        st = stats.get(r["channel_id"], {})
        if st.get("remaining", 0) > 0:
            continue
        e = by_channel.setdefault(r["channel_id"], {
            "channel_id": r["channel_id"], "title": r["title"] or r["channel_id"],
            "weekdays": [], **st})
        e["weekdays"].append(r["weekday"])
    return list(by_channel.values())


def notify_once(conn, key: str) -> bool:
    """True the first time a key is seen (caller should alert), False afterwards."""
    cur = conn.execute("INSERT OR IGNORE INTO notify_state (key, created_at) VALUES (?,?)",
                       (key, utcnow()))
    conn.commit()
    return cur.rowcount == 1


def notify_clear(conn, key: str) -> None:
    conn.execute("DELETE FROM notify_state WHERE key=?", (key,))
    conn.commit()
