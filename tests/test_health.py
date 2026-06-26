from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import health
import state_db
from config_loader import load_config


class HealthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(pathlib.Path(self.tmpdir.name) / "fleet.db")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_health_is_down_when_slots_have_no_targets(self) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, load_config())
            conn.commit()
        payload = health.build_health(self.db_path)
        self.assertEqual(payload["health"], "down")
        self.assertEqual(payload["target_count"], 0)
        self.assertEqual(payload["slot_count"], 40)

    def test_health_is_degraded_when_failure_exists(self) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, load_config())
            state_db.record_failure(
                conn,
                "guard",
                severity="warning",
                error_type="RuntimeError",
                message="test failure",
            )
            state_db.set_slot_target(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "profile_key": "4090:batch:2048",
                    "mode": "base_fill",
                    "decision_price_usd": 0.64,
                    "expected_profit_day": 1.0,
                    "reason": "test",
                },
            )
            conn.commit()
        payload = health.build_health(self.db_path)
        self.assertEqual(payload["health"], "degraded")
        self.assertEqual(len(payload["runtime_failures"]), 1)

    def test_health_reports_replica_quota_blockers_without_marking_down(self) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, load_config())
            state_db.set_slot_target(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "profile_key": "4090:batch:2048",
                    "mode": "base_fill",
                    "decision_price_usd": 0.64,
                    "expected_profit_day": 1.0,
                    "reason": "test",
                },
            )
            state_db.upsert_org_replica_quota(
                conn,
                {
                    "org_label": "kray",
                    "quota": 0,
                    "used": 0,
                    "available": 0,
                    "status": "zero_quota",
                    "reason": "container_replicas_quota_zero",
                    "source": "test",
                },
            )
            conn.commit()

        payload = health.build_health(self.db_path)

        self.assertEqual(payload["health"], "healthy")
        self.assertEqual(len(payload["quota_blockers"]), 1)
        self.assertEqual(payload["quota_blockers"][0]["org_label"], "kray")


if __name__ == "__main__":
    unittest.main()
