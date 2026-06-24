from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from types import ModuleType
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


class LegacyPrlGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_dir = pathlib.Path(self.tmpdir.name)
        self.old_env = os.environ.copy()
        os.environ.update(
            {
                "SALAD_PRL_STATE_DIR": str(self.state_dir),
                "PRL_GUARD_ORGS": "kray",
                "PRL_FLEET_ORGS": "kray",
                "PRL_WALLET": "test-wallet",
                "SALAD_API_KEY": "test-key",
                "SALAD_API_KEY_2": "test-key",
                "PRL_STUCK_NON_LIVE_MIN_ACTIVE_SLOTS": "0",
                "PRL_STUCK_NON_LIVE_SECONDS": "3600",
                "PRL_EMPTY_STUCK_NON_LIVE_SECONDS": "3600",
                "PRL_STUCK_RUNNING_ZERO_DEFER_SECONDS": "0",
            }
        )
        for name in (
            "legacy_prl_guard_test",
            "prl_profit_snapshot_guard",
            "kray_prl_watch_guard",
            "kry1_prl_watch_guard",
            "kray2_prl_watch_guard",
            "kray3_prl_watch_guard",
        ):
            sys.modules.pop(name, None)
        self.guard = self.load_module("legacy_prl_guard_test", SCRIPTS / "salad_prl_guard.py")

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_env)
        self.tmpdir.cleanup()

    def load_module(self, name: str, path: pathlib.Path) -> ModuleType:
        spec = importlib.util.spec_from_file_location(name, path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    def write_recent_slot_action(self, slot: str, *, age_seconds: float = 60.0) -> None:
        payload = {
            "at": time.time() - age_seconds,
            "org": "kray",
            "slot": slot,
            "action": "patched",
            "reason": "test_recent_patch",
        }
        action_path = self.state_dir / "prl_slot_actions.json"
        action_path.write_text(json.dumps({f"kray/{slot}": payload}), encoding="utf-8")
        detail_path = self.guard.slot_action_detail_path("kray", slot)
        detail_path.parent.mkdir(parents=True, exist_ok=True)
        detail_path.write_text(json.dumps(payload), encoding="utf-8")

    def stuck_snapshot(self, slot: str) -> dict[str, Any]:
        return {
            "fresh_workers": 3,
            "running_no_live_billable_slots": [],
            "stale_current_workers": [],
            "negative_slots": [],
            "underperform_slots": [],
            "org_discrepancies": [{"org": "kray", "active_salad_slots": 10}],
            "totals": {"profit_day": 10.0},
            "stuck_non_live_slots": [
                {
                    "org": "kray",
                    "slot": slot,
                    "status": "deploying",
                    "running": 0,
                    "creating": 0,
                    "allocating": 1,
                    "empty_pending": True,
                    "state_age_seconds": 7200.0,
                    "requested_gpus": ["4090"],
                }
            ],
        }

    def test_recent_watcher_action_defers_stuck_slot_cleanup(self) -> None:
        slot = "prl-kray-roi-01"
        self.write_recent_slot_action(slot)
        stopped: list[tuple[str, str, str]] = []
        self.guard.snapshot.build_snapshot = lambda _price: self.stuck_snapshot(slot)
        self.guard.stop_slot = lambda org, slot_name, reason: stopped.append((org, slot_name, reason))

        self.guard.tick()

        self.assertEqual(stopped, [])

    def test_old_watcher_action_allows_stuck_slot_cleanup(self) -> None:
        slot = "prl-kray-roi-01"
        self.write_recent_slot_action(slot, age_seconds=self.guard.STUCK_NON_LIVE_RETARGET_COOLDOWN_SECONDS + 1)
        stopped: list[tuple[str, str, str]] = []

        def stop_slot(org: str, slot_name: str, reason: str) -> dict[str, Any]:
            stopped.append((org, slot_name, reason))
            return {"org": org, "slot": slot_name, "state": "stopped", "retargeted": None}

        self.guard.snapshot.build_snapshot = lambda _price: self.stuck_snapshot(slot)
        self.guard.stop_slot = stop_slot

        self.guard.tick()

        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0][0], "kray")
        self.assertEqual(stopped[0][1], slot)


if __name__ == "__main__":
    unittest.main()
