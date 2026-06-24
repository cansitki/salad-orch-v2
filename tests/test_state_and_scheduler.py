from __future__ import annotations

import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest


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


if __name__ == "__main__":
    unittest.main()
