from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import guard
import state_db
from config_loader import load_config


class GuardDecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(pathlib.Path(self.tmpdir.name) / "fleet.db")
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, load_config())
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "observed_profile_key": "3090:batch:2048",
                    "observed_status": "running",
                    "protected": True,
                },
            )
            conn.commit()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def make_issue_old(self, issue_type: str) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.record_guard_issue(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "issue_type": issue_type,
                    "first_seen_utc": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(timespec="seconds"),
                    "payload": {},
                },
            )
            conn.commit()

    def test_guard_retargets_no_hash_after_grace(self) -> None:
        self.make_issue_old("no_hash")
        decisions = guard.enforce_issues(
            db_path=self.db_path,
            decision_price=0.64,
            apply=False,
            analysis={
                "fresh_workers": 3,
                "running_no_live_billable_slots": [
                    {"org": "kray", "slot": "prl-kray-roi-01", "cost_day": 1.0}
                ],
                "negative_slots": [],
            },
        )
        self.assertEqual(decisions[0]["action"], "retarget")
        self.assertNotEqual(decisions[0]["target_profile_key"], "3090:batch:2048")
        with state_db.connect(self.db_path) as conn:
            target = conn.execute(
                """
                SELECT profile_key, reason
                FROM slot_targets
                WHERE org_label = 'kray' AND slot_name = 'prl-kray-roi-01'
                """
            ).fetchone()
        self.assertEqual(target["profile_key"], decisions[0]["target_profile_key"])
        self.assertEqual(target["reason"], "guard_no_hash_retarget")

    def test_guard_stops_when_no_profitable_replacement_exists(self) -> None:
        self.make_issue_old("negative")
        decisions = guard.enforce_issues(
            db_path=self.db_path,
            decision_price=0.01,
            apply=False,
            analysis={
                "fresh_workers": 3,
                "running_no_live_billable_slots": [],
                "negative_slots": [
                    {
                        "org": "kray",
                        "slot": "prl-kray-roi-01",
                        "gpu": "3090",
                        "priority": "batch",
                        "profit_day": -1.0,
                    }
                ],
            },
        )
        self.assertEqual(decisions[0]["action"], "stop")
        self.assertIsNone(decisions[0]["target_profile_key"])

    def test_successful_apply_clears_slot_runtime_failure(self) -> None:
        self.make_issue_old("negative")
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.record_failure(
                conn,
                guard.guard_failure_component("kray", "prl-kray-roi-01", "negative"),
                severity="warning",
                error_type="TypeError",
                message="old apply failure",
            )
            conn.commit()

        with patch("guard.apply_guard_target", return_value={"action": "retarget", "applied": True}):
            decisions = guard.enforce_issues(
                db_path=self.db_path,
                decision_price=0.64,
                apply=True,
                analysis={
                    "fresh_workers": 3,
                    "running_no_live_billable_slots": [],
                    "negative_slots": [
                        {
                            "org": "kray",
                            "slot": "prl-kray-roi-01",
                            "gpu": "3090",
                            "priority": "batch",
                            "profit_day": -1.0,
                        }
                    ],
                },
            )

        self.assertEqual(decisions[0]["action"], "retarget")
        with state_db.connect(self.db_path) as conn:
            failures = conn.execute("SELECT COUNT(*) FROM runtime_failures").fetchone()[0]
            attempt = conn.execute("SELECT ok FROM attempts ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(failures, 0)
        self.assertEqual(attempt["ok"], 1)

    def test_guard_applies_once_when_same_slot_has_multiple_issues(self) -> None:
        self.make_issue_old("no_hash")
        self.make_issue_old("negative")

        with patch("guard.apply_guard_target", return_value={"action": "retarget", "applied": True}) as apply_mock:
            decisions = guard.enforce_issues(
                db_path=self.db_path,
                decision_price=0.64,
                apply=True,
                analysis={
                    "fresh_workers": 3,
                    "running_no_live_billable_slots": [
                        {"org": "kray", "slot": "prl-kray-roi-01", "cost_day": 1.0}
                    ],
                    "negative_slots": [
                        {
                            "org": "kray",
                            "slot": "prl-kray-roi-01",
                            "gpu": "3090",
                            "priority": "batch",
                            "profit_day": -1.0,
                        }
                    ],
                },
            )

        self.assertEqual(apply_mock.call_count, 1)
        self.assertEqual([decision["action"] for decision in decisions], ["retarget", "skip_duplicate"])

    def test_guard_snapshot_updates_slot_hashrate_and_workers(self) -> None:
        fake_snapshot = {
            "live_market_prl_price": 0.64,
            "fresh_workers": 1,
            "running_no_live_billable_slots": [],
            "totals": {
                "th": 111.5,
                "cost_day": 2.16,
                "revenue_day": 3.0,
                "profit_day": 0.84,
            },
            "slots": [
                {
                    "worker": "kray-prl-roi-01-pearlfortune-inst-1",
                    "slot": "prl-kray-roi-01",
                    "org": "kray",
                    "gpu": "3090",
                    "priority": "batch",
                    "th": 111.5,
                    "cost_day": 2.16,
                    "profit_day": 0.84,
                    "last_stats_at": "2026-06-24T12:00:00+00:00",
                }
            ],
        }

        with patch("guard.snapshot.build_snapshot", return_value=fake_snapshot):
            payload = guard.run_once(db_path=self.db_path, price=0.64, apply=False)

        self.assertEqual(payload["issue_count"], 0)
        with state_db.connect(self.db_path) as conn:
            slot = conn.execute(
                """
                SELECT observed_status, observed_profile_key, live_hashrate_th, protected
                FROM slots
                WHERE org_label = 'kray' AND slot_name = 'prl-kray-roi-01'
                """
            ).fetchone()
            worker = conn.execute("SELECT * FROM workers").fetchone()
            snapshot = conn.execute(
                """
                SELECT profile_key, th
                FROM profit_snapshots
                WHERE scope = 'slot'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        self.assertEqual(slot["observed_status"], "running")
        self.assertEqual(slot["observed_profile_key"], "3090:batch:2048")
        self.assertEqual(slot["live_hashrate_th"], 111.5)
        self.assertEqual(slot["protected"], 1)
        self.assertEqual(worker["worker_name"], "kray-prl-roi-01-pearlfortune-inst-1")
        self.assertEqual(worker["org_label"], "kray")
        self.assertEqual(worker["slot_name"], "prl-kray-roi-01")
        self.assertEqual(worker["instance_id"], "inst-1")
        self.assertEqual(worker["gpu_key"], "3090")
        self.assertEqual(worker["reported_hashrate_th"], 111.5)
        self.assertEqual(worker["stale"], 0)
        self.assertEqual(snapshot["profile_key"], "3090:batch:2048")
        self.assertEqual(snapshot["th"], 111.5)


if __name__ == "__main__":
    unittest.main()
