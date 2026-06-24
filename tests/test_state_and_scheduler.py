from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_scheduler
import state_db
from config_loader import load_config


class StateAndSchedulerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(pathlib.Path(self.tmpdir.name) / "fleet.db")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_state_db_syncs_default_orgs_as_ten_slot_units(self) -> None:
        config = load_config()
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, config)
            conn.commit()
            orgs = conn.execute("SELECT COUNT(*) FROM organizations WHERE enabled = 1").fetchone()[0]
            slots = conn.execute("SELECT COUNT(*) FROM slots").fetchone()[0]
        self.assertEqual(orgs, 4)
        self.assertEqual(slots, 40)
        self.assertEqual(config.target_slot_count(), 40)

    def test_scheduler_assigns_diversified_profitable_batch_targets(self) -> None:
        payload = fleet_scheduler.schedule_once(
            db_path=self.db_path,
            price=0.64,
            fee=0.01,
            dry_run=False,
            width=10,
        )
        self.assertEqual(payload["assigned_targets"], 40)
        self.assertEqual(len(payload["profile_counts"]), 10)
        self.assertTrue(all(":low:" not in key for key in payload["profile_counts"]))
        with state_db.connect(self.db_path) as conn:
            target_count = conn.execute("SELECT COUNT(*) FROM slot_targets").fetchone()[0]
            profile_count = conn.execute("SELECT COUNT(*) FROM gpu_profiles").fetchone()[0]
        self.assertEqual(target_count, 40)
        self.assertGreaterEqual(profile_count, 10)

    def test_scheduler_preserves_protected_running_slot_target(self) -> None:
        config = load_config()
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, config)
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
        fleet_scheduler.schedule_once(db_path=self.db_path, price=0.64, fee=0.01, dry_run=False)
        with state_db.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT profile_key, protected, reason
                FROM slot_targets
                WHERE org_label = 'kray' AND slot_name = 'prl-kray-roi-01'
                """
            ).fetchone()
        self.assertEqual(row["profile_key"], "3090:batch:2048")
        self.assertEqual(row["protected"], 1)
        self.assertIn("protected_observed_profile", row["reason"])

    def test_scheduler_respects_recent_zero_availability_for_org_profile(self) -> None:
        config = load_config()
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, config)
            state_db.upsert_profile_availability(
                conn,
                {
                    "org_label": "kray",
                    "profile_key": "4090:batch:2048",
                    "available_count": 0,
                    "ok": True,
                },
            )
            conn.commit()
        payload = fleet_scheduler.schedule_once(db_path=self.db_path, price=0.64, fee=0.01, dry_run=False)
        kray_4090 = [
            target
            for target in payload["targets"]
            if target["org_label"] == "kray" and target["profile_key"] == "4090:batch:2048"
        ]
        self.assertEqual(kray_4090, [])

    def test_scheduler_respects_active_wildcard_search_cooldown(self) -> None:
        config = load_config()
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, config)
            state_db.record_search_state(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "*",
                    "profile_key": "4090:batch:2048",
                    "sleep_until_utc": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(timespec="seconds"),
                    "attempts": 20,
                    "reason": "availability_zero",
                },
            )
            conn.commit()
        payload = fleet_scheduler.schedule_once(db_path=self.db_path, price=0.64, fee=0.01, dry_run=False)
        self.assertFalse(
            any(
                target["org_label"] == "kray" and target["profile_key"] == "4090:batch:2048"
                for target in payload["targets"]
            )
        )


if __name__ == "__main__":
    unittest.main()
