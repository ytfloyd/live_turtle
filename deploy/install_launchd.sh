#!/bin/bash
#
# deploy/install_launchd.sh
#
# Installs the turtle daily rebalance as a macOS launchd user agent.
#
#   ./deploy/install_launchd.sh
#
# The agent fires hourly. scripts/daily_run.sh only acts once per UTC day
# (stamp file at data/.last_run_utc), so hourly firing simply means the run
# happens promptly after 00:00 UTC and self-heals if the Mac was asleep.
#
# Runs as your user, from your repo, with your keys. Nothing leaves the Mac.

set -euo pipefail

LABEL="com.turtle.daily"
REPO="${TURTLE_REPO:-$HOME/live_turtle}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
RUNNER="$REPO/scripts/daily_run.sh"

[ -d "$REPO" ]     || { echo "ERROR repo not found at $REPO"; exit 1; }
[ -f "$RUNNER" ]   || { echo "ERROR runner not found at $RUNNER"; exit 1; }
[ -f "$REPO/.env" ] || { echo "ERROR $REPO/.env not found — the job needs your CDP config"; exit 1; }

chmod +x "$RUNNER"
mkdir -p "$HOME/Library/LaunchAgents" "$REPO/logs"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$RUNNER</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>TURTLE_REPO</key>
        <string>$REPO</string>
        <key>HOME</key>
        <string>$HOME</string>
        <key>PATH</key>
        <string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>

    <key>WorkingDirectory</key>
    <string>$REPO</string>

    <!-- Fire hourly; the runner is idempotent per UTC day. -->
    <key>StartInterval</key>
    <integer>3600</integer>

    <!-- Also try once immediately on login, in case the Mac was off. -->
    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>$REPO/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$REPO/logs/launchd.err.log</string>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLIST_EOF

# Reload cleanly whether or not it was already installed.
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
launchctl enable "gui/$UID/$LABEL"

echo "Installed $LABEL"
echo "  plist   $PLIST"
echo "  runner  $RUNNER"
echo "  logs    $REPO/logs/"
echo ""
echo "Verify:   launchctl list | grep turtle"
echo "Run now:  launchctl kickstart -k gui/$UID/$LABEL"
echo "Watch:    tail -f $REPO/logs/summary.log"
echo "Remove:   ./deploy/uninstall_launchd.sh"
