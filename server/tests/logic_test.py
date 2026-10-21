"""Stdlib-only tests of core logic (no fastapi needed). Run: python3 tests/logic_test.py"""
import os
import sys
import tempfile

tmp = tempfile.mkdtemp()
os.environ["DAILYTELLY_DATA"] = tmp
os.environ["YOUTUBE_API_KEY"] = ""
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, security  # noqa: E402
from app.platform_store import account_context
account_context.set({"id": "a"*32, "status": "active", "verified_at": 1})
from app.youtube import parse_duration, _DUR  # noqa: E402,F401

FAIL = []
def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAIL.append(name)

db.init_db()
conn = db.get_conn()

# seed
vids = [
    ("v_new",   600, "2026-07-01T00:00:00Z"),
    ("v_old",   500, "2026-01-01T00:00:00Z"),
    ("v_short", 300, "2026-06-01T00:00:00Z"),
    ("v_long",  900, "2026-06-02T00:00:00Z"),
    ("v_black", 480, "2026-06-03T00:00:00Z"),
    ("v_gone",  480, "2026-06-04T00:00:00Z"),
]
for vid, dur, pub in vids:
    conn.execute("INSERT INTO videos VALUES (?,?,?,?,?,?,1,0,?,0,1234)",
                 (vid, "UCtest", "Title " + vid, None, dur, pub, db.utcnow()))
conn.execute("UPDATE videos SET blacklisted=1 WHERE video_id='v_black'")
conn.execute("UPDATE videos SET available=0 WHERE video_id='v_gone'")
conn.commit()

# --- eligibility + sorting ---
vs = db.eligible_videos(conn, ["UCtest"], 420, 780)
check("eligible: filters length/blacklist/availability",
      [v["video_id"] for v in vs] == ["v_new", "v_old"])
check("eligible: newest first", vs[0]["published_at"] > vs[1]["published_at"])
check("eligible: has thumbnail+title+progress fields",
      all(k in vs[0] for k in ("title", "thumbnail_url", "position_seconds", "watched_pct")))

# --- progress lifecycle (shared, device-agnostic) ---
r = db.apply_progress(conn, "v_old", 0, "start", "tablet-1")
check("progress: start", r["ok"] and not r["completed"])
r = db.apply_progress(conn, "v_old", 200, "heartbeat", "tablet-1")
check("progress: 40%", r["watched_pct"] == 40)
r = db.apply_progress(conn, "v_old", 50, "heartbeat", "tablet-2")  # other device, rewind
vs = db.eligible_videos(conn, ["UCtest"], 420, 780)
v_old = next(v for v in vs if v["video_id"] == "v_old")
check("progress: shared across devices, resume=50, max kept",
      v_old["position_seconds"] == 50 and v_old["watched_pct"] == 40)
row = conn.execute("SELECT play_count, last_device_id FROM watch_progress WHERE video_id='v_old'").fetchone()
check("progress: play_count=1, last device recorded",
      row["play_count"] == 1 and row["last_device_id"] == "tablet-2")

r = db.apply_progress(conn, "v_old", 460, "heartbeat", "tablet-1")  # 92%
check("progress: >=90% completes", r["completed"])
vs = db.eligible_videos(conn, ["UCtest"], 420, 780)
v_old = next(v for v in vs if v["video_id"] == "v_old")
check("eligible: completed still listed, bar full",
      v_old["completed"] and v_old["watched_pct"] == 100)

r = db.apply_progress(conn, "v_new", 100, "ended", "tablet-1")
check("progress: 'ended' completes regardless of position", r["completed"])
vs = db.eligible_videos(conn, ["UCtest"], 420, 780)
check("eligible: both completed, both visible",
      all(v["completed"] for v in vs) and len(vs) == 2)
check("eligible: view_count present", all(v["view_count"] == 1234 for v in vs))

check("progress: unknown video -> None",
      db.apply_progress(conn, "nope", 1, "heartbeat", "d") is None)
check("progress: position clamped to duration",
      db.apply_progress(conn, "v_short", 9999, "heartbeat", "d")["watched_pct"] == 100)

# --- reset zeroes progress ---
conn.execute("DELETE FROM watch_progress WHERE video_id='v_old'"); conn.commit()
vs = db.eligible_videos(conn, ["UCtest"], 420, 780)
v_old = next((v for v in vs if v["video_id"] == "v_old"), None)
check("reset: zero progress, not completed",
      v_old and v_old["position_seconds"] == 0 and v_old["watched_pct"] == 0
      and not v_old["completed"])

# --- range change is retroactive ---
ids = {v["video_id"] for v in db.eligible_videos(conn, ["UCtest"], 420, 960)}
check("range widened: too_long now eligible", ids == {"v_new", "v_old", "v_long"})
vs = db.eligible_videos(conn, ["UCtest"], 100, 960)
v_short = next((v for v in vs if v["video_id"] == "v_short"), None)
check("range widened further: short visible and completed (from clamp test)",
      v_short is not None and v_short["completed"])

