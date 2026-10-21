"""Starter isolation and one optional welcome check-in, without live services."""
import datetime as dt
import json
import sqlite3

import httpx
import pytest

from test_platform import client,signin
from app import db,mailer,platform_store as store,reminders,starter


def prepare(client):
    owner=signin(client,'hello@thedailytelly.com')
    client.patch('/account/reminder',json={'enabled':False})
    context=store.account_context.set(owner)
    try:
        conn=db.get_conn()
        for day in range(7):
            channel='UC'+str(day)*22
            conn.execute('INSERT INTO channels VALUES(?,?,?,?,?,?)',(day,channel,'Channel '+str(day),'','UU'+str(day)*22,db.utcnow()))
            conn.execute('INSERT INTO videos(video_id,channel_id,title,thumbnail_url,duration_seconds,published_at,first_seen_at) VALUES(?,?,?,?,?,?,?)',
                         ('video-'+str(day),channel,'Public video '+str(day),'',600,db.utcnow(),db.utcnow()))
        conn.execute("INSERT INTO videos(video_id,channel_id,title,duration_seconds,published_at,first_seen_at,blacklisted) VALUES('blocked',?,'Not for sharing',600,?,?,1)",('UC'+'0'*22,db.utcnow(),db.utcnow()))
        conn.execute("INSERT INTO watch_progress(video_id,completed) VALUES('video-0',1)")
        conn.commit();conn.close()
    finally:
        store.account_context.reset(context)
    entries=[{'weekday':day,'channel_id':'UC'+str(day)*22} for day in range(7)]
    result=client.put('/ops/starter',json={'enabled':True,'entries':entries})
    assert result.status_code==200,result.text
    assert result.json()['videos']==7
    return owner,entries


def counts(account):
    with sqlite3.connect(store.library_path(account['id'])) as conn:
        return {table:conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('channels','videos','watch_progress')}


def test_starter_copies_only_public_metadata_and_tour_is_per_account(client):
    owner,_=prepare(client)
    first=signin(client,'first@example.com')
    assert counts(first)=={'channels':7,'videos':7,'watch_progress':0}
    assert counts(owner)=={'channels':7,'videos':8,'watch_progress':1}
    state=client.get('/account/setup').json()
    assert state['show_tour'] and state['starter_applied'] and state['reminder_enabled']
    assert client.post('/account/setup/seen').status_code==200
    assert not client.get('/account/setup').json()['show_tour']
    second=signin(client,'second@example.com')
    assert second['id']!=first['id'] and client.get('/account/setup').json()['show_tour']
    with sqlite3.connect(store.library_path(first['id'])) as conn:
        conn.execute('DELETE FROM channels')
    signin(client,'first@example.com')
    assert counts(first)['channels']==0  # Never restore removed starter entries on later sign-in.


def test_defaults_are_owner_only_and_apply_to_future_accounts(client):
    owner,entries=prepare(client)
    first=signin(client,'first@example.com')
    assert client.get('/ops/starter').status_code==403
    assert client.put('/ops/starter',json={'entries':entries}).status_code==403
    signin(client,'hello@thedailytelly.com')
    assert len(client.get('/ops/starter').json()['choices'])==7
    assert client.put('/ops/starter',json={'entries':entries[:1]}).status_code==200
    second=signin(client,'second@example.com')
    assert counts(first)['channels']==7 and counts(second)['channels']==1
    signin(client,'hello@thedailytelly.com')
    assert client.put('/ops/starter',json={'enabled':False,'entries':entries}).status_code==200
    empty=signin(client,'empty@example.com')
    assert counts(empty)['channels']==0 and not client.get('/account/setup').json()['show_tour']


def test_invalid_defaults_do_not_replace_template(client):
    owner,entries=prepare(client)
    for invalid in [entries+entries[:1],[{'weekday':7,'channel_id':'UC'+'0'*22}],
                    [{'weekday':0,'channel_id':'UC'+'x'*22}]]:
        assert client.put('/ops/starter',json={'entries':invalid}).status_code in (400,422)
    assert len(client.get('/ops/starter').json()['entries'])==7


@pytest.mark.parametrize('instant,expected',[
    ('2026-10-21T13:00:00+00:00','2026-10-23T00:00:00+00:00'),
    ('2026-10-22T01:00:00+00:00','2026-10-24T00:00:00+00:00'),
    ('2026-03-07T12:00:00+00:00','2026-03-09T00:00:00+00:00'),
    ('2026-10-31T12:00:00+00:00','2026-11-02T01:00:00+00:00'),
])
def test_8pm_after_24_hours_handles_eastern_daylight_saving(instant,expected):
    stamp=int(dt.datetime.fromisoformat(instant).timestamp())
    due=reminders.reminder_time(stamp)
    assert due==int(dt.datetime.fromisoformat(expected).timestamp())
    assert due>=stamp+86400
    assert dt.datetime.fromtimestamp(due,reminders.EASTERN).hour==20


