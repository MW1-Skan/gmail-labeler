#!/bin/bash
# Watchdog for the personal Gmail labeler (com.skan.gmail-labeler).
#
# The labeler writes to personal/labeler.log on EVERY 15-minute run (even when
# there is nothing to do), so a stale log always means broken or not running.
# launchd's StartInterval already retries every 15 minutes — this job only
# DETECTS and alerts (macOS notification + personal/watchdog.log); it never
# restarts or modifies anything.
#
# Cause-specific advice for the failure classes we've actually seen:
#   - dead Gmail refresh token (invalid_grant in labeler.err.log) -> re-consent hint
#   - job not loaded in launchd -> reload hint
#   - anything else (import hang, broken venv, disk trouble) -> generic alert

set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$REPO_ROOT/personal/labeler.log"
ERR="$REPO_ROOT/personal/labeler.err.log"
WLOG="$REPO_ROOT/personal/watchdog.log"
MARKER="$REPO_ROOT/personal/.watchdog-last-notify"

STALE_SECONDS=2700        # 45 min = 3 missed 15-minute runs
RENOTIFY_SECONDS=14400    # nag again at most every 4h while still broken

mtime_of() {  # file mtime as epoch; BSD stat on macOS, GNU stat elsewhere (tests)
    if [ "$(uname)" = "Darwin" ]; then
        stat -f %m "$1" 2>/dev/null || echo 0
    else
        stat -c %Y "$1" 2>/dev/null || echo 0
    fi
}

wlog() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$WLOG"
}

notify() {
    if command -v osascript >/dev/null 2>&1; then
        osascript -e "display notification \"$1\" with title \"Gmail labeler DOWN\"" \
            >/dev/null 2>&1 || true
    fi
}

now=$(date +%s)
age=$(( now - $(mtime_of "$LOG") ))

if [ "$age" -lt "$STALE_SECONDS" ]; then
    if [ -f "$MARKER" ]; then   # was broken, is healthy again
        rm -f "$MARKER"
        wlog "recovered: labeler is running again"
        notify "Recovered — labeler is running again."
    fi
    exit 0
fi

reason="no completed run for $(( age / 60 )) min — check personal/labeler.err.log"
if tail -5 "$ERR" 2>/dev/null | grep -qiE 'invalid_grant|RefreshError'; then
    reason="Gmail token expired/revoked. Fix: rm personal/token.json, then .venv/bin/python personal/labeler.py --dry-run (browser re-consent)"
elif command -v launchctl >/dev/null 2>&1 && ! launchctl list | grep -q 'com\.skan\.gmail-labeler$'; then
    reason="job not loaded. Fix: launchctl load ~/Library/LaunchAgents/com.skan.gmail-labeler.plist"
fi

wlog "STALE: $reason"

if [ $(( now - $(mtime_of "$MARKER") )) -ge "$RENOTIFY_SECONDS" ]; then
    notify "$reason"
    touch "$MARKER"
    wlog "notification fired"
fi
exit 0
