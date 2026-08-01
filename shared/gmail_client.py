"""Gmail API helpers shared by the personal CLI and the web product.

All functions take an authenticated `service` (googleapiclient resource); how the
credentials were obtained (installed-app token.json vs web-flow token from a DB) is
the caller's concern.
"""

import base64
import html
import logging
import random
import re
import time

from googleapiclient.errors import HttpError

BODY_CHARS = 300

log = logging.getLogger("labeler")


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


def ensure_labels(service, names: list[str]) -> dict[str, str]:
    """Return {label name: label id}, creating missing labels."""
    existing = execute_with_backoff(service.users().labels().list(userId="me"))
    by_name = {l["name"]: l["id"] for l in existing.get("labels", [])}
    ids = {}
    for name in names:
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


def build_query(label_names: list[str], days: int | None = None,
                after: str | None = None) -> str:
    """Inbox query excluding already-labeled mail (cheap idempotency)."""
    exclusions = " ".join(f"-label:{name}" for name in label_names)
    if after:
        return f"in:inbox after:{after} {exclusions}"
    return f"in:inbox newer_than:{days}d {exclusions}"


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


def fetch_email(service, msg_id: str, skip_label_ids: set[str]) -> dict | None:
    """Fetch one message; return None if it already carries one of skip_label_ids."""
    msg = execute_with_backoff(
        service.users().messages().get(userId="me", id=msg_id, format="full")
    )
    if skip_label_ids & set(msg.get("labelIds", [])):
        return None
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    body = extract_body(msg["payload"]) or msg.get("snippet", "")
    return {
        "id": msg_id,
        "sender": headers.get("from", "(unknown)"),
        "subject": headers.get("subject", "(no subject)"),
        "excerpt": re.sub(r"\s+", " ", body)[:BODY_CHARS].strip(),
    }


def apply_labels(service, to_apply: dict[str, list[str]], label_ids: dict[str, str]):
    """Apply labels grouped by category: {category: [message ids]}."""
    for cat, ids in to_apply.items():
        execute_with_backoff(
            service.users().messages().batchModify(
                userId="me", body={"ids": ids, "addLabelIds": [label_ids[cat]]}
            )
        )
        log.info("Applied %s to %d email(s)", cat, len(ids))
