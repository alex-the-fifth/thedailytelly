"""Verified email accounts, explicit TV pairing, and privacy-limited owner APIs."""
import json
import os
import re
import secrets
import sqlite3

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from . import db, mailer, platform_store as store, starter

router = APIRouter()


def public_account(account):
    return {'email': account['email'], 'created_at': account['created_at'],
            'is_owner': store.is_owner(account), 'verified': bool(account['verified_at'])}


class EmailIn(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    accepted: bool
    reminder: bool = True


@router.get('/auth/mode')
def auth_mode():
    return {'email_enabled': os.environ.get('DAILYTELLY_MAIL_MODE', 'file') != 'disabled'}


@router.post('/auth/email', status_code=202)
def request_link(body: EmailIn, request: Request, response: Response):
    if os.environ.get('DAILYTELLY_MAIL_MODE') == 'disabled':
        raise HTTPException(503, 'Public registration is not open yet. Email sign-in is being prepared.')
    email = body.email.strip().lower()
    if not body.accepted:
        raise HTTPException(400, 'Please confirm you are an adult and accept the policies')
    if not re.fullmatch(r'[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+', email) or not email.isascii():
        raise HTTPException(400, 'Enter a valid email address')
    peer = request.client.host if request.client else 'unknown'
    store.limit('email-peer', peer, 10, 900)
    store.limit('email-address', email, 3, 900)
    store.limit('email-address-day', email, 5, 86400)
    store.limit('email-platform-hour', 'service', int(os.environ.get('DAILYTELLY_EMAIL_HOURLY_BUDGET','100')), 3600)
    store.limit('email-platform-day', 'service', int(os.environ.get('DAILYTELLY_EMAIL_DAILY_BUDGET','500')), 86400)
    token, intent = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with store.registry() as conn:
        created = conn.execute('INSERT OR IGNORE INTO accounts (id,email,created_at) VALUES (?,?,?)',
                     (secrets.token_hex(16), email, store.now()))
        account = conn.execute('SELECT * FROM accounts WHERE email=?', (email,)).fetchone()
        if created.rowcount:
            conn.execute('INSERT INTO account_setup(account_id,reminder_enabled) VALUES(?,?)',(account['id'],int(body.reminder)))
        elif not account['verified_at']:
            conn.execute('UPDATE account_setup SET reminder_enabled=? WHERE account_id=?',(int(body.reminder),account['id']))
        conn.execute('DELETE FROM email_links WHERE account_id=?', (account['id'],))
        conn.execute('INSERT INTO email_links VALUES (?,?,?,?)',
                     (store.digest(token), account['id'], store.digest(intent), store.now()+900))
    try:
        mailer.send_signin(email, token)
    except Exception:
        with store.registry() as conn:
            conn.execute('DELETE FROM email_links WHERE hash=?', (store.digest(token),))
        raise HTTPException(503, 'Email delivery is temporarily unavailable. Please try again later.')
    response.set_cookie(store.INTENT_COOKIE, intent, max_age=900, httponly=True,
                        secure=store.secure_cookie(), samesite='lax', path='/auth')
    return {'message': 'Check your email for a sign-in link. Open it in this browser.'}


class VerifyIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)


class OperatorIn(VerifyIn):
    accepted: bool


