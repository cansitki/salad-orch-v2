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

Current implementation note:

- Default config is still `4 organizations * 10 slots = 40 target slots`.
- Scaling is config-driven; adding an org means adding another 10-slot org definition.
- Evaluate scale by slot coverage per org, not by assuming 1000 GPUs are already available.

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

availability_probe
  -> writes per-org/profile Salad availability and no-GPU cooldown state

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
| `availability_probe.py` | 1 | Track Salad GPU availability and no-GPU cooldowns. |
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
| `runtime_failures` | Last failure per component, sanitized and operator-visible. |
| `guard_issues` | Persistent no-hash/negative issue state with first-seen grace tracking. |
| `api_rate_limits` | Per API key env request budget shared by org workers and guard. |
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

Current onboarding behavior:

- default orgs are preserved unless `SALAD_FLEET_ORGS_JSON` explicitly replaces the whole list
- `SALAD_FLEET_EXTRA_ORGS_JSON` appends new 10-slot organizations to the default/current list
- `scripts/config_loader.py --validate` checks duplicate labels, slugs, slot prefixes, slot names, and invalid slot counts
- `scripts/config_loader.py --check-secrets` verifies required env vars exist without printing values

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

Current fee override:

- Normal conservative default remains `PRL_PEARL_FEE_RATE=0.05`.
- For the current next-24h low-fee window, run scheduler commands with `PRL_PEARL_FEE_RATE=0.01` or set `PRL_TEMP_PEARL_FEE_RATE=0.01` plus `PRL_TEMP_PEARL_FEE_UNTIL_UTC`.
- Keep the fee explicit in command output and DB risk mode so reports show which assumption produced each target set.

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
- In optimize mode, live protected slots are retargeted only when the best eligible replacement clears the configured upgrade delta.
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

Current API budget behavior:

- every Salad API request made through `org_worker.py` is throttled by `api_key_env`
- orgs sharing one API key env share one SQLite budget window
- `PRL_SALAD_API_MAX_REQUESTS_PER_MINUTE` controls the per-key budget
- `0` disables throttling for local tests only

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

Current implementation behavior:

- `scripts/guard.py --once` analyzes and records decisions without live Salad actions.
- `scripts/guard.py --once --apply` can patch/reallocate or stop slots after grace.
- no-hash grace is 60 seconds.
- negative grace is 90 seconds.
- decisions are persisted to `guard_issues`, `attempts`, and `events`.
- runtime failures are persisted to `runtime_failures`.
- guard v2 live actions use the same per-key API budget as org workers.
- `--apply-legacy` remains available for the old guard path.

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

### `health.py`

Read-only status surface for runtime supervision.

Required output:

- overall health: `healthy`, `degraded`, or `down`
- target coverage
- stale heartbeats
- runtime failures
- active guard issues
- latest risk mode and price sample
- slot status counts
- API rate-limit windows

This script must not call live Salad APIs. It reads the scheduler DB only, so it
can be used frequently by `/goal` supervision without triggering API churn.

### `shadow_compare.py`

Read-only shadow-mode target validation.

Responsibilities:

- compare scheduler targets against observed slot state
- flag missing targets and targets for unknown slots
- flag unsafe targets: missing profile score, blocked risk tier, or below minimum profit
- warn on protected running mismatches outside optimize mode
- warn, but do not block, protected running fill-mode slots that are positive but below the new-candidate minimum profit
- report target diversification so one profile does not dominate the entire fleet

Protected running slots with negative expected profit must not be preserved as
fill targets. The scheduler should assign a profitable replacement target, while
guard/live rollout authority controls when the active negative slot is actually
retargeted or stopped.

This script must not call live Salad APIs. It reads the scheduler DB only.

### `rollout.py`

Controlled rollout runner for live testing.

Responsibilities:

- run scheduler target assignment
- optionally run org workers in shadow or apply mode
- rerun scheduler target assignment after org worker observations so protected running slots are reconciled
- optionally run guard v2 in dry-run or apply mode
- collect reporter and health output
- enforce safety gates before the operator expands scope
- require explicit all-org confirmation before full live worker apply

