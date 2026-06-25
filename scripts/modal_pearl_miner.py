#!/usr/bin/env python3
from __future__ import annotations

import os
import pathlib
import subprocess
import time

import modal


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
ENV_FILE = pathlib.Path(os.environ.get("SALAD_PRL_ENV", str(REPO_ROOT / ".env")))

MINER_RELEASE_TAG = os.environ.get("PRL_WATCH_MINER_RELEASE_TAG", "v.1.1.8")
MINER_PACKAGE_VERSION = os.environ.get("PRL_WATCH_MINER_PACKAGE_VERSION", "v1.1.8")
MINER_BINARY = os.environ.get("PRL_WATCH_MINER_BINARY", "miner-cuda12")
MINER_URL = os.environ.get(
    "PRL_WATCH_MINER_URL",
    f"https://github.com/pearlfortune/pearl-miner/releases/download/{MINER_RELEASE_TAG}/pearlfortune-{MINER_PACKAGE_VERSION}.tar.gz",
)
POOL_PROXY = os.environ.get("PRL_POOL_PROXY", "global.pearlfortune.org:443")
GPU = os.environ.get("MODAL_PEARL_GPU", "T4")

app = modal.App("pearlfortune-modal-miner")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ca-certificates", "curl", "tar", "pciutils", "procps")
)


def load_env_file() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@app.function(image=image, gpu=GPU, timeout=24 * 60 * 60)
def mine(wallet: str, worker: str, duration_seconds: int = 900) -> str:
    root = pathlib.Path("/opt/pearlfortune")
    package = root / "pearlfortune.tar.gz"
    miner_dir = root / "pearlfortune"
    miner = miner_dir / MINER_BINARY
    root.mkdir(parents=True, exist_ok=True)

    print(f"[modal-pearl] gpu={GPU} worker={worker} duration={duration_seconds}s")
    subprocess.run(["nvidia-smi"], check=False)
    subprocess.run(
        [
            "curl",
            "-L",
            "--fail",
            "--retry",
            "10",
            "--retry-delay",
            "5",
            "--connect-timeout",
            "20",
            "--max-time",
            "240",
            "-o",
            str(package),
            MINER_URL,
        ],
        check=True,
    )
    if miner_dir.exists():
        subprocess.run(["rm", "-rf", str(miner_dir)], check=True)
    subprocess.run(["tar", "xzf", str(package), "-C", str(root)], check=True)
    if not miner.exists():
        fallback = miner_dir / "miner"
        if fallback.exists():
            miner = fallback
    miner.chmod(0o755)

    started_at = time.monotonic()
    deadline = started_at + max(60, int(duration_seconds))
    runs = 0
    while time.monotonic() < deadline:
        runs += 1
        remaining = max(1, int(deadline - time.monotonic()))
        print(f"[modal-pearl] launching miner run={runs} remaining={remaining}s")
        proc = subprocess.Popen(
            [
                str(miner),
                "--proxy",
                POOL_PROXY,
                "--address",
                wallet,
                "--worker",
                worker,
                "-gpu",
            ],
            cwd=str(miner_dir),
        )
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=20)
            break
        print(f"[modal-pearl] miner exited rc={proc.returncode}; restarting in 10s")
        time.sleep(min(10, max(0, deadline - time.monotonic())))
    return f"worker={worker} duration={int(time.monotonic() - started_at)}s"


@app.local_entrypoint()
def main(
    duration: int = 900,
    worker_prefix: str = "modal-pearl-test",
    wallet: str | None = None,
    background: bool = True,
) -> None:
    load_env_file()
    selected_wallet = wallet or os.environ.get("PRL_WALLET")
    if not selected_wallet:
        raise RuntimeError("PRL_WALLET must be set in .env or passed with --wallet")
    worker = f"{worker_prefix}-{int(time.time())}"
    print(f"[modal-pearl] launching gpu={GPU} worker={worker} duration={duration}s")
    if background:
        call = mine.spawn(selected_wallet, worker, duration)
        print(f"[modal-pearl] spawned worker={worker} call_id={call.object_id}")
        return
    result = mine.remote(selected_wallet, worker, duration)
    print(f"[modal-pearl] result {result}")
