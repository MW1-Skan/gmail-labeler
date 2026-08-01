# Phase A local test runbook (ROADMAP steps 4–6)

Everything here runs on the Mac mini from the repo root. The automated suite
(`web/tests/`) already exercises the whole pipeline with Stripe/Google/Anthropic
faked, so what's left to prove manually is exactly the three real integrations:
your Stripe account, your Google OAuth client, and your Anthropic key.

## 0. One-time sanity (2 min)

```bash
.venv/bin/pip install -r web/requirements.txt -r web/requirements-dev.txt
.venv/bin/python -m pytest web/tests/ -q        # needs no .env, no network: must be green
```

Google Cloud console checklist (the two classic time-wasters):
- Credentials → your **Web application** client → Authorized redirect URIs contains
  **exactly** `http://localhost:8000/oauth/callback` (http, port 8000, no trailing
  slash; `localhost`, not `127.0.0.1`).
- OAuth consent screen → Audience → **Test users** contains BOTH your main Gmail
  and the secondary "fake customer" account.

## 1. Start the stack (3 terminals)

```bash
# T1 — web app
.venv/bin/uvicorn web.app:app --reload

# T2 — worker
.venv/bin/python web/worker.py

# T3 — Stripe webhook forwarding
stripe listen --forward-to localhost:8000/stripe-webhook
```

T3 prints `Your webhook signing secret is whsec_...` — it MUST equal
`STRIPE_WEBHOOK_SECRET` in `web/.env`. If you update `.env`, **restart T1**
(env is read at startup). T2 should log `Worker started`.

## 2. The customer flow (secondary Google account)

| # | Do | Expect |
|---|----|--------|
| 1 | Open http://localhost:8000 in a private window | Landing page, €5 |
| 2 | Click pay → Stripe Checkout | Card `4242 4242 4242 4242`, any future expiry/CVC, any name; use the secondary address as email |
| 3 | Pay | T3 logs `checkout.session.completed` → `[200]`; browser lands on Google's consent screen |
| 4 | Google warns “app isn’t verified” | Normal in Testing mode: **Advanced → Go to … (unsafe)** |
| 5 | Consent screen | Sign in with the **secondary** account; tick **all** checkboxes (the Gmail one is required — unticking it now shows a friendly retry page, not a crash) |
| 6 | Categories editor | Rename/remove, **add a custom one** (e.g. `Kids` / “school and daycare mail”) → Start labeling |
| 7 | /status page | Progress bar; T2 logs `Submitted batch …`; Batch API typically finishes small jobs in minutes (poll every 30 s) |
| 8 | Done ✅ | Open the secondary account's Gmail: labels exist and are applied |
| 9 | https://myaccount.google.com/permissions (secondary account) | The app is **gone** (token revoked) — the one-shot privacy promise |

## 3. When something breaks

| Symptom | Cause | Fix |
|---|---|---|
| Google: `Error 400: redirect_uri_mismatch` | Registered URI ≠ `http://localhost:8000/oauth/callback` exactly | Fix in console (scheme/port/slash/localhost); takes effect in ~1 min |
| Google: `Access blocked` / `Error 403: access_denied` at sign-in | Account not in consent screen **Test users** | Add it, retry |
| T3 shows `[400]` on every event | `whsec_` mismatch (stale value in `.env`, or a Dashboard-endpoint secret instead of the CLI one) | Copy the secret T3 printed at startup into `web/.env`, restart T1 |
| T3 shows `[500]` | Bug in the webhook handler | Read T1's traceback; the known stripe-v15 `.get()` crash is fixed and covered by tests |
| `OAuth 2 MUST utilize https` after consent | `BASE_URL` in `.env` isn't `http://localhost...` (auto-relaxed only for localhost) | Set `BASE_URL=http://localhost:8000` for local testing |
| Friendly page “Gmail permission was not granted” | You unticked the Gmail checkbox | Click retry, tick it |
| Stuck on “Preparing your job” | Worker not running, or job errored | Check T2; `sqlite3 web/labeler.db "SELECT id,status,total,done_count,error FROM jobs;"` |
| T2: `authentication_error` / `invalid x-api-key` | Bad `ANTHROPIC_API_KEY` in `web/.env` | Re-paste from platform.claude.com; restart T2 |
| Batch runs forever | Check the batch state | platform.claude.com → Batches |
| Paid but `/start` bounced home | Payment not `paid` (declined test card?) | Use `4242…`; check the session in the Stripe dashboard |

Useful peeks:

```bash
sqlite3 web/labeler.db "SELECT id,email,paid_at IS NOT NULL paid,subscribed,token_encrypted IS NOT NULL has_token FROM users;"
sqlite3 web/labeler.db "SELECT id,user_id,kind,status,total,done_count,error FROM jobs ORDER BY id DESC LIMIT 5;"
```

Full reset between attempts (test data only): stop T1+T2, `rm web/labeler.db`,
restart. If a token was left behind (job errored), also remove the app at
myaccount.google.com/permissions.

## 4. Step 6 — cost + quality benchmark (your own mailbox)

Read-only: samples your already-labeled mail (ground truth = your existing
labels), classifies it through the exact production path (same prompts, Haiku,
Batch API 50% off), applies nothing, and never rewrites `personal/token.json`.
The API key is read from `web/.env` in-process — nothing is exported to the shell.

```bash
.venv/bin/python web/benchmark.py --report web/benchmark_report.md
# variants:
#   --sync                      instant results, 2x the batch price (~cents anyway)
#   --per-category 60           bigger sample
#   --model claude-sonnet-4-5   quality ceiling / cost comparison (~3x Haiku)
```

Default sample (≤40/category ≈ ≤360 emails) costs well under $0.10. The report
gives: overall + per-category agreement, top confusions, 40 disagreements to
eyeball (Haiku is sometimes right and the old label wrong — judge by reading,
not by the percentage alone), real tokens/email, and the extrapolated cost for
1k/2.5k/5k/10k-email mailboxes to compare against ROADMAP's table. Cross-check
actual billing at platform.claude.com → Usage.

Decision gate (ROADMAP): if agreement is high and disagreements look like
coin-flips → ship Haiku. If Haiku loses clearly on categories customers care
about → rerun with `--model claude-sonnet-4-5` and compare both quality and the
extrapolated per-mailbox cost before switching `MODEL` in `.env`.