# --- multi-channel merge, newest first across channels ---
conn.execute("INSERT INTO videos VALUES (?,?,?,?,?,?,1,0,?,0,1234)",
             ("v2_mid", "UCtest2", "Title v2_mid", None, 600, "2026-03-01T00:00:00Z", db.utcnow()))
vs = db.eligible_videos(conn, ["UCtest", "UCtest2"], 420, 780)
ids = [v["video_id"] for v in vs]
# v_new is completed (from earlier test) -> goes last; unwatched interleaved:
# round 1 = UCtest2's v2_mid (2026-03) and UCtest's v_old (2026-01)
check("multi-channel: unwatched interleaved first, watched last",
      ids == ["v2_mid", "v_old", "v_new"])

# --- round-robin interleave, pure function ---
def mk(vid, ch, pub): return {"video_id": vid, "channel_id": ch, "published_at": pub}
seq = [  # already newest-first overall; channel A posts a lot, C rarely
    mk("a1","A","2026-10-09"), mk("a2","A","2026-10-08"), mk("b1","B","2026-10-07"),
    mk("a3","A","2026-10-06"), mk("c1","C","2026-10-05"), mk("b2","B","2026-10-04"),
    mk("a4","A","2026-10-03"), mk("b3","B","2026-10-02"),
]
got = [v["video_id"] for v in db.interleave_by_channel(seq)]
check("interleave: one per channel per round, newest first within round",
      got == ["a1","b1","c1",  "a2","b2",  "a3","b3",  "a4"])
check("interleave: empty", db.interleave_by_channel([]) == [])
check("interleave: single channel keeps newest-first",
      [v["video_id"] for v in db.interleave_by_channel([mk("x","X","2"), mk("y","X","1")])] == ["x","y"])
check("multi-channel: empty list -> no videos", db.eligible_videos(conn, [], 0, 9999) == [])

# --- embed_blocked hides video; clearing restores it ---
conn.execute("UPDATE videos SET embed_blocked=1 WHERE video_id='v2_mid'"); conn.commit()
ids = [v["video_id"] for v in db.eligible_videos(conn, ["UCtest", "UCtest2"], 420, 780)]
check("embed_blocked: hidden", "v2_mid" not in ids)
conn.execute("UPDATE videos SET embed_blocked=0 WHERE video_id='v2_mid'"); conn.commit()
ids = [v["video_id"] for v in db.eligible_videos(conn, ["UCtest", "UCtest2"], 420, 780)]
check("embed_blocked: cleared -> visible", "v2_mid" in ids)

# --- history: watched only, newest-watched first, playable only for today's channels ---
conn.execute("INSERT OR IGNORE INTO channels VALUES (5,'UCtest','T',NULL,'UUtest','x')")
conn.execute("INSERT OR IGNORE INTO channels VALUES (6,'UCtest2','T2',NULL,'UUtest2','x')")
conn.execute("DELETE FROM watch_progress")
conn.execute("""INSERT INTO watch_progress VALUES
  ('v_new', 600, 600, 1, 1, '2026-07-01T10:00:00Z', '2026-07-03T10:00:00Z', 'd'),
  ('v_old', 480, 480, 0, 1, '2026-07-01T10:00:00Z', '2026-07-05T10:00:00Z', 'd'),
  ('v2_mid', 300, 300, 0, 1, '2026-07-01T10:00:00Z', '2026-07-04T10:00:00Z', 'd')""")
conn.commit()
h = db.history_videos(conn, ["UCtest"], 420, 780)
check("history: completed + >=95% only (v2_mid at 50% excluded)",
      [v["video_id"] for v in h] == ["v_old", "v_new"])
check("history: sorted by last watched desc",
      h[0]["last_played_at"] > h[1]["last_played_at"])
check("history: playable for today's channel", all(v["playable"] for v in h))
h2 = db.history_videos(conn, ["UCtest2"], 420, 780)
check("history: other-day channel listed but not playable",
      len(h2) == 2 and not any(v["playable"] for v in h2))
conn.execute("DELETE FROM channels WHERE weekday IN (5,6)")
conn.execute("DELETE FROM watch_progress")
conn.commit()

# --- keyword blacklist ---
pat = db._keyword_regex("kill")
check("keyword: whole word, case-insensitive", bool(pat.search("KILL the Dragon")))
check("keyword: no partial-word match", not pat.search("Learning Skills"))
pat = db._keyword_regex("fight*")
check("keyword: * prefix wildcard", bool(pat.search("Big Fighting Robots"))
      and bool(pat.search("fight!")) and not pat.search("Firefighter"))
check("keyword: phrase", bool(db._keyword_regex("scary movie").search("A Scary Movie Night")))
conn.execute("UPDATE videos SET title='Monster Fight Club' WHERE video_id='v_new'")
conn.execute("INSERT INTO blocked_keywords VALUES ('fight*', 'x')")
conn.commit()
ids = [v["video_id"] for v in db.eligible_videos(conn, ["UCtest"], 420, 780)]
check("keyword: blocked video hidden from app", "v_new" not in ids and "v_old" in ids)
conn.execute("DELETE FROM blocked_keywords"); conn.commit()
ids = [v["video_id"] for v in db.eligible_videos(conn, ["UCtest"], 420, 780)]
check("keyword: removing keyword restores video", "v_new" in ids)

