"""One optional, idempotent welcome check-in at 8 p.m. America/New_York."""
import datetime as dt
import hashlib
import hmac
from html import escape
import json
import os
import re
import threading
from zoneinfo import ZoneInfo

from fastapi import APIRouter,HTTPException,Request
from pydantic import BaseModel,Field
from . import config,db,mailer,platform_store as store

router=APIRouter()
EASTERN=ZoneInfo('America/New_York')
DAYS=('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')
dispatch_lock=threading.Lock()


def reminder_time(registered_at):
    threshold=dt.datetime.fromtimestamp(registered_at+86400,dt.timezone.utc).astimezone(EASTERN)
    evening=dt.datetime.combine(threshold.date(),dt.time(20),tzinfo=EASTERN)
    if evening<threshold:
        evening=dt.datetime.combine(threshold.date()+dt.timedelta(days=1),dt.time(20),tzinfo=EASTERN)
    return int(evening.timestamp())


def preference_token(account_id):
    signature=hmac.new(config.secret_key().encode(),('welcome-preference:'+account_id).encode(),hashlib.sha256).hexdigest()
    return account_id+'.'+signature


def summary_payload(account):
    context=store.account_context.set(account)
    try:
        conn=db.get_conn()
        try:
            channels=[dict(row) for row in conn.execute('SELECT weekday,channel_id,title FROM channels ORDER BY weekday,title')]
            total=conn.execute('SELECT COUNT(*) FROM videos').fetchone()[0]
            eligible=sum(row['eligible'] for row in db.channel_stats(conn).values())
        finally:
            conn.close()
    finally:
        store.account_context.reset(context)
    # channel_stats is keyed by unique channel, so a channel on two days is counted once.
    unique=len({row['channel_id'] for row in channels})
    schedule='\n'.join(DAYS[row['weekday']]+': '+row['title'] for row in channels) or 'Your schedule is ready for its first channel.'
    preference=store.base_url()+'/email-preferences#'+preference_token(account['id'])
    intro=f'Your week has {unique} selected channels and {total:,} found videos; {eligible:,} currently fit your viewing rules.'
    text=('A little hello from The Daily Telly.\n\n'+intro+'\n\n'+schedule+
          '\n\nMonday can be Sesame Street day. Tuesday can have its own familiar favourite. A few choices, on their own days, leave room for the rest of life.'
          '\n\nAdd another channel, swap a day, or keep things just as they are:\n'+store.base_url()+'/app'+
          '\n\nThis is your one optional welcome check-in, not a recurring newsletter. Skip it or manage your preference: '+preference+
          '\n\nFree and open source. We add no ads or tracking. YouTube playback has its own advertising and data practices.')
    rows=''.join('<tr><td style="padding:9px 10px;border-bottom:1px solid #dde3d9;font-size:12px">'+DAYS[row['weekday']]+'</td><td style="padding:9px 10px;border-bottom:1px solid #dde3d9">'+escape(row['title'])+'</td></tr>' for row in channels)
    content='<p>A little hello, now that you’ve had time to settle in.</p><p>'+escape(intro)+'</p>'
    if rows:
        content+='<table role="presentation" width="100%" style="border-collapse:collapse;background:#edf2e8;border-radius:12px">'+rows+'</table>'
    content+='<p>A familiar favourite on its own day. Something to look forward to, and room for life beyond the screen.</p><p>Add another channel, swap a day, or keep your week just as it is. This is your routine to shape.</p>'
    footer='Your one optional welcome check-in; no recurring newsletter. <a href="'+escape(preference,quote=True)+'" style="color:#183d34">Manage this reminder</a>.<br>YouTube playback has its own advertising and data practices.'
    return {'from':os.environ.get('DAILYTELLY_MAIL_FROM','The Daily Telly <hello@thedailytelly.com>'),
            'to':[account['email']],'subject':'Your little week of telly is taking shape', 'text':text,
            'html':mailer.branded_html('A little routine. A lot to look forward to.','How’s your week looking?',content,('Make the week your own',store.base_url()+'/app'),footer)}


def dispatch_reminders(at=None):
    timestamp=store.now() if at is None else at
    if dt.datetime.fromtimestamp(timestamp,EASTERN).hour!=20 or not dispatch_lock.acquire(blocking=False):
        return
    try:
        with store.registry() as conn:
            jobs=[dict(row) for row in conn.execute('''SELECT r.*,a.email,a.status,a.verified_at FROM welcome_reminders r
              JOIN accounts a ON a.id=r.account_id JOIN account_setup s ON s.account_id=a.id
              WHERE r.state IN ('pending','sending') AND r.due_at<=? AND s.reminder_enabled=1
              AND a.status='active' AND a.verified_at IS NOT NULL ORDER BY r.due_at LIMIT 20''',(timestamp,))]
        for job in jobs:
            if job['attempts']>=6 or (job['first_attempt'] and timestamp-job['first_attempt']>23*3600):
                with store.registry() as conn:
                    conn.execute("UPDATE welcome_reminders SET state='failed',payload=NULL WHERE account_id=?",(job['account_id'],))
                continue
            try:
                payload=json.loads(job['payload']) if job['payload'] else summary_payload({'id':job['account_id'],'email':job['email'],'status':job['status'],'verified_at':job['verified_at']})
                with store.registry() as conn:
                    # Re-check preference/status immediately before sending, including account deletion.
                    active=conn.execute('''SELECT 1 FROM accounts a JOIN account_setup s ON s.account_id=a.id
                      JOIN welcome_reminders r ON r.account_id=a.id WHERE a.id=? AND a.status='active'
                      AND s.reminder_enabled=1 AND r.state IN ('pending','sending')''',(job['account_id'],)).fetchone()
                    if not active:
                        continue
                    conn.execute("UPDATE welcome_reminders SET state='sending',first_attempt=COALESCE(first_attempt,?),attempts=attempts+1,payload=? WHERE account_id=?",
                                 (timestamp,json.dumps(payload),job['account_id']))
                mailer.send_reminder(payload,job['account_id'])
                with store.registry() as conn:
                    conn.execute("UPDATE welcome_reminders SET state='sent',sent_at=?,payload=NULL WHERE account_id=?",(timestamp,job['account_id']))
            except Exception:
                # Provider retries reuse the frozen payload and idempotency key; no email/body logs.
                continue
    finally:
        dispatch_lock.release()


class UnsubscribeIn(BaseModel):
    token: str=Field(min_length=97,max_length=97)


@router.post('/email-preferences')
def unsubscribe(body: UnsubscribeIn,request: Request):
    store.limit('welcome-preference',request.client.host if request.client else '',20,900)
    identifier=body.token.split('.')[0]
    if not re.fullmatch(r'[a-f0-9]{32}',identifier) or not hmac.compare_digest(body.token,preference_token(identifier)):
        raise HTTPException(400,'This preference link is invalid')
    with store.registry() as conn:
        conn.execute('UPDATE account_setup SET reminder_enabled=0 WHERE account_id=?',(identifier,))
        conn.execute("UPDATE welcome_reminders SET state='cancelled',payload=NULL WHERE account_id=? AND sent_at IS NULL",(identifier,))
    return {'message':'All set. We won’t send the welcome check-in. Sign-in emails will still work normally.'}
