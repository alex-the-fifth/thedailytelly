"""Household console endpoints. Every route requires a verified browser session."""
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import config, db, notify, youtube
from .auth import request_info, require_admin
from . import platform_store as store


def _log(request: Request, action: str, detail: str = ""):
    ip, ua = request_info(request)
    conn = db.get_conn()
    try:
        db.log_activity(conn, "parent", action, detail, ip, ua)
        conn.commit()
    finally:
        conn.close()

router = APIRouter(prefix="/admin", tags=["admin"])


# ---- channels ----

@router.get("/channels", dependencies=[Depends(require_admin)])
def get_channels():
    conn = db.get_conn()
    try:
        rows = conn.execute("SELECT * FROM channels ORDER BY weekday, title").fetchall()
        stats = db.channel_stats(conn)
        out = []
        for r in rows:
            d = dict(r)
            d["stats"] = stats.get(r["channel_id"],
                                   {"total": 0, "eligible": 0, "played": 0, "remaining": 0})
            out.append(d)
        return {"channels": out}
    finally:
        conn.close()


class ChannelIn(BaseModel):
    weekday: int = Field(ge=0, le=6)
    query: str


@router.put("/channels", dependencies=[Depends(require_admin)])
def set_channel(body: ChannelIn, request: Request, background: BackgroundTasks):
    account = store.require_account()
    store.limit("channel-add", account["id"], 20, 3600)
    conn = db.get_conn()
    try:
        if conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0] >= 70:
            raise HTTPException(409, "Channel schedule limit reached (70 entries)")
    finally:
        conn.close()
    try:
        ch = youtube.resolve_channel(body.query)
    except youtube.YouTubeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    conn = db.get_conn()
    try:
        conn.execute(
            """INSERT INTO channels (weekday, channel_id, title, thumbnail_url, uploads_playlist_id, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(weekday, channel_id) DO UPDATE SET
                 title=excluded.title, thumbnail_url=excluded.thumbnail_url,
                 uploads_playlist_id=excluded.uploads_playlist_id, updated_at=excluded.updated_at""",
            (body.weekday, ch["channel_id"], ch["title"], ch["thumbnail_url"],
             ch["uploads_playlist_id"], db.utcnow()),
        )
        conn.commit()
    finally:
        conn.close()
    _log(request, "channel_added", f"{ch['title']} -> weekday {body.weekday}")
    background.add_task(youtube.sync_all, True, False)
    return {"ok": True, "channel": ch}


@router.delete("/channels/{weekday}/{channel_id}", dependencies=[Depends(require_admin)])
def remove_channel(weekday: int, channel_id: str, request: Request):
    conn = db.get_conn()
    try:
        conn.execute("DELETE FROM channels WHERE weekday=? AND channel_id=?",
                     (weekday, channel_id))
        conn.commit()
        removed = db.delete_orphan_videos(conn)  # drop videos of unconfigured channels
        return {"ok": True, "videos_removed": removed}
    finally:
        conn.close()
        _log(request, "channel_removed", f"{channel_id} from weekday {weekday}")


# ---- settings ----

@router.get("/settings", dependencies=[Depends(require_admin)])
def get_settings():
    conn = db.get_conn()
    try:
        s = db.get_settings(conn)
        return {
            "min_seconds": s["min_seconds"], "max_seconds": s["max_seconds"],
            "sync_interval_minutes": s["sync_interval_minutes"],
            "timezone": s["timezone"], "last_sync_at": s["last_sync_at"],
            "youtube_api_key_set": bool(config.YOUTUBE_API_KEY),
        }
    finally:
        conn.close()


class SettingsIn(BaseModel):
    min_seconds: int = Field(ge=0)
    max_seconds: int = Field(ge=1)
    sync_interval_minutes: int = Field(ge=60, le=1440)
    timezone: str


