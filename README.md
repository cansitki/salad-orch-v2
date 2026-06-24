# Salad PRL GPU Runbook

Public runbook for running a SaladCloud GPU fleet that mines PearlFortune PRL.

This repository includes the public, sanitized version of the automation code.
It explains the operating model, thresholds, environment variables, monitoring
flow, and safety rules. It intentionally does not include live API keys, cookies,
private logs, or machine-local secrets.

## Reader And Goal

This is for a future operator who needs to understand or recreate the current
Salad PRL automation without reading the private VM history.

After reading this, the operator should be able to:

1. Set up the required secrets locally.
2. Start the fleet in fill mode.
3. Verify whether GPUs are hashing and profitable.
4. Know when to wait, rotate, stop, or optimize.

## Current Strategy

The current strategy is:

1. Fill every available SaladCloud slot with batch-priority GPUs that are not
   expected to lose money at the conservative PRL decision price.
2. Do not optimize too early. A lower-profit active GPU is better than an empty
   slot while GPU availability is scarce.
3. Once every org is full of live hashing GPUs, switch to optimize mode and
   replace weaker GPUs with better ones.

The important rule is:

```text
Fill first. Optimize after the fleet is full.
```

## Organizations

The fleet currently uses four SaladCloud organizations:

| Public label | Salad organization slug | API key env var |
| --- | --- | --- |
| kray | kray | SALAD_API_KEY_2 |
| kry1 | kry1 | SALAD_API_KEY_KRY1 |
| kray2 | kray2 | SALAD_API_KEY_2 |
| kray3 | kray3 | SALAD_API_KEY_2 |

Each org targets 10 container groups:

```text
prl-kray-roi-01  ... prl-kray-roi-10
prl-kry1-roi-01  ... prl-kry1-roi-10
prl-kray2-roi-01 ... prl-kray2-roi-10
prl-kray3-roi-01 ... prl-kray3-roi-10
```

Total target capacity: 40 active or pending slots.

## Secret Handling

Never commit any of these:

```text
SALAD_API_KEY
SALAD_API_KEY_2
SALAD_API_KEY_KRY1
Cloudflare cookies
Salad portal cookies
cf_clearance values
private logs with headers
```

Use environment variables or a private `.env` file only. The public docs should
refer to secret names, not secret values.

Example private environment:

```bash
SALAD_API_KEY_2=<private key for kray/kray2/kray3>
SALAD_API_KEY_KRY1=<private key for kry1>
PRL_WALLET=<public PearlFortune wallet address>
```

## Repository Layout

The runnable code lives in `scripts/`.

