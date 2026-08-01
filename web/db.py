"""SQLite persistence for the web product.

Stores the minimum: users, per-user categories, job progress, and the encrypted
Gmail refresh token (deleted the moment a one-shot job finishes unless the user
subscribed). Email contents are never stored.
"""

import sqlite3
import time
from contextlib import contextmanager

from cryptography.fernet import Fernet

from web.config import DB_PATH, FERNET_KEY
from shared.categories import DEFAULT_CATEGORIES

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    google_sub TEXT UNIQUE,
    paid_at REAL,
    subscribed INTEGER NOT NULL DEFAULT 0,
    token_encrypted BLOB,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    position INTEGER NOT NULL,
    UNIQUE(user_id, name)
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL DEFAULT 'backfill',      -- backfill | sweep
    status TEXT NOT NULL DEFAULT 'pending',     -- pending | running | done | error
    total INTEGER NOT NULL DEFAULT 0,
    done_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sweeps (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    last_sweep_at REAL NOT NULL
);
"""


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)


# --- users & tokens -------------------------------------------------------

def _fernet() -> Fernet:
    return Fernet(FERNET_KEY.encode())


def upsert_user(email: str, google_sub: str) -> int:
    with conn() as c:
        row = c.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if row:
            c.execute("UPDATE users SET google_sub = ? WHERE id = ?", (google_sub, row["id"]))
            return row["id"]
        cur = c.execute(
            "INSERT INTO users (email, google_sub, created_at) VALUES (?, ?, ?)",
            (email, google_sub, time.time()),
        )
        return cur.lastrowid


def mark_paid(email: str):
    with conn() as c:
        row = c.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if row:
            c.execute("UPDATE users SET paid_at = ? WHERE id = ?", (time.time(), row["id"]))
        else:
            c.execute("INSERT INTO users (email, paid_at, created_at) VALUES (?, ?, ?)",
                      (email, time.time(), time.time()))


def get_user(user_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def save_token(user_id: int, token_json: str):
    with conn() as c:
        c.execute("UPDATE users SET token_encrypted = ? WHERE id = ?",
                  (_fernet().encrypt(token_json.encode()), user_id))


def load_token(user_id: int) -> str | None:
    with conn() as c:
        row = c.execute("SELECT token_encrypted FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row or not row["token_encrypted"]:
        return None
    return _fernet().decrypt(row["token_encrypted"]).decode()


def delete_token(user_id: int):
    with conn() as c:
        c.execute("UPDATE users SET token_encrypted = NULL WHERE id = ?", (user_id,))


# --- categories -----------------------------------------------------------

def seed_default_categories(user_id: int):
    with conn() as c:
        existing = c.execute("SELECT COUNT(*) n FROM categories WHERE user_id = ?",
                             (user_id,)).fetchone()["n"]
        if existing:
            return
        c.executemany(
            "INSERT INTO categories (user_id, name, description, position) VALUES (?, ?, ?, ?)",
            [(user_id, name, desc, i) for i, (name, desc) in enumerate(DEFAULT_CATEGORIES)],
        )


def get_categories(user_id: int) -> list[tuple[str, str]]:
    with conn() as c:
        rows = c.execute(
            "SELECT name, description FROM categories WHERE user_id = ? ORDER BY position",
            (user_id,)).fetchall()
    return [(r["name"], r["description"]) for r in rows]


def replace_categories(user_id: int, categories: list[tuple[str, str]]):
    with conn() as c:
        c.execute("DELETE FROM categories WHERE user_id = ?", (user_id,))
        c.executemany(
            "INSERT INTO categories (user_id, name, description, position) VALUES (?, ?, ?, ?)",
            [(user_id, name, desc, i) for i, (name, desc) in enumerate(categories)],
        )


# --- jobs -----------------------------------------------------------------

def create_job(user_id: int, kind: str = "backfill") -> int:
    now = time.time()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO jobs (user_id, kind, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (user_id, kind, now, now),
        )
        return cur.lastrowid


def claim_next_job():
    """Atomically move the oldest pending job to running and return it."""
    with conn() as c:
        row = c.execute(
            "SELECT * FROM jobs WHERE status = 'pending' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if not row:
            return None
        c.execute("UPDATE jobs SET status = 'running', updated_at = ? WHERE id = ?",
                  (time.time(), row["id"]))
        return row


def update_job(job_id: int, **fields):
    sets = ", ".join(f"{k} = ?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE jobs SET {sets}, updated_at = ? WHERE id = ?",
                  (*fields.values(), time.time(), job_id))


def get_job(job_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def latest_job_for_user(user_id: int):
    with conn() as c:
        return c.execute(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
            (user_id,)).fetchone()


# --- daily sweep bookkeeping ---------------------------------------------

def subscribers_due_for_sweep(older_than_seconds: float = 24 * 3600) -> list[int]:
    cutoff = time.time() - older_than_seconds
    with conn() as c:
        rows = c.execute(
            """SELECT u.id FROM users u
               LEFT JOIN sweeps s ON s.user_id = u.id
               WHERE u.subscribed = 1 AND u.token_encrypted IS NOT NULL
                 AND (s.last_sweep_at IS NULL OR s.last_sweep_at < ?)""",
            (cutoff,)).fetchall()
    return [r["id"] for r in rows]


def mark_swept(user_id: int):
    with conn() as c:
        c.execute(
            "INSERT INTO sweeps (user_id, last_sweep_at) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_sweep_at = excluded.last_sweep_at",
            (user_id, time.time()))
