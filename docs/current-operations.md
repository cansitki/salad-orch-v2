# Current Operations Plan

Reader: a future operator or agent that needs to keep the Salad PRL fleet profitable without private chat history.

Post-read action: start or audit the automation, understand why it waits or rotates, and update the fleet without exposing secrets.

## Operating Goal

Keep the fleet full of profitable GPUs first, then optimize quality.

The current policy is:

1. Fill all target slots across `kray`, `kry1`, `kray2`, and `kray3`.
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

Target capacity is 40 active or pending slots.

Slot names follow:

```text
prl-kray-roi-01  ... prl-kray-roi-10
prl-kry1-roi-01  ... prl-kry1-roi-10
prl-kray2-roi-01 ... prl-kray2-roi-10
prl-kray3-roi-01 ... prl-kray3-roi-10
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
