import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DAILYTELLY_DATA", str(BASE_DIR / "data")))
DB_PATH = DATA_DIR / "dailytelly.db"
STATIC_DIR = BASE_DIR / "static"
PLAYER_FILE = BASE_DIR / "templates" / "player.html"
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
SESSION_TTL_SECONDS = 12 * 3600
COOKIE_NAME = "dailytelly_session"
COOKIE_SECURE = os.environ.get("DAILYTELLY_COOKIE_SECURE", "0") == "1"


def secret_key() -> str:
    """Persist a signing key without logging it or changing an existing key."""
    configured = os.environ.get("DAILYTELLY_SECRET")
    if configured:
        if len(configured) < 32:
            raise RuntimeError("DAILYTELLY_SECRET must contain at least 32 characters")
        return configured
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    key_file = DATA_DIR / "secret_key"
    try:
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return key_file.read_text().strip()
    with os.fdopen(fd, "w") as stream:
        key = os.urandom(32).hex()
        stream.write(key)
    return key