| File | Purpose |
| --- | --- |
| `scripts/salad_prl_watch.py` | Per-org Salad watcher. Creates, starts, rotates, and protects slots. |
| `scripts/salad_prl_profit_snapshot.py` | Combines Salad state, pool stats, PRL price, costs, and profit. |
| `scripts/salad_prl_guard.py` | Stops or reallocates no-hash, negative-profit, and underperforming slots. |
| `scripts/salad_prl_nonstop_supervisor.py` | Keeps tmux sessions alive and switches fill/optimize mode. |
| `scripts/start_watchers.sh` | Starts all org watchers and the guard. |
| `scripts/start_supervisor.sh` | Starts the nonstop supervisor. |
| `scripts/state_db.py` | SQLite state DB, schema, heartbeats, events, targets, scores. |
| `scripts/config_loader.py` | Multi-org config loader. Defaults to 4 orgs * 10 slots, supports JSON org config. |
| `scripts/price_oracle.py` | Samples PearlFortune/SafeTrade price and writes trailing risk mode. |
| `scripts/availability_probe.py` | Samples Salad GPU availability per org/profile and records no-GPU cooldowns. |
| `scripts/profit_model.py` | Deterministic expected profit and break-even model with configurable Pearl fee. |
| `scripts/profile_scorer.py` | Scores GPU profiles using expected profit and recent attempt history. |
| `scripts/fleet_scheduler.py` | Central dry-run-safe target assignment across all org slots. |
| `scripts/org_worker.py` | Per-org worker that consumes scheduler targets; live actions require `--apply`. |
| `scripts/guard.py` | Guard v2. Analyzes by default; `--apply` retargets/stops no-hash or negative slots after grace. |
| `scripts/supervisor.py` | Scheduler control tick and tmux process plan for the new stack. |
| `scripts/reporter.py` | CLI/JSON status report from the scheduler DB. |
| `scripts/health.py` | Read-only health check for targets, stale heartbeats, guard issues, and runtime failures. |
| `scripts/shadow_compare.py` | Read-only target-vs-observed mismatch and unsafe-target report for shadow mode. |
| `scripts/rollout.py` | Controlled shadow/one-org/all-org/guard rollout runner with safety gates. |
| `scripts/runtime_monitor.py` | Safe runtime monitor loop for repeated shadow gates and explicitly confirmed live actions. |
| `scripts/rollback.py` | Rollout checkpoint create/list/restore helper for scheduler targets. |
| `scripts/maintenance.py` | Dry-run-first SQLite retention/compaction helper for long-running fleets. |
| `.env.example` | Safe template for local secrets and runtime settings. |

The current operating plan is documented in `docs/current-operations.md`.
The planned next-generation scheduler is documented in
`docs/fleet-scheduler-build-plan.md`.

The public scripts are intentionally parameterized. Secrets are read from env
vars or `.env`, not from source code.

## Setup

Install dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Create a private env file:

```bash
cp .env.example .env
```

Fill in:

```text
PRL_WALLET
SALAD_API_KEY_2
SALAD_API_KEY_KRY1
```

No SafeTrade API key is needed. SafeTrade is used only through its public
PRL/USDT ticker endpoint for price discovery.

## New Scheduler Shadow Mode

The new deterministic scheduler can be run without live Salad changes. This is
the default validation path.

Initialize the DB and sync the default 4 organizations:

```bash
python3 scripts/state_db.py --init --sync-config --status
```

Use the current low-fee window by overriding Pearl fee to 1%:

```bash
PRL_PEARL_FEE_RATE=0.01 python3 scripts/fleet_scheduler.py --price 0.64 --fee 0.01
python3 scripts/reporter.py
```

Probe live Salad availability for the highest-profit batch profile only:

```bash
python3 scripts/availability_probe.py --profile-limit 1
```

Dry-run one organization worker:

```bash
python3 scripts/org_worker.py --org kry1
```

`org_worker.py` does not perform live Salad actions unless `--apply` is passed.
Even with `--apply`, running and pending slots are protected by default. Live
retargeting requires explicit `--allow-live-retarget`; pending retargeting
requires explicit `--allow-pending-retarget`.

Run tests:

```bash
python3 -m unittest discover -s tests -v
```

To add a new organization, add one JSON object with `slots: 10` and an API key
environment variable name. Use `SALAD_FLEET_EXTRA_ORGS_JSON` to append to the
default orgs; use `SALAD_FLEET_ORGS_JSON` only when intentionally replacing the
whole org list. Do not put the API key value in git.

Example append:

```bash
export SALAD_FLEET_EXTRA_ORGS_JSON='[{"label":"kray4","slug":"kray4","api_key_env":"SALAD_API_KEY_KRAY4","slot_prefix":"prl-kray4-roi","slots":10,"enabled":true}]'
export SALAD_API_KEY_KRAY4=<private key in local shell or .env>
python3 scripts/config_loader.py --validate
python3 scripts/config_loader.py --check-secrets
```

The validator catches duplicate labels, slugs, slot prefixes, slot names,
non-positive slot counts, and missing key env vars when `--check-secrets` is
used.

Supervisor process plan:

```bash
python3 scripts/supervisor.py --print-plan
python3 scripts/supervisor.py --ensure
```

`--ensure` starts missing tmux sessions and restarts sessions only when their
heartbeat is stale. Use `--no-restart-stale` for a start-missing-only pass.

Optional DB maintenance process:

```bash
python3 scripts/maintenance.py
python3 scripts/maintenance.py --apply
python3 scripts/supervisor.py --print-plan --include-maintenance
python3 scripts/supervisor.py --ensure --include-maintenance --maintenance-apply
```

Maintenance prunes historical rows only. It does not delete organizations,
slots, targets, heartbeats, guard issues, runtime failures, workers, or cooldowns.

Read-only scheduler health:

```bash
python3 scripts/health.py
python3 scripts/health.py --json
```

API rate budget:

```bash
PRL_SALAD_API_MAX_REQUESTS_PER_MINUTE=120
```

`org_worker.py` and guard v2 throttle Salad API calls through SQLite by API key
environment variable. This matters because multiple orgs can share one API key
env var; for example `kray`, `kray2`, and `kray3` share one configured budget.
Set the value to `0` only for a local test where throttling must be disabled.

Fresh operator report with live snapshots:

```bash
python3 scripts/reporter.py --refresh --refresh-timeout 45
```

If live Salad/PearlFortune lookups are slow, the reporter returns the latest DB
state with `refresh_error=...` instead of blocking indefinitely.

## Controlled Live Test Path

Use this path when moving from shadow mode to live control.

The recommended entrypoint is `scripts/rollout.py`. It is read-only by default
unless `--apply-workers` or `--apply-guard` is passed.

1. DB-only smoke test:

   ```bash
   PRL_PEARL_FEE_RATE=0.01 python3 scripts/rollout.py --stage shadow --price 0.64 --fee 0.01 --skip-workers --skip-guard
   python3 scripts/shadow_compare.py
   ```

2. Shadow all orgs with live read-only worker observations:

   ```bash
   PRL_PEARL_FEE_RATE=0.01 python3 scripts/rollout.py --stage shadow --price 0.64 --fee 0.01 --require-secrets
   ```

   Rollout runs one scheduler pass, collects live read-only worker observations,
   then runs a second scheduler pass so protected running slots are reflected in
   the final target table. In one-shot mode, stale process heartbeats are
   warnings; add `--require-fresh-heartbeats` when validating under tmux
   supervision.

   Continuous read-only monitor loop:

   ```bash
   PRL_PEARL_FEE_RATE=0.01 python3 scripts/runtime_monitor.py --loop --interval 120 --price 0.64 --fee 0.01 --require-secrets
   ```

3. Apply one org only:

   ```bash
   PRL_PEARL_FEE_RATE=0.01 python3 scripts/rollout.py --stage one-org --org kry1 --price 0.64 --fee 0.01 --apply-workers --require-secrets
   ```

   Safer monitor-gated one-org apply:

   ```bash
   PRL_PEARL_FEE_RATE=0.01 python3 scripts/runtime_monitor.py --once --price 0.64 --fee 0.01 --require-secrets --apply-one-org --org kry1 --confirm-live-actions
   ```

   Live apply stages create a rollback checkpoint automatically before the
   scheduler writes new targets.

4. Enable guard v2 live actions only after the dry-run decisions look correct:

   ```bash
   python3 scripts/rollout.py --stage guard-apply --apply-guard --require-secrets
   ```

   Safer monitor-gated guard apply:

   ```bash
   PRL_PEARL_FEE_RATE=0.01 python3 scripts/runtime_monitor.py --once --price 0.64 --fee 0.01 --require-secrets --apply-guard --confirm-live-actions
   ```

5. Start tmux supervision for the full new stack:

   ```bash
   python3 scripts/supervisor.py --print-plan
   python3 scripts/supervisor.py --ensure
   ```

