# Salad Orch v2 Live Runbook

Reader: an operator or agent starting the deterministic Salad PRL control plane
without private chat history.

Post-read action: start `salad-orch-v2` in read-only live mode, verify whether
it is safe, then choose whether to apply one organization.

## What This Is

`salad-orch-v2` is the deterministic scheduler stack for SaladCloud PRL mining.

It replaces ad hoc per-org target choice with:

- one SQLite state database
- one price oracle
- one availability probe
- one central fleet scheduler
- one worker per Salad organization
- one global guard
- one runtime monitor for safe live testing

The scheduler decides target GPU profiles. Workers execute those targets. The
guard handles no-hash and negative-profit active slots.

## Safety Defaults

Default commands are read-only or DB-only.

Live Salad mutations require one of these explicit flags:

- `--apply-workers`
- `--apply-guard`
- `--confirm-live-actions`
- `--confirm-all-orgs`
- `--confirm-live-retarget`

Do not run all-org live apply until one-org apply has been stable.

## Required Private Environment

Create a local `.env` from the example and fill values privately:

```bash
cp .env.example .env
```

Required values:

```text
PRL_WALLET
SALAD_API_KEY_2
SALAD_API_KEY_KRY1
```

Do not commit `.env`, API key values, cookies, bearer tokens, or private logs.

## Current Runtime Policy

Current fill policy:

```text
decision price: 0.64 USD/PRL
temporary Pearl fee: 0.01 when the low-fee window is active
normal conservative Pearl fee: 0.05
minimum new-candidate profit: 0.05 USD/day
no-hash grace: 60 seconds
negative live grace: 90 seconds
pending retarget grace: 60 seconds for controlled tests
no-GPU sleep trigger: 3600 seconds
no-GPU sleep duration: 900 seconds
```

Current org shape:

```text
kray  = 10 slots
kry1  = 10 slots
kray2 = 10 slots
kray3 = 10 slots
total = 40 slots
```

New organizations should be added as more 10-slot config units.

## First Live Read-Only Check

Run a DB-only smoke first:

```bash
PRL_PEARL_FEE_RATE=0.01 python3 scripts/rollout.py --stage shadow --price 0.64 --fee 0.01 --skip-workers --skip-guard
python3 scripts/shadow_compare.py
```

Then run a live read-only monitor tick:

```bash
PRL_PEARL_FEE_RATE=0.01 python3 scripts/runtime_monitor.py --once --runner-timeout-seconds 90 --price 0.64 --fee 0.01 --require-secrets
```

Expected safe output shape:

```text
monitor ok=True action=none shadow=True health=healthy targets=40/40 ...
```

Acceptable temporary output:

```text
monitor ok=False ... shadow_failed=monitor_runner_error
shadow_error=ReadTimeout: ...
```

A timeout means Salad or pool APIs were slow. The monitor uses a subprocess hard
timeout by default, so it should fail the tick instead of hanging indefinitely.
Retry later or let the loop continue. When DB fallback is available, the timeout
output also includes latest read-only target coverage, health, live hashing,
no-hash, negative, and stuck counts plus `shadow_fallback=db`. That fallback is
an operator summary only; the shadow gate is still failed and no live action
should run from that tick.

## Start Persistent Read-Only Monitoring

Use a dedicated tmux session:

```bash
REPO_ROOT=$(pwd)
tmux kill-session -t salad-orch-v2-monitor 2>/dev/null || true
tmux new-session -d -s salad-orch-v2-monitor \
  "cd \"$REPO_ROOT\" && PYTHONUNBUFFERED=1 PRL_PEARL_FEE_RATE=0.01 python3 scripts/runtime_monitor.py --loop --interval 120 --runner-timeout-seconds 90 --price 0.64 --fee 0.01 --require-secrets"
```

Inspect it:

```bash
tmux capture-pane -pt salad-orch-v2-monitor -S -80
```

For active fill mode, use the pending-only live monitor after the read-only
monitor has shown safe targets:

```bash
PRL_PEARL_FEE_RATE=0.01 python3 scripts/runtime_monitor.py --loop --interval 60 --runner-timeout-seconds 240 --fee 0.01 --require-secrets --apply-all-orgs-pending --guard-on-issues-every 1 --guard-actionable-only --confirm-live-actions --pending-retarget-after-seconds 60 --worker-parallelism 4 --skip-shadow-workers
```

This still runs a shadow gate first, but `--skip-shadow-workers` makes that
preflight DB-only so the same cycle does not spend Salad API requests twice. The
action pass still performs live worker observations before patching stale
creating/allocating slots across all orgs, but it does not pass
`--allow-live-retarget`, so running slots remain protected.
`--guard-actionable-only` keeps fill moving while no-hash or negative slots are
still inside grace, but switches immediately to guard once the read-only guard
probe has a retarget/stop decision.
`--pending-retarget-after-seconds` is also applied to the scheduler's
pending-target protection window for that monitor tick, so target selection and
live patching use the same grace period. When
`PRL_PENDING_PROFILE_COOLDOWN_SECONDS` is not explicitly set, the monitor also
uses this value for stale pending profile cooldowns so failed searches rotate
again on the same cadence.
`--worker-parallelism 4` runs each organization in an isolated process, which is
faster than the old sequential all-org scan without sharing watcher environment
between orgs. The rollout layer only runs one organization per Salad API key in
the same worker batch, so orgs sharing one key do not exhaust the same
per-minute request budget at once.
Leave `--price` unset in this mode. The scheduler then uses `price_oracle.py`
risk mode: base 0.64 by default, `boost_fill` when the confirmed trailing PRL
price supports 0.70+ conditions, and risk-off when the trailing price weakens.

