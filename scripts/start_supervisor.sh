#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
STATE_DIR=${SALAD_PRL_STATE_DIR:-"$REPO_ROOT/state"}
LOG_DIR="$STATE_DIR/logs"
mkdir -p "$LOG_DIR"

SUPERVISOR="$SCRIPT_DIR/salad_prl_nonstop_supervisor.py"
SESSION=kray-prl-nonstop-supervisor
OUT="$LOG_DIR/kray_prl_nonstop_supervisor.out"

tmux has-session -t "$SESSION" 2>/dev/null && tmux kill-session -t "$SESSION"
tmux new-session -d -s "$SESSION" "cd $REPO_ROOT && python3 $SUPERVISOR --interval 60 --max-heartbeat-age-seconds 600 >> $OUT 2>&1"
tmux list-sessions | rg '(kray|kry1|kray2|kray3)-prl-(watch|guard|nonstop-supervisor)'
