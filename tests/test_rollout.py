from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rollout
import state_db


class RolloutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(pathlib.Path(self.tmpdir.name) / "fleet.db")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_shadow_db_only_rollout_builds_safe_targets(self) -> None:
        payload = rollout.run_rollout(
            stage="shadow",
            db_path=self.db_path,
            price=0.64,
            fee=0.01,
            skip_workers=True,
            skip_guard=True,
        )
        self.assertTrue(payload["gates"]["ok"])
        self.assertEqual(payload["gates"]["coverage"], {"assigned_targets": 40, "target_slots": 40})
        self.assertEqual(payload["health"]["health"], "healthy")
        self.assertEqual(payload["report"]["assigned_targets"], 40)

    def test_gate_fails_when_runtime_failure_exists(self) -> None:
        scheduler_payload = {
            "mode": "base_fill",
            "assigned_targets": 40,
            "target_slots": 40,
        }
        report_payload = {
            "running_no_live_billable_slots": [],
            "negative_slots": [],
        }
        health_payload = {
            "health": "degraded",
            "runtime_failures": [{"component": "guard"}],
            "stale_heartbeats": [],
        }
        gates = rollout.evaluate_gates(
            db_path=self.db_path,
            scheduler_payload=scheduler_payload,
            worker_payloads=[],
            guard_payload=None,
            report_payload=report_payload,
            health_payload=health_payload,
            allow_degraded=False,
        )
        self.assertFalse(gates["ok"])
        self.assertEqual(gates["failed"][0]["gate"], "runtime_failures")

    def test_gate_fails_when_target_coverage_is_incomplete(self) -> None:
        gates = rollout.evaluate_gates(
            db_path=self.db_path,
            scheduler_payload={"mode": "base_fill", "assigned_targets": 39, "target_slots": 40},
            worker_payloads=[],
            guard_payload=None,
            report_payload={"running_no_live_billable_slots": [], "negative_slots": []},
            health_payload={"health": "healthy", "runtime_failures": [], "stale_heartbeats": []},
            allow_degraded=False,
        )
        self.assertFalse(gates["ok"])
        self.assertEqual(gates["failed"][0]["gate"], "target_coverage")

    def test_stale_heartbeats_warn_by_default_for_one_shot_rollout(self) -> None:
        gates = rollout.evaluate_gates(
            db_path=self.db_path,
            scheduler_payload={"mode": "base_fill", "assigned_targets": 40, "target_slots": 40},
            worker_payloads=[],
            guard_payload=None,
            report_payload={"running_no_live_billable_slots": [], "negative_slots": []},
            health_payload={
                "health": "degraded",
                "runtime_failures": [],
                "stale_heartbeats": [{"process_name": "fleet_scheduler"}],
            },
            allow_degraded=False,
        )
        self.assertTrue(gates["ok"])
        self.assertEqual(gates["warnings"][0]["gate"], "stale_heartbeats")

    def test_stale_heartbeats_can_be_required_as_hard_gate(self) -> None:
        gates = rollout.evaluate_gates(
            db_path=self.db_path,
            scheduler_payload={"mode": "base_fill", "assigned_targets": 40, "target_slots": 40},
            worker_payloads=[],
            guard_payload=None,
            report_payload={"running_no_live_billable_slots": [], "negative_slots": []},
            health_payload={
                "health": "degraded",
                "runtime_failures": [],
                "stale_heartbeats": [{"process_name": "fleet_scheduler"}],
            },
            allow_degraded=False,
            require_fresh_heartbeats=True,
        )
        self.assertFalse(gates["ok"])
        self.assertEqual(gates["failed"][0]["gate"], "stale_heartbeats")

    def test_all_org_live_apply_requires_confirmation(self) -> None:
        with self.assertRaises(SystemExit):
            rollout.run_rollout(
                stage="all-orgs",
                db_path=self.db_path,
                apply_workers=True,
                skip_workers=True,
                skip_guard=True,
            )

    def test_live_retarget_requires_confirmation(self) -> None:
        with self.assertRaises(SystemExit):
            rollout.run_rollout(
                stage="one-org",
                org_label="kry1",
                db_path=self.db_path,
                apply_workers=True,
                allow_live_retarget=True,
                skip_workers=True,
                skip_guard=True,
            )

    def test_live_apply_rollout_creates_checkpoint_before_scheduler_tick(self) -> None:
        payload = rollout.run_rollout(
            stage="one-org",
            org_label="kry1",
            db_path=self.db_path,
            price=0.64,
            fee=0.01,
            apply_workers=True,
            skip_workers=True,
            skip_guard=True,
        )
        self.assertIsNotNone(payload["checkpoint"])
        self.assertEqual(payload["checkpoint"]["stage"], "one-org")

    def test_pending_cooldown_triggers_same_cycle_second_worker_pass(self) -> None:
        scheduler_payload = {
            "mode": "base_fill",
            "assigned_targets": 40,
            "target_slots": 40,
        }
        report_payload = {
            "assigned_targets": 40,
            "target_slots": 40,
            "active_pending_slots": 0,
            "live_hashing_gpus": 0,
            "running_no_live_billable_slots": [],
            "negative_slots": [],
            "stuck_slots": [],
        }
        health_payload = {
            "health": "healthy",
            "target_count": 40,
            "slot_count": 40,
            "runtime_failures": [],
            "guard_issues": [],
            "stale_heartbeats": [],
        }
        shadow_payload = {
            "ok": True,
            "unsafe_targets": [],
            "missing_targets": [],
            "mismatches": [],
            "warnings": [],
            "diversification": {"unique_target_profiles": 4, "top_profile_share": 0.25},
        }
        worker_passes = [
            [
                {
                    "org": "kry1",
                    "apply": True,
                    "targets": 10,
                    "action_counts": {"cooldown_pending": 1},
                    "results": [{"slot_name": "roi01", "action": "cooldown_pending", "ok": True}],
                }
            ],
            [
                {
                    "org": "kry1",
                    "apply": True,
                    "targets": 10,
                    "action_counts": {"patch": 1},
                    "results": [{"slot_name": "roi01", "action": "patch", "ok": True}],
                }
            ],
        ]

        with (
            mock.patch.object(rollout.fleet_scheduler, "schedule_once", return_value=scheduler_payload) as schedule_once,
            mock.patch.object(rollout.reporter, "build_report", return_value=report_payload),
            mock.patch.object(rollout.health, "build_health", return_value=health_payload),
            mock.patch.object(rollout.shadow_compare, "build_shadow_compare", return_value=shadow_payload),
            mock.patch.object(rollout, "_run_org_workers", side_effect=worker_passes) as run_workers,
        ):
            payload = rollout.run_rollout(
                stage="one-org",
                org_label="kry1",
                db_path=self.db_path,
                price=0.64,
                fee=0.01,
                apply_workers=True,
                allow_pending_retarget=True,
                skip_guard=True,
            )

        self.assertEqual(run_workers.call_count, 2)
        self.assertEqual(schedule_once.call_count, 3)
        self.assertEqual(len(payload["workers"]), 2)
        self.assertEqual(payload["workers"][0]["action_counts"], {"cooldown_pending": 1})
        self.assertEqual(payload["workers"][1]["action_counts"], {"patch": 1})


if __name__ == "__main__":
    unittest.main()