@router.post('/auth/operator')
def operator_signin(body: OperatorIn, request: Request, response: Response):
    if os.environ.get('DAILYTELLY_MAIL_MODE') != 'disabled':
        raise HTTPException(404, 'Not available')
    if not body.accepted:
        raise HTTPException(400, 'Confirm you are the adult platform owner and accept the policies')
    store.limit('operator-link', request.client.host if request.client else '', 20, 900)
    session = secrets.token_urlsafe(32)
    with store.registry() as conn:
        conn.execute('BEGIN IMMEDIATE')
        link = conn.execute('SELECT * FROM operator_links WHERE hash=? AND expires_at>?',
                            (store.digest(body.token), store.now())).fetchone()
        if not link:
            raise HTTPException(400, 'Owner link expired or already used. Issue a new one on the server.')
        row = conn.execute('SELECT * FROM accounts WHERE id=?', (link['account_id'],)).fetchone()
        if not row or not store.is_owner(dict(row)):
            raise HTTPException(403, 'Owner access is unavailable')
        conn.execute('DELETE FROM operator_links WHERE account_id=?', (row['id'],))
        conn.execute('UPDATE accounts SET last_login_at=?,policy_version=?,accepted_at=? WHERE id=?',
                     (store.now(),store.POLICY_VERSION,store.now(),row['id']))
        conn.execute('INSERT INTO sessions VALUES (?,?,?)', (store.digest(session), row['id'], store.now()+store.SESSION_TTL_SECONDS))
    response.set_cookie(store.SESSION_COOKIE,session,max_age=store.SESSION_TTL_SECONDS,httponly=True,
                        secure=store.secure_cookie(),samesite='lax',path='/')
    store.audit('operator','owner_link_redeemed',row['id'])
    return public_account(dict(row))


@router.post('/auth/verify')
def verify(body: VerifyIn, request: Request, response: Response):
    store.limit('verify-peer', request.client.host if request.client else '', 30, 900)
    intent = request.cookies.get(store.INTENT_COOKIE, '')
    with store.registry() as conn:
        conn.execute('BEGIN IMMEDIATE')
        link = conn.execute('''SELECT * FROM email_links WHERE hash=? AND intent_hash=?
            AND expires_at>?''', (store.digest(body.token), store.digest(intent), store.now())).fetchone()
        if not link:
            raise HTTPException(400, 'Link expired, already used, or opened in another browser. Request a new link.')
        account = conn.execute('SELECT * FROM accounts WHERE id=?', (link['account_id'],)).fetchone()
        if account['status'] == 'suspended':
            raise HTTPException(403, 'This account is suspended')
        conn.execute('DELETE FROM email_links WHERE account_id=?', (account['id'],))
        conn.execute('''UPDATE accounts SET verified_at=COALESCE(verified_at,?), last_login_at=?,
            status='active', policy_version=?, accepted_at=? WHERE id=?''',
            (store.now(), store.now(), store.POLICY_VERSION, store.now(), account['id']))
        account = dict(conn.execute('SELECT * FROM accounts WHERE id=?', (account['id'],)).fetchone())
    store.initialize_library(account)
    starter.apply_starter(account)
    session = secrets.token_urlsafe(32)
    with store.registry() as conn:
        conn.execute('INSERT INTO sessions VALUES (?,?,?)', (store.digest(session), account['id'], store.now()+store.SESSION_TTL_SECONDS))
    response.set_cookie(store.SESSION_COOKIE, session, max_age=store.SESSION_TTL_SECONDS, httponly=True,
                        secure=store.secure_cookie(), samesite='lax', path='/')
    response.delete_cookie(store.INTENT_COOKIE, path='/auth')
    return public_account(account)


@router.post('/auth/logout')
def logout(request: Request, response: Response):
    with store.registry() as conn:
        conn.execute('DELETE FROM sessions WHERE hash=?', (store.digest(request.cookies.get(store.SESSION_COOKIE, '')),))
    response.delete_cookie(store.SESSION_COOKIE, path='/')
    return {'ok': True}


@router.get('/auth/me')
def me(account=Depends(store.require_account)):
    return public_account(account)


class PairStart(BaseModel):
    label: str = Field(default='Android TV', min_length=1, max_length=40)


@router.post('/pair/start')
def pair_start(body: PairStart, request: Request):
    store.limit('pair-start', request.client.host if request.client else '', 20, 3600)
    device_code = secrets.token_urlsafe(32)
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    user_code = ''.join(secrets.choice(alphabet) for _ in range(8))
    with store.registry() as conn:
        conn.execute('''INSERT INTO pairing
            (device_hash,code_hash,expires_at,label,created_at) VALUES (?,?,?,?,?)''',
            (store.digest(device_code), store.digest(user_code), store.now()+600, body.label, store.now()))
    return {'device_code': device_code, 'user_code': user_code[:4]+'-'+user_code[4:],
            'verification_uri': store.base_url()+'/activate', 'expires_in': 600, 'interval': 5}


