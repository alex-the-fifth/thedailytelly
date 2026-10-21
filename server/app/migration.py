"""Import a consistent legacy SQLite backup into one existing verified account."""
import hashlib
from pathlib import Path
import secrets
import sqlite3

from . import db, platform_store as store


def inspect_backup(source):
    source = Path(source).resolve(strict=True)
    # immutable=1 is safe only for a consistent backup, never a live WAL database.
    if source.with_name(source.name+'-wal').exists():
        raise ValueError('Use a consistent SQLite .backup copy, not a live WAL database')
    conn = sqlite3.connect(source.as_uri()+'?mode=ro&immutable=1', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Backup failed integrity check')
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'channels','videos','settings','watch_progress'} <= tables:
            raise ValueError('Not a compatible Daily Telly database')
        counts = {name: conn.execute('SELECT COUNT(*) FROM '+name).fetchone()[0]
                  for name in ('channels','videos','watch_progress')}
        return source, conn, counts
    except BaseException:
        conn.close()
        raise


def import_backup(source, email, apply=False):
    source, legacy, counts = inspect_backup(source)
    try:
        with store.registry() as registry:
            row = registry.execute("SELECT * FROM accounts WHERE email=? AND status='active' AND verified_at IS NOT NULL",
                                   (email.strip().lower(),)).fetchone()
            if not row:
                raise ValueError('Create the destination account and verify its email before importing')
            account = dict(row)
            if registry.execute('SELECT 1 FROM imports WHERE account_id=?', (account['id'],)).fetchone():
                raise ValueError('This account already has a completed import')
        if not apply:
            return {'dry_run': True, **counts}
        store.initialize_library(account)
        context = store.account_context.set(account)
        try:
            destination = db.get_conn()
            try:
                destination.execute('BEGIN IMMEDIATE')
                if destination.execute('SELECT COUNT(*) FROM channels').fetchone()[0] or destination.execute('SELECT COUNT(*) FROM videos').fetchone()[0]:
                    raise ValueError('Destination library must be empty; refusing to overwrite it')
                for table in ('channels','videos','watch_progress','blocked_keywords'):
                    present = legacy.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                    if not present:
                        continue
                    target_columns = {row['name'] for row in destination.execute('PRAGMA table_info('+table+')')}
                    source_columns = [row['name'] for row in legacy.execute('PRAGMA table_info('+table+')')]
                    columns = [column for column in source_columns if column in target_columns]
                    names = ','.join(columns)
                    placeholders = ','.join('?' for _ in columns)
                    for row in legacy.execute('SELECT '+names+' FROM '+table):
                        destination.execute('INSERT INTO '+table+' ('+names+') VALUES ('+placeholders+')', tuple(row))
                settings = legacy.execute('SELECT * FROM settings WHERE id=1').fetchone()
                for key in ('min_seconds','max_seconds','sync_interval_minutes','timezone'):
                    if settings and key in settings.keys():
                        destination.execute('UPDATE settings SET '+key+'=? WHERE id=1', (settings[key],))
                # Deliberately omit old devices/tokens, sessions, passwords, activity/IP/UA logs, and Pushover keys.
                destination.commit()
            except BaseException:
                destination.rollback()
                raise
            finally:
                destination.close()
        finally:
            store.account_context.reset(context)
        fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
        with store.registry() as registry:
            registry.execute('INSERT INTO imports VALUES (?,?,?)', (account['id'], fingerprint, store.now()))
        store.audit('migration', 'library_imported', account['id'])
        return {'dry_run': False, **counts}
    finally:
        legacy.close()
