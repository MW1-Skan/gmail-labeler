#!/usr/bin/env python3
"""Gmail auto-labeler.

Classifies recent inbox messages into a fixed set of categories using the
Claude Agent SDK (subscription auth) and applies the matching Gmail label.
Dry-run by default; pass --apply to modify Gmail.
"""

import argparse
import asyncio
import base64
import html
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
PROJECT_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = PROJECT_DIR / "credentials.json"
TOKEN_FILE = PROJECT_DIR / "token.json"

CATEGORIES = [
    "Newsletter",
    "Marketing",
    "Shopping",
    "Papers",
    "Finance",
    "Dev",
    "Security",
    "Immo",
    "Other",
]

BATCH_SIZE = 10  # emails per LLM call
BODY_CHARS = 300

SYSTEM_PROMPT = """You are an email classifier. You will receive a numbered list of emails \
(sender, subject, and a short excerpt of the body). Classify EACH email into exactly one \
of these categories:

- Newsletter: editorial newsletters and content digests the user deliberately subscribed \
to and reads — curated articles, analysis, dev/AI/tech digests (Substack, TLDR, Pragmatic \
Engineer, The Batch, etc.). Editorial digest content goes here, never to Dev. Promotional \
brand mail is NOT Newsletter — that is Marketing.
- Marketing: promotional and commercial bulk mail — sales, discounts, coupons, product \
announcements, welcome emails, re-engagement campaigns ("we miss you"), loyalty-program \
promos, brand blasts. If an email is ambiguous between Newsletter and Marketing (e.g. a \
brand mixing content with promotion, job-alert digests), choose Marketing.
- Shopping: transactional purchase mail ONLY — order confirmations, receipts, invoices \
for purchases, shipping/delivery updates, refunds and returns. Promotional mail from \
shops or e-commerce brands is Marketing, NOT Shopping.
- Papers: government, official paperwork, taxes administration, fines, CAF, secu, utilities.
- Finance: any mail from banks or financial institutions.
- Dev: transactional developer-tool mail only (GitHub, CI/CD, monitoring, cloud providers, \
package registries). NOT newsletters.
- Security: verification/2FA codes, one-time passwords, login alerts, password resets.
- Immo: real estate — apartment listings, agency contacts, visit confirmations, \
SeLoger/PAP/Leboncoin immo alerts.
- Other: everything else, including personal mail from friends, mail from myself, and \
anything ambiguous.

Each email belongs to one and only one category.

Respond with ONLY a JSON array of category names, one per email, in the same order as the \
input. No explanations, no markdown fences, nothing else. Example: ["Newsletter", "Other"]"""

log = logging.getLogger("labeler")


# ---------------------------------------------------------------------------
# Gmail auth / API helpers
# ---------------------------------------------------------------------------

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


def execute_with_backoff(request, max_tries=6):
    """Execute a Gmail API request, retrying 429/5xx with exponential backoff."""
    for attempt in range(max_tries):
        try:
            return request.execute()
        except HttpError as e:
            status = e.resp.status
            if status in (429, 500, 502, 503) and attempt < max_tries - 1:
                delay = min(2 ** attempt + random.random(), 60)
                log.warning("Gmail API %s, retrying in %.1fs", status, delay)
                time.sleep(delay)
            else:
                raise


