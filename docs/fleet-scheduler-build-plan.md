# Fleet Scheduler Build Plan

Reader: a future Codex/GSD agent that must build the next Salad PRL scaling system without relying on private chat history.

Post-read action: implement, verify, and operate a deterministic fleet scheduler that scales by adding Salad organizations, where each enabled organization contributes a fixed set of 10 GPU slots.

## Goal

Build infrastructure that keeps SaladCloud GPU slots full of profitable PearlFortune PRL miners while handling volatile PRL price, scarce GPU availability, and many organizations.

The target behavior is:

1. Fill all available funded slots with GPUs that are expected to be profitable under the current risk policy.
2. Prefer fast live hashing capacity over perfect theoretical profit while the fleet is underfilled.
3. Protect every live hashing GPU that is profitable under the active policy.
4. Rotate or stop billable no-hash slots after grace.
5. Rotate or stop live negative slots after grace.
6. Delay long no-GPU sleep until a slot has failed to find GPUs for one hour.
7. Once the fleet is full or near-full, optimize toward higher-profit GPUs without causing churn.

The runtime should be deterministic Python code. LLMs and subagents are for build, audit, and debugging, not for live control decisions.

## Organization Scaling Model

The system does not assume a fixed final GPU count.

Current shape:

```text
4 organizations * 10 slots = 40 target slots
```

Growth shape:

```text
N organizations * 10 slots = total target slots
```

Can can add more organizations over time if the system proves profitable and stable. The scheduler must therefore make onboarding a new organization mostly configuration-driven:

1. Add org slug.
2. Add public label.
3. Add API key environment variable name.
4. Add slot prefix.
5. Add funding/balance monitoring if available.
6. Start one worker process for that org.

The scheduler should treat every organization as a 10-slot unit by default, but should not hardcode a maximum number of organizations.

## Non-Goals

- Do not automate browser cookie flows for normal runtime control.
- Do not store secrets in source, logs, docs, or database dumps.
- Do not use an LLM as the live scheduler.
- Do not optimize active profitable GPUs before the fleet has enough live coverage.
- Do not chase one live PRL price tick without trailing-price confirmation.

## Current Baseline

The current repository already has:

| Component | Current role |
| --- | --- |
| Per-org watcher | Creates, starts, patches, rotates, and protects slots. |
| Profit snapshot | Calculates cost, revenue, no-hash, and profit. |
| Guard | Handles no-hash, negative, and underperforming slots. |
| Supervisor | Keeps tmux sessions alive. |
| Candidate ranker | Ranks profile profit at a chosen PRL price. |
| Public runbook | Documents current fill-first operations. |

The next version should keep the working pieces but replace independent per-org target selection with a central scheduler.

## Architecture

Use one central scheduler with per-organization workers.

```text
price_oracle
  -> writes price history and risk mode

profit_model
  -> computes expected profit per GPU profile

profile_scorer
  -> ranks profiles by profit, availability, time-to-hash, and failure history

fleet_scheduler
  -> assigns target profiles to every org/slot

org_worker per organization
  -> executes assigned target for that org

guard
  -> protects profit, clears no-hash, rotates negative slots

supervisor
  -> keeps all processes alive

reporter
  -> summarizes status and problems

state_db
  -> stores shared state, history, and decisions
```

The scheduler decides. Workers execute. The guard enforces safety.

## Runtime Processes

Expected long-running processes:

| Process | Count | Responsibility |
| --- | ---: | --- |
| `price_oracle.py` | 1 | Track PRL price, trailing windows, and risk mode. |
| `fleet_scheduler.py` | 1 | Assign target GPU profiles across all slots. |
| `org_worker.py` | 1 per org | Execute Salad create/start/patch/reallocate actions. |
| `guard.py` | 1 | Enforce no-hash, negative, and underperform rules. |
| `supervisor.py` | 1 | Restart stale processes and preserve nonstop operation. |
| `reporter.py` | 1 optional | Emit CLI/Discord/status summaries. |

The current watcher can be refactored into `org_worker.py` instead of rewritten from scratch.

## Required Modules And Scripts

### `state_db.py`

Owns SQLite schema, migrations, and typed helper functions.

Tables:

| Table | Purpose |
| --- | --- |
| `organizations` | Org slug, public label, key env var, slot count, enabled flag. |
| `slots` | Desired and observed state for each slot. |
| `gpu_profiles` | Static profile metadata: GPU key, priority, memory, expected TH. |
| `profile_prices` | Salad hourly prices per profile/priority. |
| `price_history` | PearlFortune and SafeTrade price samples. |
| `risk_modes` | Current mode and thresholds used by scheduler. |
| `slot_targets` | Scheduler output: desired profile per slot. |
| `attempts` | Every create/patch/start/reallocate attempt and result. |
| `workers` | Pool workers mapped to org/slot/instance. |
| `profit_snapshots` | Fleet and per-slot profit estimates. |
| `profile_scores` | Profile score over time. |
| `heartbeats` | Runtime health for each process. |
| `events` | Structured operational events. |

