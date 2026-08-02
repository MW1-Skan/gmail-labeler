"""Test environment: hermetic env vars + fixtures.

Env vars are set BEFORE anything imports web.config; python-dotenv never overrides
variables that are already set, so these tests run identically with or without a
real (gitignored) web/.env on the machine — no real key is ever read or needed.
"""

import os
import sys
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="labeler-e2e-"))
os.environ["DB_PATH"] = str(_TMP / "test.db")
os.environ["BASE_URL"] = "http://localhost:8000"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-placeholder-never-used"
os.environ["MODEL"] = "claude-haiku-4-5"
os.environ["GOOGLE_CLIENT_ID"] = "test-client.apps.googleusercontent.com"
os.environ["GOOGLE_CLIENT_SECRET"] = "GOCSPX-test-placeholder"
os.environ["STRIPE_SECRET_KEY"] = "sk_test_placeholder"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test_placeholder"
os.environ["PRICE_EUR_CENTS"] = "500"
os.environ["FERNET_KEY"] = Fernet.generate_key().decode()
os.environ["SESSION_SECRET"] = "session-secret-for-tests-only"
os.environ["BACKFILL_DAYS"] = "365"
os.environ["MAX_EMAILS_PER_JOB"] = "10000"

import pytest
from fastapi.testclient import TestClient

from web import config, db  # noqa: E402  (env above must come first)
from web.app import app


@pytest.fixture(autouse=True)
def fresh_db():
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    db.init_db()
    yield


@pytest.fixture
def client():
    with TestClient(app, follow_redirects=False) as c:
        yield c
