"""Operator-curated public metadata, copied once into a new private library."""
import json
import secrets
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from . import db, platform_store as store

router = APIRouter()
CHANNEL_FIELDS = ('channel_id','title','thumbnail_url','uploads_playlist_id','updated_at')
VIDEO_FIELDS = ('video_id','channel_id','title','thumbnail_url','duration_seconds','published_at','available','view_count')


class Entry(BaseModel):
    weekday: int = Field(ge=0,le=6)
    channel_id: str = Field(pattern=r'^UC[A-Za-z0-9_-]{22}$')


class DefaultsIn(BaseModel):
    enabled: bool = True
    entries: list[Entry] = Field(min_length=1,max_length=70)


def save_defaults(entries, source, enabled=True):
    pairs={(entry.weekday,entry.channel_id) for entry in entries}
    if len(pairs)!=len(entries):
        raise HTTPException(400,'Choose each channel only once per day')
    path=store.library_path(source['id'])
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=15) as library:
        library.row_factory=sqlite3.Row
        catalog={row['channel_id']:dict(row) for row in library.execute('SELECT * FROM channels')}
        ids={entry.channel_id for entry in entries}
        if not ids <= catalog.keys():
            raise HTTPException(400,'Choose channels already in your own library')
        keywords=db.keyword_matcher(library)
        videos=[]
        for row in library.execute('SELECT * FROM videos WHERE available=1 AND blacklisted=0 AND embed_blocked=0'):
            if row['channel_id'] in ids and not keywords(row['title']):
                videos.append((row['video_id'],row['channel_id'],json.dumps({key:row[key] for key in VIDEO_FIELDS})))
        channels=[(day,identifier,json.dumps({key:catalog[identifier][key] for key in CHANNEL_FIELDS})) for day,identifier in sorted(pairs)]
    version=secrets.token_hex(8)
    with store.registry() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM starter_channels')
        conn.execute('DELETE FROM starter_videos')
        conn.executemany('INSERT INTO starter_channels VALUES(?,?,?)',channels)
        conn.executemany('INSERT INTO starter_videos VALUES(?,?,?)',videos)
        conn.execute('INSERT OR REPLACE INTO starter_config VALUES(1,?,?,?,?)',(int(enabled),version,source['id'],store.now()))
    store.audit(source['id'],'starter_defaults_updated',version)
    return {'ok':True,'version':version,'channels':len(channels),'videos':len(videos)}


def apply_starter(account):
    """A household marker makes a retry safe across the two SQLite databases."""
    with store.registry() as registry:
        setup=registry.execute('SELECT * FROM account_setup WHERE account_id=?',(account['id'],)).fetchone()
        if not setup or setup['state']!='pending':
            return
        template=registry.execute('SELECT * FROM starter_config WHERE id=1 AND enabled=1').fetchone()
        channels=list(registry.execute('SELECT * FROM starter_channels ORDER BY weekday,channel_id')) if template else []
        version=template['version'] if template else None
        context=store.account_context.set(account)
        try:
            library=db.get_conn()
            try:
                library.execute('BEGIN IMMEDIATE')
                library.execute('CREATE TABLE IF NOT EXISTS starter_applied(id INTEGER PRIMARY KEY CHECK(id=1),version TEXT NOT NULL)')
                applied=library.execute('SELECT version FROM starter_applied WHERE id=1').fetchone()
                empty=library.execute('SELECT COUNT(*) FROM channels').fetchone()[0]==0 and library.execute('SELECT COUNT(*) FROM videos').fetchone()[0]==0
                if applied:
                    version=applied['version']; state='applied'
                elif empty and channels:
                    for row in channels:
                        channel=json.loads(row['payload'])
                        library.execute('INSERT INTO channels VALUES(?,?,?,?,?,?)',(row['weekday'],*[channel[key] for key in CHANNEL_FIELDS]))
                    for row in registry.execute('SELECT payload FROM starter_videos'):
                        video=json.loads(row['payload'])
                        library.execute('''INSERT INTO videos(video_id,channel_id,title,thumbnail_url,duration_seconds,
                          published_at,available,view_count,first_seen_at) VALUES(?,?,?,?,?,?,?,?,?)''',
                          (*[video[key] for key in VIDEO_FIELDS],db.utcnow()))
                    library.execute("UPDATE settings SET min_seconds=60,max_seconds=1800,timezone='America/New_York',last_sync_at=? WHERE id=1",(db.utcnow(),))
                    library.execute('INSERT INTO starter_applied VALUES(1,?)',(version,))
                    state='applied'
                else:
                    state='skipped'
                library.commit()
            except BaseException:
                library.rollback(); raise
            finally:
                library.close()
        finally:
            store.account_context.reset(context)
        registry.execute('UPDATE account_setup SET state=?,version=? WHERE account_id=?',(state,version,account['id']))
        if setup['reminder_enabled']:
            from .reminders import reminder_time
            registry.execute('INSERT OR IGNORE INTO welcome_reminders(account_id,due_at) VALUES(?,?)',
                             (account['id'],reminder_time(account['verified_at'])))


