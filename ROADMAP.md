# Gmail Labeler → Commercial Web App ("€5 inbox cleanup")

## Context

The personal labeler works and runs on the Mac mini. The commercial idea (€5 one-shot
365-day cleanup + optional €2/mo subscription) has been designed and **the code is
already built and committed**: the repo is restructured into `personal/`, `web/`,
`shared/`, and the web MVP (FastAPI + Stripe + Google OAuth + categories editor +
Batch-API worker) boots and passes smoke tests. What remains is almost entirely
**external setup only the owner can do** (Anthropic key, Google OAuth client, Stripe),
then testing, deployment, and validation. This plan lays out those steps in order, plus
a detailed Anthropic API cost analysis.

## Current status (done ✅)

- ✅ Monorepo restructure (`personal/`, `web/`, `shared/`), git history in place
- ✅ Personal labeler verified post-move; launchd job reloaded and running
- ✅ `web/` MVP: landing → Stripe Checkout → OAuth → per-user categories editor →
  job queue → Batch-API worker → progress page → token revoke on completion
- ✅ Privacy policy page (`/privacy`) — needed for the Google consent screen
- ✅ Mac mini deployment kit in `web/deploy/`: KeepAlive launchd plists for
  uvicorn + worker, Cloudflare Tunnel config, runbook (hosting cost: €0; the
  Vercel-free route was ruled out — no long-running workers, ephemeral disk,
  Hobby plan forbids commercial use)
- ✅ `web/.env` filled: generated `FERNET_KEY`/`SESSION_SECRET` + real Anthropic
  API key, Google OAuth web client, Stripe test keys (Phase A steps 1–3)
- ⏳ **Next: Phase A steps 4–6** — local end-to-end test, then the cost/quality
  benchmark on the owner's own mailbox

---

## Step-by-step: what YOU do next

### Phase A — Credentials + local end-to-end test (~1–2 evenings, ~€5 total)

1. **Anthropic API key** (~10 min)
   - platform.claude.com → sign in → Billing → buy the minimum **$5 prepaid credits**
     (API is prepaid; this comfortably covers all testing).
   - API Keys → create key → paste into `web/.env` as `ANTHROPIC_API_KEY`.
   - Important: keep it ONLY in `web/.env` — never `export` it in your shell, or the
     personal labeler will refuse to run (by design, so your subscription auth is
     never silently overridden).

2. **Google OAuth web client** (~15 min)
   - console.cloud.google.com → same project as the personal labeler → APIs & Services
     → Credentials → Create credentials → OAuth client ID → type **Web application**.
   - Authorized redirect URI: `http://localhost:8000/oauth/callback`
   - Copy client ID + secret into `web/.env`.
   - OAuth consent screen → Audience: add **Test users**: your own Gmail + a secondary
     Google account (the "fake customer"). Testing mode = max 100 users, no
     verification needed.

3. **Stripe (test mode)** (~15 min)
   - dashboard.stripe.com → create account → Developers → API keys → copy the
     **test** secret key (`sk_test_...`) into `web/.env`.
   - `brew install stripe/stripe-cli/stripe`, then
     `stripe listen --forward-to localhost:8000/stripe-webhook` — copy the printed
     `whsec_...` into `web/.env` as `STRIPE_WEBHOOK_SECRET`.

4. **Run it** (two terminals from the repo root)
   ```bash
   .venv/bin/uvicorn web.app:app --reload
   .venv/bin/python web/worker.py
   ```

5. **Walk the full customer flow** with the secondary Google account:
   landing → pay with test card `4242 4242 4242 4242` → Google OAuth (expect the
   "unverified app" warning — click Advanced → continue) → edit categories (try adding
   a custom one) → watch the progress page → check labels in that account's Gmail →
   verify the app is gone from myaccount.google.com/permissions (token revoked).

6. **Cost + quality benchmark on your own mailbox** — the most important test.
   Go through the flow with your main account. Since your mail is already labeled by
   the personal labeler, either compare spot-checks on new mail, or temporarily point
   `BACKFILL_DAYS` at a window and compare Haiku's decisions against your existing
   labels. Then read the actual spend in the Anthropic console → this gives your
   **real cost-per-mailbox number** (expected: well under €1 — see cost section).
   If Haiku misclassifies too much, set `MODEL=claude-sonnet-4-5`-class model in
   `.env` and re-check both quality and cost (~3× Haiku).

