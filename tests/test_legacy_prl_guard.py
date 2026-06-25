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

    def low_fresh_no_hash_snapshot(self, slot: str) -> dict[str, Any]:
        return {
            "fresh_workers": 2,
            "running_no_live_billable_slots": [
                {
                    "org": "kray",
                    "slot": slot,
                    "cost_day": 4.0,
                    "state_age_seconds": 7200.0,
                }
            ],
            "stale_current_workers": [],
            "negative_slots": [],
            "underperform_slots": [],
            "org_discrepancies": [{"org": "kray", "active_salad_slots": 10}],
            "totals": {"profit_day": -10.0, "market_profit_day": -10.0},
            "stuck_non_live_slots": [],
        }

    def no_hash_snapshot(self, slot: str) -> dict[str, Any]:
        return {
            "fresh_workers": 10,
            "running_no_live_billable_slots": [
                {
                    "org": "kray",
                    "slot": slot,
                    "cost_day": 4.0,
                    "state_age_seconds": 7200.0,
                    "grace_seconds": 900,
                }
            ],
            "stale_current_workers": [],
            "negative_slots": [],
            "underperform_slots": [],
            "org_discrepancies": [{"org": "kray", "active_salad_slots": 10}],
            "totals": {"profit_day": 10.0, "market_profit_day": 10.0},
            "stuck_non_live_slots": [],
            "slots": [],
        }

    def negative_snapshot(self, slot: str) -> dict[str, Any]:
        return {
            "fresh_workers": 10,
            "running_no_live_billable_slots": [],
            "stale_current_workers": [],
            "negative_slots": [],
            "underperform_slots": [],
            "org_discrepancies": [{"org": "kray", "active_salad_slots": 10}],
            "totals": {"profit_day": 10.0, "market_profit_day": 10.0},
            "stuck_non_live_slots": [],
            "slots": [
                {
                    "org": "kray",
                    "slot": slot,
                    "gpu": "4070tis",
                    "priority": "low",
                    "profit_day": -1.0,
                    "market_profit_day": -0.5,
                    "th": 90.0,
                }
            ],
        }

    def band_negative_market_positive_snapshot(self, slot: str) -> dict[str, Any]:
        snap = self.negative_snapshot(slot)
        snap["slots"][0]["market_profit_day"] = 0.25
        return snap

    def log_events(self) -> list[dict[str, Any]]:
        if not self.guard.LOG.exists():
            return []
        return [json.loads(line) for line in self.guard.LOG.read_text(encoding="utf-8").splitlines()]

    def test_recent_watcher_action_defers_stuck_slot_cleanup(self) -> None:
        slot = "prl-kray-roi-01"
        self.write_recent_slot_action(slot)
        stopped: list[tuple[str, str, str]] = []
        self.guard.snapshot.build_snapshot = lambda _price: self.stuck_snapshot(slot)
        self.guard.stop_slot = lambda org, slot_name, reason: stopped.append((org, slot_name, reason))

        self.guard.tick()

        self.assertEqual(stopped, [])

    def test_old_watcher_action_recycles_stuck_slot_cleanup(self) -> None:
        slot = "prl-kray-roi-01"
        self.write_recent_slot_action(slot, age_seconds=self.guard.STUCK_NON_LIVE_RETARGET_COOLDOWN_SECONDS + 1)
        recycled: list[tuple[str, str, str]] = []

        def recycle_zero_running_slot(org: str, slot_name: str, reason: str) -> list[dict[str, Any]]:
            recycled.append((org, slot_name, reason))
            return [{"org": org, "slot": slot_name, "state": "hidden_pending_restarted", "retargeted": None}]

        self.guard.snapshot.build_snapshot = lambda _price: self.stuck_snapshot(slot)
        self.guard.recycle_zero_running_slot = recycle_zero_running_slot

        self.guard.tick()

        self.assertEqual(len(recycled), 1)
        self.assertEqual(recycled[0][0], "kray")
        self.assertEqual(recycled[0][1], slot)
        self.assertIn("zero_running", recycled[0][2])

    def test_pending_recycle_writes_persistent_slot_action_cooldown(self) -> None:
        slot = "prl-kray-roi-01"
        reallocated: list[tuple[str, str, str]] = []
        testcase = self

        class FakeWatcher:
            def slot_state(self, slot_name: str) -> tuple[None, list[dict[str, Any]]]:
                testcase.assertEqual(slot_name, slot)
                return None, [{"id": "instance-1", "state": "creating", "ready": False, "started": False}]

            def reallocate(self, slot_name: str, instance_id: str, reason: str) -> None:
                reallocated.append((slot_name, instance_id, reason))

        self.guard.watchers = {"kray": FakeWatcher()}

        actions = self.guard.recycle_zero_running_slot("kray", slot, "test_zero_running")

        self.assertEqual(len(actions), 1)
        self.assertEqual(reallocated, [(slot, "instance-1", "test_zero_running")])
        recent = self.guard.recent_slot_action("kray", slot)
        self.assertIsNotNone(recent)
        assert recent is not None
        self.assertEqual(recent["action"], "reallocated_pending")
        self.assertEqual(recent["reason"], "test_zero_running")

    def test_low_fresh_pool_sample_skips_no_hash_reallocation(self) -> None:
        slot = "prl-kray-roi-01"
        reallocated: list[tuple[str, str, str]] = []
        self.guard.SEEN_SINCE[("kray", slot)] = time.time() - 7200.0
        self.guard.snapshot.build_snapshot = lambda _price: self.low_fresh_no_hash_snapshot(slot)
        self.guard.reallocate_slot = lambda org, slot_name, reason, retarget=True: reallocated.append(
            (org, slot_name, reason)
        )

        self.guard.tick()

        self.assertEqual(reallocated, [])

    def test_no_hash_reallocation_writes_specific_log_event(self) -> None:
        slot = "prl-kray-roi-01"
        self.guard.SEEN_SINCE[("kray", slot)] = time.time() - 7200.0
        self.guard.snapshot.build_snapshot = lambda _price: self.no_hash_snapshot(slot)
        self.guard.reallocate_slot = lambda org, slot_name, reason, retarget=True: [
            {"org": org, "slot": slot_name, "reason": reason, "retargeted": None}
        ]

        self.guard.tick()

        events = self.log_events()
        self.assertTrue(any(row.get("event") == "no_hash_slot_reallocated" for row in events))

    def test_negative_reallocation_writes_specific_log_event(self) -> None:
        slot = "prl-kray-roi-01"
        self.guard.NEGATIVE_SLOT_SEEN_SINCE[("kray", slot)] = time.time() - 7200.0
        self.guard.snapshot.build_snapshot = lambda _price: self.negative_snapshot(slot)
        self.guard.reallocate_slot = lambda org, slot_name, reason, retarget=True: [
            {"org": org, "slot": slot_name, "reason": reason, "retargeted": None}
        ]

        self.guard.tick()

        events = self.log_events()
        self.assertTrue(any(row.get("event") == "negative_slot_reallocated" for row in events))

    def test_band_negative_market_positive_slot_is_not_reallocated(self) -> None:
        slot = "prl-kray-roi-01"
        self.guard.NEGATIVE_SLOT_SEEN_SINCE[("kray", slot)] = time.time() - 7200.0
        self.guard.snapshot.build_snapshot = lambda _price: self.band_negative_market_positive_snapshot(slot)
        reallocated: list[tuple[str, str, str]] = []
        self.guard.reallocate_slot = lambda org, slot_name, reason, retarget=True: reallocated.append(
            (org, slot_name, reason)
        )

        self.guard.tick()

        self.assertEqual(reallocated, [])
        self.assertNotIn(("kray", slot), self.guard.NEGATIVE_SLOT_SEEN_SINCE)


if __name__ == "__main__":
    unittest.main()
