"""Web product configuration — everything comes from web/.env (see .env.example)."""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

WEB_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(WEB_DIR.parent))  # repo root, for `shared`
load_dotenv(WEB_DIR / ".env")

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
DB_PATH = Path(os.environ.get("DB_PATH", WEB_DIR / "labeler.db"))

# oauthlib refuses plain-http OAuth (fine in prod, fatal for localhost testing) and
# raises "Scope has changed" when Google normalizes/reorders the granted scopes.
# Local http is allowed only for loopback addresses; scope relaxation is always on
# (the callback verifies the Gmail scope itself and re-prompts if it was unchecked).
if BASE_URL.startswith(("http://localhost", "http://127.")):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

# Anthropic — API billing (NOT the personal subscription)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("MODEL", "claude-haiku-4-5")

# Google OAuth (Web application client)
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = f"{BASE_URL}/oauth/callback"
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_SCOPES = [GMAIL_MODIFY_SCOPE,
                "https://www.googleapis.com/auth/userinfo.email", "openid"]

# Stripe
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
PRICE_EUR_CENTS = int(os.environ.get("PRICE_EUR_CENTS", "500"))

# Secrets
FERNET_KEY = os.environ.get("FERNET_KEY", "")          # cryptography.fernet.Fernet.generate_key()
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")  # any long random string

# Job limits
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "365"))
MAX_EMAILS_PER_JOB = int(os.environ.get("MAX_EMAILS_PER_JOB", "10000"))
MAX_CATEGORIES = 15


def require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"ERROR: {name} is not set — copy web/.env.example to web/.env and fill it in.")
    return value
