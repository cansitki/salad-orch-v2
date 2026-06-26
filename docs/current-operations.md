# Current Operations Plan

Reader: a future operator or agent that needs to keep the Salad PRL fleet profitable without private chat history.

Post-read action: start or audit the automation, understand why it waits or rotates, and update the fleet without exposing secrets.

## Operating Goal

Keep the fleet full of profitable GPUs first, then optimize quality.

The current policy is:

1. Fill all target slots across `kray`, `kry1`, `kray2`, and `kray3`.
   When `kry2`/`kr1`/`kr2`/`kr3` are enabled through
   `PRL_FLEET_EXTRA_ORGS_JSON`, include them in the same central scheduler
   scope.
2. Accept lower-profit GPUs during fill as long as they remain profitable at the conservative decision price.
3. Do not stop a newly running no-hash slot immediately. Wait the no-hash grace window.
4. Rotate or stop billable no-hash slots after grace.
5. Rotate live hashing GPUs only when they are negative beyond grace, or later in optimize mode when the fleet is full.

## Current Runtime Mode

Default mode is `fill`.

Fill mode uses:

| Setting | Current value |
| --- | --- |
| Decision PRL price | `0.64` USD |
| Minimum candidate profit | `0.05` USD/day |
| Allowed priorities | `batch,low` |
| No-hash grace | `60` seconds |
| Negative live slot grace | `90` seconds |
| Allocating rotation | `45` seconds |
| Creating progress grace | `120` seconds |
| Empty creating grace | `60` seconds |
| Watcher poll | `15` seconds |
| Guard poll | `15` seconds |
| Snapshot HTTP timeout | `4` seconds, `1` attempt |
| Underperform rotation | disabled in fill mode |
| Live upgrades | disabled in fill mode |
| No-GPU sleep trigger | after `3600` seconds without finding GPUs |
| No-GPU sleep duration | `900` seconds |

Optimize mode is reserved for when all target orgs are full of live hashing workers. It uses a lower decision price of `0.62` USD and enables underperform/live upgrade checks.

## Organizations And Slots

The public org labels are:

| Label | Target slots | API key variable |
| --- | ---: | --- |
| `kray` | 10 | `SALAD_API_KEY_2` |
| `kry1` | 10 | `SALAD_API_KEY_KRY1` |
| `kray2` | 10 | `SALAD_API_KEY_2` |
| `kray3` | 10 | `SALAD_API_KEY_2` |
| `kry2` | 10 | `SALAD_API_KEY_KRY1` when sharing the kry1 token; otherwise `SALAD_API_KEY_KRY2` |
| `kr1` | 10 | `SALAD_API_KEY_KR1` |
| `kr2` | 10 | `SALAD_API_KEY_KR1` when sharing the kr1 token; otherwise `SALAD_API_KEY_KR2` |
| `kr3` | 10 | `SALAD_API_KEY_KR1` when sharing the kr1 token; otherwise `SALAD_API_KEY_KR3` |
| `alpha1` | 10 | `SALAD_API_KEY_ALPHA` |
| `alpha2` | 10 | `SALAD_API_KEY_ALPHA` when sharing the alpha1 token |

Base target capacity is 40 active or pending slots. With `kry2`, `kr1`, `kr2`,
`kr3`, `alpha1`, and `alpha2` enabled, target capacity is 100 slots.

Slot names follow:

```text
prl-kray-roi-01  ... prl-kray-roi-10
prl-kry1-roi-01  ... prl-kry1-roi-10
prl-kray2-roi-01 ... prl-kray2-roi-10
prl-kray3-roi-01 ... prl-kray3-roi-10
prl-kry2-roi-01  ... prl-kry2-roi-10
prl-kr1-roi-01   ... prl-kr1-roi-10
prl-kr2-roi-01   ... prl-kr2-roi-10
prl-kr3-roi-01   ... prl-kr3-roi-10
prl-alpha1-roi-01 ... prl-alpha1-roi-10
prl-alpha2-roi-01 ... prl-alpha2-roi-10
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
kray-prl-watch
kry1-prl-watch
kray2-prl-watch
kray3-prl-watch
kray-prl-guard
kray-prl-nonstop-supervisor
salad-pearl-monitor
```

The four watcher sessions can run independently. The guard consumes the same public state and reuses the watcher logic for retargeting. The supervisor should keep the sessions alive instead of relying on a manual terminal.

## Start Commands

Create a private `.env` from `.env.example`, fill only local secrets, then run:

```bash
PRL_FLEET_MODE=fill bash scripts/start_watchers.sh
PRL_FLEET_MODE=fill bash scripts/start_supervisor.sh
```

Do not commit `.env`.

## Monitoring Commands

Check process health:

```bash
ps -eo pid,etimes,cmd | rg 'salad_prl_(watch|guard|nonstop_supervisor)' | rg -v rg
tmux ls | rg 'kray-prl|kry1-prl|kray2-prl|kray3-prl|salad-pearl-monitor'
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
python3 scripts/fleet_audit.py --loop --interval 300 --balance-interval 3600 --balance-file state/salad_balances.json
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
`quota_capacity=used/capacity blocked=X balance_blocked=Y unknown=Z` lines.
When an org moves from quota 0 back to positive quota, the DB records an
`org_replica_quota_restored` event and the normal monitor loop can fill it.

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
