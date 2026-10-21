"""Provider-signature, recipient isolation, retry and attachment forwarding checks."""
import base64
import hashlib
import hmac
import json
import time

import httpx
import pytest

from test_platform import client
from app import platform_store as store, routes_inbound as inbound

EMAIL_ID = '4ef9a417-02e9-4d39-ad75-9611e0fcc33c'
SECRET = 'whsec_' + base64.b64encode(b'test-only-signing-secret-32bytes!!').decode()


def signed(body, timestamp=None):
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    content = ('msg_test.' + timestamp + '.').encode() + body
    signature = base64.b64encode(hmac.digest(base64.b64decode(SECRET[6:]),content,'sha256')).decode()
    return {'svix-id':'msg_test','svix-timestamp':timestamp,'svix-signature':'v1,'+signature,
            'Content-Type':'application/json'}


def configure(monkeypatch):
    monkeypatch.setenv('DAILYTELLY_RESEND_WEBHOOK_SECRET',SECRET)
    monkeypatch.setenv('DAILYTELLY_RESEND_RECEIVING_KEY','test-only-key')
    monkeypatch.setenv('DAILYTELLY_INBOUND_FORWARD_TO','operator@example.com')
    monkeypatch.setenv('DAILYTELLY_INBOUND_ADDRESS','hello@thedailytelly.com')
    monkeypatch.setenv('DAILYTELLY_MAIL_FROM','The Daily Telly <hello@thedailytelly.com>')


def event():
    return json.dumps({'type':'email.received','data':{'email_id':EMAIL_ID}}).encode()


def test_official_svix_signature_vector(monkeypatch):
    # Independent published vector: https://docs.svix.com/receiving/verifying-payloads/how-manual
    monkeypatch.setattr(inbound.time,'time',lambda:1731705121)
    assert inbound.verify_signature(b'{"event_type":"ping","data":{"success":true}}',
        {'svix-id':'msg_loFOjxBNrRLzqYUf','svix-timestamp':'1731705121',
         'svix-signature':'v1,rAvfW3dJ/X/qxhsaXPOyyCGmRKsaKWcsNccKXlIktD0='},
        'whsec_plJ3nmyCDGBKInavdOK15jsl')


def test_webhook_rejects_unsigned_tampered_expired_and_large(client,monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(inbound,'forward_email',lambda _:pytest.fail('Invalid event forwarded'))
    assert client.post('/webhooks/resend/inbound',content=event()).status_code==400
    assert client.post('/webhooks/resend/inbound',content=event()+b' ',headers=signed(event())).status_code==400
    for timestamp in (int(time.time())-301,int(time.time())+301):
        assert client.post('/webhooks/resend/inbound',content=event(),headers=signed(event(),timestamp)).status_code==400
    raw=b'x'*(inbound.MAX_BODY+1)
    assert client.post('/webhooks/resend/inbound',content=raw,headers=signed(raw)).status_code==413
    assert client.post('/webhooks/resend/inbound',content=b'[]',headers=signed(b'[]')).status_code==400


def test_webhook_disabled_until_all_credentials_present(client,monkeypatch):
    configure(monkeypatch)
    monkeypatch.delenv('DAILYTELLY_RESEND_RECEIVING_KEY')
    assert client.post('/webhooks/resend/inbound',content=event(),headers=signed(event())).status_code==503


def install_provider(monkeypatch, recipient='hello@thedailytelly.com', fail=False, loop=False):
    calls=[]
    def provider(request):
        assert request.headers['Authorization']=='Bearer test-only-key'
        calls.append(request)
        if request.method=='POST':
            return httpx.Response(500 if fail else 200,json={'id':'forwarded'})
        if request.url.path.endswith('/attachments'):
            return httpx.Response(200,json={'has_more':False,'data':[{
                'filename':'drawing.png','content_type':'image/png','content_id':'drawing',
                'download_url':'https://inbound-cdn.resend.com/drawing'}]})
        return httpx.Response(200,json={'to':[recipient],'from':'Parent <parent@example.com>',
            'reply_to':['reply@example.com'],'subject':'Question','text':'Private support message',
            'html':'<p>Private support message<img src="cid:drawing"></p>',
            'headers':{'X-DailyTelly-Forwarded':'1'} if loop else {}})
    original=httpx.Client
    monkeypatch.setattr(inbound.httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(provider),**kwargs))
    monkeypatch.setattr(inbound,'attachment_content',lambda url,remaining:b'png-data')
    return calls


def test_fixed_recipient_reply_to_attachments_and_deduplication(client,monkeypatch):
    configure(monkeypatch)
    calls=install_provider(monkeypatch)
    for _ in range(2):
        assert client.post('/webhooks/resend/inbound',content=event(),headers=signed(event())).status_code==200
    posts=[r for r in calls if r.method=='POST']
    assert len(posts)==1
    payload=json.loads(posts[0].content)
    assert payload['to']==['operator@example.com']
    assert payload['from']=='The Daily Telly <hello@thedailytelly.com>'
    assert payload['reply_to']=='reply@example.com'
    assert payload['attachments'][0]['content']==base64.b64encode(b'png-data').decode()
    assert payload['attachments'][0]['content_id']=='drawing'
    assert payload['headers']['Auto-Submitted']=='auto-forwarded'
    assert posts[0].headers['Idempotency-Key']=='telly-inbound-'+EMAIL_ID
    with store.registry() as conn:
        records=[dict(row) for row in conn.execute('SELECT * FROM inbound_forwards')]
    assert len(records)==1 and records[0]['hash']==hashlib.sha256(EMAIL_ID.encode()).hexdigest()
    assert 'Private support message' not in json.dumps(records)


@pytest.mark.parametrize('recipient,loop',[('other@thedailytelly.com',False),('hello@thedailytelly.com',True)])
def test_other_addresses_and_forwarding_loops_do_not_send(client,monkeypatch,recipient,loop):
    configure(monkeypatch)
    calls=install_provider(monkeypatch,recipient=recipient,loop=loop)
    assert client.post('/webhooks/resend/inbound',content=event(),headers=signed(event())).status_code==200
    assert not [r for r in calls if r.method=='POST']


def test_delivery_failure_is_retryable_without_marking_success(client,monkeypatch):
    configure(monkeypatch)
    calls=install_provider(monkeypatch,fail=True)
    response=client.post('/webhooks/resend/inbound',content=event(),headers=signed(event()))
    assert response.status_code==503 and 'test-only-key' not in response.text
    with store.registry() as conn:assert conn.execute('SELECT COUNT(*) FROM inbound_forwards').fetchone()[0]==0
    assert len([r for r in calls if r.method=='POST'])==1


def test_attachment_download_does_not_send_api_credential(monkeypatch):
    from contextlib import contextmanager
    @contextmanager
    def download(method,url,**kwargs):
        assert 'headers' not in kwargs and kwargs['follow_redirects'] is False
        yield httpx.Response(200,content=b'1234',request=httpx.Request(method,url))
    monkeypatch.setattr(inbound.httpx,'stream',download)
    assert inbound.attachment_content('https://inbound-cdn.resend.com/file',4)==b'1234'
    with pytest.raises(RuntimeError):inbound.attachment_content('https://inbound-cdn.resend.com/file',3)
    for url in ['http://inbound-cdn.resend.com/file','https://evil.example/file','https://resend.com.evil.example/file','https://localhost/file']:
        with pytest.raises(RuntimeError):inbound.attachment_content(url,4)