Default behavior must stay non-destructive. Live actions require
`--apply-workers` or `--apply-guard`.

Live apply stages create a rollout checkpoint before scheduler targets are
rewritten.

One-shot rollout commands treat stale process heartbeats as warnings by default
because a full read-only pass can exceed a component heartbeat TTL while it is
still progressing. Use `--require-fresh-heartbeats` for supervised runtime gates.

### `runtime_monitor.py`

Safe ongoing runtime monitor for `/goal` supervision.

Responsibilities:

- repeatedly run read-only shadow rollout gates
- summarize target coverage, health, live hashing count, no-hash, negative, and stuck slots
- skip live actions when shadow gates fail
- require `--confirm-live-actions` before any live apply path
- allow only one live action per tick so guard apply and worker apply do not collide
- allow explicit degraded preflight retries with `--allow-degraded-shadow` while keeping the final live action gate strict
- enforce `--runner-timeout-seconds` around each rollout stage so slow Salad API reads fail the tick instead of hanging the monitor
- when a rollout runner fails or times out, fall back to read-only DB status from `reporter.py` and `health.py` so the monitor still reports target coverage, health, live hashing, no-hash, negative, and stuck counts

Default behavior is read-only. Live action modes are:

- `--apply-guard --confirm-live-actions`
- `--apply-one-org --org <label> --confirm-live-actions`
- add `--allow-pending-retarget --pending-retarget-after-seconds N` to the one-org path when stale creating/allocating mismatches should rotate

### `rollback.py`

Rollback helper for controlled rollout.

Responsibilities:

- create a checkpoint of current scheduler `slot_targets`
- list recent rollout checkpoints
- restore `slot_targets` from a checkpoint in dry-run mode by default
- require `--apply` before writing restored targets

This script restores target state only. Live Salad containers follow restored
targets only after the operator runs org workers/rollout with explicit apply.

### `maintenance.py`

Long-running state retention and compaction helper.

Responsibilities:

- prune old historical rows from events, attempts, profit snapshots, price history, risk modes, and availability samples
- keep operational state tables intact
- run in dry-run mode by default
- optionally loop under tmux/supervisor
- optionally run `VACUUM` after applied pruning

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
- current implementation requires at least 5 samples and `PRL_BOOST_MIN_WINDOW_SECONDS` of confirmed history before `boost_fill`

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

Implemented files:

- `scripts/state_db.py`
- `scripts/config_loader.py`
- `scripts/fleet_common.py`

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

Implemented files:

- `scripts/price_oracle.py`
- `scripts/profit_model.py`

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

Implemented file:

- `scripts/profile_scorer.py`

Current behavior:

- stores `profile_scores.reason_json` with expected profit, success/failure counts, availability, live hash sample rate, no-hash sample rate, negative sample rate, average observed TH, and average time-to-hash when available
- derives profile keys from historical slot profit snapshots that only stored GPU/priority payloads, so older snapshots still contribute to scoring
- future guard slot snapshots persist `profile_key` directly for cleaner profile history
- penalizes profiles with repeated capacity failures and rate-based no-hash/negative history; rewards profiles with live hash samples and faster time-to-hash

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

Implemented file:

- `scripts/fleet_scheduler.py`

Safe default behavior:

- scheduler writes DB targets only; it does not call Salad APIs directly
- default base fill allows `batch` only
- `low` is available for boost/optimize modes only if profitable under the active fee and decision price
- optimize replacement requires `PRL_OPTIMIZE_MIN_UPGRADE_DELTA_USD_DAY` profit/day improvement
- profile assignment is diversified across the top eligible profiles instead of sending every slot to one GPU
- recent availability data caps per-org/profile assignments when present
- active no-GPU cooldowns prevent retrying a profile until the cooldown expires
- protected running slots keep their observed profile in fill mode; optimize mode can assign an upgrade target but live patching still requires worker/rollout retarget flags

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

Implemented file:

- `scripts/org_worker.py`

Safe default behavior:

- no live changes unless `--apply` is passed
- running slots are protected unless `--allow-live-retarget` is passed
- creating/allocating slots are protected unless `--allow-pending-retarget` is passed
- pending retargets still wait for `--pending-retarget-after-seconds` before patching stale creating/allocating profile mismatches
- this lets the new worker shadow existing runtime without churn
- every worker tick writes observed slot status/profile/protection state into `slots`
- `scripts/rollout.py` requires `--confirm-live-retarget` before passing live retarget authority to workers

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

Implemented file:

- `scripts/guard.py`

Current status:

- default mode analyzes and records guard issues without live actions
- `--apply` performs guard v2 retarget/stop actions after persistent grace
- `--apply-legacy` still delegates to the existing `scripts/salad_prl_guard.py`
- guard v2 records attempts, events, guard issues, and runtime failures
- successful guard snapshots persist per-slot hashrate into `slots`, persist live pool workers into `workers`, and mark workers missing from the latest snapshot as stale

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

Implemented files:

- `scripts/supervisor.py`
- `scripts/reporter.py`
- `scripts/health.py`
- `scripts/rollout.py`
- `scripts/runtime_monitor.py`

Current behavior:

- `scripts/supervisor.py --print-plan` includes price oracle, availability probe, scheduler, guard, and one worker per enabled org
- `scripts/supervisor.py --ensure` starts missing tmux sessions and restarts sessions with stale heartbeats
- `scripts/reporter.py --refresh` records fresh guard snapshots at `0.64` and `0.70`
- `scripts/reporter.py` reports live TH/hash count from fresh mapped `workers`, then `slots.live_hashrate_th`, and finally the latest per-slot `profit_snapshots` batch when newer runtime rows are unavailable
- `scripts/reporter.py` derives `profit_at_0.64` and `profit_at_0.70` from the latest fleet snapshot's PRL/day and cost when a stored scenario snapshot for that price is missing
- `scripts/reporter.py --refresh --refresh-timeout N` fails fast and reports stale DB data with `refresh_error` if live APIs hang
- `scripts/health.py --json` shows target coverage, stale heartbeats, runtime failures, and active guard issues from SQLite
- `scripts/shadow_compare.py --json` reports missing targets, unsafe targets, target/observed mismatches, and diversification
- `scripts/rollout.py` provides DB-only smoke, shadow, one-org apply, full-org apply with confirmation, and guard apply gates
- `scripts/runtime_monitor.py --loop` repeatedly runs shadow gates, reports DB fallback status on runner timeout/error, and can perform one explicitly confirmed live action after a passing gate
- `scripts/rollback.py` provides checkpoint list/restore for scheduler targets

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

Current shadow-mode commands:

```bash
PRL_PEARL_FEE_RATE=0.01 python3 scripts/rollout.py --stage shadow --price 0.64 --fee 0.01 --skip-workers --skip-guard
python3 scripts/shadow_compare.py
PRL_PEARL_FEE_RATE=0.01 python3 scripts/rollout.py --stage shadow --price 0.64 --fee 0.01 --require-secrets
PRL_PEARL_FEE_RATE=0.01 python3 scripts/runtime_monitor.py --loop --interval 120 --runner-timeout-seconds 90 --price 0.64 --fee 0.01 --require-secrets
```

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
- rollback path works through automatic rollout checkpoints and `scripts/rollback.py restore`

Recommended live sequence:

```bash
PRL_PEARL_FEE_RATE=0.01 python3 scripts/rollout.py --stage one-org --org kry1 --price 0.64 --fee 0.01 --apply-workers --require-secrets
PRL_PEARL_FEE_RATE=0.01 python3 scripts/runtime_monitor.py --once --runner-timeout-seconds 90 --price 0.64 --fee 0.01 --require-secrets --apply-one-org --org kry1 --confirm-live-actions
python3 scripts/rollback.py list
```

Only after the one-org apply is stable:

```bash
PRL_PEARL_FEE_RATE=0.01 python3 scripts/runtime_monitor.py --once --runner-timeout-seconds 90 --price 0.64 --fee 0.01 --require-secrets --apply-guard --confirm-live-actions
PRL_PEARL_FEE_RATE=0.01 python3 scripts/runtime_monitor.py --once --runner-timeout-seconds 90 --price 0.64 --fee 0.01 --require-secrets --apply-guard --confirm-live-actions --allow-degraded-shadow
python3 scripts/rollout.py --stage guard-apply --apply-guard --require-secrets
PRL_PEARL_FEE_RATE=0.01 python3 scripts/rollout.py --stage all-orgs --price 0.64 --fee 0.01 --apply-workers --confirm-all-orgs --require-secrets
python3 scripts/supervisor.py --print-plan
python3 scripts/supervisor.py --ensure
```

During the first live test, Codex should supervise health, reporter output,
guard decisions, and org-worker attempts before enabling all orgs with apply.

Rollback if one-org apply is bad:

```bash
python3 scripts/rollback.py restore <checkpoint-id>
python3 scripts/rollback.py restore <checkpoint-id> --apply
python3 scripts/org_worker.py --org kry1 --apply
```

Controlled one-org optimize after the fleet is full or manually approved:

```bash
PRL_FLEET_MODE=optimize python3 scripts/rollout.py --stage one-org --org kry1 --price 0.62 --apply-workers --allow-live-retarget --confirm-live-retarget --require-secrets
```

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

Current implementation:

- `SALAD_FLEET_EXTRA_ORGS_JSON` appends new organizations without replacing existing orgs
- `scripts/config_loader.py --validate` provides config onboarding checks
- `api_rate_limits` stores per API key env request windows
- `org_worker.py` and guard v2 enforce `PRL_SALAD_API_MAX_REQUESTS_PER_MINUTE`
- `scripts/health.py --json` reports current API rate-limit windows
- `scripts/maintenance.py` prunes historical rows with dry-run default and `--apply` for deletion
- `scripts/maintenance.py --loop --interval 21600 --apply` can run as a six-hour retention job
- `scripts/supervisor.py --include-maintenance` includes maintenance in the tmux plan
- `scripts/supervisor.py --include-maintenance --maintenance-apply` allows the maintenance loop to prune old historical rows
- maintenance does not delete organizations, slots, slot targets, heartbeats, guard issues, runtime failures, workers, or search cooldowns

## Verification Gates

Every implementation phase must run:

```bash
python3 -m compileall scripts
git diff --check
rg -n 'salad_cloud_user_|cf_clearance|Cookie[:]|Authorization: Bearer|SALAD_API_KEY_.*=salad|PRL_WALLET=prl[[:alnum:]]{20,}' .
```

Runtime verification must include:

```text
rollout.py gates
process health
fresh heartbeats
health.py status
latest guard snapshot
active guard issues
runtime failures
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
Continuously monitor, live-test, debug, and improve the Salad PRL fleet scheduler.

Start in controlled rollout:
- verify rollout.py gates, scheduler targets, health.py, reporter.py, guard.py dry-run decisions, and org_worker.py output
- apply one org first through rollout.py, then expand to all enabled orgs only if health stays safe
- use PRL_PEARL_FEE_RATE=0.01 while the next-24h Pearl fee window is active

Runtime goal:
- keep all funded slots trying to become live profitable GPUs
- protect profitable live hashing workers
- rotate no-hash slots after 60 seconds
- rotate or stop negative slots after 90 seconds
- verify profit at base, trailing, and live prices
- do not sleep no-GPU searches until one hour of unsuccessful attempts
- when the fleet is full or manually approved, switch from fill to optimize mode
- keep improving scripts, tests, docs, and observability as live failures appear
- when Can adds more organizations, onboard them as 10-slot config units without committing secrets

Do not mark complete until the live fleet is stable under supervision, health.py is healthy or explained, no live slot is losing money under the active policy, and every funded org has all slots either live profitable or actively searching for profitable GPUs.
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