# --- channel stats, exhausted detection, one-shot alerts ---
from app import notify
sent = []
notify.send = lambda title, msg, priority=0: sent.append(title)   # capture instead of pushing
conn.execute("DELETE FROM channels"); conn.execute("DELETE FROM watch_progress")
conn.execute("DELETE FROM notify_state")
conn.execute("INSERT INTO channels VALUES (2,'UCtest','Test Ch',NULL,'UUtest','x')")
conn.commit()
st = db.channel_stats(conn)["UCtest"]
# UCtest has v_new, v_old (eligible), v_short, v_long (length), v_black, v_gone (blocked)
check("stats: totals", st["total"] == 6 and st["eligible"] == 2
      and st["played"] == 0 and st["remaining"] == 2)
db.apply_progress(conn, "v_new", 600, "ended", "d")
st = db.channel_stats(conn)["UCtest"]
check("stats: played/remaining update", st["played"] == 1 and st["remaining"] == 1)
check("exhausted: not yet", db.exhausted_channels(conn) == [] and notify.check_exhausted(conn) == [])
db.apply_progress(conn, "v_old", 500, "ended", "d")
ex = notify.check_exhausted(conn)
check("exhausted: detected with weekday", len(ex) == 1 and ex[0]["weekdays"] == [2])
check("exhausted: one push", sent == ["Channel out of videos"])
notify.check_exhausted(conn); notify.check_exhausted(conn)
check("exhausted: no repeat pushes", sent == ["Channel out of videos"])
conn.execute("DELETE FROM watch_progress WHERE video_id='v_old'"); conn.commit()   # parent resets
check("exhausted: recovers", notify.check_exhausted(conn) == [])
db.apply_progress(conn, "v_old", 500, "ended", "d")
notify.check_exhausted(conn)
check("exhausted: re-armed, alerts again", sent.count("Channel out of videos") == 2)
check("notify_once", db.notify_once(conn, "k1") and not db.notify_once(conn, "k1"))
check("rate_limited", not notify.rate_limited(conn, "rl", 10) and notify.rate_limited(conn, "rl", 10))
conn.execute("DELETE FROM channels"); conn.execute("DELETE FROM watch_progress")
conn.execute("DELETE FROM notify_state"); conn.commit()

# --- orphan cleanup: removing a channel removes its videos + progress ---
conn.execute("INSERT INTO channels VALUES (0,'UCtest','T',NULL,'UUtest','x')")
conn.execute("INSERT INTO channels VALUES (1,'UCtest2','T2',NULL,'UUtest2','x')")
conn.commit()
check("orphans: none removed while channels configured", db.delete_orphan_videos(conn) == 0)
conn.execute("DELETE FROM channels WHERE channel_id='UCtest2'"); conn.commit()
removed = db.delete_orphan_videos(conn)
left = {r["video_id"] for r in conn.execute("SELECT video_id FROM videos")}
check("orphans: UCtest2 videos removed", removed == 1 and "v2_mid" not in left)
check("orphans: configured channel videos kept", "v_new" in left)

# --- activity log: no IP/UA or child events ---
db.log_activity(conn, "parent", "login", "", "192.0.2.1", "do-not-retain")
db.log_activity(conn, "tablet-1", "video_start", "private viewing detail", "192.0.2.2", "agent")
conn.commit()
rows = conn.execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT 2").fetchall()
check("activity: no IP/user-agent or child viewing event retained",
      len(rows) == 1 and rows[0]["ip"] == "" and rows[0]["user_agent"] == "" and rows[0]["detail"] == "")

# --- security helpers ---
h = security.hash_password("hunter22")
check("password: verify ok", security.verify_password("hunter22", h))
check("password: wrong rejected", not security.verify_password("hunter23", h))
check("password: garbage hash rejected", not security.verify_password("x", "not-a-hash"))
t = security.make_session_token()
check("session: valid token accepted", security.check_session_token(t))
check("session: tampered token rejected", not security.check_session_token(t[:-2] + "zz"))
check("session: expired token rejected",
      not security.check_session_token(security.make_session_token(ttl=-5)))
check("session: None/garbage rejected",
      not security.check_session_token(None) and not security.check_session_token("abc"))

# --- duration parsing ---
check("parse_duration", parse_duration("PT8M31S") == 511 and parse_duration("PT1H2M3S") == 3723
      and parse_duration("PT45S") == 45 and parse_duration("P1DT1S") == 86401
      and parse_duration("PT7M") == 420 and parse_duration("") == 0)

# --- first-run defaults ---
s = db.get_settings(conn)
check("defaults: 7-13 min window", s["min_seconds"] == 420 and s["max_seconds"] == 780)
check("defaults: device token generated", len(s["device_token"]) == 64)
check("defaults: legacy password field cannot use a known password",
      not security.verify_password("changeme", s["parent_password_hash"]))
conn.close()

print()
print("ALL TESTS PASSED" if not FAIL else f"{len(FAIL)} FAILURES: {', '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
