"""Per-household exhaustion state only. No household activity leaves the platform."""
import datetime
from . import db
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def send(title, message, priority=0):
    return


def rate_limited(conn, key: str, minutes: int) -> bool:
    """True if an alert with this key fired within the last `minutes`."""
    row = conn.execute("SELECT created_at FROM notify_state WHERE key=?", (key,)).fetchone()
    now = datetime.datetime.now(datetime.timezone.utc)
    if row:
        try:
            last = datetime.datetime.strptime(row["created_at"], "%Y-%m-%dT%H:%M:%SZ") \
                .replace(tzinfo=datetime.timezone.utc)
            if (now - last).total_seconds() < minutes * 60:
                return True
        except ValueError:
            pass
    conn.execute("INSERT OR REPLACE INTO notify_state (key, created_at) VALUES (?,?)",
                 (key, db.utcnow()))
    conn.commit()
    return False


def check_exhausted(conn) -> list[dict]:
    """Alert once when a channel runs out of eligible videos; re-arm when it recovers.
    Channels with nothing synced yet are reported in the console but not pushed
    (avoids a false alarm right after a channel is added, before its sync finishes)."""
    try:
        exhausted = db.exhausted_channels(conn)
        empty_ids = set()
        for ch in exhausted:
            empty_ids.add(ch["channel_id"])
            if ch.get("total", 0) > 0 and db.notify_once(conn, "exhausted:" + ch["channel_id"]):
                days = ", ".join(DAYS[d] for d in ch["weekdays"])
                send("Channel out of videos",
                     f"{ch['title']} ({days}) has no eligible videos left "
                     f"({ch.get('played', 0)} played of {ch.get('eligible', 0)} eligible).")
        for r in conn.execute("SELECT key FROM notify_state WHERE key LIKE 'exhausted:%'").fetchall():
            if r["key"].split(":", 1)[1] not in empty_ids:
                db.notify_clear(conn, r["key"])
        return exhausted
    except Exception:
        return []
