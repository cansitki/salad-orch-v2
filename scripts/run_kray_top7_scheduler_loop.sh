#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

unset PRL_FLEET_ORGS PRL_WATCH_SLOT_COUNT_KRAY PRL_WATCH_SLOT_PREFIX_KRAY

export PYTHONPATH=scripts
export SALAD_FLEET_CONFIG_PATH="${SALAD_FLEET_CONFIG_PATH:-config/fleet.kray-only-200.json}"
export PRL_FLEET_CONFIG_PATH="${PRL_FLEET_CONFIG_PATH:-config/fleet.kray-only-200.json}"
export PRL_ENABLED_ORGS="${PRL_ENABLED_ORGS:-kray}"

# Current kray-only optimization policy:
# - use the live PRL price from price_oracle
# - rank candidates by lowest break-even
# - allow recently unstable profiles only when profitable now
# - do not use negative break-even probes as fill targets
export PRL_FILL_MIN_PROFIT_USD_DAY="${PRL_KRAY_TOP7_MIN_PROFIT_USD_DAY:-0}"
export PRL_SCORER_USE_OBSERVED_TH="${PRL_SCORER_USE_OBSERVED_TH:-1}"
export PRL_SCORER_OBSERVED_TH_MIN_LIVE_SAMPLES="${PRL_SCORER_OBSERVED_TH_MIN_LIVE_SAMPLES:-20}"
export PRL_SCHEDULER_RANK_BY_BREAK_EVEN="${PRL_SCHEDULER_RANK_BY_BREAK_EVEN:-1}"
export PRL_SCHEDULER_RANK_BY_PROFIT="${PRL_SCHEDULER_RANK_BY_PROFIT:-0}"
export PRL_SCHEDULER_ALLOW_BREAK_EVEN_PROBES="${PRL_SCHEDULER_ALLOW_BREAK_EVEN_PROBES:-0}"
export PRL_SCHEDULER_ALLOW_UNSTABLE_PROFILES="${PRL_SCHEDULER_ALLOW_UNSTABLE_PROFILES:-1}"
export PRL_SCHEDULER_REPLACE_OUT_OF_WIDTH_OBSERVED="${PRL_SCHEDULER_REPLACE_OUT_OF_WIDTH_OBSERVED:-1}"
export PRL_SCHEDULER_FALLBACK_WITHIN_WIDTH_ONLY="${PRL_SCHEDULER_FALLBACK_WITHIN_WIDTH_ONLY:-1}"
export PRL_FILL_PREFER_REPORTED_AVAILABLE_SCORE_ORDER="${PRL_FILL_PREFER_REPORTED_AVAILABLE_SCORE_ORDER:-0}"
export PRL_SCHEDULER_REPLACEMENT_BEST_ORDER="${PRL_SCHEDULER_REPLACEMENT_BEST_ORDER:-1}"

DB_PATH="${PRL_FLEET_DB_PATH:-state/fleet_scheduler.db}"
INTERVAL_SECONDS="${PRL_KRAY_TOP7_SCHEDULER_INTERVAL_SECONDS:-300}"
WIDTH="${PRL_KRAY_TOP7_WIDTH:-7}"
FEE="${PRL_KRAY_TOP7_FEE:-0.01}"

while true; do
  price="$(sqlite3 "$DB_PATH" "select selected_price_usd from price_history where selected_price_usd is not null order by id desc limit 1")"
  python3 scripts/fleet_scheduler.py \
    --once \
    --mode base_fill \
    --price "$price" \
    --fee "$FEE" \
    --width "$WIDTH" \
    --db "$DB_PATH" \
    --json
  sleep "$INTERVAL_SECONDS"
done
