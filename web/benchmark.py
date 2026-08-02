#!/usr/bin/env python3
"""Cost + quality benchmark (ROADMAP Phase A step 6).

Runs the owner's own already-labeled mailbox through the web product's exact
classification path (same prompts, same model, same Batch API discount) and
compares the results against the labels the personal labeler already applied —
those are the ground truth. Reports agreement per category, the confusion
pairs, a sample of disagreements to eyeball, and the REAL token spend.

Strictly read-only:
  - Gmail: only messages.list / messages.get / labels.list — never modifies
    anything, never applies labels, never touches personal/token.json on disk.
  - Uses the ANTHROPIC_API_KEY from web/.env (in-process via web.config; the
    shell environment is left untouched, so the personal labeler is unaffected).

Run from the repo root on the machine that has personal/token.json:

    .venv/bin/python web/benchmark.py                     # Batch API (product path)
    .venv/bin/python web/benchmark.py --sync              # immediate, 2x batch price
    .venv/bin/python web/benchmark.py --model claude-sonnet-4-5   # quality ceiling check
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

TOKEN_FILE = REPO_ROOT / "personal" / "token.json"

# $/MTok at the synchronous API price; the Batch API is 50% of this.
PRICES_PER_MTOK = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-opus": (15.00, 75.00),
}
USD_TO_EUR = 0.93  # rough, for the summary line only


def price_for(model: str) -> tuple[float, float] | None:
    for prefix, prices in PRICES_PER_MTOK.items():
        if model.startswith(prefix):
            return prices
    return None


def gmail_service_readonly():
    """Personal token, loaded read-only (a refresh happens in memory only)."""
    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if not TOKEN_FILE.exists():
        sys.exit(f"ERROR: {TOKEN_FILE} not found — run this on the machine with the "
                 "personal labeler set up.")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())  # in-memory only; token.json is never rewritten
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def collect_sample(service, names: list[str], per_category: int, days: int) -> list[dict]:
    """Up to per_category messages per existing label; ground truth = that label.
    Messages carrying 2+ category labels are skipped (ambiguous truth)."""
    from shared.gmail_client import execute_with_backoff, fetch_email, list_message_ids

    labels_resp = execute_with_backoff(service.users().labels().list(userId="me"))
    id_to_name = {l["id"]: l["name"] for l in labels_resp.get("labels", [])}
    category_ids = {lid for lid, n in id_to_name.items() if n in names}

    sample, seen = [], set()
    for name in names:
        query = f"label:{name} newer_than:{days}d"
        try:
            mids = list_message_ids(service, query, cap=per_category, oldest_first=False)
        except Exception as e:
            print(f"  {name}: query failed ({e}); skipping")
            continue
        kept = 0
        for mid in mids:
            if mid in seen:
                continue
            seen.add(mid)
            meta = execute_with_backoff(
                service.users().messages().get(userId="me", id=mid, format="minimal"))
            cat_labels = [id_to_name[l] for l in meta.get("labelIds", [])
                          if l in category_ids]
            if len(cat_labels) != 1:
                continue  # ambiguous ground truth
            email = fetch_email(service, mid, skip_label_ids=set())
            if not email:
                continue
            email["truth"] = cat_labels[0]
            sample.append(email)
            kept += 1
        print(f"  {name}: {kept} message(s) sampled")
    return sample


def summarize(rows: list[dict], usage: list[tuple[int, int]], model: str,
              batch_mode: bool, days: int) -> str:
    """Render the quality + cost report. Pure function (unit-tested)."""
    lines = []
    n = len(rows)
    agree = sum(1 for r in rows if r["truth"] == r["pred"])
    lines.append("=" * 72)
    lines.append(f"QUALITY — {model} vs your existing labels "
                 f"({n} emails, last {days} days)")
    lines.append("=" * 72)
    lines.append(f"Overall agreement: {agree}/{n} = {100 * agree / n:.1f}%" if n
                 else "No emails sampled — is the mailbox labeled?")

    by_truth: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_truth[r["truth"]].append(r)
    lines.append("")
    lines.append(f"{'truth label':<14} {'n':>4} {'agree':>6} {'%':>7}")
    for truth, group in sorted(by_truth.items()):
        ok = sum(1 for r in group if r["pred"] == truth)
        lines.append(f"{truth:<14} {len(group):>4} {ok:>6} {100 * ok / len(group):>6.1f}%")

    confusions = Counter((r["truth"], r["pred"]) for r in rows if r["truth"] != r["pred"])
    if confusions:
        lines.append("")
        lines.append("Top confusions (your label -> model's pick):")
        for (truth, pred), count in confusions.most_common(10):
            lines.append(f"  {truth} -> {pred}: {count}")
        lines.append("")
        lines.append("Disagreements to eyeball (the model is sometimes right!):")
        shown = 0
        for r in rows:
            if r["truth"] != r["pred"] and shown < 40:
                shown += 1
                lines.append(f"  [{r['truth']} -> {r['pred']}] "
                             f"{r['sender'][:40]} — {r['subject'][:55]}")

    lines.append("")
    lines.append("=" * 72)
    lines.append("COST — actual token usage reported by the API")
    lines.append("=" * 72)
    tok_in = sum(u[0] for u in usage)
    tok_out = sum(u[1] for u in usage)
    lines.append(f"Input tokens:  {tok_in:,}")
    lines.append(f"Output tokens: {tok_out:,}")
    prices = price_for(model)
    if not usage:
        lines.append("No usage captured (all requests failed?)")
    elif prices is None:
        lines.append(f"Unknown pricing for {model} — check the console for spend.")
    else:
        p_in, p_out = prices
        mult = 0.5 if batch_mode else 1.0
        mode = "Batch API (50% off)" if batch_mode else "synchronous (full price)"
        cost = (tok_in * p_in + tok_out * p_out) / 1e6 * mult
        lines.append(f"This run, {mode}: ${cost:.4f} (~€{cost * USD_TO_EUR:.4f})")
        if n:
            per_in, per_out = tok_in / n, tok_out / n
            lines.append(f"Per email: {per_in:.0f} in / {per_out:.1f} out tokens "
                         f"(ROADMAP assumed 200 in / 5 out)")
            lines.append("")
            lines.append("Extrapolated one-shot job cost at Batch API pricing:")
            for size in (1000, 2500, 5000, 10000):
                c = size * (per_in * p_in + per_out * p_out) / 1e6 * 0.5
                lines.append(f"  {size:>6,} emails: ${c:.2f} (~€{c * USD_TO_EUR:.2f})")
        lines.append("")
        lines.append("Cross-check the billed amount: https://platform.claude.com -> Usage")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--per-category", type=int, default=40,
                    help="max messages sampled per existing label (default 40)")
    ap.add_argument("--days", type=int, default=365,
                    help="lookback window, like the product (default 365)")
    ap.add_argument("--sync", action="store_true",
                    help="synchronous API instead of Batch (instant, 2x the price)")
    ap.add_argument("--model", default=None,
                    help="override MODEL from web/.env (e.g. claude-sonnet-4-5)")
    ap.add_argument("--report", default=None,
                    help="also write the report to this file")
    args = ap.parse_args()

    if args.model:
        os.environ["MODEL"] = args.model  # must be set before web.config loads

    from web import config
    from web.classifier import classify_batch_api, classify_sync, group_emails
    from shared.categories import DEFAULT_CATEGORIES, category_names

    if not config.ANTHROPIC_API_KEY:
        sys.exit("ERROR: ANTHROPIC_API_KEY missing in web/.env")
    names = category_names(DEFAULT_CATEGORIES)

    print(f"Model: {config.MODEL} | mode: {'sync' if args.sync else 'Batch API'} | "
          f"window: {args.days}d | per-category cap: {args.per_category}")
    print("Sampling your labeled mail (read-only)...")
    service = gmail_service_readonly()
    sample = collect_sample(service, names, args.per_category, args.days)
    if not sample:
        sys.exit("Nothing sampled — no labeled mail in the window?")

    groups = group_emails(sample)
    heads_up = ""
    if (prices := price_for(config.MODEL)):
        est = (len(sample) * (200 * prices[0] + 5 * prices[1]) / 1e6
               * (0.5 if not args.sync else 1))
        heads_up = f" (rough estimate ~${est:.3f})"
    print(f"\nClassifying {len(sample)} emails in {len(groups)} groups{heads_up}...")
    usage: list[tuple[int, int]] = []
    if args.sync:
        results = classify_sync(DEFAULT_CATEGORIES, groups, usage_out=usage)
    else:
        print("Batch submitted — polls every 30s; Ctrl-C is safe but a re-run "
              "re-submits (and re-pays).")
        done_total = [0]

        def on_progress(done_groups):
            if done_groups != done_total[0]:
                done_total[0] = done_groups
                print(f"  ...{min(done_groups * 10, len(sample))}/{len(sample)} emails")
        results = classify_batch_api(DEFAULT_CATEGORIES, groups,
                                     on_progress=on_progress, usage_out=usage)

    rows = [dict(e, pred=pred)
            for group, cats in zip(groups, results)
            for e, pred in zip(group, cats)]
    report = summarize(rows, usage, config.MODEL, batch_mode=not args.sync,
                       days=args.days)
    print("\n" + report)
    if args.report:
        Path(args.report).write_text(report + "\n")
        print(f"\nReport written to {args.report}")


if __name__ == "__main__":
    main()
