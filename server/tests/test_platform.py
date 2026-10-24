"""Offline security/behavior coverage with independent browser accounts and TV clients."""
import hashlib
import json
from pathlib import Path
import sqlite3

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app import config, db, mailer, migration, platform_store as store, resources, youtube
from app.main import app

BASE = 'http://localhost:18400'


def owner_link(email='hello@thedailytelly.com'):
    import importlib.util
    path=Path(__file__).resolve().parents[2]/'scripts'/'owner-access.py'
    spec=importlib.util.spec_from_file_location('owner_access_cli',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.issue_owner_link(email).split('#')[1]


def test_closed_registration_and_single_use_owner_link(client,monkeypatch):
    monkeypatch.setenv('DAILYTELLY_MAIL_MODE','disabled')
    assert client.get('/auth/mode').json()=={'email_enabled':False}
    assert client.post('/auth/email',json={'email':'stranger@example.com','accepted':True}).status_code==503
    with store.registry() as conn:assert conn.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]==0
    with pytest.raises(ValueError):owner_link('stranger@example.com')
    token=owner_link()
    with store.registry() as conn:
        assert token not in json.dumps([dict(r) for r in conn.execute('SELECT * FROM operator_links')])
    assert client.post('/auth/operator',json={'token':token,'accepted':False}).status_code==400
    assert client.post('/auth/operator',json={'token':token,'accepted':True},headers={'Origin':'https://evil.example'}).status_code==403
    assert client.post('/auth/operator',json={'token':token,'accepted':True}).status_code==200
    assert client.get('/ops/accounts').status_code==200
    assert client.post('/auth/operator',json={'token':token,'accepted':True}).status_code==400


def test_owner_link_expiry_replacement_and_smtp_disablement(client,monkeypatch):
    monkeypatch.setenv('DAILYTELLY_MAIL_MODE','disabled')
    expired=owner_link()
    with store.registry() as conn:conn.execute('UPDATE operator_links SET expires_at=0')
    assert client.post('/auth/operator',json={'token':expired,'accepted':True}).status_code==400
    old=owner_link();new=owner_link()
    assert client.post('/auth/operator',json={'token':old,'accepted':True}).status_code==400
    monkeypatch.setenv('DAILYTELLY_MAIL_MODE','smtp')
    assert client.post('/auth/operator',json={'token':new,'accepted':True}).status_code==404


def test_production_closed_registration_keeps_https_and_owner_requirements(client,monkeypatch):
    monkeypatch.setenv('DAILYTELLY_ENV','production')
    monkeypatch.setenv('DAILYTELLY_PUBLIC_URL','https://telly.example')
    monkeypatch.setenv('DAILYTELLY_MAIL_MODE','disabled')
    store.initialize()
    monkeypatch.delenv('DAILYTELLY_OWNER_EMAILS')
    with pytest.raises(RuntimeError):store.initialize()
    monkeypatch.setenv('DAILYTELLY_OWNER_EMAILS','owner@example.com')
    monkeypatch.setenv('DAILYTELLY_PUBLIC_URL','http://telly.example')
    with pytest.raises(RuntimeError):store.initialize()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(config, 'YOUTUBE_API_KEY', '')
    monkeypatch.setenv('DAILYTELLY_ENV','development')
    monkeypatch.setenv('DAILYTELLY_PUBLIC_URL',BASE)
    monkeypatch.setenv('DAILYTELLY_MAIL_MODE','file')
    monkeypatch.setenv('DAILYTELLY_OWNER_EMAILS','hello@thedailytelly.com')
    monkeypatch.setattr(resources,'send_alert',lambda message: False)
    with TestClient(app,base_url=BASE) as current:
        yield current
    assert not app.state.scheduler.running


def token_for(email):
    messages = [json.loads(path.read_text()) for path in sorted((config.DATA_DIR/'dev-mail').glob('*.json'),key=lambda path:path.stat().st_mtime_ns)]
    return next(message['url'].split('#')[1] for message in reversed(messages) if message['to']==email)


