#!/usr/bin/env python3
"""Job worker: runs one-shot backfills and the daily subscriber sweep.

Run alongside the FastAPI app:  .venv/bin/python web/worker.py
"""

import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

import requests as http
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from web import db
from web.classifier import classify_batch_api, classify_sync, group_emails
from web.config import BACKFILL_DAYS, GMAIL_SCOPES, MAX_EMAILS_PER_JOB, require
from shared.categories import category_names
from shared.gmail_client import apply_labels, build_query, ensure_labels, fetch_email, list_message_ids

log = logging.getLogger("web")

SWEEP_DAYS = 2  # daily sweep lookback (overlap for safety; skip-labeled makes it cheap)


def gmail_service_for(user_id: int):
    token_json = db.load_token(user_id)
    if not token_json:
        raise RuntimeError(f"no Gmail token stored for user {user_id}")
    creds = Credentials.from_authorized_user_info(json.loads(token_json), GMAIL_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        db.save_token(user_id, creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False), creds


def revoke_token(user_id: int, creds):
    """Revoke Gmail access and delete the stored token (privacy promise)."""
    try:
        http.post("https://oauth2.googleapis.com/revoke",
                  params={"token": creds.token},
                  headers={"content-type": "application/x-www-form-urlencoded"},
                  timeout=10)
    except Exception as e:
        log.warning("Token revoke call failed for user %d: %s", user_id, e)
    db.delete_token(user_id)
    log.info("Revoked and deleted Gmail token for user %d", user_id)


def label_emails(user_id: int, days: int, cap: int, use_batch_api: bool,
                 job_id: int | None = None) -> int:
    """Fetch → classify → label unlabeled inbox mail for one user. Returns count."""
    service, creds = gmail_service_for(user_id)
    categories = db.get_categories(user_id)
    names = category_names(categories)
    label_ids = ensure_labels(service, names)
    skip_ids = set(label_ids.values())

    msg_ids = list_message_ids(service, build_query(names, days=days), cap,
                               oldest_first=False)
    if job_id:
        db.update_job(job_id, total=len(msg_ids))

    emails = []
    for mid in msg_ids:
        e = fetch_email(service, mid, skip_ids)
        if e:
            emails.append(e)
    if not emails:
        return 0

    groups = group_emails(emails)
    if use_batch_api:
        def on_progress(done_groups):
            if job_id:
                db.update_job(job_id, done_count=min(done_groups * 10, len(emails)))
        results = classify_batch_api(categories, groups, on_progress=on_progress)
    else:
        results = classify_sync(categories, groups)

    to_apply: dict[str, list[str]] = {}
    for group, cats in zip(groups, results):
        for e, cat in zip(group, cats):
            to_apply.setdefault(cat, []).append(e["id"])
    apply_labels(service, to_apply, label_ids)

    if job_id:
        db.update_job(job_id, done_count=len(emails))
    return len(emails)


def run_backfill_job(job):
    user_id = job["user_id"]
    try:
        n = label_emails(user_id, days=BACKFILL_DAYS, cap=MAX_EMAILS_PER_JOB,
                         use_batch_api=True, job_id=job["id"])
        db.update_job(job["id"], status="done")
        log.info("Job %d done: %d emails labeled for user %d", job["id"], n, user_id)
        user = db.get_user(user_id)
        if user and not user["subscribed"]:
            _, creds = gmail_service_for(user_id)
            revoke_token(user_id, creds)
    except Exception as e:
        log.exception("Job %d failed", job["id"])
        db.update_job(job["id"], status="error", error=str(e)[:500])


def run_daily_sweeps():
    for user_id in db.subscribers_due_for_sweep():
        try:
            n = label_emails(user_id, days=SWEEP_DAYS, cap=500, use_batch_api=False)
            log.info("Sweep for user %d: %d emails labeled", user_id, n)
        except Exception:
            log.exception("Sweep failed for user %d", user_id)
        finally:
            db.mark_swept(user_id)


def main():
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s",
                        level=logging.INFO, stream=sys.stdout)
    require("ANTHROPIC_API_KEY")
    require("FERNET_KEY")
    db.init_db()
    log.info("Worker started")
    while True:
        job = db.claim_next_job()
        if job:
            run_backfill_job(job)
            continue
        run_daily_sweeps()
        time.sleep(10)


if __name__ == "__main__":
    main()
