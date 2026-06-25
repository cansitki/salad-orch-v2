#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
MODAL_BIN=${MODAL_BIN:-"$HOME/.modal-cli-venv/bin/modal"}
DURATION=${MODAL_PEARL_MATRIX_DURATION:-300}
WORKER_PREFIX=${MODAL_PEARL_WORKER_PREFIX:-modal-pearl}
GPU_LIST=${MODAL_PEARL_GPU_LIST:-"T4 L4 A10G L40S"}

if [[ ! -x "$MODAL_BIN" ]]; then
  echo "modal CLI not found at $MODAL_BIN" >&2
  exit 2
fi

cd "$ROOT_DIR"

for gpu in $GPU_LIST; do
  safe_gpu=$(printf '%s' "$gpu" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-')
  echo "[modal-matrix] launching gpu=$gpu duration=${DURATION}s"
  if ! MODAL_PEARL_GPU="$gpu" "$MODAL_BIN" run -d scripts/modal_pearl_miner.py \
    --duration "$DURATION" \
    --worker-prefix "${WORKER_PREFIX}-${safe_gpu}"; then
    echo "[modal-matrix] launch failed gpu=$gpu" >&2
  fi
done