def signin(client,email='parent@example.com'):
    response=client.post('/auth/email',json={'email':email,'accepted':True})
    assert response.status_code==202, response.text
    token=token_for(email)
    response=client.post('/auth/verify',json={'token':token})
    assert response.status_code==200, response.text
    return store.account_for_session(client.cookies.get(store.SESSION_COOKIE))


def seed(account,title='A small discovery'):
    context=store.account_context.set(account)
    try:
        conn=db.get_conn()
        import datetime
        day=datetime.datetime.now(datetime.timezone.utc).weekday()
        conn.execute('INSERT INTO channels (weekday,channel_id,title) VALUES (?,?,?)',(day,'UCdemo',title))
        conn.execute('''INSERT INTO videos (video_id,channel_id,title,duration_seconds,published_at,first_seen_at)
                        VALUES ('same-id','UCdemo',?,600,?,?)''',(title,db.utcnow(),db.utcnow()))
        conn.commit()
        conn.close()
    finally:
        store.account_context.reset(context)


def pair(browser):
    device=TestClient(app,base_url=BASE)
    start=device.post('/pair/start',json={}).json()
    assert browser.post('/pair/approve',json={'code':start['user_code'],'confirm':True}).status_code==200
    result=device.post('/pair/poll',json={'device_code':start['device_code']}).json()
    assert result['status']=='linked'
    return device,{'Authorization':'Bearer '+result['access_token']},result


def test_anonymous_and_unverified_access_is_closed(client):
    for path in ['/admin/settings','/api/today','/auth/me','/account/devices','/ops/accounts']:
        assert client.get(path).status_code==401
    assert client.get('/healthz').json()=={'ok':True}
    assert client.get('/').status_code==200
    assert client.post('/auth/email',json={'email':'pending@example.com','accepted':True}).status_code==202
    assert client.get('/api/today').status_code==401
    with pytest.raises(HTTPException):
        db.get_conn()


def test_email_confirmation_is_explicit_single_use_and_browser_bound(client):
    client.post('/auth/email',json={'email':'parent@example.com','accepted':True})
    token=token_for('parent@example.com')
    outsider=TestClient(app,base_url=BASE)
    assert outsider.post('/auth/verify',json={'token':token}).status_code==400
    assert client.get('/verify').status_code==200  # email scanners cannot consume a GET
    assert client.post('/auth/verify',json={'token':token}).status_code==200
    assert client.post('/auth/verify',json={'token':token}).status_code==400
    assert client.get('/auth/me').json()['verified']
    assert store.SESSION_COOKIE in client.cookies


def test_email_validation_consent_and_origin(client):
    assert client.post('/auth/email',json={'email':'parent@example.com','accepted':False}).status_code==400
    assert client.post('/auth/email',json={'email':'bad\r\nBcc: x@example.com','accepted':True}).status_code==400
    assert client.post('/auth/email',json={'email':'parent@example.com','accepted':True},headers={'Origin':'https://evil.example'}).status_code==403


def test_expired_email_and_session(client):
    client.post('/auth/email',json={'email':'parent@example.com','accepted':True})
    token=token_for('parent@example.com')
    with store.registry() as conn: conn.execute('UPDATE email_links SET expires_at=0')
    assert client.post('/auth/verify',json={'token':token}).status_code==400
    # Clear old outbox copies so the helper selects the latest link deterministically.
    for path in sorted((config.DATA_DIR/'dev-mail').glob('*.json'),key=lambda path:path.stat().st_mtime_ns): path.unlink()
    signin(client)
    with store.registry() as conn: conn.execute('UPDATE sessions SET expires_at=0')
    assert client.get('/auth/me').status_code==401


