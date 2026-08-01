# Prompt for Claude Code — Gmail Auto-Labeler

Build a Python CLI tool that automatically labels my Gmail inbox using an LLM classifier.

## Context
- This runs on an always-on Mac mini (macOS). Python 3.11+, use a venv.
- I have a Claude Max subscription. The classifier must use the **Claude Agent SDK** (`claude-agent-sdk` Python package) authenticated via my subscription's Agent SDK credit — **not** an API key. Add a startup check that fails loudly if `ANTHROPIC_API_KEY` is set in the environment, since it would silently override subscription auth.
- I already have `credentials.json` (Gmail API OAuth client, installed-app flow) in the project root.

## What it does
1. Authenticate to the Gmail API with the `gmail.modify` scope. Run the installed-app OAuth flow on first run, persist the token to `token.json`, and refresh automatically afterwards.
2. Fetch recent inbox messages (default: last 3 days, configurable) that don't yet have any of my category labels.
3. For each message, extract: sender, subject, and the first ~300 characters of the plain-text body (fall back to snippet). Never send full bodies or attachments to the LLM.
4. Classify each message into exactly one category using the Claude Agent SDK. Use a strict prompt that returns only a category name from this fixed list:
   - `Newsletter` — all newsletters and digests, **including dev/AI/tech newsletters** (e.g. Substack, TLDR, Pragmatic Engineer). Newsletter-style content always goes here, never to Dev.
   - `Shopping` — orders, shipping, receipts, e-commerce
   - `Papers` — government, official paperwork, taxes administration, fines, CAF, sécu, utilities
   - `Finance` — any mail from banks or financial institutions
   - `Dev` — transactional developer-tool mail only: GitHub, CI/CD, monitoring, cloud providers, package registries. Not newsletters.
   - `Security` — verification/2FA codes, one-time passwords, login alerts, password resets
   - `Immo` — real estate: apartment listings, agency contacts, visit confirmations, SeLoger/PAP/Leboncoin immo alerts
   - `Other` — everything else, including personal mail from friends, mail from myself, and anything ambiguous
   Each email belongs to one and only one category. Validate the LLM output against this enum; if invalid or the call fails, fall back to `Other` and log a warning.
5. Create the Gmail labels if they don't exist, using these names exactly as-is (no prefix), then apply the label to each message. Do not archive or mark as read — only add labels.
6. Batch classification where sensible (e.g. up to 10 emails per LLM call, returning a JSON array) to minimize calls.

## CLI behavior
- `python labeler.py --dry-run` → prints what it *would* label, calls the LLM but touches nothing in Gmail. Make dry-run the default; require `--apply` to actually modify.
- `--days N` → lookback window (default 3).
- `--max N` → cap messages processed per run (default 50).
- `--backfill --after YYYY/MM/DD` → one-time historical mode: process all inbox messages after the given date (use Gmail search query `after:`), oldest first, respecting `--max` per run so I can process history in chunks (e.g. `--max 500`) across several sessions. `--after` is mandatory in backfill mode: if `--backfill` is passed without it, exit immediately with a clear error. Because it skips already-labeled messages, re-running the same command resumes where it left off.
- Clear, compact log output: one line per email showing sender, truncated subject, and chosen category.

## Robustness
- Idempotent: at startup, resolve the IDs of the 8 category labels, and skip any message that already carries one of them; safe to run repeatedly.
- Handle Gmail API pagination and 429/5xx with exponential backoff.
- Gracefully handle emails with no plain-text part (HTML-only → strip tags, or use snippet).
- No secrets in code; `token.json` and `credentials.json` in `.gitignore`.

## Deliverables
- `labeler.py` (single file is fine), `requirements.txt`, `.gitignore`.
- A short `README.md`: setup steps, how to run, the backfill procedure, and a **Scheduling** section for macOS launchd: provide a LaunchAgent `.plist` (in `~/Library/LaunchAgents/`) with `StartInterval` of 900 seconds that runs the script with `--apply` using the venv's Python via absolute paths, plus `StandardOutPath`/`StandardErrorPath` pointing to log files in the project directory, and the `launchctl load`/`unload` commands to install and remove it. Note that it loads automatically at boot/login, so restarts require no manual action, and each run catches up on mail received during any downtime.
- Keep it simple: no database, no framework. Standard library + `google-api-python-client`, `google-auth-oauthlib`, `claude-agent-sdk`.

## First run
After writing the code, run `--dry-run` yourself against my mailbox (I'll complete the OAuth browser flow when prompted), show me the classification results, and wait for my confirmation before I run it with `--apply`.
