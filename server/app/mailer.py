"""Requested sign-in and support messages. Never log links or message contents."""
from email.message import EmailMessage
from html import escape
import json
import os
import secrets
import smtplib
import ssl
import httpx

from . import config, platform_store as store

CONTACT_TOPICS = {'question':'A question','technical':'Something is not working',
                  'privacy':'Privacy or my account','safety':'A safety concern','feedback':'An idea or feedback'}


def branded_html(eyebrow, heading, content, button=None, footer=None):
    """Email-safe inline styles, local typography, and no remote images or pixels."""
    cta = ('<p style="margin:28px 0"><a href="'+escape(button[1],quote=True)+'" style="display:inline-block;background:#183d34;color:#fff;padding:15px 23px;border-radius:12px;text-decoration:none;font-weight:bold">'+escape(button[0])+' &rarr;</a></p>') if button else ''
    return ('<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width,initial-scale=1"></head>'
      '<body style="margin:0;background:#f6f5ee;color:#183d34;font-family:Arial,sans-serif">'
      '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f6f5ee"><tr><td style="padding:32px 16px">'
      '<table role="presentation" align="center" width="100%" cellpadding="0" cellspacing="0" style="max-width:580px"><tr><td>'
      '<p style="font-size:20px;font-weight:bold;margin:0 0 32px">&#9655; The Daily Telly</p>'
      '<p style="font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#63736c">'+escape(eyebrow)+'</p>'
      '<h1 style="font:normal 42px/1.12 Georgia,serif;letter-spacing:-1px;margin:18px 0 26px">'+escape(heading)+'</h1>'
      '<div style="background:#fff;border:1px solid #dde3d9;border-radius:18px;padding:25px;font-size:15px;line-height:1.75">'+content+cta+'</div>'
      '<p style="font-size:12px;line-height:1.8;color:#63736c;margin-top:26px">A few good channels. A little shape to the week.<br>Free, open source, and family-led. No ads or tracking added by us.</p>'
      '<p style="font-size:12px;line-height:1.8;color:#63736c">'+(footer or 'You’re receiving this because you requested it.')+'</p>'
      '</td></tr></table></td></tr></table></body></html>')


def send_signin(email, token):
    url = store.base_url() + '/verify#' + token
    if os.environ.get('DAILYTELLY_MAIL_MODE', 'file') == 'file':
        if store.production():
            raise RuntimeError('File mail is never allowed in production')
        directory = config.DATA_DIR / 'dev-mail'
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (secrets.token_hex(16)+'.json')
        with path.open('x', encoding='utf-8') as output:
            json.dump({'to': email, 'url': url}, output)
        path.chmod(0o600)
        return
    message = EmailMessage()
    message['Subject'] = 'Your Daily Telly sign-in link'
    message['From'] = os.environ['DAILYTELLY_MAIL_FROM']
    message['To'] = email
    # Always retain the plain-text alternative. HTML adds no remote resources.
    message.set_content('Confirm your email and sign in to The Daily Telly:\n\n'+url+
                        '\n\nThis link expires in 15 minutes and works only in the browser that requested it.'
                        '\nIf you did not request this email, ignore it. Never share this link.')
    message.add_alternative(branded_html('Your weekly rhythm starts here','A little telly. Your way.',
        '<p>Confirm your email to make room for a calmer week of viewing.</p><p>This sign-in link works once, lasts 15 minutes, and opens only in the browser that requested it.</p>',
        ('Confirm & sign in',url), 'If you didn’t ask for this email, you can safely ignore it. Please don’t share the link.'),subtype='html')
    send_smtp(message)


def send_contact(email, topic, content):
    if os.environ.get('DAILYTELLY_MAIL_MODE') != 'smtp':
        raise RuntimeError('Contact email delivery is not configured')
    message = EmailMessage()
    message['Subject'] = 'Daily Telly contact: '+CONTACT_TOPICS[topic]
    message['From'] = os.environ['DAILYTELLY_MAIL_FROM']
    message['To'] = os.environ.get('DAILYTELLY_CONTACT_EMAIL', 'hello@thedailytelly.com')
    message['Reply-To'] = email
    message.set_content('A visitor submitted the Daily Telly contact form.\n'
                        'The reply address is supplied by the visitor and has not been verified.\n\n'
                        'Topic: '+CONTACT_TOPICS[topic]+'\nReply address: '+email+'\n\n'+content)
    message.add_alternative(branded_html('A note from the community','Let’s talk telly.',
        '<p><b>'+escape(CONTACT_TOPICS[topic])+'</b><br>Reply address: '+escape(email)+' (supplied by the visitor; unverified)</p><div style="white-space:pre-wrap">'+escape(content)+'</div>'),subtype='html')
    send_smtp(message)


def send_reminder(payload,account_id):
    if os.environ.get('DAILYTELLY_MAIL_MODE','file')=='file':
        if store.production():
            raise RuntimeError('Development mail is disabled in production')
        folder=config.DATA_DIR/'dev-mail'
        folder.mkdir(parents=True,exist_ok=True)
        (folder/('welcome-'+account_id+'.json')).write_text(json.dumps(payload),encoding='utf-8')
        return
    key=os.environ.get('DAILYTELLY_RESEND_SENDING_KEY')
    if not key and os.environ.get('DAILYTELLY_SMTP_HOST')=='smtp.resend.com':
        key=os.environ.get('DAILYTELLY_SMTP_PASSWORD')
    if not key:
        raise RuntimeError('Welcome reminders require Resend idempotent delivery')
    with httpx.Client(timeout=20,follow_redirects=False) as client:
        response=client.post('https://api.resend.com/emails',json=payload,
            headers={'Authorization':'Bearer '+key,'Idempotency-Key':'telly-welcome-'+account_id})
        response.raise_for_status()


def send_smtp(message):
    host = os.environ['DAILYTELLY_SMTP_HOST']
    port = int(os.environ.get('DAILYTELLY_SMTP_PORT', '587'))
    context = ssl.create_default_context()
    if port == 465:
        connection = smtplib.SMTP_SSL(host, port, timeout=15, context=context)
    else:
        connection = smtplib.SMTP(host, port, timeout=15)
    with connection as smtp:
        if port != 465:
            smtp.starttls(context=context)
        if os.environ.get('DAILYTELLY_SMTP_USER'):
            smtp.login(os.environ['DAILYTELLY_SMTP_USER'], os.environ['DAILYTELLY_SMTP_PASSWORD'])
        smtp.send_message(message)