The live rule is staged: fill empty/stopped slots first, keep profitable hashing
slots protected, rotate no-hash after 60 seconds, rotate negative slots after 90
seconds, then optimize only after the enabled orgs are full or manually approved.

Protected running GPUs that are still positive but below the fill candidate
profit threshold are reported as shadow warnings, not rollout blockers. Negative
live slots are not preserved as scheduler targets; the scheduler emits a
profitable replacement target and the guard remains responsible for live
retarget/stop action after grace.

Full-org live worker apply requires an explicit confirmation flag:

```bash
PRL_PEARL_FEE_RATE=0.01 python3 scripts/rollout.py --stage all-orgs --price 0.64 --fee 0.01 --apply-workers --confirm-all-orgs --require-secrets
```

Rollback target state:

```bash
python3 scripts/rollback.py list
python3 scripts/rollback.py restore <checkpoint-id>
python3 scripts/rollback.py restore <checkpoint-id> --apply
```

Restoring a checkpoint only restores scheduler `slot_targets`. To make Salad
containers follow restored targets, run the relevant `org_worker.py --apply`
or controlled `rollout.py` command after reviewing the dry-run restore output.

Start fill mode:

```bash
PRL_FLEET_MODE=fill bash scripts/start_watchers.sh
PRL_FLEET_MODE=fill bash scripts/start_supervisor.sh
```

Generate a profit snapshot:

```bash
python3 scripts/salad_prl_profit_snapshot.py --price 0.64
```

## Mining, Pool, And Exchange

### What We Mine

The fleet mines PearlFortune PRL.

The miner runs inside SaladCloud GPU container groups. Each container downloads
the PearlFortune miner release and connects to the PearlFortune pool.

### Pool

Pool endpoint used by the miner:

```text
global.pearlfortune.org:443
```

Pool APIs used by the automation:

| Endpoint | Used for |
| --- | --- |
| `https://pearlfortune.org/api/v1/miners/<PRL_WALLET>/connections` | Maps live pool workers back to Salad slots and reads worker hashrate. |
| `https://pearlfortune.org/api/v1/stats/pool-fee-rate` | Reads pool fee so revenue uses net PRL, not gross PRL. |
| `https://pearlfortune.org/api/v1/summary?hours=24` | Calculates recent PRL per TH per day from hourly pool rewards and pool hashrate. |
| `https://pearlfortune.org/api/v1/market/price` | One public PRL/USD market price source. |

`PRL_WALLET` is required because the pool worker endpoint is wallet-scoped.
The public repo does not include the live wallet value.

### Miner

Current miner source:

```text
https://github.com/pearlfortune/pearl-miner/releases/tag/v.1.1.8
```

Current package downloaded by the container:

```text
pearlfortune-v1.1.8.tar.gz
```

Current binary:

```text
miner-cuda12
```

Current miner command shape:

```bash
miner-cuda12 --proxy global.pearlfortune.org:443 --address "$PRL_WALLET" --worker "$WORKER" -gpu
```

The container bootstrap installs basic Linux packages, downloads the miner
tarball, extracts it under `/opt/pearlfortune`, and restarts the miner loop if
the miner exits.

### Exchange / Price Source

SafeTrade is the exchange reference currently used for PRL/USDT price checks.

Public ticker endpoint:

```text
https://safe.trade/api/v2/peatio/public/markets/prlusdt/tickers
```

The code reads these SafeTrade fields:

```text
last
buy
sell
```

It uses the lowest positive SafeTrade value as the SafeTrade-side price. That
keeps the estimate conservative.

SafeTrade is not used for trading by these scripts. It is used for public price
data only.

### Price Selection

The live PRL price report uses two sources:

1. PearlFortune market price API.
2. SafeTrade PRL/USDT ticker.

When a live market price is needed, the code takes the lowest positive value
available across those sources.

During fill mode, live price does not control purchases. Fill mode uses a fixed
decision price:

```text
0.64 USD per PRL
```