@router.put("/settings", dependencies=[Depends(require_admin)])
def put_settings(body: SettingsIn, request: Request, response: Response):
    if body.min_seconds >= body.max_seconds:
        raise HTTPException(status_code=400, detail="min must be < max")
    try:
        ZoneInfo(body.timezone)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Unknown timezone: {body.timezone}")
    conn = db.get_conn()
    try:
        conn.execute("""UPDATE settings SET min_seconds=?,max_seconds=?,sync_interval_minutes=?,timezone=? WHERE id=1""",
                     (body.min_seconds,body.max_seconds,body.sync_interval_minutes,body.timezone))
        conn.commit()
        _log(request, "settings_changed")
        return {"ok": True}
    finally:
        conn.close()


# ---- videos ----

def _reason(v, s, kw_match=None) -> str:
    if not v["available"]:
        return "unavailable"
    if v["blacklisted"]:
        return "blacklisted"
    if kw_match and kw_match(v["title"]):
        return "keyword_blocked"
    if v["embed_blocked"]:
        return "embed_blocked"
    if v["duration_seconds"] < s["min_seconds"]:
        return "too_short"
    if v["duration_seconds"] > s["max_seconds"]:
        return "too_long"
    if v["completed"]:
        return "completed"
    return "eligible"


@router.get("/videos", dependencies=[Depends(require_admin)])
def list_videos(weekday: int | None = None, status: str = "all", q: str = ""):
    conn = db.get_conn()
    try:
        s = db.get_settings(conn)
        sql = """SELECT v.*, COALESCE(p.completed,0) AS completed,
                        COALESCE(p.max_position_seconds,0) AS max_position_seconds,
                        COALESCE(p.play_count,0) AS play_count, p.last_played_at
                 FROM videos v LEFT JOIN watch_progress p ON p.video_id=v.video_id"""
        args = []
        if weekday is not None:
            ids = [r["channel_id"] for r in conn.execute(
                "SELECT channel_id FROM channels WHERE weekday=?", (weekday,))]
            if not ids:
                return {"videos": []}
            sql += f" WHERE v.channel_id IN ({','.join('?' * len(ids))})"
            args.extend(ids)
        sql += " ORDER BY v.published_at DESC"
        out = []
        kw_match = db.keyword_matcher(conn)
        for r in conn.execute(sql, args):
            v = dict(r)
            v["reason"] = _reason(v, s, kw_match)
            v["blocked_keyword"] = kw_match(v["title"])
            v["watched_pct"] = (min(100, round(100 * v["max_position_seconds"] / v["duration_seconds"]))
                                if v["duration_seconds"] else 0)
            if status != "all" and v["reason"] != status:
                continue
            if q and q.lower() not in v["title"].lower():
                continue
            out.append(v)
        return {"videos": out[:500]}
    finally:
        conn.close()


