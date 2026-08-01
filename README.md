# Gmail Auto-Labeler

LLM-powered Gmail inbox labeling. Monorepo with three parts:

```
personal/   # personal CLI labeler — Claude Agent SDK, subscription auth (no API key)
web/        # commercial web product (€5 one-shot cleanup) — FastAPI + Anthropic API
shared/     # code used by both: categories/prompts, Gmail helpers, output parsing
```

Default categories: `Newsletter` (editorial content digests), `Marketing` (promotional
brand mail), `Shopping` (transactional: orders, receipts, shipping only), `Papers`,
`Finance`, `Dev`, `Security`, `Immo`, `Other`. The web product lets each user customize
this set. Labels are only *added* — nothing is archived or marked read. Already-labeled
messages are skipped, so every run is safe to repeat.

## Setup

```bash
cd /Users/skan/Documents/Projects/gmail-labeler
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt        # shared + personal deps
.venv/bin/pip install -r web/requirements.txt    # only if working on the web product
```

# Personal labeler (`personal/`)

Prerequisites:

- `personal/credentials.json` (Gmail OAuth installed-app client).
- The Claude Code CLI installed and logged in with your subscription.
- **`ANTHROPIC_API_KEY` must NOT be set** — the script exits with an error if it is,
  because an API key would silently override subscription auth.

First run opens a browser for the Gmail OAuth consent flow; the token is saved to
`personal/token.json` and refreshed automatically afterwards.

## Usage

```bash
# Dry-run (default): classifies but touches nothing in Gmail
.venv/bin/python personal/labeler.py --dry-run

# Actually apply labels
.venv/bin/python personal/labeler.py --apply

# Options
#   --days N   lookback window (default 3)
#   --max N    max messages per run (default 50)
```

## Backfill (one-time historical labeling)

Processes all inbox messages after a given date, oldest first, in chunks — reruns
automatically until done:

```bash
personal/backfill.sh 2025/10/05 500   # [AFTER_DATE] [CHUNK_SIZE]
```

## Scheduling (macOS launchd)

`personal/com.skan.gmail-labeler.plist` runs `--apply` every 15 minutes (and at
boot/login, so restarts need no manual action — each run catches up on mail received
during downtime). Logs: `personal/labeler.log` / `personal/labeler.err.log`.

```bash
cp personal/com.skan.gmail-labeler.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.skan.gmail-labeler.plist    # install
launchctl unload ~/Library/LaunchAgents/com.skan.gmail-labeler.plist  # remove
```

# Web product (`web/`)

The commercial version: user pays €5 (Stripe), authenticates with Google, optionally
customizes their category set, and a worker labels their past 365 days of mail via the
Anthropic Batch API. See `web/README.md` for architecture, environment variables, and
the go-to-market plan (unverified-app testing → Google restricted-scope verification).
