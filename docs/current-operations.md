# Current Operations Plan

Reader: a future operator or agent that needs to keep the Salad PRL fleet profitable without private chat history.

Post-read action: start or audit the automation, understand why it waits or rotates, and update the fleet without exposing secrets.

## Operating Goal

Keep the current `kray` fleet full of profitable GPUs first, then optimize quality.

The current policy is:

1. Operate only the `kray` organization for the live run. Use
   `config/fleet.kray-only-150.json` and `PRL_ENABLED_ORGS=kray`.
2. Fill up to the current `kray` target of 150 slots with the best currently
   available GPU profiles, ranked by live expected profit.
3. Do not stop a newly running no-hash slot immediately. Wait the no-hash grace window.
4. Rotate or stop billable no-hash slots after grace.
5. Rotate live hashing GPUs only when they are negative beyond grace and the
   PRL price has been stable for the configured 60-minute window.

## Current Runtime Mode

Default mode is kray-only live fill.

Fill mode uses:

| Setting | Current value |
| --- | --- |
| Fleet config | `config/fleet.kray-only-150.json` |
| Enabled org filter | `PRL_ENABLED_ORGS=kray` |
| Target slots | `150` |
| Decision PRL price | latest `price_history.selected_price_usd` |
| Minimum replacement profit | `-0.10` USD/day during scarce-GPU fill |
| Allowed priorities | current top live-ranked profiles, usually `batch` |
| No-hash grace | `120` seconds |
| Empty stuck non-live grace | `300` seconds |
| Stuck non-live grace | `600` seconds |
| Negative live slot grace | `3600` seconds |
| Negative minimum loss | `0.05` USD/day |
| Negative price stability | 60-minute history required, max `$0.03` range |
| Guard poll | `180` seconds |
| Snapshot HTTP timeout | `4` seconds, `1` attempt |
| Underperform rotation | enabled after `900` seconds if below expected range |
| Safe-fill zero-worker gate | pause above 12 active zero-worker slots |

Optimize mode is reserved for when `kray` is materially full of live hashing
workers. Do not re-enable old organizations unless Can explicitly changes the
scope.

## Organizations And Slots

The current live org label is:

| Label | Target slots | API key variable |
| --- | ---: | --- |
| `kray` | 150 | `SALAD_API_KEY_2` |

The old multi-org layout remains in `config/fleet.current.json` for reference,
but it is not the current operating scope. Current live automation must use
`config/fleet.kray-only-150.json`.

Slot names follow:

```text
prl-kray-roi-01 ... prl-kray-roi-150
```

## Script Map

The automation is intentionally plain Python plus shell launchers.

| Script | Role |
| --- | --- |
| `scripts/start_watchers.sh` | Starts all org watchers and the guard in tmux. |
| `scripts/start_supervisor.sh` | Starts the nonstop supervisor in tmux. |
| `scripts/salad_prl_watch.py` | Per-org watcher that creates, starts, patches, rotates, and protects slots. |
| `scripts/salad_prl_guard.py` | Fleet guard that retargets or stops no-hash, negative, or underperforming slots. |
| `scripts/salad_prl_profit_snapshot.py` | Combines Salad cost, pool workers, PRL emission, and price into profit reports. |
| `scripts/salad_prl_rank_candidates.py` | Ranks available Salad GPU candidates by expected profit at a chosen PRL price. |
| `scripts/salad_prl_nonstop_supervisor.py` | Keeps tmux sessions alive and selects fill/optimize mode based on live worker count. |
| `scripts/fleet_audit.py` | Records active GPU snapshots every 5 minutes and hourly org balance-vs-cost audits. |
| `scripts/portal_balances.py` | Refreshes the private local balance file from Salad Portal using a local cookie jar and optional env login. |
| `scripts/portal_multi_balances.py` | Refreshes several Salad Portal accounts and merges their org balances into the private balance file. |
| `scripts/spike_report.py` | Reports 30/60 minute negative/no-hash spike history and cooldowns repeatedly unstable profiles. |

## Runtime Sessions

Expected tmux sessions:

```text
salad-orch-v2-price
salad-orch-v2-scheduler
salad-orch-v2-guard-stuck
salad-orch-v2-safe-fill
salad-orch-v2-audit
salad-pearl-monitor
```

