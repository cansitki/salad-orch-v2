from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_scheduler
import shadow_compare
import state_db
from config_loader import load_config


class ShadowCompareTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(pathlib.Path(self.tmpdir.name) / "fleet.db")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_shadow_compare_passes_after_scheduler_targets(self) -> None:
        fleet_scheduler.schedule_once(db_path=self.db_path, price=0.64, fee=0.01, dry_run=False)
        payload = shadow_compare.build_shadow_compare(self.db_path)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["assigned_targets"], 40)
        self.assertEqual(payload["missing_targets"], [])
        self.assertGreater(payload["diversification"]["unique_target_profiles"], 1)

    def test_shadow_compare_reports_missing_targets(self) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, load_config())
            conn.commit()
        payload = shadow_compare.build_shadow_compare(self.db_path)
        self.assertFalse(payload["ok"])
        self.assertEqual(len(payload["missing_targets"]), 40)
        self.assertEqual(payload["gate_failures"][0]["gate"], "missing_targets")

    def test_shadow_compare_reports_target_without_score_as_unsafe(self) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, load_config())
            state_db.set_slot_target(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "profile_key": "unknown:batch:2048",
                    "mode": "base_fill",
                    "decision_price_usd": 0.64,
                    "expected_profit_day": 1.0,
                    "reason": "test",
                },
            )
            conn.commit()
        payload = shadow_compare.build_shadow_compare(self.db_path)
        self.assertFalse(payload["ok"])
        self.assertTrue(any(item["reason"] == "missing_profile_score" for item in payload["unsafe_targets"]))

    def test_protected_running_positive_below_min_profit_is_warning_not_failure(self) -> None:
        fleet_scheduler.schedule_once(db_path=self.db_path, price=0.64, fee=0.01, dry_run=False)
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "observed_profile_key": "3080:batch:2048",
                    "observed_status": "running",
                    "protected": True,
                },
            )
            state_db.upsert_profile_score(
                conn,
                {
                    "profile_key": "3080:batch:2048",
                    "mode": "base_fill",
                    "decision_price_usd": 0.64,
                    "expected_profit_day": 0.02,
                    "score": 1.0,
                    "risk_tier": "marginal",
                },
            )
            state_db.set_slot_target(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "profile_key": "3080:batch:2048",
                    "mode": "base_fill",
                    "decision_price_usd": 0.64,
                    "expected_profit_day": 0.02,
                    "protected": True,
                    "reason": "base_fill:protected_observed_profile",
                },
            )
            conn.commit()

        payload = shadow_compare.build_shadow_compare(self.db_path)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["unsafe_targets"], [])
        self.assertTrue(any(item["reason"] == "protected_positive_marginal" for item in payload["warnings"]))


if __name__ == "__main__":
    unittest.main()
