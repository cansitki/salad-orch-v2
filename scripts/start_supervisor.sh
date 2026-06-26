#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
ENV_FILE=${SALAD_PRL_ENV:-"$REPO_ROOT/.env"}
if [[ -f "$ENV_FILE" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -n "$line" ]] || continue
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" == *"="* ]] || continue
    key=${line%%=*}
    value=${line#*=}
    key=${key//[[:space:]]/}
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if [[ -z "${!key+x}" ]]; then
      value=${value%\"}
      value=${value#\"}
      value=${value%\'}
      value=${value#\'}
      export "$key=$value"
    fi
  done < "$ENV_FILE"
fi
STATE_DIR=${SALAD_PRL_STATE_DIR:-"$REPO_ROOT/state"}
LOG_DIR="$STATE_DIR/logs"
mkdir -p "$LOG_DIR"

SUPERVISOR="$SCRIPT_DIR/salad_prl_nonstop_supervisor.py"
PREFLIGHT="$SCRIPT_DIR/salad_prl_preflight.py"
PYTHON=${SALAD_PRL_PYTHON:-"$REPO_ROOT/.venv/bin/python"}
if [[ ! -x "$PYTHON" ]]; then
  PYTHON=python3
fi
SESSION=kray-prl-nonstop-supervisor
OUT="$LOG_DIR/kray_prl_nonstop_supervisor.out"

FLEET_MODE=${PRL_FLEET_MODE:-fill}
case "$FLEET_MODE" in
  fill) DECISION_PRICE_USD=${PRL_FILL_FIXED_DECISION_PRICE_USD:-0.64} ;;
  optimize) DECISION_PRICE_USD=${PRL_OPTIMIZE_FIXED_DECISION_PRICE_USD:-0.62} ;;
  *)
    echo "unknown PRL_FLEET_MODE: $FLEET_MODE" >&2
    exit 2
    ;;
esac

"$PYTHON" "$PREFLIGHT" --decision-price "$DECISION_PRICE_USD" --min-profitable-profiles 1

tmux has-session -t "$SESSION" 2>/dev/null && tmux kill-session -t "$SESSION"
SUPERVISOR_INTERVAL_SECONDS=${PRL_SUPERVISOR_INTERVAL_SECONDS:-30}
SUPERVISOR_MAX_HEARTBEAT_AGE_SECONDS=${PRL_SUPERVISOR_MAX_HEARTBEAT_AGE_SECONDS:-300}
supervisor_cmd() {
  printf 'cd %q && env' "$REPO_ROOT"
  local key
  for key in \
    SALAD_PRL_ENV \
    SALAD_PRL_STATE_DIR \
    SALAD_PRL_PYTHON \
    SALAD_FLEET_CONFIG_PATH \
    PRL_FLEET_CONFIG_PATH \
    SALAD_FLEET_CONFIG_JSON \
    PRL_START_WATCHERS_SCRIPT \
    PRL_SUPERVISOR_LOG \
    PRL_SUPERVISOR_ORGS \
    PRL_FLEET_ORGS \
    PRL_FLEET_MODE \
    PRL_AGGRESSIVE_DISCOVERY \
    PRL_POLL_SECONDS \
    PRL_FILL_FIXED_DECISION_PRICE_USD \
    PRL_FIXED_DECISION_PRICE_USD \
    PRL_FILL_MIN_PROFIT_USD_DAY \
    PRL_OPTIMIZE_FIXED_DECISION_PRICE_USD \
    PRL_OPTIMIZE_MIN_PROFIT_USD_DAY \
    PRL_REWARD_CALIBRATION_FACTOR \
    PRL_PRICE_BAND_USD \
    PRL_WATCH_ALLOWED_PRIORITIES \
    PRL_WATCH_BLOCKED_PROFILES \
    PRL_WATCH_COORDINATED_TARGET_OFFSETS \
    PRL_WATCH_ZERO_AVAILABLE_PROBE_BUDGET \
    PRL_WATCH_ZERO_AVAILABLE_PROBE_MAX_LIVE_WORKERS \
    PRL_NOHASH_GRACE_SECONDS \
    PRL_STALE_WORKER_GRACE_SECONDS \
    PRL_NEGATIVE_SLOT_GRACE_SECONDS \
    PRL_ALLOCATING_ROTATE_SECONDS \
    PRL_ALLOCATING_RETARGET_AVAILABLE_SECONDS \
    PRL_LOW_LIVE_MIN_LIVE_WORKERS \
    PRL_LOW_LIVE_ALLOCATING_RETARGET_AVAILABLE_SECONDS \
    PRL_EMPTY_STUCK_NON_LIVE_SECONDS \
    PRL_STUCK_NON_LIVE_SECONDS \
    PRL_STUCK_RUNNING_ZERO_DEFER_SECONDS \
    PRL_STUCK_NON_LIVE_MAX_ACTIONS \
    PRL_STUCK_NON_LIVE_MIN_ACTIVE_SLOTS \
    PRL_STUCK_NON_LIVE_TICK_BUDGET_SECONDS \
    PRL_FILL_LIVE_UPGRADE_SECONDS \
    PRL_LIVE_UPGRADE_INTERVAL_SECONDS \
    PRL_LIVE_UPGRADE_MIN_PROFIT_USD_DAY \
    PRL_LIVE_UPGRADE_MIN_TH_DELTA \
    PRL_LIVE_UPGRADE_MIN_LIVE_WORKERS \
    PRL_LIVE_UPGRADE_REQUIRE_FULL_SLOTS \
    PRL_LIVE_UPGRADE_REQUIRE_REPORTED_AVAILABLE; do
    if [[ -n "${!key+x}" ]]; then
      printf ' %s=%q' "$key" "${!key}"
    fi
  done
  printf ' %q %q --interval %q --max-heartbeat-age-seconds %q >> %q 2>&1' \
    "$PYTHON" "$SUPERVISOR" "$SUPERVISOR_INTERVAL_SECONDS" "$SUPERVISOR_MAX_HEARTBEAT_AGE_SECONDS" "$OUT"
}
tmux new-session -d -s "$SESSION" "$(supervisor_cmd)"
tmux list-sessions | rg 'prl-(watch|guard|nonstop-supervisor)'
