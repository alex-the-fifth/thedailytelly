"""YouTube Data API v3 client + channel sync logic. All calls server-side."""
import re
import threading

from . import config, db

_statuses = {}
_status_lock = threading.Lock()


def _tenant_status():
    from .platform_store import require_account
    key = require_account()["id"]
    return _statuses.setdefault(key, {
        "running": False, "started_at": None, "current_channel": None,
        "channels_done": 0, "channels_total": 0, "new_videos": 0,
        "last_result": None, "last_finished_at": None,
    })


def _set_status(**kw):
    with _status_lock:
        _tenant_status().update(kw)


def get_sync_status():
    with _status_lock:
        return dict(_tenant_status())

API = "https://www.googleapis.com/youtube/v3"


class YouTubeError(Exception):
    pass


def _get(path: str, **params) -> dict:
    import httpx  # lazy: keeps module importable without deps
    if not config.YOUTUBE_API_KEY:
        raise YouTubeError("YOUTUBE_API_KEY is not configured")
    from .platform_store import reserve_catalog_units
    try:
        reserve_catalog_units(1, 'search' if path == 'search' else 'metadata')
    except ValueError as error:
        raise YouTubeError(str(error))
    params["key"] = config.YOUTUBE_API_KEY
    r = httpx.get(f"{API}/{path}", params=params, timeout=30)
    if r.status_code != 200:
        raise YouTubeError(f"Channel service returned HTTP {r.status_code}. Try again later or contact the operator.")
    return r.json()


