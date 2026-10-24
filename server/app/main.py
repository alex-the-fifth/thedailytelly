"""Single-node platform application; account context is fail-closed on every request."""
from contextlib import asynccontextmanager
import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, platform_store as store, resources, youtube
from .routes_admin import router as library_router
from .routes_app import router as child_router
from .routes_platform import router as platform_router
from .routes_contact import router as contact_router
from .routes_inbound import router as inbound_router
from .starter import router as starter_router
from .reminders import router as reminders_router, dispatch_reminders


def sync_accounts():
    if not config.YOUTUBE_API_KEY:
        return
    with store.registry() as conn:
        accounts = [dict(row) for row in conn.execute("SELECT * FROM accounts WHERE status='active' AND verified_at IS NOT NULL")]
    for account in accounts:
        context = store.account_context.set(account)
        try:
            conn = db.get_conn()
            try:
                settings = db.get_settings(conn)
                last = settings['last_sync_at']
                due = not last or (datetime.datetime.now(datetime.timezone.utc) -
                    datetime.datetime.strptime(last, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)).total_seconds() >= settings['sync_interval_minutes']*60
            finally:
                conn.close()
            if due:
                youtube.sync_all(full=False, check_availability=True)
        except Exception:
            # Do not log addresses, request details, or household video titles.
            resources.record(500)
        finally:
            store.account_context.reset(context)


@asynccontextmanager
async def lifespan(app):
    store.initialize()
    resources.sample()
    scheduler = BackgroundScheduler()
    scheduler.add_job(resources.sample, 'interval', seconds=60, max_instances=1)
    scheduler.add_job(sync_accounts, 'interval', minutes=15, max_instances=1)
    scheduler.add_job(store.prune_household_audits, 'interval', hours=1, max_instances=1)
    scheduler.add_job(dispatch_reminders, 'cron', hour=20, minute='*/5', timezone='America/New_York', max_instances=1, misfire_grace_time=300)
    scheduler.start()
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler.shutdown(wait=True)


app = FastAPI(title='Daily Telly Platform', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.middleware('http')
async def isolation_and_headers(request: Request, call_next):
    session_token = request.cookies.get(store.SESSION_COOKIE)
    browser = store.account_for_session(session_token)
    refresh_browser_session = bool(browser and store.refresh_session_if_needed(session_token))
    account = browser
    device_id = None
    authorization = request.headers.get('authorization', '')
    if request.url.path.startswith('/api/') and authorization:
        token = authorization.split(' ', 1)[1] if authorization.lower().startswith('bearer ') else ''
        account, device_id = store.account_for_device(token)
    request.state.browser_account = browser
    request.state.device_id = device_id
    context = store.account_context.set(account)
    try:
        if request.method not in {'GET','HEAD','OPTIONS'}:
            origin = request.headers.get('origin')
            expected = store.base_url()
            if origin and origin != expected:
                return JSONResponse({'detail': 'Cross-origin requests are not allowed'}, status_code=403)
        response = await call_next(request)
        if refresh_browser_session:
            response.set_cookie(store.SESSION_COOKIE, session_token,
                                max_age=store.SESSION_TTL_SECONDS, httponly=True,
                                secure=store.secure_cookie(), samesite='lax', path='/')
        resources.record(response.status_code)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline' https://www.youtube.com; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: https://i.ytimg.com https://yt3.ggpht.com https://yt3.googleusercontent.com; "
            "frame-src https://www.youtube-nocookie.com https://www.youtube.com; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        if store.production():
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response
    finally:
        store.account_context.reset(context)


app.include_router(platform_router)
app.include_router(library_router)
app.include_router(child_router)
app.include_router(contact_router)
app.include_router(inbound_router)
app.include_router(starter_router)
app.include_router(reminders_router)


@app.get('/healthz')
def healthz():
    return {'ok': True}


def page(name):
    return FileResponse(config.STATIC_DIR/name)


@app.get('/app')
@app.get('/owner')
def dashboard():
    return page('dashboard.html')


@app.get('/login')
def login_page():
    return page('login.html')


@app.get('/verify')
def verify_page():
    return page('verify.html')


@app.get('/owner-access')
def owner_access_page():
    return page('owner-access.html')


@app.get('/activate')
def activate_page():
    return page('activate.html')


@app.get('/contact')
def contact_page():
    return page('contact.html')


@app.get('/community')
def community_page():
    return page('community.html')


@app.get('/email-preferences')
def email_preferences_page():
    return page('email-preferences.html')


@app.get('/privacy')
@app.get('/conduct')
def policies():
    return page('policies.html')


app.mount('/', StaticFiles(directory=config.STATIC_DIR, html=True), name='static')
