from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class StartWatchersShellTest(unittest.TestCase):
    def test_generic_fleet_orgs_honor_per_org_api_key_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            bin_path = tmp_path / "bin"
            bin_path.mkdir()
            state_path = tmp_path / "state"
            fake_python = bin_path / "python"
            fake_python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            fake_python.chmod(0o755)
            fake_tmux = bin_path / "tmux"
            fake_tmux.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    if [[ "$1" == "has-session" ]]; then
                      exit 1
                    fi
                    if [[ "$1" == "kill-session" ]]; then
                      exit 0
                    fi
                    if [[ "$1" == "new-session" ]]; then
                      printf 'TMUX_NEW'
                      for arg in "$@"; do
                        printf ' [%s]' "$arg"
                      done
                      printf '\\n'
                      exit 0
                    fi
                    if [[ "$1" == "list-sessions" ]]; then
                      printf 'kray-prl-watch: 1 windows\\n'
                      printf 'kry1-prl-watch: 1 windows\\n'
                      printf 'kray-prl-guard: 1 windows\\n'
                      exit 0
                    fi
                    exit 0
                    """
                ),
                encoding="utf-8",
            )
            fake_tmux.chmod(0o755)
            fake_rg = bin_path / "rg"
            fake_rg.write_text("#!/usr/bin/env bash\ncat\n", encoding="utf-8")
            fake_rg.chmod(0o755)

            env = {
                **os.environ,
                "PATH": f"{bin_path}:{os.environ['PATH']}",
                "SALAD_PRL_ENV": str(tmp_path / "missing.env"),
                "SALAD_PRL_STATE_DIR": str(state_path),
                "SALAD_PRL_PYTHON": str(fake_python),
                "PRL_FLEET_ORGS": "kray,kry1",
                "PRL_WATCH_DEFAULT_API_KEY_ENV": "SALAD_API_KEY",
                "PRL_WATCH_API_KEY_ENV_KRY1": "SALAD_API_KEY_KRY1",
                "PRL_SKIP_PROCESS_KILL": "1",
                "SALAD_API_KEY": "test-default-key",
                "SALAD_API_KEY_KRY1": "test-kry1-key",
                "PRL_WALLET": "test-wallet",
                "PRL_LIVE_UPGRADE_MIN_LIVE_WORKERS": "9",
                "PRL_LIVE_UPGRADE_GLOBAL_MIN_FRESH_WORKERS": "17",
            }

            result = subprocess.run(
                ["bash", str(ROOT / "scripts" / "start_watchers.sh")],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PRL_WATCH_NAME=kray-prl-watch", result.stdout)
        self.assertIn("PRL_WATCH_NAME=kry1-prl-watch", result.stdout)
        self.assertIn("PRL_WATCH_API_KEY_ENV=SALAD_API_KEY ", result.stdout)
        self.assertIn("PRL_WATCH_API_KEY_ENV=SALAD_API_KEY_KRY1 ", result.stdout)
        self.assertIn("KRAY2_PRL_OPTIMIZE_LIVE_MIN_LIVE_WORKERS=9 ", result.stdout)
        self.assertIn("KRAY2_PRL_OPTIMIZE_LIVE_GLOBAL_MIN_FRESH_WORKERS=17 ", result.stdout)
        self.assertIn("PRL_WATCH_GLOBAL_POOL_WORKER_PREFIXES=kray-prl-kray\\,kry1-prl-kry1 ", result.stdout)


if __name__ == "__main__":
    unittest.main()