def test_welcome_is_one_time_scoped_and_clears_snapshot(client,monkeypatch):
    prepare(client)
    account=signin(client,'parent@example.com')
    job=client.get('/account/setup').json()['reminder']
    sent=[];monkeypatch.setattr(mailer,'send_reminder',lambda payload,identifier:sent.append((payload,identifier)))
    reminders.dispatch_reminders(job['due_at']-3600)
    assert not sent
    reminders.dispatch_reminders(job['due_at'])
    reminders.dispatch_reminders(job['due_at']+300)
    assert len(sent)==1 and sent[0][1]==account['id']
    payload=sent[0][0]
    assert payload['to']==['parent@example.com'] and '7 selected channels' in payload['text']
    assert '7 found videos' in payload['text'] and 'Monday: Channel 0' in payload['text']
    assert 'Not for sharing' not in json.dumps(payload)
    assert '<img' not in payload['html'] and '#f6f5ee' in payload['html']
    with store.registry() as conn:
        row=conn.execute('SELECT * FROM welcome_reminders WHERE account_id=?',(account['id'],)).fetchone()
        assert row['state']=='sent' and row['payload'] is None


def test_failed_send_reuses_snapshot_then_unsubscribe_cancels_it(client,monkeypatch):
    prepare(client)
    account=signin(client,'parent@example.com')
    due=client.get('/account/setup').json()['reminder']['due_at']
    sent=[]
    def failure(payload,identifier):sent.append(payload);raise RuntimeError('Provider unavailable')
    monkeypatch.setattr(mailer,'send_reminder',failure)
    reminders.dispatch_reminders(due)
    with sqlite3.connect(store.library_path(account['id'])) as conn:conn.execute('DELETE FROM channels')
    reminders.dispatch_reminders(due+300)
    assert len(sent)==2 and sent[0]==sent[1]
    token=reminders.preference_token(account['id'])
    assert client.post('/email-preferences',json={'token':token[:-1]+'x'}).status_code==400
    assert client.post('/email-preferences',json={'token':token}).status_code==200
    reminders.dispatch_reminders(due+600)
    assert len(sent)==2
    with store.registry() as conn:
        row=conn.execute('SELECT * FROM welcome_reminders WHERE account_id=?',(account['id'],)).fetchone()
        assert row['state']=='cancelled' and row['payload'] is None


def test_opt_out_registration_and_existing_accounts_do_not_get_welcome(client):
    prepare(client)
    assert client.post('/auth/email',json={'email':'optout@example.com','accepted':True,'reminder':False}).status_code==202
    from test_platform import token_for
    assert client.post('/auth/verify',json={'token':token_for('optout@example.com')}).status_code==200
    state=client.get('/account/setup').json()
    assert state['reminder'] is None and not state['reminder_enabled']
    assert client.patch('/account/reminder',json={'enabled':True}).status_code==200
    assert client.get('/account/setup').json()['reminder']
    assert client.patch('/account/reminder',json={'enabled':False}).status_code==200
    with store.registry() as conn:
        conn.execute("DELETE FROM account_setup WHERE account_id IN (SELECT id FROM accounts WHERE email='optout@example.com')")
    signin(client,'optout@example.com')
    assert not client.get('/account/setup').json()['show_tour']


def test_suspension_deletion_and_retry_window_prevent_late_mail(client,monkeypatch):
    prepare(client)
    account=signin(client,'parent@example.com')
    due=client.get('/account/setup').json()['reminder']['due_at']
    with store.registry() as conn:conn.execute("UPDATE accounts SET status='suspended' WHERE id=?",(account['id'],))
    monkeypatch.setattr(mailer,'send_reminder',lambda *args:pytest.fail('Suppressed account got mail'))
    reminders.dispatch_reminders(due)
    with store.registry() as conn:
        conn.execute("UPDATE accounts SET status='active' WHERE id=?",(account['id'],))
        conn.execute("UPDATE welcome_reminders SET state='sending',first_attempt=?,attempts=1,payload='{}' WHERE account_id=?",(due-86400,account['id']))
    reminders.dispatch_reminders(due)
    with store.registry() as conn:
        assert conn.execute('SELECT state FROM welcome_reminders WHERE account_id=?',(account['id'],)).fetchone()[0]=='failed'
        conn.execute('DELETE FROM accounts WHERE id=?',(account['id'],))
        assert conn.execute('SELECT COUNT(*) FROM welcome_reminders WHERE account_id=?',(account['id'],)).fetchone()[0]==0


def test_resend_welcome_uses_stable_idempotency_without_tracking(client,monkeypatch):
    monkeypatch.setenv('DAILYTELLY_MAIL_MODE','smtp')
    monkeypatch.setenv('DAILYTELLY_SMTP_HOST','smtp.resend.com')
    monkeypatch.setenv('DAILYTELLY_SMTP_PASSWORD','test-sending-key')
    calls=[]
    def provider(request):
        calls.append(request)
        assert request.headers['Authorization']=='Bearer test-sending-key'
        assert request.headers['Idempotency-Key']=='telly-welcome-account-id'
        return httpx.Response(200,json={'id':'accepted'})
    original=httpx.Client
    monkeypatch.setattr(mailer.httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(provider),**kwargs))
    payload={'to':['parent@example.com'],'subject':'A little hello','text':'Your week is taking shape.'}
    for _ in range(2):mailer.send_reminder(payload,'account-id')
    assert len(calls)==2 and calls[0].content==calls[1].content