During optimize mode, the current configured decision price is:

```text
0.62 USD per PRL
```

This avoids overbuying based on a short-lived live price spike.

## Main Components

### Watcher

One watcher runs per Salad org. It:

- Checks Salad GPU availability.
- Builds a ranked candidate list.
- Creates missing container groups.
- Starts stopped container groups.
- Rotates long-allocating slots to other profitable candidates.
- Protects live hashing workers during fill mode.
- Writes JSON logs for every decision.

The watcher uses the SaladCloud public API with the org slug in the URL and the
matching API key in the `Salad-Api-Key` header.

### Launcher

The launcher starts all watcher sessions and the guard session in tmux.

It sets:

- Fleet mode.
- Org mapping.
- Slot prefixes.
- API key env var names.
- Profit thresholds.
- Rotation timings.
- Miner version.

### Guard

The guard watches the profit snapshot and handles bad active slots.

It acts on:

- Running slots with no live pool hashrate.
- Slots with negative profit.
- Underperforming slots, but only during optimize mode.

In fill mode, the guard is intentionally conservative. It should not kill a
hashing GPU just because it is not ideal.

`scripts/guard.py` is the new guard path. By default it records decisions only.
Passing `--apply` is required before it patches, reallocates, or stops anything.
It persists guard issues in SQLite so a no-hash or negative slot must remain bad
past the grace window before action is taken.

### Profit Snapshot

The snapshot combines:

- Salad container state.
- Salad billable cost.
- PearlFortune pool workers.
- PRL market price.
- Pool net PRL per TH per day.

It calculates:

```text
revenue_day = TH * net_prl_per_th_day * decision_prl_price
cost_day    = Salad hourly GPU price * 24
profit_day  = revenue_day - cost_day
```

The net PRL per TH per day is derived from recent PearlFortune pool stats:

```text
gross_prl_per_th = hourly_pool_reward / (hourly_pool_hashrate / 1e12)
net_prl_per_th   = gross_prl_per_th * (1 - pool_fee_rate)
```

The pool worker list is matched to Salad slots through the worker name pattern.
If Salad shows a billable running slot but the matching PearlFortune worker is
missing or has zero fresh hashrate, the guard treats it as no-hash after the
grace period.

### Nonstop Supervisor

The supervisor keeps the tmux sessions alive. It also switches modes:

- `fill` while any org has fewer than 10 live hashing workers.
- `optimize` only when all target orgs have 10 live hashing workers.

The new `scripts/supervisor.py` keeps the scheduler stack alive: price oracle,
availability probe, scheduler, guard, and one org worker per enabled
organization. It uses DB heartbeats to avoid restart loops.

## Miner Runtime

Each Salad container uses an NVIDIA CUDA runtime image and runs the PearlFortune
miner.

Current miner release:

```text
pearl-miner v1.1.8
binary: miner-cuda12
proxy: global.pearlfortune.org:443
```

The miner is downloaded from the PearlFortune GitHub release at container start.
The automation does not bake a custom image; it uses a CUDA runtime image and
bootstraps the miner on startup.

The worker naming pattern includes:

```text
<org-prefix>-<slot-token>-pearlfortune-<hostname>
```

This lets the snapshot match a pool worker back to a Salad slot.

## Fill Mode

Fill mode is the default.

Use fill mode when there are empty, stopped, creating, or allocating slots.

Current fill settings:

| Setting | Value |
| --- | --- |
| Fleet mode | fill |
| Decision PRL price | 0.64 USD |
| Allowed Salad priorities | batch, low |
| Minimum candidate profit | 0.05 USD/day |
| Live upgrades | disabled |
| Underperform optimization | disabled |
| No-hash grace | 60 seconds |
| Guard poll interval | 15 seconds |
| Guard snapshot HTTP timeout | 4 seconds, 1 attempt |
| Negative-profit threshold | profit < 0 USD/day |
| Negative-profit grace | 90 seconds |
| Allocating rotation | 45 seconds |
| Poll interval | 15 seconds |
| No-GPU sleep trigger | only after 1 hour with no GPU found |
| No-GPU sleep duration | 15 minutes |

