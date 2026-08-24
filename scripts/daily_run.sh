#!/bin/bash
#
# scripts/daily_run.sh
#
# Unattended daily rebalance. Intended to be driven by launchd (see
# deploy/install_launchd.sh), but safe to run by hand.
#
#   1. check_exits.py --live      exit positions that broke the 10-day low
#   2. execute_live.py --auto     enter new breakouts (no prompts)
#   3. place_stops.py --live      backfill any stop that failed inline
#   4. portfolio_report.py        append to data/daily_log.csv
#
# Runs at most once per UTC day. launchd fires this hourly; the stamp file
# makes repeat firings no-ops. That design survives a sleeping laptop —
# whenever the Mac next wakes after 00:00 UTC, the run happens.
#
# Refuses to trade while the executor is halted. Clearing the halt is a
# deliberate manual act:
#     uv run python scripts/execute_live.py --reset-halt
#
# Exit codes:
#   0  ran successfully, or skipped because already run today
#   1  environment problem (repo/uv/network)
#   2  skipped because the executor is halted

set -uo pipefail   # deliberately not -e: we log each step's failure and continue

REPO="${TURTLE_REPO:-$HOME/live_turtle}"
LOGDIR="$REPO/logs"
STAMP="$REPO/data/.last_run_utc"
SUMMARY="$LOGDIR/summary.log"

TODAY_UTC="$(date -u +%Y-%m-%d)"
NOW_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
LOG="$LOGDIR/$TODAY_UTC.log"

mkdir -p "$LOGDIR" "$REPO/data" 2>/dev/null

note()    { echo "[$NOW_UTC] $*" | tee -a "$SUMMARY"; }
logline() { echo "[$(date -u +%H:%M:%SZ)] $*" >> "$LOG"; }

# --- already ran today? -----------------------------------------------------
if [ -f "$STAMP" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$TODAY_UTC" ]; then
    exit 0
fi

cd "$REPO" 2>/dev/null || { note "FATAL repo not found at $REPO"; exit 1; }

# --- locate uv (launchd gives us a bare PATH) -------------------------------
UV=""
for c in "$(command -v uv 2>/dev/null)" \
         "$HOME/.local/bin/uv" \
         "/opt/homebrew/bin/uv" \
         "/usr/local/bin/uv"; do
    [ -n "$c" ] && [ -x "$c" ] && { UV="$c"; break; }
done
[ -z "$UV" ] && { note "FATAL uv not found (looked on PATH, ~/.local/bin, /opt/homebrew/bin, /usr/local/bin)"; exit 1; }

# --- network reachable? -----------------------------------------------------
# Connectivity only. Any HTTP status means we're online; the scripts do their
# own error handling from there. Checking for a specific 2xx would let a
# transient 403/429 block the entire run.
curl -s -m 15 -o /dev/null https://api.coinbase.com 2>/dev/null
case $? in
    0|22|56) : ;;                      # reached the host (any status)
    *) note "SKIP  cannot reach api.coinbase.com — will retry next hour"; exit 1 ;;
esac

{
    echo ""
    echo "==============================================================="
    echo " TURTLE DAILY RUN   $NOW_UTC"
    echo " repo=$REPO  uv=$UV"
    echo "==============================================================="
} >> "$LOG"

# --- halt check -------------------------------------------------------------
HALT_OUT="$("$UV" run python scripts/halt_status.py 2>>"$LOG")"
if [ "${HALT_OUT:0:6}" = "HALTED" ]; then
    logline "ABORT — executor halted: ${HALT_OUT:7}"
    note "HALTED  no trades. reason: ${HALT_OUT:7}"
    note "        clear with: cd $REPO && uv run python scripts/execute_live.py --reset-halt"
    exit 2
fi

run_step() {
    local label="$1"; shift
    local rc=0
    logline "--- $label ---"
    # Capture the command's status directly. Reading $? after an `if` block
    # yields the status of the `if` itself (always 0), not the command.
    "$@" >> "$LOG" 2>&1 || rc=$?
    if [ "$rc" -eq 0 ]; then
        logline "--- $label OK ---"
        return 0
    fi
    logline "--- $label FAILED rc=$rc ---"
    note "WARN  $label failed (rc=$rc) — see $LOG"
    return "$rc"
}

run_step "check_exits"      "$UV" run python scripts/check_exits.py --live
run_step "execute_live"     "$UV" run python scripts/execute_live.py --auto
run_step "place_stops"      "$UV" run python scripts/place_stops.py --live
run_step "portfolio_report" "$UV" run python scripts/portfolio_report.py

# --- did anything trip the halt during the run? -----------------------------
POST_HALT="$("$UV" run python scripts/halt_status.py 2>>"$LOG")"
if [ "${POST_HALT:0:6}" = "HALTED" ]; then
    note "HALTED during run: ${POST_HALT:7}"
fi

# --- one-line result --------------------------------------------------------
# `grep -c` prints 0 and exits 1 when there are no matches; `|| true` keeps
# that 0 rather than appending a second line via `|| echo 0`.
FILLED="$(grep -c 'FILLED —' "$LOG" 2>/dev/null || true)"
SOLD="$(grep -c '  SOLD ' "$LOG" 2>/dev/null || true)"
VALUE="$(grep -m1 'Portfolio Value' "$LOG" 2>/dev/null | awk '{print $NF}')"
note "DONE  filled=${FILLED:-0} exits=${SOLD:-0} value=${VALUE:-?}  log=$LOG"

echo "$TODAY_UTC" > "$STAMP"
exit 0