class PairApprove(BaseModel):
    code: str = Field(min_length=8, max_length=12)
    confirm: bool


@router.post('/pair/approve')
def pair_approve(body: PairApprove, account=Depends(store.require_account)):
    store.limit('pair-approve', account['id'], 10, 900)
    if not body.confirm:
        raise HTTPException(400, 'Confirm this code is displayed on your own TV')
    code = body.code.replace('-', '').replace(' ', '').upper()
    with store.registry() as conn:
        conn.execute('BEGIN IMMEDIATE')
        current = conn.execute('SELECT * FROM pairing WHERE code_hash=? AND expires_at>?',
                               (store.digest(code), store.now())).fetchone()
        if not current or current['account_id']:
            raise HTTPException(400, 'Code expired, invalid, or already approved')
        count = conn.execute('SELECT COUNT(*) FROM paired_devices WHERE account_id=?', (account['id'],)).fetchone()[0]
        if count >= 10:
            raise HTTPException(409, 'Remove an old device before linking another (limit 10)')
        conn.execute('UPDATE pairing SET account_id=? WHERE device_hash=?', (account['id'], current['device_hash']))
    return {'ok': True, 'label': current['label']}


class PairPoll(BaseModel):
    device_code: str = Field(min_length=20, max_length=200)


@router.post('/pair/poll')
def pair_poll(body: PairPoll, request: Request):
    store.limit('pair-poll-peer', request.client.host if request.client else '', 120, 60)
    store.limit('pair-poll', body.device_code, 15, 60)
    with store.registry() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM pairing WHERE device_hash=? AND expires_at>?',
                           (store.digest(body.device_code), store.now())).fetchone()
        if not row:
            raise HTTPException(400, 'expired_token')
        if store.now()-row['last_poll_at'] < 5:
            return {'status': 'slow_down', 'interval': 5}
        conn.execute('UPDATE pairing SET last_poll_at=? WHERE device_hash=?', (store.now(), row['device_hash']))
        if not row['account_id']:
            return {'status': 'authorization_pending', 'interval': 5}
        account = conn.execute("SELECT * FROM accounts WHERE id=? AND status='active'", (row['account_id'],)).fetchone()
        if not account:
            raise HTTPException(403, 'Account unavailable')
        if conn.execute('SELECT COUNT(*) FROM paired_devices WHERE account_id=?', (account['id'],)).fetchone()[0] >= 10:
            raise HTTPException(409, 'Device limit reached')
        token, device_id = secrets.token_urlsafe(32), secrets.token_hex(16)
        conn.execute('INSERT INTO paired_devices VALUES (?,?,?,?,?)',
                     (device_id, account['id'], store.digest(token), row['label'], store.now()))
        conn.execute('DELETE FROM pairing WHERE device_hash=?', (row['device_hash'],))
    return {'status': 'linked', 'access_token': token, 'token_type': 'Bearer', 'device_id': device_id}


@router.get('/account/devices')
def devices(account=Depends(store.require_account)):
    with store.registry() as conn:
        rows = conn.execute('SELECT id,label,created_at FROM paired_devices WHERE account_id=? ORDER BY created_at DESC',
                            (account['id'],)).fetchall()
    return {'devices': [dict(row) for row in rows]}


@router.delete('/account/devices/{device_id}')
def revoke(device_id: str, account=Depends(store.require_account)):
    with store.registry() as conn:
        conn.execute('DELETE FROM paired_devices WHERE id=? AND account_id=?', (device_id, account['id']))
    return {'ok': True}


@router.get('/account/export')
def export(account=Depends(store.require_account)):
    conn = db.get_conn()
    try:
        settings = db.get_settings(conn)
        data = {table: [dict(row) for row in conn.execute('SELECT * FROM '+table)]
                for table in ('channels', 'videos', 'watch_progress', 'blocked_keywords')}
        data['settings'] = {key: settings[key] for key in ('min_seconds','max_seconds','sync_interval_minutes','timezone')}
        data['account'] = public_account(account)
        return Response(json.dumps(data), media_type='application/json',
                        headers={'Content-Disposition': 'attachment; filename="dailytelly-library.json"'})
    finally:
        conn.close()


