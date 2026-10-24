#!/bin/sh
# Quit Live and reopen it with a set, so it loads freshly built plug-in binaries
# (Live keeps a plug-in loaded until it restarts) and a fresh copy of the Remote Script.
#   scripts/restart_live.sh [path/to/set.als]
# If the set has unsaved changes, Live shows a Save dialog and waits; answer it.
set -eu

SET="${1:-}"
APP="${LIVE_APP:-$(ls -d /Applications/Ableton\ Live\ 12*.app 2>/dev/null | head -1)}"
[ -n "$APP" ] || { echo "Live 12 not found in /Applications (set LIVE_APP)" >&2; exit 1; }
# Check the set before quitting Live, so a wrong path can't close the running session for nothing.
if [ -n "$SET" ] && [ ! -f "$SET" ]; then
    echo "No set at $SET (Live saves inside a '<name> Project' folder); Live left running." >&2
    exit 1
fi

if pgrep -x Live >/dev/null; then
    osascript -e 'quit app "Live"' || true
    i=0
    while pgrep -x Live >/dev/null; do
        i=$((i + 1))
        if [ "$i" -gt 120 ]; then
            echo "Live is still running after 120 s (a Save dialog?)" >&2
            exit 1
        fi
        sleep 1
    done
fi

if [ -n "$SET" ]; then
    open -a "$APP" "$SET"
else
    open -a "$APP"
fi

# Wait for the ClaudeLiveBridge control surface to answer.
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PYTHONPATH="$HERE/src" python3 -m livebridge wait --timeout "${WAIT_TIMEOUT:-120}"