Why decision price is fixed:

PRL market price is volatile. The automation should not buy GPUs based only on a
short live spike. A fixed 0.64 USD decision price is used for fill mode even when
live price is higher.

## Optimize Mode

Optimize mode is for after the fleet is full.

Current optimize settings:

| Setting | Value |
| --- | --- |
| Fleet mode | optimize |
| Decision PRL price | 0.62 USD |
| Minimum candidate profit | 0.01 USD/day |
| Live upgrade interval | 300 seconds |
| Minimum upgrade delta | 0.25 USD/day |
| Underperform grace | 120 seconds |
| Underperform ratio | 85 percent of expected TH |
| Minimum TH deficit | 10 TH |

Optimize mode can replace active GPUs if the replacement is meaningfully better.
This is intentionally disabled during fill mode.

The new scheduler only emits an optimize upgrade target when the replacement is
at least `PRL_OPTIMIZE_MIN_UPGRADE_DELTA_USD_DAY` more profitable than the
observed running profile. The worker still refuses to patch running slots unless
`--allow-live-retarget` is explicitly passed, and rollout requires
`--confirm-live-retarget` with that flag.

Controlled optimize dry-run:

```bash
PRL_FLEET_MODE=optimize python3 scripts/fleet_scheduler.py --mode optimize --price 0.62
python3 scripts/rollout.py --stage one-org --org kry1 --price 0.62 --skip-workers --skip-guard
```

Controlled live optimize for one org:

```bash
PRL_FLEET_MODE=optimize python3 scripts/rollout.py --stage one-org --org kry1 --price 0.62 --apply-workers --allow-live-retarget --confirm-live-retarget --require-secrets
```

## Candidate Policy

Batch and low-priority GPUs are allowed in fill mode, but every candidate must
pass the same conservative profit check. Unknown-profit candidates are skipped
instead of started blind.

Common candidate classes:

```text
RTX 5090 batch
RTX 4090 batch
RTX 4080 batch
RTX 5070 Ti batch
RTX 4070 Ti batch
RTX 4070 Ti Super batch
RTX 5070 batch
RTX 5080 batch
RTX 5060 Ti batch
RTX 3060 Ti batch
RTX 3070 batch
RTX 3070 Ti batch
RTX 3090 batch
RTX 3080 batch
RTX 3080 Ti batch
```

L40S is intentionally ignored in the current plan.

## Operational Loop

### 1. Start In Fill Mode

```bash
PRL_FLEET_MODE=fill bash scripts/start_watchers.sh
PRL_FLEET_MODE=fill bash scripts/start_supervisor.sh
```

Expected tmux sessions:

```text
kray-prl-watch
kry1-prl-watch
kray2-prl-watch
kray3-prl-watch
kray-prl-guard
kray-prl-nonstop-supervisor
```

### 2. Confirm The Environment

Each watcher should show:

```text
PRL_FLEET_MODE=fill
PRL_WATCH_FIXED_DECISION_PRICE_USD=0.64
PRL_WATCH_MIN_PROFIT_USD_DAY=0.05
PRL_WATCH_ALLOWED_PRIORITIES=batch,low
```

The guard should show:

```text
PRL_FIXED_DECISION_PRICE_USD=0.64
PRL_NEGATIVE_SLOT_PROFIT_DAY=0
PRL_NEGATIVE_SLOT_GRACE_SECONDS=90
PRL_UNDERPERFORM_GRACE_SECONDS=999999
PRL_NOHASH_GRACE_SECONDS=60
```

### 3. Watch Slot State

Healthy fill mode states:

```text
live_protected
allocating
creating
rotated
running_without_pool, only briefly
```