@router.get('/ops/starter')
def defaults(owner=Depends(store.require_owner)):
    context=store.account_context.set(owner)
    try:
        conn=db.get_conn()
        try:
            choices=[dict(row) for row in conn.execute('SELECT channel_id,title,COUNT(*) AS days FROM channels GROUP BY channel_id,title ORDER BY title')]
        finally:
            conn.close()
    finally:
        store.account_context.reset(context)
    with store.registry() as conn:
        config=conn.execute('SELECT * FROM starter_config WHERE id=1').fetchone()
        entries=[{'weekday':row['weekday'],**json.loads(row['payload'])} for row in conn.execute('SELECT * FROM starter_channels ORDER BY weekday,channel_id')]
        videos=conn.execute('SELECT COUNT(*) FROM starter_videos').fetchone()[0]
    return {'enabled':bool(config and config['enabled']),'version':config['version'] if config else None,
            'entries':entries,'choices':choices,'videos':videos}


@router.put('/ops/starter')
def update_defaults(body: DefaultsIn,owner=Depends(store.require_owner)):
    return save_defaults(body.entries,owner,body.enabled)


@router.get('/account/setup')
def setup_state(account=Depends(store.require_account)):
    with store.registry() as conn:
        row=conn.execute('SELECT * FROM account_setup WHERE account_id=?',(account['id'],)).fetchone()
        reminder=conn.execute('SELECT due_at,sent_at,state,first_attempt FROM welcome_reminders WHERE account_id=?',(account['id'],)).fetchone()
    return {'starter_applied':bool(row and row['state']=='applied'), 'show_tour':bool(row and row['state']=='applied' and not row['seen']),
            'reminder_enabled':bool(row and row['reminder_enabled']), 'reminder_available':bool(row and (not reminder or not reminder['first_attempt'])), 'reminder':dict(reminder) if reminder else None}


@router.post('/account/setup/seen')
def seen(account=Depends(store.require_account)):
    with store.registry() as conn:
        conn.execute('UPDATE account_setup SET seen=1 WHERE account_id=?',(account['id'],))
    return {'ok':True}


class PreferenceIn(BaseModel):
    enabled: bool


@router.patch('/account/reminder')
def reminder_preference(body: PreferenceIn,account=Depends(store.require_account)):
    with store.registry() as conn:
        row=conn.execute('SELECT * FROM account_setup WHERE account_id=?',(account['id'],)).fetchone()
        if not row:
            raise HTTPException(409,'This account has no welcome check-in pending')
        job=conn.execute('SELECT * FROM welcome_reminders WHERE account_id=?',(account['id'],)).fetchone()
        if body.enabled and job and job['first_attempt']:
            raise HTTPException(409,'This one-time check-in has already been processed')
        conn.execute('UPDATE account_setup SET reminder_enabled=? WHERE account_id=?',(int(body.enabled),account['id']))
        if body.enabled:
            from .reminders import reminder_time
            conn.execute('INSERT OR IGNORE INTO welcome_reminders(account_id,due_at) VALUES(?,?)',(account['id'],reminder_time(account['verified_at'])))
            conn.execute("UPDATE welcome_reminders SET state='pending' WHERE account_id=? AND first_attempt IS NULL",(account['id'],))
        if not body.enabled:
            conn.execute("UPDATE welcome_reminders SET state='cancelled',payload=NULL WHERE account_id=? AND sent_at IS NULL",(account['id'],))
    return {'ok':True}
