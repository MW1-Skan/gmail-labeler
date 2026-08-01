#!/usr/bin/env python3
"""Personal Gmail auto-labeler.

Classifies recent inbox messages using the Claude Agent SDK (subscription auth)
and applies the matching Gmail label. Dry-run by default; --apply to modify.
Shared logic (categories, Gmail helpers, output parsing) lives in ../shared/.
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

PERSONAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PERSONAL_DIR.parent))  # repo root, for `shared`

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from shared.categories import DEFAULT_CATEGORIES, build_system_prompt, build_user_prompt, category_names
from shared.gmail_client import apply_labels, build_query, ensure_labels, fetch_email, list_message_ids
from shared.parsing import parse_categories

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
CREDENTIALS_FILE = PERSONAL_DIR / "credentials.json"
TOKEN_FILE = PERSONAL_DIR / "token.json"

CATEGORIES = category_names(DEFAULT_CATEGORIES)
SYSTEM_PROMPT = build_system_prompt(DEFAULT_CATEGORIES)
BATCH_SIZE = 10  # emails per LLM call

log = logging.getLogger("labeler")


def gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                sys.exit(f"ERROR: {CREDENTIALS_FILE} not found.")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def check_no_api_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "ERROR: ANTHROPIC_API_KEY is set in the environment. This would silently "
            "override Claude subscription auth and bill the API key instead.\n"
            "Unset it (e.g. `unset ANTHROPIC_API_KEY`) and re-run."
        )


async def _classify_async(emails: list[dict]) -> str | None:
    options = ClaudeAgentOptions(system_prompt=SYSTEM_PROMPT, max_turns=1, tools=[])
    result_text = None
    async for message in query(prompt=build_user_prompt(emails), options=options):
        if isinstance(message, ResultMessage):
            result_text = message.result
    return result_text


def classify_batch(emails: list[dict]) -> list[str]:
    try:
        raw = asyncio.run(_classify_async(emails))
    except Exception as e:  # LLM call failed entirely
        log.warning("LLM call failed (%s); falling back to Other for %d emails", e, len(emails))
        return parse_categories(None, len(emails), CATEGORIES)
    return parse_categories(raw, len(emails), CATEGORIES)


def parse_args():
    p = argparse.ArgumentParser(description="Auto-label Gmail inbox with an LLM classifier.")
    p.add_argument("--dry-run", action="store_true", default=True,
                   help="Classify but touch nothing in Gmail (default).")
    p.add_argument("--apply", action="store_true",
                   help="Actually apply labels in Gmail.")
    p.add_argument("--days", type=int, default=3, help="Lookback window in days (default 3).")
    p.add_argument("--max", type=int, default=50, dest="max_msgs",
                   help="Max messages processed per run (default 50).")
    p.add_argument("--backfill", action="store_true",
                   help="Historical mode: process all inbox mail after --after, oldest first.")
    p.add_argument("--after", type=str, default=None,
                   help="Date YYYY/MM/DD for --backfill (mandatory in backfill mode).")
    args = p.parse_args()
    if args.backfill:
        if not args.after:
            p.error("--backfill requires --after YYYY/MM/DD")
        try:
            datetime.strptime(args.after, "%Y/%m/%d")
        except ValueError:
            p.error(f"--after must be YYYY/MM/DD, got {args.after!r}")
    return args


def main():
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO,
                        stream=sys.stdout)
    check_no_api_key()
    args = parse_args()
    apply_mode = args.apply

    service = gmail_service()
    label_ids = ensure_labels(service, CATEGORIES)
    category_label_ids = set(label_ids.values())

    query_str = build_query(CATEGORIES, days=args.days,
                            after=args.after if args.backfill else None)
    msg_ids = list_message_ids(service, query_str, args.max_msgs, oldest_first=args.backfill)
    log.info("%d candidate message(s) [%s]", len(msg_ids),
             "APPLY" if apply_mode else "DRY-RUN")

    emails = []
    for mid in msg_ids:
        e = fetch_email(service, mid, category_label_ids)
        if e:
            emails.append(e)
    if not emails:
        log.info("Nothing to do.")
        return

    to_apply: dict[str, list[str]] = {}  # category -> message ids
    for start in range(0, len(emails), BATCH_SIZE):
        batch = emails[start:start + BATCH_SIZE]
        categories = classify_batch(batch)
        for e, cat in zip(batch, categories):
            log.info("[%-10s] %s — %s", cat, e["sender"][:45], e["subject"][:60])
            to_apply.setdefault(cat, []).append(e["id"])

    if not apply_mode:
        log.info("Dry-run: no labels applied. Re-run with --apply to label %d email(s).",
                 len(emails))
        return

    apply_labels(service, to_apply, label_ids)


if __name__ == "__main__":
    main()
