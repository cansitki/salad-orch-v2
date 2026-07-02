#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
cd "$REPO_ROOT"

ENV_FILE=${SALAD_PRL_ENV:-"$REPO_ROOT/.env"}
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

PYTHON=${SALAD_PRL_PYTHON:-"$REPO_ROOT/.venv/bin/python"}
if [[ ! -x "$PYTHON" ]]; then
  PYTHON=python3
fi

ORG=${PRL_FAST_FILL_ORG:-kray}
CONFIG_PATH=${SALAD_FLEET_CONFIG_PATH:-config/fleet.kray-only-200.json}
DB_PATH=${PRL_FLEET_DB_PATH:-state/fleet_scheduler.db}
INTERVAL_SECONDS=${PRL_FAST_FILL_INTERVAL_SECONDS:-90}
WORKERS=${PRL_FAST_FILL_WORKERS:-4}
MIN_PROFIT=${PRL_FAST_FILL_MIN_PROFIT_USD_DAY:-0.00}
ACTIONABLE_LIMIT=${PRL_FAST_FILL_ACTIONABLE_LIMIT:-8}
MAX_ZERO_WORKER_ACTIVE=${PRL_FAST_FILL_MAX_ZERO_WORKER_ACTIVE:-12}
GUARD_STOP_COOLDOWN=${PRL_FAST_FILL_GUARD_STOP_COOLDOWN_SECONDS:-900}
LOG_PATH=${PRL_FAST_FILL_LOG_PATH:-state/logs/safe-fill.compact.jsonl}
ERR_LOG_PATH=${PRL_FAST_FILL_ERR_LOG_PATH:-state/logs/safe-fill.err}

mkdir -p "$(dirname "$LOG_PATH")" "$(dirname "$ERR_LOG_PATH")"

while true; do
  PRICE=$(sqlite3 "$DB_PATH" "select selected_price_usd from price_history where selected_price_usd is not null order by sampled_at_utc desc, id desc limit 1")
  if [[ -n "$PRICE" ]]; then
    SALAD_FLEET_CONFIG_PATH="$CONFIG_PATH" \
      PRL_FLEET_CONFIG_PATH="$CONFIG_PATH" \
      PRL_ENABLED_ORGS="$ORG" \
      PRL_FAST_FILL_GUARD_STOP_COOLDOWN_SECONDS="$GUARD_STOP_COOLDOWN" \
      "$PYTHON" scripts/fast_fill_targets.py \
        --org "$ORG" \
        --workers "$WORKERS" \
        --price "$PRICE" \
        --min-profit "$MIN_PROFIT" \
        --patch-existing \
        --actionable-limit "$ACTIONABLE_LIMIT" \
        --max-zero-worker-active "$MAX_ZERO_WORKER_ACTIVE" \
        --db "$DB_PATH" \
        --json 2>> "$ERR_LOG_PATH" \
      | "$PYTHON" -c 'import json, sys; print(json.dumps(json.load(sys.stdin), sort_keys=True), flush=True)' \
        >> "$LOG_PATH" 2>> "$ERR_LOG_PATH"
  else
    printf '%s\n' "missing selected_price_usd" >> "$ERR_LOG_PATH"
  fi
  sleep "$INTERVAL_SECONDS"
done