_DUR = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def parse_duration(iso: str) -> int:
    m = _DUR.fullmatch(iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _channel_from_item(item: dict) -> dict:
    cid = item["id"]
    return {
        "channel_id": cid,
        "title": item["snippet"]["title"],
        "thumbnail_url": item["snippet"]["thumbnails"].get("default", {}).get("url"),
        "uploads_playlist_id": item["contentDetails"]["relatedPlaylists"]["uploads"],
    }


def resolve_channel(query: str) -> dict:
    """Accepts a channel URL, @handle, UC... id, or free-text name."""
    q = query.strip()
    part = "snippet,contentDetails"

    m = re.search(r"youtube\.com/channel/(UC[\w-]{16,})", q)
    if m:
        q = m.group(1)
    else:
        m = re.search(r"youtube\.com/(@[\w.\-]+)", q)
        if m:
            q = m.group(1)

    if re.fullmatch(r"UC[\w-]{16,}", q):
        data = _get("channels", part=part, id=q)
        if data.get("items"):
            return _channel_from_item(data["items"][0])
        raise YouTubeError(f"No channel with id {q}")

    handle = q if q.startswith("@") else "@" + q.replace(" ", "")
    data = _get("channels", part=part, forHandle=handle)
    if data.get("items"):
        return _channel_from_item(data["items"][0])

    # last resort: search by name
    data = _get("search", part="snippet", type="channel", q=query, maxResults=1)
    if data.get("items"):
        cid = data["items"][0]["snippet"]["channelId"]
        data = _get("channels", part=part, id=cid)
        if data.get("items"):
            return _channel_from_item(data["items"][0])
    raise YouTubeError(f"Could not resolve channel: {query!r}")


def fetch_video_details(video_ids: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        data = _get("videos", part="contentDetails,snippet,status,statistics", id=",".join(batch), maxResults=50)
        for item in data.get("items", []):
            sn, st = item["snippet"], item.get("status", {})
            out.append({
                "video_id": item["id"],
                "channel_id": sn["channelId"],
                "title": sn["title"],
                "thumbnail_url": sn["thumbnails"].get("medium", {}).get("url")
                or sn["thumbnails"].get("default", {}).get("url"),
                "duration_seconds": parse_duration(item["contentDetails"].get("duration", "")),
                "published_at": sn["publishedAt"],
                "available": 1 if (st.get("privacyStatus") == "public" and st.get("embeddable", True)) else 0,
                "view_count": int(item.get("statistics", {}).get("viewCount", 0) or 0),
            })
    return out


def sync_channel(conn, channel_id: str, uploads_playlist_id: str, full: bool = False) -> int:
    """Discover new uploads. Incremental: stop paging at first already-known video."""
    known = {r["video_id"] for r in conn.execute(
        "SELECT video_id FROM videos WHERE channel_id=?", (channel_id,))}
    new_ids, page_token = [], None
    pages = 0
    while True:
        pages += 1
        params = {"part": "contentDetails", "playlistId": uploads_playlist_id, "maxResults": 50}
        if page_token:
            params["pageToken"] = page_token
        data = _get("playlistItems", **params)
        hit_known = False
        for item in data.get("items", []):
            vid = item["contentDetails"]["videoId"]
            if vid in known:
                hit_known = True
            else:
                new_ids.append(vid)
        page_token = data.get("nextPageToken")
        if not page_token or (hit_known and not full) or pages >= 10:
            break

    now = db.utcnow()
    for v in fetch_video_details(new_ids):
        conn.execute(
            """INSERT INTO videos (video_id, channel_id, title, thumbnail_url, duration_seconds,
                                   published_at, available, blacklisted, first_seen_at, view_count)
               VALUES (?,?,?,?,?,?,?,0,?,?)
               ON CONFLICT(video_id) DO UPDATE SET
                 title=excluded.title, thumbnail_url=excluded.thumbnail_url,
                 duration_seconds=excluded.duration_seconds, available=excluded.available,
                 view_count=excluded.view_count""",
            (v["video_id"], v["channel_id"], v["title"], v["thumbnail_url"],
             v["duration_seconds"], v["published_at"], v["available"], now, v["view_count"]),
        )
    conn.commit()
    return len(new_ids)


def refresh_availability(conn, channel_id: str) -> None:
    """Re-check stored videos; mark deleted/private ones unavailable."""
    ids = [r["video_id"] for r in conn.execute(
        "SELECT video_id FROM videos WHERE channel_id=?", (channel_id,))]
    if not ids:
        return
    found = {v["video_id"]: v for v in fetch_video_details(ids)}
    for vid in ids:
        v = found.get(vid)
        if v:
            conn.execute("UPDATE videos SET available=?, view_count=? WHERE video_id=?",
                         (v["available"], v["view_count"], vid))
        else:
            conn.execute("UPDATE videos SET available=0 WHERE video_id=?", (vid,))
    conn.commit()


def sync_all(full: bool = False, check_availability: bool = False) -> dict:
    """Sync every configured channel. Returns per-channel new-video counts."""
    with _status_lock:
        status = _tenant_status()
        if status["running"]:
            return {"skipped": "sync already running"}
        status.update(running=True, started_at=db.utcnow(), current_channel=None,
                       channels_done=0, channels_total=0, new_videos=0)
    conn = db.get_conn()
    results, errors = {}, {}
    try:
        db.delete_orphan_videos(conn)
        channels = conn.execute(
            "SELECT DISTINCT channel_id, uploads_playlist_id, title FROM channels "
            "WHERE uploads_playlist_id IS NOT NULL").fetchall()
        _set_status(channels_total=len(channels))
        for ch in channels:
            _set_status(current_channel=ch["title"] or ch["channel_id"])
            try:
                n = sync_channel(conn, ch["channel_id"], ch["uploads_playlist_id"], full=full)
                if check_availability:
                    refresh_availability(conn, ch["channel_id"])
                results[ch["title"] or ch["channel_id"]] = n
            except YouTubeError as e:
                errors[ch["title"] or ch["channel_id"]] = str(e)
            _set_status(channels_done=len(results) + len(errors),
                        new_videos=sum(results.values()))
        conn.execute("UPDATE settings SET last_sync_at=? WHERE id=1", (db.utcnow(),))
        summary = ", ".join(f"{k}: +{v}" for k, v in results.items()) or "no channels configured"
        if errors:
            summary += " | ERRORS: " + "; ".join(f"{k}: {v[:80]}" for k, v in errors.items())
        db.log_activity(conn, "server", "sync_error" if errors else "sync_completed", summary)
        conn.commit()
        from . import notify
        if errors:
            if db.notify_once(conn, "sync_error"):
                notify.send("Sync failed", summary[:500])
        else:
            db.notify_clear(conn, "sync_error")
        notify.check_exhausted(conn)
    except Exception as e:  # never die silently in a background task
        import traceback
        traceback.print_exc()  # visible in journalctl -u dailytelly
        db.log_activity(conn, "server", "sync_error", str(e)[:300])
        conn.commit()
        from . import notify
        if db.notify_once(conn, "sync_error"):
            notify.send("Sync failed", str(e)[:500])
    finally:
        summary = ", ".join(f"{k}: +{v}" for k, v in results.items()) or "no channels"
        if errors:
            summary += " | errors: " + "; ".join(errors)
        _set_status(running=False, current_channel=None,
                    last_result=summary[:300], last_finished_at=db.utcnow())
        conn.close()
    return {"new_videos": results, "errors": errors}