class DeleteIn(BaseModel):
    confirmation: str


@router.post('/account/delete')
def delete_account(body: DeleteIn, response: Response, account=Depends(store.require_account)):
    if body.confirmation != 'DELETE':
        raise HTTPException(400, 'Type DELETE to confirm')
    if store.is_owner(account):
        raise HTTPException(409, 'Transfer owner responsibility before deleting the owner account')
    path = store.library_path(account['id'])
    with store.registry() as conn:
        conn.execute('INSERT OR REPLACE INTO deletion_queue VALUES (?,?)', (account['id'],store.now()))
        conn.execute('INSERT OR REPLACE INTO deletion_records VALUES (?,?)', (account['id'],store.now()))
        conn.execute('DELETE FROM accounts WHERE id=?', (account['id'],))
    store.purge_library(account['id'])
    response.delete_cookie(store.SESSION_COOKIE, path='/')
    return {'ok': True}


@router.get('/ops/accounts')
def accounts(offset: int = Query(default=0, ge=0), q: str = Query(default='', max_length=254), owner=Depends(store.require_owner)):
    with store.registry() as conn:
        rows = conn.execute('''SELECT a.id,a.email,a.created_at,a.verified_at,a.last_login_at,a.status,
            (SELECT COUNT(*) FROM paired_devices d WHERE d.account_id=a.id) AS devices
            FROM accounts a WHERE a.email LIKE ? ORDER BY a.created_at DESC,a.id LIMIT 100 OFFSET ?''',
            ('%'+q.strip().lower()+'%',offset)).fetchall()
        total = conn.execute('SELECT COUNT(*) FROM accounts WHERE email LIKE ?', ('%'+q.strip().lower()+'%',)).fetchone()[0]
    return {'accounts': [dict(row) for row in rows], 'total': total, 'offset': offset, 'has_more': offset+len(rows)<total}


class StatusIn(BaseModel):
    status: str = Field(pattern='^(active|suspended)$')


@router.patch('/ops/accounts/{account_id}')
def account_status(account_id: str, body: StatusIn, owner=Depends(store.require_owner)):
    with store.registry() as conn:
        target = conn.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
        if not target:
            raise HTTPException(404, 'Account not found')
        if store.is_owner(dict(target)) or account_id == owner['id']:
            raise HTTPException(409, 'Owner accounts cannot be suspended here')
        if not target['verified_at']:
            raise HTTPException(409, 'Pending accounts must verify their email')
        conn.execute('UPDATE accounts SET status=? WHERE id=?', (body.status, account_id))
        if body.status == 'suspended':
            conn.execute('DELETE FROM sessions WHERE account_id=?', (account_id,))
            conn.execute('DELETE FROM paired_devices WHERE account_id=?', (account_id,))
            conn.execute('DELETE FROM pairing WHERE account_id=?', (account_id,))
    store.audit(owner['id'], 'account_'+body.status, account_id)
    return {'ok': True}


@router.get('/ops/metrics')
def metrics(owner=Depends(store.require_owner)):
    from . import resources
    with store.registry() as conn:
        samples = [dict(row) for row in conn.execute('SELECT * FROM resource_samples ORDER BY at DESC LIMIT 60')]
        audit = [dict(row) for row in conn.execute('SELECT * FROM operator_audit ORDER BY at DESC LIMIT 50')]
        quota = [dict(row) for row in conn.execute('SELECT * FROM catalog_quota ORDER BY day DESC')]
        reminders = {row['state']:row['total'] for row in conn.execute('SELECT state,COUNT(*) AS total FROM welcome_reminders GROUP BY state')}
    return {'current': resources.snapshot(), 'samples': list(reversed(samples)), 'audit': audit, 'catalog_quota': quota, 'welcome_reminders':reminders}
