"""Browser parent sessions and separately scoped, revocable TV credentials."""
from fastapi import HTTPException, Request
from . import platform_store as store


def require_admin(request: Request):
    if not getattr(request.state, "browser_account", None):
        raise HTTPException(401, "Sign in first")
    return store.require_account()


def require_device(request: Request):
    store.require_account()
    return getattr(request.state, "device_id", None) or "browser"


def request_info(request: Request):
    return "", ""  # no retained IPs/user agents in household logs
