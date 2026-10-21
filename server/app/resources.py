"""Aggregate operational health; no request URLs, client IDs, or viewer telemetry."""
import os
import threading
import time

import httpx
import psutil

from . import config, platform_store as store

_lock = threading.Lock()
_requests = 0
_errors = 0
_started = time.monotonic()
_process = psutil.Process()


def record(status):
    global _requests, _errors
    with _lock:
        _requests += 1
        _errors += int(status >= 500)


def snapshot():
    with _lock:
        requests, errors = _requests, _errors
    host_cpu, host_memory = psutil.cpu_percent(), psutil.virtual_memory().percent
    process_mb = _process.memory_info().rss / 1024**2
    cpu_limit = float(os.environ.get('DAILYTELLY_CPU_LIMIT', '0'))
    memory_limit = float(os.environ.get('DAILYTELLY_MEMORY_LIMIT_MB', '0'))
    cpu = min(100, _process.cpu_percent()/cpu_limit) if cpu_limit > 0 else host_cpu
    memory = min(100, process_mb/memory_limit*100) if memory_limit > 0 else host_memory
    return {'cpu': cpu, 'memory': memory,
            'disk': psutil.disk_usage(str(config.DATA_DIR)).percent,
            'host_cpu': host_cpu, 'host_memory': host_memory,
            'scope': 'process vs configured limits' if cpu_limit > 0 or memory_limit > 0 else 'host',
            'process_mb': round(process_mb, 1),
            'requests': requests, 'errors': errors, 'uptime_seconds': int(time.monotonic()-_started),
            'pushover_configured': bool(os.environ.get('DAILYTELLY_PUSHOVER_USER') and os.environ.get('DAILYTELLY_PUSHOVER_TOKEN'))}


def send_alert(message):
    if not store.production():
        return False
    user, token = os.environ.get('DAILYTELLY_PUSHOVER_USER'), os.environ.get('DAILYTELLY_PUSHOVER_TOKEN')
    if not user or not token:
        return False
    try:
        response = httpx.post('https://api.pushover.net/1/messages.json', timeout=10,
                              data={'user': user, 'token': token, 'title': 'Daily Telly server load', 'message': message})
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def sample():
    current = snapshot()
    limits = {name: float(os.environ.get('DAILYTELLY_ALERT_'+name.upper(), default))
              for name, default in [('cpu','85'),('memory','85'),('disk','90')]}
    high = [name for name, threshold in limits.items() if current[name] >= threshold]
    should_alert = False
    with store.registry() as conn:
        conn.execute('INSERT OR REPLACE INTO resource_samples VALUES (?,?,?,?,?,?)',
                     (store.now(), current['cpu'], current['memory'], current['disk'], current['requests'], current['errors']))
        previous = conn.execute("SELECT * FROM alert_state WHERE key='load'").fetchone()
        consecutive = (previous['consecutive']+1 if previous else 1) if high else 0
        sent = previous['last_sent'] if previous else 0
        should_alert = consecutive >= 3 and store.now()-sent >= 1800
        conn.execute("INSERT OR REPLACE INTO alert_state VALUES ('load',?,?)", (sent, consecutive))
    if should_alert:
        message = ', '.join(f'{name}: {current[name]:.1f}%' for name in high) + ' for 3+ samples. Check the owner dashboard.'
        if send_alert(message):
            with store.registry() as conn:
                conn.execute("UPDATE alert_state SET last_sent=? WHERE key='load'", (store.now(),))
    store.cleanup()
