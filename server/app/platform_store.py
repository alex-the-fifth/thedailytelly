"""Platform identity store. Household content lives in separately scoped databases."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import time

from fastapi import HTTPException
from . import config

account_context = ContextVar('account', default=None)
SESSION_COOKIE = 'dailytelly_account'
INTENT_COOKIE = 'dailytelly_signin'
SESSION_TTL_SECONDS = 30 * 86400
POLICY_VERSION = '2026-10-21'


def now():
    return int(time.time())


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def base_url():
    return os.environ.get('DAILYTELLY_PUBLIC_URL', 'http://127.0.0.1:18400').rstrip('/')


def production():
    return os.environ.get('DAILYTELLY_ENV', 'development') == 'production'


def secure_cookie():
    return production() or base_url().startswith('https://')


@contextmanager
def registry():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DATA_DIR / 'platform.sqlite3', timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA journal_mode=WAL')
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize():
    if production():
        if not base_url().startswith('https://'):
            raise RuntimeError('Production requires an HTTPS DAILYTELLY_PUBLIC_URL')
        mode = os.environ.get('DAILYTELLY_MAIL_MODE')
        if mode not in {'smtp', 'disabled'}:
            raise RuntimeError('Production requires SMTP delivery or closed registration')
        required = ('DAILYTELLY_OWNER_EMAILS',) if mode == 'disabled' else ('DAILYTELLY_SMTP_HOST', 'DAILYTELLY_MAIL_FROM', 'DAILYTELLY_OWNER_EMAILS')
        for key in required:
            if not os.environ.get(key):
                raise RuntimeError(f'Production requires {key}')
    elif not base_url().startswith(('http://127.0.0.1:', 'http://localhost:', 'https://localhost:')):
        raise RuntimeError('Development mail mode is restricted to a localhost public URL')
    config.secret_key()
    with registry() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS accounts (
          id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
          created_at INTEGER NOT NULL, verified_at INTEGER,
          last_login_at INTEGER, status TEXT NOT NULL DEFAULT 'pending',
          policy_version TEXT, accepted_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS sessions (
          hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
          expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS email_links (
          hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
          intent_hash TEXT NOT NULL, expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS operator_links (
          hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
          expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pairing (
          device_hash TEXT PRIMARY KEY, code_hash TEXT NOT NULL UNIQUE,
          expires_at INTEGER NOT NULL, last_poll_at INTEGER NOT NULL DEFAULT 0,
          account_id TEXT REFERENCES accounts(id) ON DELETE CASCADE,
          label TEXT NOT NULL DEFAULT 'TV', created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS paired_devices (
          id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
          token_hash TEXT NOT NULL UNIQUE, label TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rate_limits (
          key TEXT PRIMARY KEY, count INTEGER NOT NULL, expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS operator_audit (
          id INTEGER PRIMARY KEY, actor_id TEXT NOT NULL, action TEXT NOT NULL,
          target_id TEXT NOT NULL, at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS resource_samples (
          at INTEGER PRIMARY KEY, cpu REAL NOT NULL, memory REAL NOT NULL, disk REAL NOT NULL,
          requests INTEGER NOT NULL, errors INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS alert_state (
          key TEXT PRIMARY KEY, last_sent INTEGER NOT NULL, consecutive INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS imports (
          account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
          fingerprint TEXT NOT NULL, imported_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS inbound_forwards (
          hash TEXT PRIMARY KEY, at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS starter_config (
          id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL,
          version TEXT NOT NULL, source_account_id TEXT, updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS starter_channels (
          weekday INTEGER NOT NULL, channel_id TEXT NOT NULL, payload TEXT NOT NULL,
          PRIMARY KEY(weekday,channel_id)
        );
        CREATE TABLE IF NOT EXISTS starter_videos (
          video_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS account_setup (
          account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
          state TEXT NOT NULL DEFAULT 'pending', version TEXT, seen INTEGER NOT NULL DEFAULT 0,
          reminder_enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS welcome_reminders (
          account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
          due_at INTEGER NOT NULL, first_attempt INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
          sent_at INTEGER, state TEXT NOT NULL DEFAULT 'pending', payload TEXT
        );
        CREATE TABLE IF NOT EXISTS deletion_queue (
          account_id TEXT PRIMARY KEY, requested_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS deletion_records (
          account_id TEXT PRIMARY KEY, deleted_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS devices_account ON paired_devices(account_id);
        CREATE INDEX IF NOT EXISTS sessions_account ON sessions(account_id);
        CREATE TABLE IF NOT EXISTS catalog_quota (
          day TEXT NOT NULL, bucket TEXT NOT NULL, units INTEGER NOT NULL,
          PRIMARY KEY (day,bucket)
        );
        ''')
        conn.execute('DELETE FROM sessions WHERE expires_at <= ?', (now(),))


def owner_emails():
    return {value.strip().lower() for value in os.environ.get('DAILYTELLY_OWNER_EMAILS', '').split(',') if value.strip()}


def is_owner(account):
    return bool(account and account['status'] == 'active' and account['verified_at']
                and account['email'] in owner_emails())


def account_for_session(token):
    if not token:
        return None
    with registry() as conn:
        row = conn.execute('''SELECT a.* FROM accounts a JOIN sessions s ON a.id=s.account_id
            WHERE s.hash=? AND s.expires_at>? AND a.status='active' AND a.verified_at IS NOT NULL''',
            (digest(token), now())).fetchone()
        return dict(row) if row else None


def refresh_session_if_needed(token):
    """Extend an active browser session when fewer than 15 days remain."""
    if not token:
        return False
    current = now()
    with registry() as conn:
        row = conn.execute('SELECT expires_at FROM sessions WHERE hash=? AND expires_at>?',
                           (digest(token), current)).fetchone()
        if not row or row['expires_at'] - current > SESSION_TTL_SECONDS // 2:
            return False
        conn.execute('UPDATE sessions SET expires_at=? WHERE hash=? AND expires_at>? ',
                     (current + SESSION_TTL_SECONDS, digest(token), current))
        return True


def account_for_device(token):
    if not token:
        return None, None
    with registry() as conn:
        row = conn.execute('''SELECT a.*, d.id AS paired_device_id FROM accounts a
            JOIN paired_devices d ON a.id=d.account_id
            WHERE d.token_hash=? AND a.status='active' AND a.verified_at IS NOT NULL''',
            (digest(token),)).fetchone()
        return (dict(row), row['paired_device_id']) if row else (None, None)


def require_account():
    account = account_context.get()
    if not account or account['status'] != 'active' or not account['verified_at']:
        raise HTTPException(401, 'Sign in with a verified email first')
    return account


def require_owner():
    account = require_account()
    if not is_owner(account):
        raise HTTPException(403, 'Platform owner access required')
    return account


def library_path(account_id=None):
    account_id = account_id or require_account()['id']
    # IDs are generated locally, not filesystem paths supplied by users.
    if len(account_id) != 32 or any(c not in '0123456789abcdef' for c in account_id):
        raise RuntimeError('Invalid internal account identifier')
    return config.DATA_DIR / 'libraries' / f'{account_id}.sqlite3'


def initialize_library(account):
    from . import db
    context = account_context.set(account)
    try:
        library_path().parent.mkdir(parents=True, exist_ok=True)
        db.init_db()
    finally:
        account_context.reset(context)


def limit(kind, value, maximum, seconds):
    # The store retains only keyed digests, never raw IP addresses or device fingerprints.
    key = hmac.new(config.secret_key().encode(), f'{kind}:{value}'.encode(), hashlib.sha256).hexdigest()
    denied = False
    wait = seconds
    with registry() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM rate_limits WHERE expires_at<=?', (now(),))
        row = conn.execute('SELECT * FROM rate_limits WHERE key=?', (key,)).fetchone()
        if row and row['count'] >= maximum:
            denied, wait = True, row['expires_at'] - now()
        else:
            conn.execute('''INSERT INTO rate_limits VALUES (?,1,?)
                ON CONFLICT(key) DO UPDATE SET count=count+1''', (key, now()+seconds))
    if denied:
        raise HTTPException(429, 'Please wait before trying again', headers={'Retry-After': str(max(1,wait))})


def audit(actor, action, target):
    with registry() as conn:
        conn.execute('INSERT INTO operator_audit (actor_id,action,target_id,at) VALUES (?,?,?,?)',
                     (actor, action, target, now()))


def cleanup():
    with registry() as conn:
        for table in ('sessions', 'email_links', 'operator_links', 'pairing', 'rate_limits'):
            conn.execute(f'DELETE FROM {table} WHERE expires_at<=?', (now(),))
        conn.execute("DELETE FROM accounts WHERE status='pending' AND created_at<?", (now()-86400,))
        conn.execute('DELETE FROM operator_audit WHERE at<?', (now()-30*86400,))
        conn.execute('DELETE FROM resource_samples WHERE at<?', (now()-86400,))
        conn.execute('DELETE FROM deletion_records WHERE deleted_at<?', (now()-7*86400,))
        conn.execute("UPDATE welcome_reminders SET state='failed',payload=NULL WHERE first_attempt<? AND state='sending'",(now()-23*3600,))
        pending = [row['account_id'] for row in conn.execute('SELECT account_id FROM deletion_queue')]
    for account_id in pending:
        purge_library(account_id)
    if not production():
        for path in (config.DATA_DIR/'dev-mail').glob('*.json'):
            if path.stat().st_mtime < now()-900:
                path.unlink(missing_ok=True)


def purge_library(account_id):
    path = library_path(account_id)
    try:
        for candidate in (path, path.with_name(path.name+'-wal'), path.with_name(path.name+'-shm')):
            candidate.unlink(missing_ok=True)
    except OSError:
        return False  # An in-flight Windows connection may still hold a file; maintenance retries.
    with registry() as conn:
        conn.execute('DELETE FROM deletion_queue WHERE account_id=?', (account_id,))
    return True


def prune_household_audits():
    import datetime
    cutoff = (datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ')
    with registry() as conn:
        ids = [row['id'] for row in conn.execute("SELECT id FROM accounts WHERE verified_at IS NOT NULL")]
    for account_id in ids:
        path = library_path(account_id)
        if not path.exists():
            continue
        conn = sqlite3.connect(path, timeout=5)
        try:
            conn.execute('DELETE FROM activity_log WHERE ts<?', (cutoff,))
            conn.commit()
        finally:
            conn.close()


def reserve_catalog_units(units, bucket='metadata'):
    import datetime
    from zoneinfo import ZoneInfo
    day = datetime.datetime.now(ZoneInfo('America/Los_Angeles')).date().isoformat()
    if bucket not in {'metadata','search'}:
        raise ValueError('Unknown catalog quota bucket')
    maximum = int(os.environ.get('DAILYTELLY_YOUTUBE_SEARCH_BUDGET','90')) if bucket=='search' else int(os.environ.get('DAILYTELLY_YOUTUBE_DAILY_BUDGET','9000'))
    with registry() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT units FROM catalog_quota WHERE day=? AND bucket=?',(day,bucket)).fetchone()
        if (row['units'] if row else 0)+units > maximum:
            raise ValueError("Today's catalog refresh capacity has been reached. Try again tomorrow.")
        conn.execute('''INSERT INTO catalog_quota VALUES (?,?,?)
            ON CONFLICT(day,bucket) DO UPDATE SET units=units+excluded.units''',(day,bucket,units))
        conn.execute('DELETE FROM catalog_quota WHERE day<>?',(day,))