def test_signin_rate_limits_and_no_plaintext_peer_storage(client):
    for _ in range(3): assert client.post('/auth/email',json={'email':'parent@example.com','accepted':True}).status_code==202
    assert client.post('/auth/email',json={'email':'parent@example.com','accepted':True}).status_code==429
    with store.registry() as conn:
        rows=[dict(row) for row in conn.execute('SELECT * FROM rate_limits')]
    assert 'parent@example.com' not in json.dumps(rows) and 'testclient' not in json.dumps(rows)


def test_tokens_only_stored_as_hashes(client):
    signin(client)
    session=client.cookies.get(store.SESSION_COOKIE)
    with store.registry() as conn:
        row=conn.execute('SELECT hash FROM sessions').fetchone()
        assert row['hash']==store.digest(session) and row['hash']!=session


def test_browser_session_persists_for_30_days_and_extends_while_active(client):
    client.post('/auth/email',json={'email':'parent@example.com','accepted':True})
    response=client.post('/auth/verify',json={'token':token_for('parent@example.com')})
    assert response.status_code==200
    assert f'max-age={store.SESSION_TTL_SECONDS}' in response.headers.get('set-cookie','').lower()
    cookie=client.cookies.get(store.SESSION_COOKIE)
    with store.registry() as conn:
        conn.execute('UPDATE sessions SET expires_at=? WHERE hash=?',
                     (store.now()+store.SESSION_TTL_SECONDS//4,store.digest(cookie)))
    response=client.get('/auth/me')
    assert response.status_code==200
    assert f'max-age={store.SESSION_TTL_SECONDS}' in response.headers.get('set-cookie','').lower()
    with store.registry() as conn:
        expiry=conn.execute('SELECT expires_at FROM sessions WHERE hash=?',
                            (store.digest(cookie),)).fetchone()['expires_at']
    assert expiry>=store.now()+store.SESSION_TTL_SECONDS-2


def test_two_accounts_with_same_video_id_are_isolated(client):
    first=signin(client,'first@example.com');seed(first,'First household')
    second_client=TestClient(app,base_url=BASE)
    second=signin(second_client,'second@example.com');seed(second,'Second household')
    assert client.get('/api/today').json()['videos'][0]['title']=='First household'
    assert second_client.get('/api/today').json()['videos'][0]['title']=='Second household'
    client.post('/api/progress',json={'video_id':'same-id','position_seconds':300,'event':'heartbeat'})
    assert second_client.get('/api/today').json()['videos'][0]['position_seconds']==0
    client.post('/admin/videos/same-id/blacklist')
    assert client.get('/api/today').json()['videos']==[]
    assert len(second_client.get('/api/today?account_id='+first['id']).json()['videos'])==1


def test_sync_status_is_scoped(client):
    first=signin(client,'first@example.com')
    second_browser=TestClient(app,base_url=BASE);second=signin(second_browser,'second@example.com')
    context=store.account_context.set(first)
    try: youtube._set_status(current_channel='Private channel')
    finally: store.account_context.reset(context)
    assert second_browser.get('/admin/sync-status').json()['current_channel'] is None


def test_pairing_requires_confirmed_browser_and_explicit_approval(client):
    start=client.post('/pair/start',json={}).json()
    assert len(start['device_code'])>=40 and len(start['user_code'])==9
    assert start['device_code'] not in start['verification_uri']
    assert client.post('/pair/approve',json={'code':start['user_code'],'confirm':True}).status_code==401
    signin(client)
    assert client.post('/pair/approve',json={'code':start['user_code'],'confirm':False}).status_code==400
    pending=client.post('/pair/poll',json={'device_code':start['device_code']}).json()
    assert pending['status']=='authorization_pending'
    assert 'access_token' not in pending


def test_tv_pairing_is_single_use_playback_only_and_revocable(client):
    account=signin(client);seed(account)
    device,headers,result=pair(client)
    assert device.get('/api/today',headers=headers).status_code==200
    assert device.get('/admin/settings',headers=headers).status_code==401
    assert device.get('/ops/accounts',headers=headers).status_code==401
    assert device.get('/account/export',headers=headers).status_code==401
    assert client.delete('/account/devices/'+result['device_id']).status_code==200
    assert device.get('/api/today',headers=headers).status_code==401


def test_expired_and_redeemed_codes_cannot_be_reused(client):
    signin(client)
    start=client.post('/pair/start',json={}).json()
    assert client.post('/pair/approve',json={'code':start['user_code'],'confirm':True}).status_code==200
    assert client.post('/pair/poll',json={'device_code':start['device_code']}).json()['status']=='linked'
    assert client.post('/pair/poll',json={'device_code':start['device_code']}).status_code==400
    expired=client.post('/pair/start',json={}).json()
    with store.registry() as conn: conn.execute('UPDATE pairing SET expires_at=0')
    assert client.post('/pair/approve',json={'code':expired['user_code'],'confirm':True}).status_code==400


def test_wrong_household_cannot_revoke_device(client):
    signin(client,'first@example.com');device,headers,result=pair(client)
    other=TestClient(app,base_url=BASE);signin(other,'second@example.com')
    other.delete('/account/devices/'+result['device_id'])
    assert device.get('/api/today',headers=headers).status_code==200


def test_history_and_eligibility_cannot_bypass_rules(client):
    account=signin(client);seed(account)
    assert client.post('/api/progress',json={'video_id':'same-id','position_seconds':570,'event':'heartbeat'}).json()['completed']
    assert client.get('/api/history').json()['videos'][0]['playable']
    assert client.post('/admin/videos/same-id/blacklist').status_code==200
    assert client.get('/api/player?v=same-id').status_code==403
    assert client.post('/api/progress',json={'video_id':'same-id','position_seconds':10,'event':'start'}).status_code==403


def test_no_child_viewing_events_ip_or_user_agent_in_activity(client):
    account=signin(client);seed(account)
    client.get('/api/today',headers={'User-Agent':'do-not-retain-this'})
    client.post('/api/progress',json={'video_id':'same-id','position_seconds':10,'event':'start'})
    assert client.get('/admin/activity').json()['activity']==[]


def test_owner_audit_is_separate_from_household_admin(client):
    account=signin(client);seed(account,'Private family title')
    assert client.get('/ops/accounts').status_code==403
    owner=TestClient(app,base_url=BASE);signin(owner,'hello@thedailytelly.com')
    assert owner.get('/auth/me').json()['is_owner']
    accounts=owner.get('/ops/accounts').json()
    assert 'Private family title' not in json.dumps(accounts)
    assert owner.patch('/ops/accounts/'+account['id'],json={'status':'suspended'}).status_code==200
    assert client.get('/api/today').status_code==401
    audit=owner.get('/ops/metrics').json()['audit']
    assert audit[0]['action']=='account_suspended'


def test_export_contains_only_own_library_and_no_credentials(client):
    account=signin(client);seed(account)
    data=client.get('/account/export').json()
    assert data['videos'][0]['title']=='A small discovery'
    for secret in ('device_token','parent_password_hash','pushover_token','session'):
        assert secret not in json.dumps(data)


def test_account_deletion_revokes_tv_and_removes_own_content(client):
    account=signin(client);seed(account)
    device,headers,_=pair(client)
    path=store.library_path(account['id'])
    assert client.post('/account/delete',json={'confirmation':'wrong'}).status_code==400
    assert client.post('/account/delete',json={'confirmation':'DELETE'}).status_code==200
    assert not path.exists()
    assert device.get('/api/today',headers=headers).status_code==401
    assert client.get('/auth/me').status_code==401


def test_resource_alert_sustained_threshold_and_cooldown(client,monkeypatch):
    sent=[]
    monkeypatch.setattr(resources,'snapshot',lambda:{'cpu':95,'memory':30,'disk':40,'requests':1,'errors':0})
    monkeypatch.setattr(resources,'send_alert',lambda message:sent.append(message) or True)
    for _ in range(3):resources.sample()
    assert len(sent)==1 and 'cpu' in sent[0]
    for _ in range(3):resources.sample()
    assert len(sent)==1
    assert '@' not in sent[0]


def test_legacy_import_preserves_library_not_secrets_and_never_changes_source(client,tmp_path):
    account=signin(client,'legacy@example.com');seed(account,'My old library')
    context=store.account_context.set(account)
    try:
        conn=db.get_conn()
        conn.execute("UPDATE settings SET device_token='old-secret',pushover_token='old-push'")
        db.apply_progress(conn,'same-id',200,'pause','old-device')
        backup=tmp_path/'legacy-backup.sqlite3'
        target=sqlite3.connect(backup);conn.backup(target);target.close();conn.close()
    finally:store.account_context.reset(context)
    other=TestClient(app,base_url=BASE);destination=signin(other,'destination@example.com')
    before=hashlib.sha256(backup.read_bytes()).hexdigest()
    assert migration.import_backup(backup,'destination@example.com')['dry_run']
    assert migration.import_backup(backup,'destination@example.com',True)['videos']==1
    assert before==hashlib.sha256(backup.read_bytes()).hexdigest()
    assert other.get('/api/today').json()['videos'][0]['position_seconds']==200
    context=store.account_context.set(destination)
    try:
        conn=db.get_conn();settings=db.get_settings(conn)
        assert settings['device_token']!='old-secret' and settings['pushover_token']==''
        conn.close()
    finally:store.account_context.reset(context)
    with pytest.raises(ValueError,match='already'):
        migration.import_backup(backup,'destination@example.com',True)


def test_production_cannot_use_development_mail(client,monkeypatch):
    monkeypatch.setenv('DAILYTELLY_ENV','production')
    monkeypatch.setenv('DAILYTELLY_PUBLIC_URL','https://thedailytelly.com')
    with pytest.raises(RuntimeError,match='SMTP'):
        store.initialize()


def test_logout_invalidates_server_session(client):
    signin(client);cookie=client.cookies.get(store.SESSION_COOKIE)
    assert client.post('/auth/logout').status_code==200
    assert not store.account_for_session(cookie)


@pytest.mark.parametrize('port',[465,587])
def test_smtp_signin_is_encrypted_branded_and_accessible(client,monkeypatch,port):
    captured={}
    class SMTP:
        def __init__(self,host,selected_port,**kwargs):
            assert host=='smtp.resend.com' and selected_port==port
            if port==465:assert kwargs['context'].check_hostname
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def starttls(self,context):
            assert context.check_hostname;captured['starttls']=True
        def login(self,user,password):assert user=='resend' and password=='synthetic-test-secret'
        def send_message(self,message):captured['message']=message
    monkeypatch.setattr(mailer.smtplib,'SMTP_SSL',SMTP)
    monkeypatch.setattr(mailer.smtplib,'SMTP',SMTP)
    for key,value in {'DAILYTELLY_MAIL_MODE':'smtp','DAILYTELLY_SMTP_HOST':'smtp.resend.com','DAILYTELLY_SMTP_PORT':str(port),
                      'DAILYTELLY_SMTP_USER':'resend','DAILYTELLY_SMTP_PASSWORD':'synthetic-test-secret',
                      'DAILYTELLY_MAIL_FROM':'The Daily Telly <hello@thedailytelly.com>'}.items():monkeypatch.setenv(key,value)
    mailer.send_signin('parent@example.com','synthetic-link-token')
    message=captured['message']
    assert message['Subject']=='Your Daily Telly sign-in link'
    assert message.get_content_type()=='multipart/alternative'
    assert '#f6f5ee' in message.get_body(preferencelist=('html',)).get_content()
    assert '<img' not in message.get_body(preferencelist=('html',)).get_content()
    assert BASE+'/verify#synthetic-link-token' in message.get_body(preferencelist=('plain',)).get_content()
    assert 'synthetic-test-secret' not in message.as_string()
    assert captured.get('starttls',False)==(port==587)
    monkeypatch.setenv('DAILYTELLY_CONTACT_EMAIL','hello@thedailytelly.com')
    mailer.send_contact('reply@example.com','technical','Please help with a TV pairing problem.')
    contact=captured['message']
    assert contact['To']=='hello@thedailytelly.com' and contact['Reply-To']=='reply@example.com'
    assert contact['From']=='The Daily Telly <hello@thedailytelly.com>'
    assert contact['Subject']=='Daily Telly contact: Something is not working'
    assert contact.get_content_type()=='multipart/alternative'
    assert 'Please help with a TV pairing problem.' in contact.get_body(preferencelist=('plain',)).get_content()
    assert 'has not been verified' in contact.get_body(preferencelist=('plain',)).get_content()


def test_contact_is_public_delivers_once_and_does_not_store_message(client,monkeypatch):
    sent=[];monkeypatch.setattr(mailer,'send_contact',lambda *args:sent.append(args))
    assert client.get('/contact').status_code==200
    body={'email':'Reply@Example.com','topic':'privacy','message':'Please explain account deletion.','to':'unrelated@example.com'}
    response=client.post('/contact',json=body)
    assert response.status_code==200
    assert sent==[('reply@example.com','privacy','Please explain account deletion.')]
    with store.registry() as conn:
        assert conn.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]==0
        rows=json.dumps([dict(r) for r in conn.execute('SELECT * FROM rate_limits')])
        assert 'reply@example.com' not in rows and body['message'] not in rows


@pytest.mark.parametrize('changes,status',[
    ({'email':'bad\r\nBcc: other@example.com'},400),
    ({'message':' '*12},400),
    ({'message':'x'*5001},422),
    ({'topic':'question\r\nBcc:other@example.com'},422),
    ({'message':'A null byte\x00 is not allowed'},400),
])
def test_contact_rejects_invalid_or_injected_input(client,monkeypatch,changes,status):
    sent=[];monkeypatch.setattr(mailer,'send_contact',lambda *args:sent.append(args))
    body={'email':'reply@example.com','message':'A real support question.',**changes}
    assert client.post('/contact',json=body).status_code==status
    assert not sent


def test_contact_honeypot_and_origin_block_delivery(client,monkeypatch):
    sent=[];monkeypatch.setattr(mailer,'send_contact',lambda *args:sent.append(args))
    body={'email':'reply@example.com','message':'A real support question.'}
    assert client.post('/contact',json=body,headers={'Origin':'https://unrelated.example'}).status_code==403
    assert client.post('/contact',json={**body,'website':'bot.invalid'}).status_code==200
    assert not sent


def test_contact_rate_limit_and_global_budget(client,monkeypatch):
    sent=[];monkeypatch.setattr(mailer,'send_contact',lambda *args:sent.append(args))
    body={'email':'reply@example.com','message':'A real support question.'}
    for _ in range(3):assert client.post('/contact',json=body).status_code==200
    response=client.post('/contact',json=body)
    assert response.status_code==429 and response.headers.get('retry-after')
    assert len(sent)==3
    with store.registry() as conn:conn.execute('DELETE FROM rate_limits')
    monkeypatch.setenv('DAILYTELLY_CONTACT_DAILY_BUDGET','1')
    store.limit('contact-day','service',1,86400)
    assert client.post('/contact',json=body).status_code==429
    assert len(sent)==3


def test_contact_failure_is_generic(client,monkeypatch):
    def fail(*args):raise RuntimeError('secret-smtp-password-and-user-message')
    monkeypatch.setattr(mailer,'send_contact',fail)
    response=client.post('/contact',json={'email':'reply@example.com','message':'A real support question.'})
    assert response.status_code==503
    assert 'secret-smtp' not in response.text and 'hello@thedailytelly.com' in response.text


def test_email_delivery_failure_is_generic(client,monkeypatch):
    def failure(*args): raise RuntimeError('smtp credential should not be disclosed')
    monkeypatch.setattr(mailer,'send_signin',failure)
    response=client.post('/auth/email',json={'email':'parent@example.com','accepted':True})
    assert response.status_code==503 and 'credential' not in response.text
    with store.registry() as conn:
        assert conn.execute('SELECT COUNT(*) FROM email_links').fetchone()[0]==0


def test_concurrent_link_redemption_has_one_winner(client):
    from concurrent.futures import ThreadPoolExecutor
    client.post('/auth/email',json={'email':'parent@example.com','accepted':True})
    token=token_for('parent@example.com')
    intent=client.cookies.get(store.INTENT_COOKIE)
    def redeem(_):
        contender=TestClient(app,base_url=BASE)
        contender.cookies.set(store.INTENT_COOKIE,intent,domain='localhost.local',path='/auth')
        return contender.post('/auth/verify',json={'token':token}).status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(redeem,range(2)))
    assert sorted(results)==[200,400]