The scheduler, safe-fill, and guard sessions must all include
`SALAD_FLEET_CONFIG_PATH=config/fleet.kray-only-150.json`,
`PRL_FLEET_CONFIG_PATH=config/fleet.kray-only-150.json`, and
`PRL_ENABLED_ORGS=kray`.

## Start Commands

Create a private `.env` from `.env.example`, fill only local secrets, then run
the kray-only v2 scheduler stack:

```bash
SALAD_FLEET_CONFIG_PATH=config/fleet.kray-only-150.json PRL_FLEET_CONFIG_PATH=config/fleet.kray-only-150.json PRL_ENABLED_ORGS=kray python3 scripts/supervisor.py --ensure --runtime-monitor-apply
```

The live `salad-orch-v2-*` tmux sessions are currently managed directly because
the operating scope is intentionally restricted to `kray`.

The legacy watcher-first stack is still available when explicitly needed:

```bash
PRL_FLEET_MODE=fill bash scripts/start_watchers.sh
PRL_FLEET_MODE=fill bash scripts/start_supervisor.sh
```

Do not commit `.env`.

## Monitoring Commands

Check process health:

```bash
ps -eo pid,etimes,cmd | rg 'scripts/(price_oracle|fleet_scheduler|guard|fast_fill_targets)' | rg -v rg
tmux ls | rg 'salad-orch-v2|salad-pearl-monitor'
```

Run a conservative profit snapshot:

```bash
env PRL_SNAPSHOT_HTTP_TIMEOUT_SECONDS=3 PRL_SNAPSHOT_HTTP_ATTEMPTS=1 \
  python3 scripts/salad_prl_profit_snapshot.py --price 0.64
```

Rank currently available candidates for one org:

```bash
python3 scripts/salad_prl_rank_candidates.py --price 0.64 --org kray
```

Watch guard actions:

```bash
tail -f state/logs/prl_nohash_guard.log
```

Check active GPUs and hourly balance-vs-cost audits:

```bash
SALAD_FLEET_CONFIG_PATH=config/fleet.kray-only-150.json PRL_FLEET_CONFIG_PATH=config/fleet.kray-only-150.json PRL_ENABLED_ORGS=kray python3 scripts/fleet_audit.py --loop --interval 300 --balance-interval 3600 --balance-file state/salad_balances.json
python3 scripts/portal_balances.py --loop --interval 900 --balance-file state/salad_balances.json --cookie-jar state/portal_cookies.txt
SALAD_PORTAL_BALANCE_EMAILS="account1@example.com,account2@example.com" python3 scripts/portal_multi_balances.py --loop --interval 900 --balance-file state/salad_balances.json
python3 scripts/spike_report.py --heartbeat --loop --interval 300
```

`spike_report.py` reads guard-written `slot_spike_events`. With `--heartbeat`,
profiles marked unstable also get temporary wildcard cooldown rows for every
enabled org (`org/*/profile`). Defaults: 3 spikes in 30m, 5 spikes in 60m, or
3 affected slots in 60m triggers a 3600 second cooldown. The display `--limit`
does not cap cooldown scanning; `PRL_SPIKE_COOLDOWN_SCAN_LIMIT` defaults to
1000. Use `PRL_SPIKE_AUTO_COOLDOWN_PROFILES=0` or `--no-auto-cooldown` to keep
reporting without applying cooldowns.

Guard v2 uses a separate short retry cooldown for successful no-hash/negative
retargets: `PRL_GUARD_RETARGET_COOLDOWN_SECONDS` defaults to `120`. Keep this
shorter than `PRL_PENDING_PROFILE_COOLDOWN_SECONDS` so a retarget that
immediately lands on another bad GPU can be corrected without waiting through
the longer search cooldown.

### Hash Safety Guard For Scarce-GPU Fill

When the fleet is intentionally filling many scarce Salad slots, do not judge a
slot only by whether Salad reports an active GPU. Also guard for two waste
patterns:

- `underperform`: the slot has pool hash, but the hashrate is below the
  expected range for the observed GPU/profile.
- `stuck_no_live`: Salad reports the slot as active/creating/allocating, but no
  fresh Pearl worker maps to that slot after the stuck grace.

The live July 2026 kray-only fill used this controlled guard loop:

```bash
tmux new-session -d -s salad-orch-v2-guard-stuck -c /home/coder/projects/salad '
zsh -lc '"'"'
set -a; . ./.env; set +a
while true; do
  PRICE=$(sqlite3 state/fleet_scheduler.db "select selected_price_usd from price_history where selected_price_usd is not null order by sampled_at_utc desc, id desc limit 1")
  SALAD_FLEET_CONFIG_PATH=config/fleet.kray-only-150.json \
  PRL_FLEET_CONFIG_PATH=config/fleet.kray-only-150.json \
  PRL_ENABLED_ORGS=kray \
  PRL_FILL_MIN_PROFIT_USD_DAY=0.00 \
  PRL_OPTIMIZE_MIN_PROFIT_USD_DAY=0.00 \
  PRL_GUARD_ENABLED_ISSUES=no_hash,underperform,stuck_no_live,negative \
  PRL_GUARD_STOP_WITHOUT_TARGET_ISSUES=no_hash,stuck_no_live,negative \
  PRL_GUARD_NOHASH_GRACE_SECONDS=120 \
  PRL_EMPTY_STUCK_NON_LIVE_SECONDS=300 \
  PRL_STUCK_NON_LIVE_SECONDS=600 \
  PRL_GUARD_NEGATIVE_GRACE_SECONDS=3600 \
  PRL_GUARD_NEGATIVE_MIN_LOSS_USD_DAY=0.05 \
  PRL_GUARD_NEGATIVE_PRICE_STABILITY_REQUIRE_HISTORY=1 \
  PRL_GUARD_NEGATIVE_PRICE_STABILITY_WINDOW_MINUTES=60 \
  PRL_GUARD_NEGATIVE_PRICE_STABILITY_MAX_RANGE_USD=0.03 \
  PRL_GUARD_NEGATIVE_BYPASS_STABILITY_IF_WINDOW_MAX_UNPROFITABLE=1 \
  PRL_GUARD_REPLACEMENT_MODE=base_fill \
  PRL_GUARD_REPLACEMENT_MIN_PROFIT_USD_DAY=0.00 \
  PRL_GUARD_ALLOW_NEGATIVE_REPLACEMENTS=0 \
  PRL_GUARD_ALLOW_UNSTABLE_REPLACEMENTS=1 \
  PRL_GUARD_UNDERPERFORM_RATIO=0.85 \
  PRL_GUARD_UNDERPERFORM_MIN_DEFICIT_TH=10 \
  PRL_GUARD_UNDERPERFORM_GRACE_SECONDS=900 \
  PRL_GUARD_MAX_ACTIONS_PER_RUN=16 \
  PRL_GUARD_RETARGET_COOLDOWN_SECONDS=420 \
  python3 scripts/guard.py --once --apply --db state/fleet_scheduler.db --price "$PRICE" --json \
    2>> state/logs/guard-stuck.err |
    python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin), sort_keys=True), flush=True)" \
    >> state/logs/guard-stuck.compact.jsonl
  sleep 180
done
'"'"'
'
```

Safety points:

- The loop uses the live PRL price from `price_history`, not the risk-off
  decision price, so replacement ranking follows current market reality.
- Snapshot issue detection is deliberately faster than the default: running
  no-hash slots are eligible after 120 seconds, empty pending/deploying slots
  after 300 seconds, and other stuck non-live slots after 600 seconds.
- It applies at most 16 live actions per tick and waits 420 seconds before
  touching the same slot again. This keeps bad billable slots from burning too
  long while still avoiding whole-fleet churn.
- `stuck_no_live` does not stop a slot when no replacement target exists; it
  waits instead. This avoids mass emptying slots when Salad availability is
  thin.
- `negative` is enabled, but normally requires all of these before action:
  one-hour grace, at least $0.05/day loss, and a real 60-minute price-history
  window whose PRL range is at most $0.03. The kray profit-protect override can
  bypass the stability wait only when the slot is still estimated unprofitable
  at the maximum PRL price seen in that 60-minute window.

For the same scarce-GPU, below-breakeven fill mode, keep the central scheduler
aligned with the guard by ranking target profiles by live expected profit and
allowing temporarily unstable profiles when they are still the least-loss
choice:

