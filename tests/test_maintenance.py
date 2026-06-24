from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import maintenance
import state_db
import supervisor


class MaintenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(pathlib.Path(self.tmpdir.name) / "fleet.db")
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            conn.execute(
                """
                INSERT INTO events(at_utc, source, level, event_type, message, payload_json)
                VALUES('2026-01-01T00:00:00+00:00', 'test', 'info', 'old', 'old event', '{}')
                """
            )
            conn.execute(
                """
                INSERT INTO events(at_utc, source, level, event_type, message, payload_json)
                VALUES(datetime('now'), 'test', 'info', 'new', 'new event', '{}')
                """
            )
            conn.commit()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def event_count(self) -> int:
        with state_db.connect(self.db_path) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def test_dry_run_counts_without_deleting(self) -> None:
        payload = maintenance.maintenance_once(db_path=self.db_path, dry_run=True, retention_days={"events": 7})
        self.assertGreaterEqual(payload["deleted"]["events"], 1)
        self.assertEqual(self.event_count(), 2)

    def test_apply_deletes_old_rows_and_writes_heartbeat(self) -> None:
        payload = maintenance.maintenance_once(db_path=self.db_path, dry_run=False, retention_days={"events": 7})
        self.assertGreaterEqual(payload["deleted"]["events"], 1)
        with state_db.connect(self.db_path) as conn:
            old_count = conn.execute("SELECT COUNT(*) FROM events WHERE event_type = 'old'").fetchone()[0]
            heartbeat = conn.execute("SELECT * FROM heartbeats WHERE process_name = 'maintenance'").fetchone()
        self.assertEqual(old_count, 0)
        self.assertIsNotNone(heartbeat)

    def test_supervisor_can_include_maintenance_process(self) -> None:
        plan = supervisor.process_plan(include_maintenance=True, maintenance_apply=True, db_path=self.db_path)
        maintenance_items = [item for item in plan if item["name"] == "salad-maintenance"]
        self.assertEqual(len(maintenance_items), 1)
        self.assertIn("--apply", maintenance_items[0]["cmd"])

    def test_supervisor_tmux_sessions_load_dotenv(self) -> None:
        command = supervisor.tmux_command("salad-test", ["python3", "scripts/price_oracle.py", "--loop"])

        self.assertEqual(command[:5], ["tmux", "new-session", "-d", "-s", "salad-test"])
        self.assertIn("if [ -f .env ]; then set -a; . ./.env; set +a; fi", command[-1])
        self.assertIn("python3 scripts/price_oracle.py --loop", command[-1])


if __name__ == "__main__":
    unittest.main()
