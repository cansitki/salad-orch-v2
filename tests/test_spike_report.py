from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import spike_report
import state_db


class SpikeReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self.tmpdir.name)
        self.db_path = str(self.tmp_path / "fleet.db")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def record_unstable_profile(self, profile_key: str = "4070tis:low:2048") -> None:
        now = datetime.now(UTC).replace(microsecond=0)
        gpu, priority, _memory = profile_key.split(":", 2)
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            for index in range(3):
                state_db.record_slot_spike_event(
                    conn,
                    {
                        "at_utc": (now - timedelta(minutes=index + 1)).isoformat(timespec="seconds"),
                        "org_label": "kray",
                        "slot_name": f"prl-kray-roi-{index + 1:02d}",
                        "issue_type": "negative",
                        "profile_key": profile_key,
                        "gpu_key": gpu,
                        "priority": priority,
                        "profit_day": -0.5,
                    },
                )
            conn.commit()

    def test_heartbeat_applies_wildcard_cooldowns_for_unstable_profiles(self) -> None:
        self.record_unstable_profile()
        env = {
            "SALAD_PRL_ENV": str(self.tmp_path / "missing.env"),
            "PRL_ENABLED_ORGS": "kray,kry1",
            "PRL_SPIKE_PROFILE_COOLDOWN_SECONDS": "1800",
        }

        with patch.dict(os.environ, env, clear=True):
            summary = spike_report.report_once(
                db_path=self.db_path,
                write_heartbeat=True,
                apply_cooldowns=True,
                limit=10,
            )

        self.assertEqual(summary["cooldown_profile_count"], 1)
        self.assertEqual(summary["cooldown_org_profile_count"], 2)
        with state_db.connect(self.db_path) as conn:
            cooldowns = state_db.active_search_cooldowns(conn)
            rows = conn.execute(
                """
                SELECT org_label, slot_name, profile_key, reason
                FROM search_cooldowns
                ORDER BY org_label
                """
            ).fetchall()
            heartbeat = conn.execute(
                "SELECT payload_json FROM heartbeats WHERE process_name = 'spike_report'"
            ).fetchone()

        self.assertIn(("kray", "*", "4070tis:low:2048"), cooldowns)
        self.assertIn(("kry1", "*", "4070tis:low:2048"), cooldowns)
        self.assertEqual([row["reason"] for row in rows], ["unstable_recent_spikes", "unstable_recent_spikes"])
        payload = json.loads(heartbeat["payload_json"])
        self.assertEqual(payload["cooldown_profiles"], 1)
        self.assertEqual(payload["cooldown_org_profiles"], 2)

    def test_read_only_report_does_not_apply_cooldowns(self) -> None:
        self.record_unstable_profile()
        env = {
            "SALAD_PRL_ENV": str(self.tmp_path / "missing.env"),
            "PRL_ENABLED_ORGS": "kray,kry1",
        }

        with patch.dict(os.environ, env, clear=True):
            summary = spike_report.report_once(db_path=self.db_path, write_heartbeat=False, limit=10)

        self.assertNotIn("cooldowns", summary)
        with state_db.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM search_cooldowns").fetchone()[0]
        self.assertEqual(count, 0)

    def test_heartbeat_stale_window_can_match_loop_interval(self) -> None:
        self.record_unstable_profile()

        spike_report.report_once(
            db_path=self.db_path,
            write_heartbeat=True,
            apply_cooldowns=False,
            heartbeat_stale_after_seconds=600,
            limit=10,
        )

        with state_db.connect(self.db_path) as conn:
            heartbeat = conn.execute(
                "SELECT stale_after_seconds FROM heartbeats WHERE process_name = 'spike_report'"
            ).fetchone()
        self.assertEqual(heartbeat["stale_after_seconds"], 600)

    def test_cooldown_scan_is_not_limited_by_display_limit(self) -> None:
        self.record_unstable_profile("4070tis:low:2048")
        self.record_unstable_profile("5090:batch:2048")
        env = {
            "SALAD_PRL_ENV": str(self.tmp_path / "missing.env"),
            "PRL_ENABLED_ORGS": "kray,kry1",
            "PRL_SPIKE_COOLDOWN_SCAN_LIMIT": "100",
        }

        with patch.dict(os.environ, env, clear=True):
            summary = spike_report.report_once(
                db_path=self.db_path,
                write_heartbeat=True,
                apply_cooldowns=True,
                limit=1,
            )

        self.assertEqual(len(summary["profiles"]), 1)
        self.assertEqual(summary["cooldown_profile_count"], 2)
        with state_db.connect(self.db_path) as conn:
            cooldowns = state_db.active_search_cooldowns(conn)

        self.assertIn(("kray", "*", "4070tis:low:2048"), cooldowns)
        self.assertIn(("kray", "*", "5090:batch:2048"), cooldowns)
        self.assertIn(("kry1", "*", "4070tis:low:2048"), cooldowns)
        self.assertIn(("kry1", "*", "5090:batch:2048"), cooldowns)


if __name__ == "__main__":
    unittest.main()