def test_open_file_deletion_is_retried_without_restoring_access(client,monkeypatch):
    account=signin(client);seed(account)
    path=store.library_path(account['id'])
    original=Path.unlink
    def held_file(current,*args,**kwargs):
        if current==path: raise PermissionError('simulated open file')
        return original(current,*args,**kwargs)
    with monkeypatch.context() as context:
        context.setattr(Path,'unlink',held_file)
        assert client.post('/account/delete',json={'confirmation':'DELETE'}).status_code==200
        assert path.exists()
        assert client.get('/api/today').status_code==401
    store.cleanup()
    assert not path.exists()
    with store.registry() as conn:
        assert conn.execute('SELECT COUNT(*) FROM deletion_records').fetchone()[0]==1


def test_parallel_household_requests_do_not_share_context(client):
    from concurrent.futures import ThreadPoolExecutor
    first=signin(client,'first@example.com');seed(first,'First household')
    second=TestClient(app,base_url=BASE);seed(signin(second,'second@example.com'),'Second household')
    clients=[client,second]*5
    with ThreadPoolExecutor(max_workers=4) as executor:
        titles=list(executor.map(lambda browser:browser.get('/api/today').json()['videos'][0]['title'],clients))
    assert titles==['First household','Second household']*5


def test_catalog_quota_is_atomic_and_shared(client,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setenv('DAILYTELLY_YOUTUBE_DAILY_BUDGET','1')
    def reserve(_):
        try: store.reserve_catalog_units(1); return 'allowed'
        except ValueError: return 'denied'
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(reserve,range(2)))==['allowed','denied']


def test_development_never_sends_pushover(monkeypatch):
    monkeypatch.setenv('DAILYTELLY_ENV','development')
    monkeypatch.setenv('DAILYTELLY_PUSHOVER_USER','test-only')
    monkeypatch.setenv('DAILYTELLY_PUSHOVER_TOKEN','test-only')
    def no_network(*args,**kwargs): raise AssertionError('Development must not send operator alerts')
    monkeypatch.setattr(resources.httpx,'post',no_network)
    assert resources.send_alert('test') is False


def test_concurrent_household_progress_keeps_furthest_position(client):
    from concurrent.futures import ThreadPoolExecutor
    account=signin(client);seed(account)
    def progress(position):
        return client.post('/api/progress',json={'video_id':'same-id','position_seconds':position,'event':'heartbeat'}).status_code
    with ThreadPoolExecutor(max_workers=3) as executor:
        assert list(executor.map(progress,[400,100,200]))==[200,200,200]
    assert client.get('/api/today').json()['videos'][0]['watched_pct']==67
