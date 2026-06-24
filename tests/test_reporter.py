from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import reporter
import state_db
from config_loader import load_config


class ReporterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(pathlib.Path(self.tmpdir.name) / "fleet.db")
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, load_config())
            conn.commit()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_live_th_falls_back_to_latest_slot_snapshots(self) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.record_profit_snapshot(
                conn,
                {
                    "at_utc": "2026-06-24T11:59:00+00:00",
                    "scope": "slot",
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-03",
                    "profile_key": "4090:batch:2048",
                    "decision_price_usd": 0.64,
                    "th": 150.0,
                    "cost_day": 3.36,
                    "revenue_day": 4.0,
                    "profit_day": 0.64,
                    "payload": {"worker": "kray-prl-old"},
                },
            )
            state_db.record_profit_snapshot(
                conn,
                {
                    "at_utc": "2026-06-24T12:00:00+00:00",
                    "scope": "slot",
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "profile_key": "3090:batch:2048",
                    "decision_price_usd": 0.64,
                    "th": 100.5,
                    "cost_day": 2.16,
                    "revenue_day": 3.0,
                    "profit_day": 0.84,
                    "payload": {"worker": "kray-prl-test"},
                },
            )
            state_db.record_profit_snapshot(
                conn,
                {
                    "at_utc": "2026-06-24T12:00:00+00:00",
                    "scope": "slot",
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-02",
                    "profile_key": "3080:batch:2048",
                    "decision_price_usd": 0.64,
                    "th": 0,
                    "cost_day": 1.44,
                    "revenue_day": 0,
                    "profit_day": -1.44,
                    "payload": {"worker": "NO_POOL_HASHRATE"},
                },
            )
            conn.commit()

        report = reporter.build_report(self.db_path)

        self.assertEqual(report["live_th_source"], "profit_snapshots")
        self.assertEqual(report["live_hashing_gpus"], 1)
        self.assertEqual(report["snapshot_live_hashing_gpus"], 1)
        self.assertEqual(report["live_th"], 100.5)
        self.assertEqual(report["snapshot_live_th"], 100.5)
        self.assertEqual(report["snapshot_live_at_utc"], "2026-06-24T12:00:00+00:00")

    def test_slot_hashrate_wins_over_snapshot_fallback(self) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "observed_status": "running",
                    "observed_profile_key": "3090:batch:2048",
                    "live_hashrate_th": 111.5,
                },
            )
            state_db.record_profit_snapshot(
                conn,
                {
                    "at_utc": "2026-06-24T12:00:00+00:00",
                    "scope": "slot",
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "profile_key": "3090:batch:2048",
                    "decision_price_usd": 0.64,
                    "th": 100.5,
                    "cost_day": 2.16,
                    "revenue_day": 3.0,
                    "profit_day": 0.84,
                    "payload": {"worker": "kray-prl-test"},
                },
            )
            conn.commit()

        report = reporter.build_report(self.db_path)

        self.assertEqual(report["live_th_source"], "slots")
        self.assertEqual(report["live_hashing_gpus"], 1)
        self.assertEqual(report["live_th"], 111.5)
        self.assertEqual(report["snapshot_live_th"], 100.5)


if __name__ == "__main__":
    unittest.main()
