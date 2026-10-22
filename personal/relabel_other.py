#!/usr/bin/env python3
"""Re-classify mail filed under Other and move it to its real category.

Cleanup for mail that landed in Other because classification failed (e.g. the
Claude subscription was cut off) rather than because of what it is. Every email
labeled Other since --after goes through the labeler's classifier again: a real
category gets its label and loses Other; Other again leaves the email untouched.
Mail that also carries a real category label is skipped. Dry-run by default.

    .venv/bin/python personal/relabel_other.py --after 2026/10/01          # dry-run
    .venv/bin/python personal/relabel_other.py --after 2026/10/01 --apply
"""

import argparse
import logging
import os
import sys
from datetime import datetime

# labeler puts the repo root on sys.path, so it must be imported before `shared`.
from labeler import BATCH_SIZE, CATEGORIES, check_no_api_key, classify_batch, gmail_service
from shared.categories import FALLBACK_CATEGORY
from shared.gmail_client import ensure_labels, execute_with_backoff, fetch_email, list_message_ids

log = logging.getLogger("labeler")


def parse_args():
    p = argparse.ArgumentParser(description="Re-classify emails labeled Other since a date.")
    p.add_argument("--after", required=True,
                   help="Start date YYYY/MM/DD, inclusive from local midnight.")
    p.add_argument("--apply", action="store_true",
                   help="Actually change labels in Gmail (default: dry-run).")
    p.add_argument("--max", type=int, default=None, dest="max_msgs",
                   help="Process at most N emails, newest first (default: all).")
    args = p.parse_args()
    try:
        args.after_epoch = int(datetime.strptime(args.after, "%Y/%m/%d").timestamp())
    except ValueError:
        p.error(f"--after must be YYYY/MM/DD, got {args.after!r}")
    return args


def main():
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO,
                        stream=sys.stdout)
    check_no_api_key()
    args = parse_args()

    service = gmail_service()
    label_ids = ensure_labels(service, CATEGORIES)
    real_categories = [c for c in CATEGORIES if c != FALLBACK_CATEGORY]
    # Epoch seconds rather than YYYY/MM/DD, which Gmail reads as midnight Pacific time.
    query_str = (f"label:{FALLBACK_CATEGORY} after:{args.after_epoch} "
                 + " ".join(f"-label:{c}" for c in real_categories))
    msg_ids = list_message_ids(service, query_str, args.max_msgs, oldest_first=False)
    log.info("%d email(s) labeled %s since %s [%s]", len(msg_ids), FALLBACK_CATEGORY,
             args.after, "APPLY" if args.apply else "DRY-RUN")

    skip_ids = {label_ids[c] for c in real_categories}
    moved = kept = unclassified = 0
    for start in range(0, len(msg_ids), BATCH_SIZE):
        batch = [e for mid in msg_ids[start:start + BATCH_SIZE]
                 if (e := fetch_email(service, mid, skip_ids))]
        if not batch:
            continue
        to_move: dict[str, list[str]] = {}  # category -> message ids
        for e, cat in zip(batch, classify_batch(batch)):
            if cat is None:
                unclassified += 1
                continue
            log.info("[%-10s] %s — %s", cat, e["sender"][:45], e["subject"][:60])
            if cat == FALLBACK_CATEGORY:
                kept += 1
            else:
                to_move.setdefault(cat, []).append(e["id"])
        if args.apply:  # batch by batch, so an interrupted run keeps its progress
            for cat, ids in to_move.items():
                execute_with_backoff(service.users().messages().batchModify(
                    userId="me", body={"ids": ids, "addLabelIds": [label_ids[cat]],
                                       "removeLabelIds": [label_ids[FALLBACK_CATEGORY]]}))
        moved += sum(len(ids) for ids in to_move.values())

    log.info("%s %d email(s) out of %s, %d stay %s, %d unclassified%s",
             "Moved" if args.apply else "Dry-run: would move", moved, FALLBACK_CATEGORY,
             kept, FALLBACK_CATEGORY, unclassified,
             " (re-run later to retry them)" if unclassified else "")


if __name__ == "__main__":
    main()
    # Same Agent SDK shutdown workaround as labeler.py (exit code 120).
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