```bash
tmux new-session -d -s salad-orch-v2-scheduler -c /home/coder/projects/salad '
zsh -lc '"'"'
set -a; . ./.env; set +a
while true; do
  PRICE=$(sqlite3 state/fleet_scheduler.db "select selected_price_usd from price_history where selected_price_usd is not null order by sampled_at_utc desc, id desc limit 1")
  PRL_SCHEDULER_ALLOW_UNSTABLE_PROFILES=1 \
  PRL_SCHEDULER_RANK_BY_PROFIT=1 \
  PRL_FILL_MIN_PROFIT_USD_DAY=0.00 \
  PRL_OPTIMIZE_MIN_PROFIT_USD_DAY=0.00 \
  SALAD_FLEET_CONFIG_PATH=config/fleet.kray-only-150.json \
  PRL_FLEET_CONFIG_PATH=config/fleet.kray-only-150.json \
  PRL_ENABLED_ORGS=kray \
  python3 scripts/fleet_scheduler.py --once --mode base_fill --price "$PRICE" --fee 0.01 --width 2 --db state/fleet_scheduler.db
  sleep 120
done
'"'"'
'
```

This mode is intentionally for profit-protect operation: it can keep trying
scarce profiles, but it should not schedule a new expected-negative target.
Without `PRL_SCHEDULER_ALLOW_UNSTABLE_PROFILES=1`, the scheduler can prefer a
"safe" but less profitable GPU profile over a recently unstable profile with
better expected profit.

Apply scheduler targets with a gated fast-fill loop. This loop limits actual
create/patch/start actions after skipping already-active containers, and pauses
new starts while too many active/pending slots have no fresh Pearl worker:

```bash
tmux new-session -d -s salad-orch-v2-safe-fill -c /home/coder/projects/salad '
zsh -lc '"'"'
set -a; . ./.env; set +a
while true; do
  PRICE=$(sqlite3 state/fleet_scheduler.db "select selected_price_usd from price_history where selected_price_usd is not null order by sampled_at_utc desc, id desc limit 1")
  SALAD_FLEET_CONFIG_PATH=config/fleet.kray-only-150.json \
  PRL_FLEET_CONFIG_PATH=config/fleet.kray-only-150.json \
  PRL_ENABLED_ORGS=kray \
  PRL_FAST_FILL_GUARD_STOP_COOLDOWN_SECONDS=900 \
  python3 scripts/fast_fill_targets.py \
    --org kray \
    --workers 4 \
    --price "$PRICE" \
    --min-profit 0.00 \
    --patch-existing \
    --actionable-limit 8 \
    --max-zero-worker-active 12 \
    --db state/fleet_scheduler.db \
    --json >> state/logs/safe-fill.compact.jsonl
  sleep 90
done
'"'"'
'
```

`PRL_FAST_FILL_GUARD_STOP_COOLDOWN_SECONDS` prevents safe-fill from
immediately restarting a slot that guard just stopped for no-hash/stuck
behavior. Keep it above the guard loop interval during profit-protect mode so a
bad slot does not burn cost in a stop/start loop.

Profit reports should keep two numbers separate:

- `hashing_only`: current run-rate for slots with fresh Pearl workers. This is
  the profit number used for live mining decisions.
- `zero_worker_active`: temporary probing/deploying cost. Treat it as a guard
  pressure signal, not as the final daily mining run-rate, unless it persists
  beyond the no-hash/stuck windows.

Watch it with:

```bash
tail -f state/logs/guard-stuck.compact.jsonl
tail -f state/logs/safe-fill.compact.jsonl
tail -f state/logs/guard-stuck.err
```

For the current fill-first operation with scarce Salad GPUs, run the monitor
with `--pending-status-retarget-after-seconds 180`. That rotates
creating/allocating/deploying slots sooner than the previous five-minute wait,
while still giving fresh pending slots enough time to settle before retargeting.

Check Salad replica quota when funded orgs refuse to start:

```bash
python3 - <<'PY'
import os, sys
sys.path.insert(0, "scripts")
from config_loader import load_config
print([org.label for org in load_config().enabled_orgs()])
PY
```

