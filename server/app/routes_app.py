"""Endpoints used by the child Android app (thin wrappers around app.db logic)."""
import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import config, db, notify
from .auth import request_info, require_device

router = APIRouter(prefix="/api", tags=["app"])


@router.get("/today")
def today(request: Request, device_id: str = Depends(require_device)):
    conn = db.get_conn()
    try:
        ip, ua = request_info(request)
        db.log_activity(conn, device_id, "app_open", "", ip, ua)
        conn.commit()
        s = db.get_settings(conn)
        now = datetime.datetime.now(ZoneInfo(s["timezone"]))
        weekday = now.weekday()  # 0=Monday
        chs = conn.execute(
            "SELECT * FROM channels WHERE weekday=? ORDER BY title", (weekday,)).fetchall()
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        watched_last_hour = conn.execute(
            "SELECT COUNT(*) AS n FROM watch_progress WHERE last_played_at >= ?",
            (cutoff,)).fetchone()["n"]
        latest = conn.execute(
            "SELECT COALESCE(MAX(app_version),0) AS v FROM devices").fetchone()["v"]
        today_ids = {c["channel_id"] for c in chs}
        exhausted = [e["title"] for e in notify.check_exhausted(conn)
                     if e["channel_id"] in today_ids]
        return {
            "date": now.date().isoformat(),
            "weekday": weekday,
            "timezone": s["timezone"],
            "watched_last_hour": watched_last_hour,
            "latest_app_version": latest,
            "exhausted_channels": exhausted,
            "channels": [{"title": c["title"], "thumbnail_url": c["thumbnail_url"]}
                         for c in chs],
            "videos": db.eligible_videos(conn, [c["channel_id"] for c in chs],
                                         s["min_seconds"], s["max_seconds"]),
        }
    finally:
        conn.close()


@router.get("/history")
def history(device_id: str = Depends(require_device)):
    conn = db.get_conn()
    try:
        s = db.get_settings(conn)
        weekday = datetime.datetime.now(ZoneInfo(s["timezone"])).weekday()
        today_ids = [r["channel_id"] for r in conn.execute(
            "SELECT channel_id FROM channels WHERE weekday=?", (weekday,))]
        return {"videos": db.history_videos(conn, today_ids,
                                            s["min_seconds"], s["max_seconds"])}
    finally:
        conn.close()


def require_playable(conn, video_id: str) -> None:
    video = conn.execute("SELECT * FROM videos WHERE video_id=?", (video_id,)).fetchone()
    if not video:
        raise HTTPException(404, "Unknown video")
    settings = db.get_settings(conn)
    weekday = datetime.datetime.now(ZoneInfo(settings["timezone"])).weekday()
    scheduled = conn.execute(
        "SELECT 1 FROM channels WHERE weekday=? AND channel_id=?",
        (weekday, video["channel_id"]),
    ).fetchone()
    allowed = (scheduled and video["available"] and not video["blacklisted"]
               and not video["embed_blocked"]
               and settings["min_seconds"] <= video["duration_seconds"] <= settings["max_seconds"]
               and not db.keyword_matcher(conn)(video["title"]))
    if not allowed:
        raise HTTPException(403, "This video is not available under today's rules")


@router.get("/playback/{video_id}")
def playback(video_id: str, device_id: str = Depends(require_device)):
    conn = db.get_conn()
    try:
        require_playable(conn, video_id)
        return {"ok": True}
    finally:
        conn.close()


@router.get("/player")
def player(v: str, device_id: str = Depends(require_device)):
    conn = db.get_conn()
    try:
        require_playable(conn, v)
    finally:
        conn.close()
    return FileResponse(config.PLAYER_FILE, headers={"Cache-Control": "no-store"})


class ProgressIn(BaseModel):
    video_id: str
    position_seconds: int = Field(ge=0)
    event: str = Field(pattern="^(start|heartbeat|pause|ended)$")


@router.post("/progress")
def progress(body: ProgressIn, request: Request, device_id: str = Depends(require_device)):
    conn = db.get_conn()
    try:
        require_playable(conn, body.video_id)
        result = db.apply_progress(conn, body.video_id, body.position_seconds,
                                   body.event, device_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Unknown video")
        if result.get("completed"):
            notify.check_exhausted(conn)
        if body.event != "heartbeat":  # heartbeats are too noisy to log
            ip, ua = request_info(request)
            v = conn.execute("SELECT title FROM videos WHERE video_id=?",
                             (body.video_id,)).fetchone()
            db.log_activity(conn, device_id, f"video_{body.event}",
                            f"{v['title'] if v else body.video_id} @ {body.position_seconds}s",
                            ip, ua)
            conn.commit()
        return result
    finally:
        conn.close()


class UnplayableIn(BaseModel):
    video_id: str


@router.post("/unplayable")
def unplayable(body: UnplayableIn, request: Request,
               device_id: str = Depends(require_device)):
    """Device reports YouTube refused embedded playback -> hide from the grid."""
    conn = db.get_conn()
    try:
        require_playable(conn, body.video_id)
        cur = conn.execute("UPDATE videos SET embed_blocked=1 WHERE video_id=?",
                           (body.video_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Unknown video")
        ip, ua = request_info(request)
        db.log_activity(conn, device_id, "video_unplayable", body.video_id, ip, ua)
        conn.commit()
        v = conn.execute("SELECT title FROM videos WHERE video_id=?", (body.video_id,)).fetchone()
        notify.send("Video won't play",
                    f"YouTube refused to play \"{v['title'] if v else body.video_id}\" on "
                    f"{device_id}. It has been hidden (console → Videos → embed blocked).", -1)
        return {"ok": True}
    finally:
        conn.close()
