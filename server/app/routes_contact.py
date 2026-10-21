"""A fixed-destination contact form, without a message database or tracking."""
import os
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import mailer, platform_store as store

router = APIRouter()
SENT = {'message': 'Thank you — your message has been sent. We can reply to the email address you provided.'}


class ContactIn(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    topic: Literal['question','technical','privacy','safety','feedback'] = 'question'
    message: str = Field(min_length=10, max_length=5000)
    website: str = Field(default='', max_length=200)


@router.post('/contact', status_code=200)
def contact(body: ContactIn, request: Request):
    peer = request.client.host if request.client else 'unknown'
    store.limit('contact-peer', peer, 3, 3600)
    # A decoy field catches simple form bots without a third-party challenge.
    if body.website.strip():
        return SENT
    email = body.email.strip().lower()
    content = body.message.strip()
    if not email.isascii() or any(ord(c)<33 or ord(c)>126 for c in email) or not re.fullmatch(r'[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+', email):
        raise HTTPException(400, 'Enter a valid reply email address')
    if len(content)<10 or '\x00' in content:
        raise HTTPException(400, 'Please write a message of at least 10 characters')
    store.limit('contact-email', email, 3, 86400)
    store.limit('contact-hour', 'service', int(os.environ.get('DAILYTELLY_CONTACT_HOURLY_BUDGET','10')), 3600)
    store.limit('contact-day', 'service', int(os.environ.get('DAILYTELLY_CONTACT_DAILY_BUDGET','30')), 86400)
    try:
        mailer.send_contact(email, body.topic, content)
    except Exception:
        raise HTTPException(503, 'Your message could not be sent. Please try later or email hello@thedailytelly.com directly.')
    return SENT