`org_worker.py` calls Salad's `/organizations/<org>/quotas` endpoint before
live start/patch/create actions. If `container_replicas_quota=0`, the worker
records `skip_zero_replica_quota` attempts and marks slots as `zero_quota`.
That means the org may still have credit, but Salad currently allows zero GPU
replicas there; there is no profitable fill action until the quota is raised.
Quota reads are persisted in `org_replica_quotas`, including positive quota
reads. Use `python3 scripts/health.py --json` to inspect `quota_blockers`, or
`python3 scripts/reporter.py` for the concise `quota_blockers=N/M` and
`quota_capacity=used/capacity fillable_now=F blocked=X balance_blocked=Y
unknown=Z` lines. `fillable_now` means the org has both positive balance and
available Salad replica quota, so the live loop can try to fill those slots
immediately.
Use `python3 scripts/reporter.py --capacity-limit 0` to print every org in the
top-up and quota-blocked action lists.
When an org moves from quota 0 back to positive quota, the DB records an
`org_replica_quota_restored` event and the normal monitor loop can fill it.

Supervisor-started tmux sessions also enable a bounded zero-balance credit
probe: `PRL_AVAILABILITY_ZERO_BALANCE_CREDIT_PROBE=1`. The probe only touches
orgs whose Portal balance file says `0.00` while Salad quota still reports
available replicas. It runs one `org_worker` pass with the zero-balance skip
temporarily disabled. A `no_credits_available` response creates an org cooldown
(`PRL_AVAILABILITY_ZERO_BALANCE_CREDIT_PROBE_COOLDOWN_SECONDS`, default `900`),
so the system retries occasionally without spending ten failed requests every
minute. If Salad accepts the create, the slot fill path proceeds immediately.

## Profit Model

The snapshot uses:

```text
revenue_day = live TH * net PRL per TH per day * decision PRL price
cost_day    = Salad hourly GPU cost * 24
profit_day  = revenue_day - cost_day
```

For a billable running slot with no matching fresh pool worker, the snapshot records `TH=0`, `revenue_day=0`, and `profit_day=-cost_day`. That means the slot is a no-hash cost drag, not necessarily a bad GPU.

For a live hashing GPU, negative means the measured hashrate is too low for its Salad cost at the decision price.

## Decision Rules

During fill mode:

- Start only candidates expected to be profitable at `0.64` with at least `0.05` USD/day buffer.
- Keep live hashing GPUs protected unless they are negative past the negative grace window.
- Let new `running_without_pool` slots live for 60 seconds.
- If a no-hash slot persists beyond grace, retarget it to the next profitable candidate.
- If no profitable replacement is available, stop the slot.
- Keep rotating empty or allocating slots to catch scarce capacity.
- Do not enter long no-GPU sleep until a slot has failed to find GPUs for an hour.

During optimize mode:

- Use `0.62` USD as decision price.
- Allow underperform and live-upgrade checks.
- Replace active GPUs only when the replacement improves expected daily profit enough to justify churn.

## State Interpretation

Healthy states:

```text
live_protected
allocating
creating
rotated
running_without_pool, but only briefly
```

Action states:

```text
slot_retargeted
slot_stop_requested
candidate_profit_ok
candidate_skipped_low_expected_profit
candidate_skipped_capacity_reserved
```

Warning states:

```text
running_without_pool beyond grace
negative_slot_observed beyond grace
tick_failed repeating
API 401/403/404
```

## Mining And Price Inputs

The fleet mines PearlFortune PRL with the PearlFortune miner release `v.1.1.8`.

Miner settings:

```text
release: pearl-miner v1.1.8
binary: miner-cuda12
pool: global.pearlfortune.org:443
```

Public price inputs:

| Source | Use |
| --- | --- |
| PearlFortune market API | PRL/USD reference price |
| SafeTrade PRL/USDT ticker | exchange-side PRL price check |

The scripts use public market data for reporting. Fill decisions use the fixed decision price to avoid chasing short-lived live price spikes.

## Latest Verified Example

On 2026-06-24 at 10:56 UTC, the guard reported:

```text
decision price: 0.64 USD
fresh workers: 30
cost: 33.12 USD/day
profit at decision price: 6.93 USD/day
no-hash slots: 0
negative slots: 0
```

This is a point-in-time example, not a guarantee. Always run a fresh snapshot before acting.

## Public Boundary

This repo may include:

- strategy
- thresholds
- public org labels
- script names
- public API endpoints
- non-secret environment variable names
- sanitized examples

This repo must not include:

- Salad API key values
- cookies or Cloudflare clearance values
- private auth headers
- private wallet control credentials
- raw logs that contain secrets
- `.env`

Before pushing, run a diff and search for secret-looking strings.
