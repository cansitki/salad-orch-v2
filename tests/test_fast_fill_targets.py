from __future__ import annotations

import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fast_fill_targets


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


if __name__ == "__main__":
    unittest.main()
