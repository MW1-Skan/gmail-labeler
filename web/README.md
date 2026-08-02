# Inbox Labeler — web product

€5 one-shot Gmail cleanup: Stripe Checkout → Google OAuth → per-user categories →
worker labels the past 365 days via the Anthropic Batch API → Gmail access revoked.

## Components

| File | Role |
|---|---|
| `app.py` | FastAPI: landing, checkout, Stripe webhook, Google OAuth, categories editor, status |
| `worker.py` | Job runner: one-shot backfills (Batch API) + daily subscriber sweep (sync) |
| `classifier.py` | Anthropic API classification (paid, per-token — separate from the personal labeler's subscription auth) |
| `db.py` | SQLite: users, per-user categories, jobs, Fernet-encrypted Gmail tokens |
| `config.py` | Env-driven config, loaded from `web/.env` |
| `benchmark.py` | Read-only cost/quality benchmark of the owner's mailbox (ROADMAP step 6) |
| `tests/` | Automated E2E suite — full pipeline with Stripe/Google/Gmail/Anthropic faked |

## Setup

1. `cp web/.env.example web/.env` and fill everything in (the file documents where
   each value comes from: Anthropic key, Google OAuth web client, Stripe keys,
   generated secrets).
2. Google Cloud console: on the existing project, create a **Web application** OAuth
   client with redirect URI `{BASE_URL}/oauth/callback`; consent screen External +
   **Testing** mode (up to 100 manually-added test users, no verification needed yet).
3. Install deps: `.venv/bin/pip install -r web/requirements.txt`
4. Run both processes from the repo root:
   ```bash
   .venv/bin/uvicorn web.app:app --reload          # web server
   .venv/bin/python web/worker.py                  # job worker
   ```
5. Local Stripe webhook: `stripe listen --forward-to localhost:8000/stripe-webhook`
   (copy the printed `whsec_...` into `.env`).

## Testing

Automated (no `.env`, no network — all externals faked):

```bash
.venv/bin/pip install -r web/requirements-dev.txt
.venv/bin/python -m pytest web/tests/ -q
```

Manual test-mode flow — follow **`web/TESTING.md`** (click-by-click runbook with a
debug table): landing → pay with card `4242 4242 4242 4242` → Google OAuth with a
**test user** account → adjust categories → job runs → labels appear in that
account's Gmail → verify the app disappears from
https://myaccount.google.com/permissions (token revoked).

## Privacy invariants (keep true — they're the product's trust story)

- Only sender, subject, and a 300-char excerpt are sent to the classifier. Bodies are
  never stored anywhere.
- Gmail tokens are Fernet-encrypted at rest and revoked + deleted when a one-shot job
  finishes (kept only for active subscribers).
- Labels are only added — never archive, delete, mark read, or forward.

## Deployment (Mac mini, free)

See `deploy/README.md` — launchd plists for the app + worker and a Cloudflare Tunnel
config expose the mini as `https://yourdomain.com` for the cost of the domain alone.

## Path to public launch (when validation succeeds)

1. Google restricted-scope verification for `gmail.modify`: privacy policy page,
   homepage, demo video, scope justification.
2. CASA Tier 2 self-serve assessment (~$540–1,000/yr, allow 4–12 weeks total).
3. Move Stripe to live keys, deploy on a small VPS, add the €2/mo subscription
   (`users.subscribed=1` enables the daily sweep in `worker.py`; billing via Stripe
   subscriptions — not yet wired).
