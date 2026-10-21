"""Signed Resend inbound events, forwarded to one private operator destination."""
import base64
from email.utils import getaddresses
import hashlib
import hmac
import html
import json
import os
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from . import platform_store as store

router = APIRouter()
forward_lock = threading.Lock()
MAX_BODY = 64 * 1024
MAX_ATTACHMENTS = 10 * 1024 * 1024


def verify_signature(raw, headers, secret):
    """Svix's documented HMAC format; verify the exact bytes before JSON parsing."""
    try:
        identifier = headers['svix-id']
        timestamp = headers['svix-timestamp']
        if abs(int(timestamp) - time.time()) > 300 or len(identifier) > 200:
            return False
        key = base64.b64decode(secret.removeprefix('whsec_'), validate=True)
        if len(key) < 16:
            return False
        signed = (identifier + '.' + timestamp + '.').encode() + raw
        expected = base64.b64encode(hmac.digest(key, signed, 'sha256')).decode()
        return any(version == 'v1' and hmac.compare_digest(signature, expected)
                   for version, separator, signature in
                   (item.partition(',') for item in headers['svix-signature'].split()) if separator)
    except (KeyError, ValueError, TypeError):
        return False


def addresses(values):
    if isinstance(values, str):
        values = [values]
    return [address.lower() for _, address in getaddresses(values or [])
            if '@' in address and not any(c in address for c in '\r\n')]


def api(client, method, path, **kwargs):
    for attempt in range(3):
        with client.stream(method, 'https://api.resend.com' + path, **kwargs) as response:
            if response.status_code != 429 or attempt == 2:
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 8*1024*1024:
                        raise RuntimeError('Provider response is too large')
                return json.loads(content)
        time.sleep(attempt + 1)


def attachment_content(url, remaining):
    # URLs are obtained from Resend's authenticated API, never from webhook input.
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.endswith('.resend.com') or parsed.username:
        raise RuntimeError('Unexpected attachment host')
    # Use a separate request so the API credential never reaches a CDN URL.
    with httpx.stream('GET', url, timeout=20, follow_redirects=False) as response:
        response.raise_for_status()
        content = bytearray()
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > remaining:
                raise RuntimeError('Attachment size limit exceeded')
    return bytes(content)


def forward_email(identifier):
    if not forward_lock.acquire(blocking=False):
        raise HTTPException(503, 'Please retry this event')
    try:
        fingerprint = hashlib.sha256(identifier.encode()).hexdigest()
        with store.registry() as conn:
            if conn.execute('SELECT 1 FROM inbound_forwards WHERE hash=?', (fingerprint,)).fetchone():
                return
        with httpx.Client(timeout=20, follow_redirects=False,
                          headers={'Authorization': 'Bearer ' + os.environ['DAILYTELLY_RESEND_RECEIVING_KEY']}) as client:
            email = api(client, 'GET', '/emails/receiving/' + identifier + '?html_format=cid')
            allowed = os.environ.get('DAILYTELLY_INBOUND_ADDRESS', 'hello@thedailytelly.com').lower()
            recipients = sum((addresses(email.get(field)) for field in ('to','cc','bcc','received_for')), [])
            if allowed not in recipients:
                return
            headers = {k.lower(): v for k, v in (email.get('headers') or {}).items()}
            if headers.get('x-dailytelly-forwarded') == '1':
                return
            destination = os.environ['DAILYTELLY_INBOUND_FORWARD_TO']
            if destination.lower() == allowed:
                raise RuntimeError('Forwarding would loop')
            payload = {
                'from': os.environ['DAILYTELLY_MAIL_FROM'], 'to': [destination],
                'subject': '[Daily Telly] ' + (email.get('subject') or '(no subject)'),
                'text': 'Forwarded for ' + allowed + '\nFrom: ' + (email.get('from') or '(unknown)') + '\n\n' +
                        (email.get('text') or 'This message contains HTML. Open the HTML version to read it.'),
                'headers': {'X-DailyTelly-Forwarded':'1', 'Auto-Submitted':'auto-forwarded'},
            }
            reply = addresses(email.get('reply_to')) or addresses(email.get('from'))
            if reply:
                payload['reply_to'] = reply[0]
            if email.get('html'):
                payload['html'] = '<p>Forwarded for ' + html.escape(allowed) + '<br>From: ' + html.escape(email.get('from') or '(unknown)') + '</p><hr>' + email['html']
            attachments = []
            remaining = MAX_ATTACHMENTS
            after = None
            while True:
                suffix = '?limit=100' + ('&after=' + str(UUID(after)) if after else '')
                page = api(client, 'GET', '/emails/receiving/' + identifier + '/attachments' + suffix)
                for item in page['data']:
                    content = attachment_content(item['download_url'], remaining)
                    remaining -= len(content)
                    attachment = {'filename': item.get('filename') or 'attachment',
                                  'content': base64.b64encode(content).decode(),
                                  'content_type': item['content_type']}
                    if item.get('content_id'):
                        attachment['content_id'] = item['content_id'].strip('<>')
                    attachments.append(attachment)
                    if len(attachments) > 100:
                        raise RuntimeError('Too many attachments')
                if not page.get('has_more'):
                    break
                if not page['data'] or after == page['data'][-1]['id']:
                    raise RuntimeError('Invalid attachment pagination')
                after = page['data'][-1]['id']
            if attachments:
                payload['attachments'] = attachments
            api(client, 'POST', '/emails', json=payload,
                headers={'Idempotency-Key': 'telly-inbound-' + identifier})
        with store.registry() as conn:
            conn.execute('INSERT OR IGNORE INTO inbound_forwards(hash,at) VALUES(?,?)', (fingerprint,store.now()))
            conn.execute('DELETE FROM inbound_forwards WHERE at<?', (store.now()-90*86400,))
    finally:
        forward_lock.release()


@router.post('/webhooks/resend/inbound')
async def inbound(request: Request):
    secret = os.environ.get('DAILYTELLY_RESEND_WEBHOOK_SECRET')
    if not secret or not os.environ.get('DAILYTELLY_RESEND_RECEIVING_KEY') or not os.environ.get('DAILYTELLY_INBOUND_FORWARD_TO'):
        raise HTTPException(503, 'Inbound forwarding is not configured')
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY:
            raise HTTPException(413, 'Event is too large')
    if not verify_signature(bytes(raw), request.headers, secret):
        raise HTTPException(400, 'Invalid webhook signature')
    try:
        event = json.loads(raw)
        if event['type'] != 'email.received':
            return {'ok':True}
        identifier = str(UUID(event['data']['email_id']))
    except (ValueError, TypeError, KeyError):
        raise HTTPException(400, 'Invalid event')
    try:
        await run_in_threadpool(forward_email, identifier)
    except Exception:
        # A non-2xx response lets Resend retry; never log mail bodies or API errors.
        raise HTTPException(503, 'Forwarding is temporarily unavailable; retry this event')
    return {'ok':True}