### Phase B — Deploy publicly (a weekend, ~€15/mo running costs)

7. Buy a domain (~€10/yr) and a small VPS (Hetzner ~€4–5/mo, or fly.io free tier).
8. Deploy: clone repo, venv, `web/.env` with `BASE_URL=https://yourdomain`,
   run uvicorn + worker under systemd, HTTPS via Caddy (2-line config).
9. Update Google: add `https://yourdomain/oauth/callback` as a redirect URI.
10. Stripe: Dashboard → Webhooks → add `https://yourdomain/stripe-webhook` endpoint
    (new `whsec_` for prod); switch to **live** API keys when ready to charge.
11. Write the privacy-policy page (required by Google even in testing; the landing
    page's privacy bullets are the skeleton — expand into a `/privacy` route).

### Phase C — Validate with real users (2–4 weeks)

12. Add up to 100 test users in the Google console (friends first, then FR
    communities — the Papers/CAF/Immo angle is the hook). They pay real €5.
13. Track per customer: mailbox size, Anthropic spend, complaints about label
    quality, refunds. Kill or continue based on: do strangers pay + refer?

### Phase D — Scale gate (only if validation succeeds)

14. Google restricted-scope verification + CASA Tier 2 (~$540–1,000/yr, start 4–12
    weeks before you need public access) — removes the 100-user cap and the warning.
15. Wire the subscription: Stripe subscription checkout + webhook setting
    `users.subscribed=1` (the daily-sweep worker logic already exists), and a
    completion email (currently the status page is the only notification).

### Known code gaps (deliberately deferred; build in Phase D)
- Subscription billing not wired (flag + daily sweep exist; no Stripe subscription flow)
- No completion email (status page only)
- No admin dashboard (query SQLite directly for now)

---

## Anthropic API costs in detail

Pricing used (Haiku 4.5): **$1 / MTok input, $5 / MTok output**; the worker uses the
**Batch API = 50% off** → effective **$0.50 / MTok in, $2.50 / MTok out**.

Token math per email (measured shape of our prompt):
- Input ≈ 200 tokens/email (≈65 amortized system prompt + ≈135 for sender, subject,
  300-char excerpt, in groups of 10)
- Output ≈ 5 tokens/email (JSON array of category names)

| Mailbox size (365 days) | Input tokens | Batch cost | ≈ EUR |
|---|---|---|---|
| 1,000 emails | 0.2 M | $0.11 | €0.10 |
| 2,500 emails | 0.5 M | $0.28 | €0.26 |
| 5,000 emails (typical) | 1.0 M | $0.56 | €0.52 |
| 10,000 emails (job cap) | 2.0 M | $1.12 | ~€1.05 |

**Per-customer unit economics on €5:** LLM ~€0.10–1.05 (worst case at the cap) +
Stripe fee ~€0.33–0.40 → **gross margin ≈ €3.6–4.5 per customer (~75–90%)**.

Other cost lines:
- **Your testing (Phase A):** one full run of your own mailbox + retries ≈ €1–2.
  The $5 minimum credit purchase covers everything.
- **Subscribers (daily sweep, sync API at full price):** ~30 emails/day ≈ 6k input
  tokens/day ≈ **€0.01/month per subscriber** — negligible vs €2/mo.
- **If Haiku quality disappoints → Sonnet-class model:** ~3× the table above
  (typical mailbox ≈ €1.6, cap ≈ €3.1) — margin shrinks but stays positive; decide
  from the Phase A benchmark, not upfront.
- **Not Anthropic but adjacent:** Gmail API free; VPS ~€5/mo; domain ~€10/yr;
  CASA ~$540+/yr only at Phase D.

Cost-control guards already in the code: `MAX_EMAILS_PER_JOB=10000` caps any single
customer; `PRICE_EUR_CENTS`, `MODEL`, `BACKFILL_DAYS` are all `.env` knobs.

## Verification (per phase)

- **A:** full test-mode flow with secondary account → labels in Gmail, token revoked,
  Anthropic console shows spend matching the table above (±30%).
- **B:** same flow on the live domain over HTTPS; Stripe live-mode €5 charge to
  yourself; webhook delivery shows 200 in the Stripe dashboard.
- **C:** ≥1 stranger completes payment→labels without hand-holding; support requests
  and refunds logged.