Database rules:

- No secrets.
- Store env var names, not values.
- WAL mode enabled.
- Every process writes heartbeat.
- Every state mutation records an event.

### `config_loader.py`

Loads org definitions and runtime knobs from env/config.

Configuration must support:

- multiple orgs
- per-org API key env var
- per-org slot prefix
- per-org target slot count
- enabled/disabled orgs
- default fleet mode
- risk thresholds
- miner version

Example public shape:

```yaml
organizations:
  - label: kray
    slug: kray
    api_key_env: SALAD_API_KEY_2
    slot_prefix: prl-kray-roi
    slots: 10
    enabled: true
```

The actual API key value stays in `.env`.

### `price_oracle.py`

Samples price from:

- PearlFortune market API
- SafeTrade PRL/USDT ticker

Stores:

- current price
- current bid/sell if available
- trailing min 15m
- trailing min 30m
- trailing min 1h
- trailing average 30m
- trailing average 1h
- source spread
- stale/error state

Price policy:

```text
base decision price = 0.64
optimize decision price = 0.60 to 0.62
boost decision price = min(trailing_min_30m, current_bid_or_market) - 0.02
risk-off trigger = trailing_min_15m < 0.68
```

Do not use one instant price tick as a buy signal.

### `profit_model.py`

Computes expected and observed profit.

Formula:

```text
revenue_day = th * prl_per_th_day * decision_price * (1 - pearl_fee)
cost_day    = salad_hourly_price * 24
profit_day  = revenue_day - cost_day
```

Use a conservative Pearl fee of `5%` for decision ranking unless explicitly configured otherwise.

The model must output:

- expected profile profit
- observed live slot profit
- break-even PRL price per profile
- minimum safe price per profile
- margin at base price
- margin at boost price
- margin at live price

### `profile_scorer.py`

Ranks GPU profiles using both theoretical profit and real execution history.

Inputs:

- expected profit
- Salad availability
- recent patch/start success
- time-to-hash after start
- no-hash rate
- negative-after-start rate
- current active/pending reservation count
- priority type
- risk tier

Suggested score:

```text
score =
  expected_profit_weight
  + live_success_rate_weight
  + fast_hash_weight
  + availability_weight
  - no_hash_penalty
  - negative_penalty
  - repeated_capacity_failure_penalty
  - over_target_penalty
```

Important behavior:

- A highly profitable GPU that never allocates should be temporarily penalized.
- A lower-profit GPU that reliably reaches live hash should rise during fill mode.
- Profiles with repeated no-hash should cool down before being retried.

### `fleet_scheduler.py`

The central decision maker.

Responsibilities:

1. Read all organizations, slots, profile scores, and risk mode.
2. Generate target profiles for every slot.
3. Spread demand across profitable profiles instead of making every slot chase one GPU.
4. Reserve scarce candidates globally across organizations.
5. Write `slot_targets`.
6. Avoid touching live protected slots unless guard or optimize policy requests it.

Scheduling modes:

| Mode | Purpose |
| --- | --- |
| `base_fill` | Use safe profiles profitable at base price. |
| `boost_fill` | Add profiles profitable under trailing high price. |
| `risk_off` | Stop adding risky profiles; guard existing risky slots. |
| `optimize` | Replace weaker live GPUs after full/near-full. |
| `maintenance` | Restart stale workers and repair drift. |

Target assignment rules:

- Underfilled orgs get first priority.
- Empty/stopped slots get targets before live slots.
- Allocating slots rotate after timeout.
- Creating slots rotate if no progress after timeout.
- `running_without_pool` waits no-hash grace, then rotates.
- Live profitable slots are protected in fill mode.
- Target selection should be diversified by org and slot index.

### `org_worker.py`

Executes target state for one organization.

Responsibilities:

- read `slot_targets` for its org
- query Salad container state
- query/refresh GPU price catalog
- create missing container group
- patch wrong target
- start stopped target
- reallocate pending/running instances after target changes
- record every action in `attempts`
- write heartbeat

It should not independently choose a GPU profile except as a temporary fallback when scheduler is stale.

### `guard.py`

Global safety controller.

Rules:

| Condition | Action |
| --- | --- |
| billable no-hash after 60s | retarget to next profitable profile; stop if none exists |
| fleet profit negative because of no-hash | retarget no-hash with priority |
| live slot negative after 90s | retarget/stop |
| profile becomes risky after price drops | mark risky; rotate only after configured persistence |
| underperforming live slot in optimize mode | replace if better profile is available |
| underperforming live slot in fill mode | keep if profitable |

Guard must prefer retargeting before stopping when a profitable replacement exists.

### `supervisor.py`

Keeps the system alive.

Responsibilities:

- ensure price oracle, scheduler, guard, and org workers are running
- restart stale processes
- preserve no-GPU timers across restarts
- avoid restart loops
- write heartbeat
- expose a simple health report

No-GPU behavior:

```text
do not sleep on short no-GPU periods
if a slot/profile search has failed for 3600 seconds, sleep that search for 900 seconds
continue other orgs/slots while one search sleeps
```

### `reporter.py`

Produces operator-readable status.

Required output:

- total target slots
- active/pending slots
- live hashing GPUs
- live TH
- no-hash billable slots
- negative slots
- profit at 0.64
- profit at 0.70
- profit at live price
- risky profiles
- top profile scores
- stuck slots
- process heartbeat status

Output targets:

- CLI JSON
- CLI table
- optional Discord/Nicolas integration

## GPU Risk Tiers

Use conservative 5% Pearl fee for tiers.

At PRL `0.64`, safe batch profiles are currently:

```text
4090 batch
5090 batch
4080 batch
4070 Ti batch
5070 Ti batch
5070 batch
4070 Ti Super batch
5090 Laptop batch
3070 batch
3060 Ti batch
5060 Ti batch
3070 Ti batch
3090 batch, only as near-break-even fallback
```

At PRL `0.70`, additional batch profiles become profitable:

```text
5080 batch
3080 Ti batch
3080 batch
```

At PRL `0.70`, low-priority profiles are still marginal:

```text
4080 low, tiny margin
5090 low, near break-even
```

Default policy:

- use batch profiles first
- block low profiles in base fill
- allow marginal low profiles only in controlled boost mode
- never allow profiles that are negative under the active decision price

## Volatile Price Strategy

Do not use current live price alone.

Use tiers:

```text
base_fill:
  decision price = 0.64
  allowed profiles = safe at 0.64 with 5% fee

boost_fill:
  enabled when trailing_min_30m >= 0.70
  decision price = min(trailing_min_30m, current bid/market) - 0.02
  allowed profiles = base + profiles profitable at boost price

aggressive_boost:
  enabled when trailing_min_1h >= 0.72
  can include marginal profiles with explicit cap

risk_off:
  enabled when trailing_min_15m < 0.68
  block new risky starts
  let guard rotate risky negative slots after persistence
```

Hysteresis:

- require sustained high price before expanding allowed profiles
- require sustained lower price before cutting live risky slots
- do not flip modes on one sample

## Subagents

Subagents should not control the live fleet.

Use subagents for:

- code audit
- profit model review
- Salad API behavior investigation
- historical success-rate analysis
- security and secret review
- documentation cold-read

Do not use subagents for:

- deciding live buys every tick
- holding API keys
- issuing live stop/start actions independently
- running unreviewed shell commands against production

The production control plane must be deterministic code.

## Implementation Roadmap

### Phase 1: State Foundation

Deliver:

- SQLite database module
- migrations
- org/profile config loader
- heartbeat/events helpers
- no secrets persisted

Acceptance:

- database initializes from empty state
- orgs and profiles are loaded
- heartbeat writes and expires correctly
- secret scan passes

### Phase 2: Price Oracle And Profit Model

Deliver:

- price sampling loop
- trailing windows
- conservative 5% fee support
- break-even profile table
- profit model tests

Acceptance:

- reports base/boost/risk-off mode from price history
- correctly classifies profiles at `0.64` and `0.70`
- handles API failures without crashing

### Phase 3: Profile Scoring

Deliver:

- profile score table
- success/failure counters
- time-to-hash metrics
- cooldown/penalty logic

Acceptance:

- repeated unavailable profiles are penalized
- fast successful profiles are promoted during fill
- score changes are explainable from stored events

### Phase 4: Central Scheduler

Deliver:

- central target assignment
- per-org/slot diversification
- global reservation logic
- target persistence in DB
- dry-run mode

Acceptance:

- 40 slots do not all target the same profile
- underfilled orgs receive targets first
- live protected slots are not retargeted in fill mode
- dry-run explains every target decision

### Phase 5: Per-Org Worker Refactor

Deliver:

- one worker process per org
- worker consumes scheduler targets
- existing create/start/patch/reallocate behavior preserved
- action results stored in DB

Acceptance:

- worker can run for one org in isolation
- worker can create and start stopped slots
- worker can patch/reallocate wrong target
- no live profitable slot is changed unless policy allows it

### Phase 6: Global Guard

Deliver:

- no-hash enforcement
- negative slot enforcement
- risk-off enforcement
- underperform optimization only in optimize mode
- stop-if-no-profitable-replacement behavior

Acceptance:

- billable no-hash is cleared after grace
- negative live slots are cleared after grace
- fill-mode profitable live slots are protected
- guard decisions are recorded and explainable

### Phase 7: Supervisor And Reporter

Deliver:

- process supervisor
- persistent no-GPU timers
- status reporter
- health checks
- optional Discord/Nicolas output

Acceptance:

- killing a worker causes controlled restart
- stale heartbeat triggers restart
- no-GPU sleep starts only after one hour
- reporter shows slot/profit/health summary

### Phase 8: Shadow Mode

Run new scheduler without controlling production.

Deliver:

- scheduler dry-run targets beside current watcher targets
- comparison report
- mismatch analysis

Acceptance:

- shadow targets are more diversified than current watcher targets
- no unsafe target appears
- profile tier decisions match expected risk mode

### Phase 9: Controlled Rollout

Roll out one org at a time.

Order:

1. one low-risk org
2. all current orgs
3. additional orgs as keys/funds are added

Acceptance:

- each org maintains or improves live profitable GPU count
- no-hash and negative slots are cleared
- process restarts are handled
- rollback path works

### Phase 10: Multi-Organization Scale Readiness

Prepare for repeatedly adding new 10-slot organizations when the current system is profitable and stable.

Deliver:

- config-driven org onboarding
- rate-limit budget per API key
- DB compaction/retention
- summary dashboards
- runbook updates

Acceptance:

- adding a 10-slot org is config-only except for providing its private API key in the runtime environment
- scheduler spreads targets across old and new orgs without all orgs chasing the same profile
- API backoff prevents request storms
- status remains readable as org count grows

## Verification Gates

Every implementation phase must run:

```bash
python3 -m compileall scripts
git diff --check
rg -n 'salad_cloud_user_|cf_clearance|Cookie:|Authorization: Bearer|SALAD_API_KEY_.*=salad|PRL_WALLET=prl[[:alnum:]]{20,}' .
```

Runtime verification must include:

```text
process health
fresh heartbeats
latest guard snapshot
profit at base decision price
no-hash slots
negative slots
stuck slots
```

Do not claim completion without fresh command output.

## Public Safety Boundary

Allowed in repo:

- architecture
- thresholds
- public endpoints
- env var names
- sanitized examples
- code that reads secrets from env

Never commit:

- API key values
- cookies
- Cloudflare clearance values
- bearer tokens
- private wallet/control credentials
- `.env`
- raw private logs

## Suggested `/goal` Prompt

Use this as the next build goal:

```text
Build the next-generation Salad PRL fleet scheduler described in docs/fleet-scheduler-build-plan.md.

Keep the existing watcher/guard behavior working while refactoring toward:
- SQLite shared state
- price oracle with trailing windows and 5% Pearl fee
- deterministic profit model
- profile scorer using real success/no-hash/time-to-hash history
- central fleet scheduler that assigns diversified per-slot targets across orgs
- per-org workers that execute scheduler targets
- global guard enforcing no-hash, negative, risk-off, and optimize rules
- supervisor and reporter

Work in phases. After each phase, verify with tests or fresh runtime checks, update docs, avoid secrets, and keep the live fleet profitable. Do not mark complete until every phase and acceptance gate in docs/fleet-scheduler-build-plan.md is satisfied.
```

## Suggested Ongoing Runtime `/goal`

After the infrastructure exists and is deployed, use a separate nonstop runtime/debug goal:

```text
Continuously monitor and debug the Salad PRL fleet scheduler. Keep all funded slots trying to become live profitable GPUs, protect profitable live workers, rotate no-hash or negative slots after grace, verify profit at base and live prices, and only sleep no-GPU searches after one hour of unsuccessful attempts. Do not stop when the fleet is full; switch to optimize mode and improve profit while keeping the fleet safe.
```

## Open Decisions

These can be decided during implementation:

- exact SQLite migration framework
- whether reporter outputs to Nicolas/Discord immediately or later
- exact near-full threshold for entering optimize mode
- whether boost mode can use marginal low-priority profiles
- retention period for detailed event history

Default conservative answers:

- use plain SQL migrations
- build CLI reporter first
- optimize only after all enabled orgs are full or manually approved
- keep low-priority profiles blocked except explicit boost experiments
- retain detailed events for at least 7 days
