from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta

import requests


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fast_fill_targets
import state_db


def container(status: str, **counts: int) -> dict:
    return {
        "current_state": {
            "status": status,
            "instance_status_counts": counts,
        }
    }


def target(slot: str, existing: dict | None = None, *, workers: int = 0, th: float = 0.0) -> dict:
    return {
        "slot_name": slot,
        "profile_key": "3060ti:batch:2048",
        "live_worker_count": workers,
        "live_worker_th": th,
        "_existing_container": existing,
    }


class FastFillTargetSelectionTest(unittest.TestCase):
    def test_actionable_limit_applies_after_skipping_active_targets(self) -> None:
        targets = [
            target("active", container("running", running_count=1), workers=1, th=80.0),
            target("missing-1"),
            target("missing-2"),
        ]

        actionable, skipped = fast_fill_targets._split_actionable_targets(
            targets,
            touch_active=False,
            actionable_limit=1,
        )

        self.assertEqual([item["slot_name"] for item in actionable], ["missing-1"])
        self.assertEqual([item["action"] for item in skipped], ["skip_active_container", "defer_actionable_limit"])
        self.assertEqual(skipped[0]["slot_name"], "active")
        self.assertEqual(skipped[1]["slot_name"], "missing-2")

    def test_active_without_hash_detects_pending_or_running_slots_only(self) -> None:
        rows = [
            target("running-nohash", container("running", running_count=1)),
            target("deploying-nohash", container("deploying", allocating_count=1)),
            target("running-hashing", container("running", running_count=1), workers=1, th=70.0),
            target("missing"),
        ]

        self.assertEqual(
            [fast_fill_targets._active_without_hash_target(row) for row in rows],
            [True, True, False, False],
        )

    def test_min_profit_filter_skips_negative_targets(self) -> None:
        rows = [
            {**target("negative"), "expected_profit_day": -0.01},
            {**target("positive"), "expected_profit_day": 0.02},
        ]

        eligible, skipped = fast_fill_targets._split_min_profit_targets(rows, min_profit_day=0.0)

        self.assertEqual([item["slot_name"] for item in eligible], ["positive"])
        self.assertEqual([item["action"] for item in skipped], ["skip_below_min_profit"])
        self.assertEqual(skipped[0]["slot_name"], "negative")

    def test_recent_guard_stop_cooldown_skips_actionable_target(self) -> None:
        targets = [
            target("recent-stop"),
            target("normal"),
        ]
        cooldowns = {"recent-stop": {"age_seconds": 30.0, "remaining_seconds": 570.0}}

        actionable, skipped = fast_fill_targets._split_actionable_targets(
            targets,
            touch_active=False,
            actionable_limit=0,
            guard_stop_cooldowns=cooldowns,
        )

        self.assertEqual([item["slot_name"] for item in actionable], ["normal"])
        self.assertEqual(skipped[0]["slot_name"], "recent-stop")
        self.assertEqual(skipped[0]["action"], "skip_recent_guard_stop")
        self.assertEqual(skipped[0]["cooldown_remaining_seconds"], 570.0)

    def test_recent_guard_stop_cooldowns_reads_attempts_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(pathlib.Path(tmpdir) / "fleet.db")
            with state_db.connect(db_path) as conn:
                state_db.init_db(conn)
                state_db.record_attempt(
                    conn,
                    {
                        "at_utc": (datetime.now(UTC) - timedelta(seconds=60)).isoformat(timespec="seconds"),
                        "org_label": "kray",
                        "slot_name": "recent-stop",
                        "action": "guard_stop",
                        "ok": True,
                    },
                )
                state_db.record_attempt(
                    conn,
                    {
                        "at_utc": (datetime.now(UTC) - timedelta(seconds=3600)).isoformat(timespec="seconds"),
                        "org_label": "kray",
                        "slot_name": "old-stop",
                        "action": "guard_stop",
                        "ok": True,
                    },
                )
                conn.commit()

            cooldowns = fast_fill_targets._recent_guard_stop_cooldowns(
                db_path,
                "kray",
                cooldown_seconds=600,
            )

        self.assertIn("recent-stop", cooldowns)
        self.assertNotIn("old-stop", cooldowns)
        self.assertGreater(cooldowns["recent-stop"]["remaining_seconds"], 0)

    def test_touch_active_patch_does_not_start_already_active_container(self) -> None:
        class Watch:
            ORG = "kray"
            PROJECT = "default"

            class Candidate:
                def __init__(self, label, priority, gpu_keys, memory):
                    self.label = label
                    self.priority = priority
                    self.gpu_keys = gpu_keys
                    self.memory = memory

            def __init__(self):
                self.requests = []

            def container_payload(self, slot_name, _candidate):
                return {"name": slot_name, "container": {"resources": {"gpu_classes": ["gpu-rtx-3070"]}}}

            def request(self, method, path, payload=None, **kwargs):
                self.requests.append((method, path, kwargs))
                if method == "POST" and path.endswith("/containers"):
                    response = requests.Response()
                    response.status_code = 400
                    response._content = b'{"title":"container already exists"}'
                    raise requests.HTTPError(response=response)
                return {"ok": True}

        row = {
            **target("active", container("running", running_count=1)),
            "profile_key": "3070:batch:4096",
            "label": "RTX 3070 batch",
            "priority": "batch",
            "gpu_key": "3070",
            "memory_mb": 4096,
        }
        watch = Watch()

        result = fast_fill_targets._fast_apply_one(
            watch,
            row,
            start_after=True,
            patch_existing=True,
            touch_active=True,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "patch")
        self.assertTrue(result["patched"])
        self.assertFalse(result["started"])
        self.assertFalse(any(method == "POST" and path.endswith("/start") for method, path, _ in watch.requests))


if __name__ == "__main__":
    unittest.main()
