from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock


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
        maintenance_items = [item for item in plan if item["name"] == "salad-orch-v2-maintenance"]
        self.assertEqual(len(maintenance_items), 1)
        self.assertIn("--apply", maintenance_items[0]["cmd"])

    def test_supervisor_availability_probe_includes_low_priority(self) -> None:
        plan = supervisor.process_plan(db_path=self.db_path)
        probe = next(item for item in plan if item["name"] == "salad-orch-v2-availability")

        self.assertIn("--priorities", probe["cmd"])
        self.assertIn("batch,low", probe["cmd"])
        self.assertIn("--org-parallelism", probe["cmd"])
        self.assertIn("10", probe["cmd"])
        self.assertIn("--profile-parallelism", probe["cmd"])
        self.assertIn("4", probe["cmd"])
        self.assertIn("--interval", probe["cmd"])
        self.assertIn("60", probe["cmd"])

    def test_supervisor_availability_parallelism_can_be_overridden(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "PRL_AVAILABILITY_ORG_PARALLELISM": "6",
                "PRL_AVAILABILITY_PROFILE_PARALLELISM": "3",
            },
            clear=False,
        ):
            plan = supervisor.process_plan(db_path=self.db_path)
        probe = next(item for item in plan if item["name"] == "salad-orch-v2-availability")

        self.assertEqual(probe["cmd"][probe["cmd"].index("--org-parallelism") + 1], "6")
        self.assertEqual(probe["cmd"][probe["cmd"].index("--profile-parallelism") + 1], "3")

    def test_supervisor_includes_fleet_audit_process(self) -> None:
        plan = supervisor.process_plan(db_path=self.db_path)
        audit = next(item for item in plan if item["name"] == "salad-orch-v2-audit")

        self.assertEqual(audit["heartbeat"], "fleet_audit")
        self.assertIn("fleet_audit.py", " ".join(audit["cmd"]))
        self.assertIn("--interval", audit["cmd"])
        self.assertIn("300", audit["cmd"])
        self.assertIn("--balance-interval", audit["cmd"])
        self.assertIn("3600", audit["cmd"])
        self.assertIn("--balance-file", audit["cmd"])
        self.assertIn("state/salad_balances.json", audit["cmd"])

    def test_supervisor_includes_portal_balance_process(self) -> None:
        with mock.patch.object(supervisor, "has_multi_balance_accounts", return_value=True):
            plan = supervisor.process_plan(db_path=self.db_path)
        balances = next(item for item in plan if item["name"] == "salad-orch-v2-balances")

        self.assertEqual(balances["heartbeat"], "portal_balances")
        self.assertIn("portal_multi_balances.py", " ".join(balances["cmd"]))
        self.assertIn("--interval", balances["cmd"])
        self.assertIn("60", balances["cmd"])
        self.assertIn("--balance-file", balances["cmd"])
        self.assertIn("state/salad_balances.json", balances["cmd"])

    def test_supervisor_can_fallback_to_single_portal_balance_process(self) -> None:
        with mock.patch.object(supervisor, "has_multi_balance_accounts", return_value=False):
            plan = supervisor.process_plan(db_path=self.db_path)
        balances = next(item for item in plan if item["name"] == "salad-orch-v2-balances")

        self.assertIn("portal_balances.py", " ".join(balances["cmd"]))
        self.assertIn("--cookie-jar", balances["cmd"])
        self.assertIn("state/portal_cookies.txt", balances["cmd"])

    def test_supervisor_portal_balance_interval_can_be_overridden(self) -> None:
        with mock.patch.dict("os.environ", {"PRL_PORTAL_BALANCE_INTERVAL_SECONDS": "120"}, clear=False):
            plan = supervisor.process_plan(db_path=self.db_path)
        balances = next(item for item in plan if item["name"] == "salad-orch-v2-balances")

        interval_index = balances["cmd"].index("--interval") + 1
        self.assertEqual(balances["cmd"][interval_index], "120")

    def test_supervisor_tmux_sessions_load_dotenv(self) -> None:
        command = supervisor.tmux_command("salad-test", ["python3", "scripts/price_oracle.py", "--loop"])

        self.assertEqual(command[:5], ["tmux", "new-session", "-d", "-s", "salad-test"])
        self.assertIn("if [ -f .env ]; then set -a; . ./.env; set +a; fi", command[-1])
        self.assertIn("unset PRL_ENABLED_ORGS", command[-1])
        self.assertIn("export SALAD_FLEET_CONFIG_PATH=${SALAD_FLEET_CONFIG_PATH:-config/fleet.current.json}", command[-1])
        self.assertIn(
            "export PRL_AVAILABILITY_ZERO_BALANCE_CREDIT_PROBE=${PRL_AVAILABILITY_ZERO_BALANCE_CREDIT_PROBE:-1}",
            command[-1],
        )
        self.assertIn("python3 scripts/price_oracle.py --loop", command[-1])

    def test_supervisor_includes_runtime_monitor_process(self) -> None:
        plan = supervisor.process_plan(db_path=self.db_path)
        monitor = next(item for item in plan if item["name"] == "salad-orch-v2-monitor")

        self.assertEqual(monitor["heartbeat"], "runtime_monitor")
        self.assertIn("runtime_monitor.py", " ".join(monitor["cmd"]))
        self.assertIn("--loop", monitor["cmd"])
        self.assertIn("--skip-shadow-workers", monitor["cmd"])
        self.assertNotIn("--apply-all-orgs-pending", monitor["cmd"])

    def test_supervisor_runtime_monitor_can_apply_all_orgs_when_enabled(self) -> None:
        plan = supervisor.process_plan(runtime_monitor_apply=True, db_path=self.db_path)
        monitor = next(item for item in plan if item["name"] == "salad-orch-v2-monitor")

        self.assertIn("--apply-all-orgs-pending", monitor["cmd"])
        self.assertIn("--confirm-live-actions", monitor["cmd"])
        self.assertIn("--require-secrets", monitor["cmd"])

    def test_supervisor_apply_workers_does_not_also_apply_runtime_monitor(self) -> None:
        plan = supervisor.process_plan(apply_workers=True, db_path=self.db_path)
        monitor = next(item for item in plan if item["name"] == "salad-orch-v2-monitor")

        self.assertNotIn("--apply-all-orgs-pending", monitor["cmd"])

    def test_supervisor_uses_live_stack_session_names(self) -> None:
        names = {item["name"] for item in supervisor.process_plan(db_path=self.db_path)}

        self.assertIn("salad-orch-v2-price", names)
        self.assertIn("salad-orch-v2-availability", names)
        self.assertIn("salad-orch-v2-balances", names)
        self.assertIn("salad-orch-v2-guard", names)
        self.assertIn("salad-orch-v2-monitor", names)


if __name__ == "__main__":
    unittest.main()