Bad states:

```text
running_without_pool for more than 60 seconds
negative profit for more than 120 seconds
tick_failed repeating
API 401/403/404 for an org
```

### 4. Wait Long Enough For Hash

Do not stop a new running slot immediately. The miner needs time to install,
download, start, connect to the pool, and appear in pool stats.

Current rule:

```text
No-hash grace = 60 seconds
```

If it starts hashing before 60 seconds, keep it.

### 5. Rotate Allocating Slots

If a slot is allocating for too long and another profitable candidate is
available, rotate it.

Current rule:

```text
Allocating rotation = 45 seconds
```

When Salad has low GPU availability, many slots will stay allocating. This is
normal. The point is to keep all slots trying.

## Live Price Check

Use two PRL price sources:

```text
PearlFortune market price API
SafeTrade PRL/USDT ticker
```

Live price is useful for reporting current profit, but not for fill decisions.
Fill decisions use the fixed conservative price.

Source details:

| Source | Endpoint | Fields |
| --- | --- | --- |
| PearlFortune market | `https://pearlfortune.org/api/v1/market/price` | `data.price_usd` |
| SafeTrade PRL/USDT | `https://safe.trade/api/v2/peatio/public/markets/prlusdt/tickers` | `ticker.last`, `ticker.buy`, `ticker.sell` |

The reported live price uses the lowest positive available value from the source
set, so it is conservative when sources disagree.

Example live check output from 2026-06-24:

```text
PearlFortune price: about 0.689 USD
SafeTrade last/sell: about 0.69 USD
SafeTrade buy: about 0.70 USD
```

## Example Status Snapshot

Example snapshot from 2026-06-24 after adding `kry1` correctly:

```text
Target slots: 40
Active or pending slots: 40
Live GPUs: 9
Hashrate: about 1059 TH
No-hash billable slots: 0
Net profit at conservative 0.64 USD PRL: about 3.03 USD/day
Net profit at live 0.689 USD PRL: about 4.95 USD/day
```

This is a fill-stage snapshot, not the optimized final state.

## Troubleshooting

### API returns 404 for an org

The value used in the URL must be the Salad organization slug, not a user id or
API token.

Correct:

```text
/organizations/kry1/...
```

Wrong:

```text
/organizations/<api-key-or-user-id>/...
```

### API returns 401 or 403

The API key is missing, invalid, blocked, or does not have access to that org.

Check:

```text
org slug
API key env var name
key-to-org mapping
Cloudflare or API access errors
```

### Container start returns Pending error

This can happen immediately after creating a container group:

```text
Starting a container group is not allowed while in Pending status.
```

Do not panic. The next watcher tick should start it after Salad moves the group
out of Pending.

### Profit is temporarily negative during fill

This usually happens when a billable container is running but has not appeared in
pool stats yet.

The guard waits 60 seconds before acting. If the miner appears in pool stats,
the slot is kept. If it stays no-hash, the guard retargets it to the next
profitable candidate. If no profitable replacement is available, the guard stops
the slot instead of reallocating the same unprofitable GPU.

The same rule applies to slots that stay negative past the negative-profit
grace window: retarget first, stop if no profitable replacement is available.

### Everything feels slow

Each cycle checks Salad availability across many GPU classes and multiple orgs.
The runtime uses a 15 second poll interval, 45 second allocating rotation, and
shorter HTTP timeout so it can catch newly available GPUs faster during scarce
availability. API timeouts and scarce availability can still make a full cycle
take longer than the nominal poll interval.

## Public Repo Boundary

This repository should remain safe to keep public.

Allowed:

```text
runbook
thresholds
strategy
public org labels
non-secret env var names
general command shapes
```

Not allowed:

```text
API key values
cookies
auth headers
Cloudflare clearance values
raw private logs
private wallet/control credentials
```

If source scripts are added later, sanitize them first and review every diff for
secret values before pushing.
