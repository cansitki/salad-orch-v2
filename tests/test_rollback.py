from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fleet_scheduler
import rollback
import state_db


class RollbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(pathlib.Path(self.tmpdir.name) / "fleet.db")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def first_target_profile(self) -> str:
        with state_db.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT profile_key
                FROM slot_targets
                WHERE org_label = 'kray' AND slot_name = 'prl-kray-roi-01'
                """
            ).fetchone()
        return str(row["profile_key"])

    def test_checkpoint_restore_round_trip(self) -> None:
        fleet_scheduler.schedule_once(db_path=self.db_path, price=0.64, fee=0.01, dry_run=False)
        original = self.first_target_profile()
        created = rollback.create_checkpoint(self.db_path, name="before-test", stage="test")
        with state_db.connect(self.db_path) as conn:
            state_db.set_slot_target(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "profile_key": "changed:batch:2048",
                    "mode": "base_fill",
                    "decision_price_usd": 0.64,
                    "expected_profit_day": 1.0,
                    "reason": "test_mutation",
                },
            )
            conn.commit()
        self.assertEqual(self.first_target_profile(), "changed:batch:2048")
        dry_run = rollback.restore_checkpoint(self.db_path, checkpoint_id=int(created["id"]), apply=False)
        self.assertFalse(dry_run["apply"])
        self.assertEqual(self.first_target_profile(), "changed:batch:2048")
        applied = rollback.restore_checkpoint(self.db_path, checkpoint_id=int(created["id"]), apply=True)
        self.assertTrue(applied["apply"])
        self.assertEqual(self.first_target_profile(), original)

    def test_list_checkpoints(self) -> None:
        fleet_scheduler.schedule_once(db_path=self.db_path, price=0.64, fee=0.01, dry_run=False)
        rollback.create_checkpoint(self.db_path, name="one", stage="test")
        payload = rollback.list_checkpoints(self.db_path)
        self.assertEqual(payload["checkpoints"][0]["name"], "one")


if __name__ == "__main__":
    unittest.main()