def ensure_labels(service) -> dict[str, str]:
    """Return {category name: label id}, creating missing labels."""
    existing = execute_with_backoff(service.users().labels().list(userId="me"))
    by_name = {l["name"]: l["id"] for l in existing.get("labels", [])}
    ids = {}
    for name in CATEGORIES:
        if name in by_name:
            ids[name] = by_name[name]
        else:
            created = execute_with_backoff(
                service.users().labels().create(
                    userId="me",
                    body={
                        "name": name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
            )
            log.info("Created label %s", name)
            ids[name] = created["id"]
    return ids


def list_message_ids(service, query_str: str, cap: int | None, oldest_first: bool) -> list[str]:
    """List message ids matching a Gmail query (newest first from the API)."""
    ids: list[str] = []
    page_token = None
    while True:
        req = service.users().messages().list(
            userId="me", q=query_str, maxResults=500, pageToken=page_token
        )
        resp = execute_with_backoff(req)
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        # In newest-first mode we can stop as soon as we have enough;
        # oldest-first needs the full list to find the tail.
        if not page_token or (not oldest_first and cap and len(ids) >= cap):
            break
    if oldest_first:
        ids.reverse()
    if cap:
        ids = ids[:cap]
    return ids


def strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def _decode_part(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")


def extract_body(payload: dict) -> str:
    """Best-effort plain-text body: text/plain part, else stripped HTML."""
    stack = [payload]
    html_fallback = ""
    while stack:
        part = stack.pop(0)
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if mime == "text/plain" and data:
            return _decode_part(data)
        if mime == "text/html" and data and not html_fallback:
            html_fallback = strip_html(_decode_part(data))
        stack.extend(part.get("parts", []))
    return html_fallback


def fetch_email(service, msg_id: str, category_label_ids: set[str]) -> dict | None:
    """Fetch one message; return None if it already carries a category label."""
    msg = execute_with_backoff(
        service.users().messages().get(userId="me", id=msg_id, format="full")
    )
    if category_label_ids & set(msg.get("labelIds", [])):
        return None
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    body = extract_body(msg["payload"]) or msg.get("snippet", "")
    return {
        "id": msg_id,
        "sender": headers.get("from", "(unknown)"),
        "subject": headers.get("subject", "(no subject)"),
        "excerpt": re.sub(r"\s+", " ", body)[:BODY_CHARS].strip(),
    }


# ---------------------------------------------------------------------------
# Classification via Claude Agent SDK
# ---------------------------------------------------------------------------

def check_no_api_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(
            "ERROR: ANTHROPIC_API_KEY is set in the environment. This would silently "
            "override Claude subscription auth and bill the API key instead.\n"
            "Unset it (e.g. `unset ANTHROPIC_API_KEY`) and re-run."
        )


async def _classify_async(emails: list[dict]) -> str | None:
    prompt_lines = []
    for i, e in enumerate(emails, 1):
        prompt_lines.append(
            f"{i}. From: {e['sender']}\n   Subject: {e['subject']}\n   Excerpt: {e['excerpt']}"
        )
    options = ClaudeAgentOptions(system_prompt=SYSTEM_PROMPT, max_turns=1, tools=[])
    result_text = None
    async for message in query(prompt="\n".join(prompt_lines), options=options):
        if isinstance(message, ResultMessage):
            result_text = message.result
    return result_text


def parse_categories(raw: str | None, n: int) -> list[str]:
    """Parse and validate the LLM output; invalid entries fall back to Other."""
    if not raw:
        log.warning("Empty LLM response; falling back to Other for %d emails", n)
        return ["Other"] * n
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    try:
        parsed = json.loads(match.group(0) if match else raw)
        assert isinstance(parsed, list)
    except (json.JSONDecodeError, AssertionError):
        log.warning("Unparseable LLM response %r; falling back to Other", raw[:200])
        return ["Other"] * n
    out = []
    for i in range(n):
        cat = parsed[i] if i < len(parsed) else None
        if cat not in CATEGORIES:
            log.warning("Invalid category %r for email %d; using Other", cat, i + 1)
            cat = "Other"
        out.append(cat)
    return out


def classify_batch(emails: list[dict]) -> list[str]:
    try:
        raw = asyncio.run(_classify_async(emails))
    except Exception as e:  # LLM call failed entirely
        log.warning("LLM call failed (%s); falling back to Other for %d emails", e, len(emails))
        return ["Other"] * len(emails)
    return parse_categories(raw, len(emails))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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
    label_ids = ensure_labels(service)
    category_label_ids = set(label_ids.values())

    # Exclude already-labeled messages in the query itself (cheap idempotency);
    # fetch_email() double-checks label ids as a safety net.
    exclusions = " ".join(f"-label:{name}" for name in CATEGORIES)
    if args.backfill:
        query_str = f"in:inbox after:{args.after} {exclusions}"
    else:
        query_str = f"in:inbox newer_than:{args.days}d {exclusions}"

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
            subject = e["subject"][:60]
            log.info("[%-10s] %s — %s", cat, e["sender"][:45], subject)
            to_apply.setdefault(cat, []).append(e["id"])

    if not apply_mode:
        log.info("Dry-run: no labels applied. Re-run with --apply to label %d email(s).",
                 len(emails))
        return

    for cat, ids in to_apply.items():
        execute_with_backoff(
            service.users().messages().batchModify(
                userId="me", body={"ids": ids, "addLabelIds": [label_ids[cat]]}
            )
        )
        log.info("Applied %s to %d email(s)", cat, len(ids))


if __name__ == "__main__":
    main()