@router.post("/videos/{video_id}/blacklist", dependencies=[Depends(require_admin)])
def toggle_blacklist(video_id: str, request: Request):
    conn = db.get_conn()
    try:
        cur = conn.execute(
            "UPDATE videos SET blacklisted = 1 - blacklisted WHERE video_id=?", (video_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Unknown video")
        conn.commit()
        b = conn.execute("SELECT blacklisted FROM videos WHERE video_id=?", (video_id,)).fetchone()
        _log(request, "blacklist_on" if b["blacklisted"] else "blacklist_off", video_id)
        return {"ok": True, "blacklisted": bool(b["blacklisted"])}
    finally:
        conn.close()


@router.post("/videos/{video_id}/clear-embed-block", dependencies=[Depends(require_admin)])
def clear_embed_block(video_id: str):
    conn = db.get_conn()
    try:
        conn.execute("UPDATE videos SET embed_blocked=0 WHERE video_id=?", (video_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@router.post("/videos/{video_id}/reset-progress", dependencies=[Depends(require_admin)])
def reset_progress(video_id: str, request: Request):
    conn = db.get_conn()
    try:
        conn.execute("DELETE FROM watch_progress WHERE video_id=?", (video_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()
        _log(request, "reset_progress", video_id)


# ---- keyword blacklist ----

@router.get("/keywords", dependencies=[Depends(require_admin)])
def get_keywords():
    conn = db.get_conn()
    try:
        kw_match = db.keyword_matcher(conn)
        titles = [r["title"] for r in conn.execute("SELECT title FROM videos")]
        rows = conn.execute(
            "SELECT keyword, created_at FROM blocked_keywords ORDER BY keyword").fetchall()
        out = []
        for r in rows:
            pat = db._keyword_regex(r["keyword"])
            out.append({"keyword": r["keyword"], "created_at": r["created_at"],
                        "matches": sum(1 for t in titles if pat.search(t or ""))})
        return {"keywords": out}
    finally:
        conn.close()


class KeywordIn(BaseModel):
    keyword: str = Field(min_length=1, max_length=100)


@router.post("/keywords", dependencies=[Depends(require_admin)])
def add_keyword(body: KeywordIn, request: Request):
    kw = " ".join(body.keyword.split())  # normalise whitespace
    if not kw or kw.replace("*", "") == "":
        raise HTTPException(status_code=400, detail="Keyword is empty")
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO blocked_keywords (keyword, created_at) VALUES (?,?)",
            (kw, db.utcnow()))
        conn.commit()
    finally:
        conn.close()
    _log(request, "keyword_added", kw)
    return {"ok": True, "keyword": kw}


@router.delete("/keywords/{keyword}", dependencies=[Depends(require_admin)])
def remove_keyword(keyword: str, request: Request):
    conn = db.get_conn()
    try:
        conn.execute("DELETE FROM blocked_keywords WHERE keyword=?", (keyword,))
        conn.commit()
    finally:
        conn.close()
    _log(request, "keyword_removed", keyword)
    return {"ok": True}


# ---- history / devices / sync ----

@router.get("/history", dependencies=[Depends(require_admin)])
def history(q: str = "", limit: int = 500):
    """Watch history, newest first. q filters by title or channel (case-insensitive)."""
    limit = max(1, min(limit, 2000))
    conn = db.get_conn()
    try:
        sql = """SELECT p.*, v.title, v.thumbnail_url, v.duration_seconds, v.channel_id,
                        (SELECT c.title FROM channels c WHERE c.channel_id = v.channel_id
                         LIMIT 1) AS channel_title
                 FROM watch_progress p JOIN videos v ON v.video_id=p.video_id
                 WHERE p.last_played_at IS NOT NULL"""
        args: list = []
        rows = conn.execute(sql + " ORDER BY p.last_played_at DESC").fetchall()
        needle = q.strip().casefold()
        out = []
        for r in rows:
            if needle and needle not in (r["title"] or "").casefold() \
                    and needle not in (r["channel_title"] or "").casefold():
                continue
            out.append(dict(r))
            if len(out) >= limit:
                break
        tz = db.get_settings(conn)["timezone"]
        return {"history": out, "timezone": tz, "total_matches": len(out)}
    finally:
        conn.close()


@router.post("/sync", dependencies=[Depends(require_admin)])
def trigger_sync(request: Request, background: BackgroundTasks, full: bool = False):
    store.limit("manual-sync", store.require_account()["id"], 4, 3600)
    _log(request, "sync_triggered")
    background.add_task(youtube.sync_all, full, True)
    return {"ok": True, "message": "Sync started"}


@router.get("/alerts", dependencies=[Depends(require_admin)])
def alerts():
    conn = db.get_conn()
    try:
        return {"exhausted": notify.check_exhausted(conn)}
    finally:
        conn.close()


@router.get("/sync-status", dependencies=[Depends(require_admin)])
def sync_status():
    return youtube.get_sync_status()


@router.get("/activity", dependencies=[Depends(require_admin)])
def activity():
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM activity_log ORDER BY id DESC LIMIT 300").fetchall()
        return {"activity": [dict(r) for r in rows]}
    finally:
        conn.close()
