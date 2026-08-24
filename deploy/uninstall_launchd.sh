#!/bin/bash
#
# deploy/uninstall_launchd.sh
#
# Stops and removes the turtle daily agent. Leaves the repo, logs, audit DB,
# and any open positions untouched — this only stops the schedule.
#
# To also flatten the book:  uv run python scripts/close_all.py --live

set -uo pipefail

LABEL="com.turtle.daily"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null && echo "Stopped $LABEL" \
    || echo "$LABEL was not loaded"

if [ -f "$PLIST" ]; then
    rm -f "$PLIST"
    echo "Removed $PLIST"
fi

echo ""
echo "Schedule removed. Open positions and their stops are UNCHANGED."
echo "To flatten:  cd ~/live_turtle && uv run python scripts/close_all.py --live"
