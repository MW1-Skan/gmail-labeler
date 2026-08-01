# Gmail Auto-Labeler

Automatically labels your Gmail inbox using an LLM classifier (Claude Agent SDK,
authenticated via your Claude subscription — no API key).

Categories: `Newsletter` (editorial content digests), `Marketing` (promotional brand mail),
`Shopping` (transactional: orders, receipts, shipping only), `Papers`, `Finance`, `Dev`,
`Security`, `Immo`, `Other`.
Labels are only *added* — nothing is archived or marked read. Already-labeled messages
are skipped, so it's safe to run repeatedly.

## Setup

```bash
cd /Users/skan/Documents/Projects/gmail-labeler
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Prerequisites:

- `credentials.json` (Gmail OAuth installed-app client) in the project root.
- The Claude Code CLI installed and logged in with your subscription (`claude` on PATH).
- **`ANTHROPIC_API_KEY` must NOT be set** — the script exits with an error if it is,
  because an API key would silently override subscription auth.

First run opens a browser for the Gmail OAuth consent flow; the token is saved to
`token.json` and refreshed automatically afterwards.

## Usage

```bash
# Dry-run (default): classifies but touches nothing in Gmail
.venv/bin/python labeler.py --dry-run

# Actually apply labels
.venv/bin/python labeler.py --apply

# Options
#   --days N   lookback window (default 3)
#   --max N    max messages per run (default 50)
```

## Backfill (one-time historical labeling)

Processes all inbox messages after a given date, oldest first. `--after` is mandatory.
Because already-labeled messages are skipped, re-running the same command resumes where
it left off — process history in chunks:

```bash
.venv/bin/python labeler.py --backfill --after 2024/01/01 --max 500 --apply
# ...run again until it reports "Nothing to do."
```

## Scheduling (macOS launchd)

Runs `--apply` every 15 minutes on the always-on Mac mini. Create
`~/Library/LaunchAgents/com.skan.gmail-labeler.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.skan.gmail-labeler</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/skan/Documents/Projects/gmail-labeler/.venv/bin/python</string>
        <string>/Users/skan/Documents/Projects/gmail-labeler/labeler.py</string>
        <string>--apply</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/skan/Documents/Projects/gmail-labeler</string>
    <key>EnvironmentVariables</key>
    <dict>
        <!-- launchd's default PATH doesn't include Homebrew; the Agent SDK
             needs to find the `claude` CLI -->
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>StartInterval</key>
    <integer>900</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/skan/Documents/Projects/gmail-labeler/labeler.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/skan/Documents/Projects/gmail-labeler/labeler.err.log</string>
</dict>
</plist>
```

Install / remove:

```bash
launchctl load ~/Library/LaunchAgents/com.skan.gmail-labeler.plist
launchctl unload ~/Library/LaunchAgents/com.skan.gmail-labeler.plist
```

Once loaded, the agent starts automatically at boot/login — restarts require no manual
action. Each run looks back over the last 3 days, so any mail received during downtime
is caught up on the next run. Logs are written to `labeler.log` / `labeler.err.log` in
the project directory.
