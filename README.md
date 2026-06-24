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
| `.env.example` | Safe template for local secrets and runtime settings. |

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

Start fill mode:

```bash
PRL_FLEET_MODE=fill bash scripts/start_watchers.sh
PRL_FLEET_MODE=fill bash scripts/start_supervisor.sh
```

Generate a profit snapshot:

```bash
python3 scripts/salad_prl_profit_snapshot.py --price 0.64
```

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

### Nonstop Supervisor

The supervisor keeps the tmux sessions alive. It also switches modes:

- `fill` while any org has fewer than 10 live hashing workers.
- `optimize` only when all target orgs have 10 live hashing workers.

## Miner Runtime

Each Salad container uses an NVIDIA CUDA runtime image and runs the PearlFortune
miner.

Current miner release:

```text
pearl-miner v1.1.8
binary: miner-cuda12
proxy: global.pearlfortune.org:443
```

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
| Allowed Salad priorities | batch |
| Minimum candidate profit | 0 USD/day |
| Live upgrades | disabled |
| Underperform optimization | disabled |
| No-hash grace | 60 seconds |
| Negative-profit threshold | profit < 0 USD/day |
| Negative-profit grace | 120 seconds |
| Allocating rotation | 90 seconds |
| Poll interval | 30 seconds |
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

## Candidate Policy

Only batch-priority GPUs are used in the current public strategy.

Low-priority profiles are blocked for now because the goal is to get stable
profitable GPUs first and optimize later.

Blocked low profiles:

```text
4080:low
4070tis:low
5070:low
5090:low
```

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
PRL_FLEET_MODE=fill bash start-watchers.sh
PRL_FLEET_MODE=fill bash start-supervisor.sh
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
PRL_WATCH_MIN_PROFIT_USD_DAY=0
PRL_WATCH_ALLOWED_PRIORITIES=batch
```

The guard should show:

```text
PRL_FIXED_DECISION_PRICE_USD=0.64
PRL_NEGATIVE_SLOT_PROFIT_DAY=0
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
Allocating rotation = 90 seconds
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
the slot is kept. If it stays no-hash, the guard stops or reallocates it.

### Everything feels slow

Each cycle checks Salad availability across many GPU classes and multiple orgs.
API timeouts and scarce availability can make one full cycle take 30 to 90
seconds. This is expected during low availability.

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