Run the price oracle beside the monitor:

```bash
PRL_PEARL_FEE_RATE=0.01 PRL_BOOST_MIN_WINDOW_SECONDS=300 python3 scripts/price_oracle.py --loop --interval 60
```

Run the availability probe beside the monitor so the scheduler has fresh
per-org capacity hints instead of rotating profitable profiles blindly:

```bash
PRL_PEARL_FEE_RATE=0.01 python3 scripts/availability_probe.py --loop --interval 300 --priorities batch,low --org-parallelism 2
```

The probe uses the same API budget limiter as live workers, so it should slow
itself down instead of exhausting a shared Salad key. It probes organizations
in parallel only when they use different API key env vars; orgs sharing one key
are automatically batched apart. The default organization parallelism is 2 and
can also be set with `PRL_AVAILABILITY_ORG_PARALLELISM`. The default
availability heartbeat stale window is 1800 seconds
(`PRL_AVAILABILITY_STALE_AFTER_SECONDS`) because probing `batch,low` across
multiple orgs can take longer than one monitor tick. The scheduler and guard
use the same freshness window by default so long probe runs still guide target
selection.

Stop it:

```bash
tmux kill-session -t salad-orch-v2-monitor
```

## Status Commands

Use these often:

```bash
python3 scripts/health.py
python3 scripts/reporter.py
python3 scripts/shadow_compare.py
python3 scripts/rollback.py list
```

Use this when a fresh live profit snapshot is needed:

```bash
python3 scripts/reporter.py --refresh --refresh-timeout 45
```

If refresh times out, reporter should return stale DB state with a
`refresh_error` instead of hanging.

## Controlled One-Org Apply

Use this only after read-only shadow is safe:

```bash
PRL_PEARL_FEE_RATE=0.01 python3 scripts/runtime_monitor.py --once --runner-timeout-seconds 90 --price 0.64 --fee 0.01 --require-secrets --apply-one-org --org kry1 --confirm-live-actions --allow-pending-retarget --pending-retarget-after-seconds 60
```

This can patch stale creating/allocating mismatches after the grace period.
Running profitable GPUs remain protected unless a separate live-retarget flag is
used.

After one-org apply:

```bash
python3 scripts/health.py
python3 scripts/reporter.py
python3 scripts/shadow_compare.py
sqlite3 state/fleet_scheduler.db "SELECT org_label, slot_name, action, profile_key, ok, substr(at_utc,1,19) FROM attempts ORDER BY id DESC LIMIT 20;"
```

## Guard Apply

Use guard apply only when no-hash or negative slots persist past grace and the
read-only guard decision is correct:

```bash
PRL_PEARL_FEE_RATE=0.01 python3 scripts/runtime_monitor.py --once --runner-timeout-seconds 90 --price 0.64 --fee 0.01 --require-secrets --apply-guard --confirm-live-actions
```

If a previous guard apply left a runtime failure and the retry reason is
understood:

```bash
PRL_PEARL_FEE_RATE=0.01 python3 scripts/runtime_monitor.py --once --runner-timeout-seconds 90 --price 0.64 --fee 0.01 --require-secrets --apply-guard --confirm-live-actions --allow-degraded-shadow
```

## Full-Orgs Apply

Full apply is intentionally harder to run:

```bash
PRL_PEARL_FEE_RATE=0.01 python3 scripts/rollout.py --stage all-orgs --price 0.64 --fee 0.01 --apply-workers --confirm-all-orgs --require-secrets
```

Run this only after one-org apply has improved or preserved live profitable GPU
count and no-hash/negative slots are under control.

## Rollback

Live apply stages create target checkpoints before rewriting scheduler targets.

List checkpoints:

```bash
python3 scripts/rollback.py list
```

Dry-run restore:

```bash
python3 scripts/rollback.py restore <checkpoint-id>
```

Apply restore:

```bash
python3 scripts/rollback.py restore <checkpoint-id> --apply
```

Rollback restores scheduler targets only. Containers follow restored targets
only after an explicit worker or rollout apply.

## When It Is Safe To Optimize

Stay in fill mode until all funded slots are either live profitable or actively
searching for profitable GPUs.

Use optimize mode only when:

- no active slot is negative under the active policy
- no paid no-hash slot is beyond grace
- most or all enabled slots are live hashing
- the expected replacement clears the configured profit delta

One-org optimize command:

```bash
PRL_FLEET_MODE=optimize PRL_PEARL_FEE_RATE=0.01 python3 scripts/rollout.py --stage one-org --org kry1 --price 0.62 --apply-workers --allow-live-retarget --confirm-live-retarget --require-secrets
```

## GitHub Public Boundary

The repository may include code, thresholds, public endpoints, env var names,
and sanitized examples.

The repository must not include API keys, cookies, bearer tokens, private wallet
control credentials, or raw logs with headers.

Before each push:

```bash
python3 -m compileall scripts tests
python3 -m unittest discover -s tests -v
git diff --check
rg -n 'salad_cloud_user_|cf_clearance|Cookie[:]|Authorization: Bearer|SALAD_API_KEY_.*=salad|PRL_WALLET=prl[[:alnum:]]{20,}' .
```
