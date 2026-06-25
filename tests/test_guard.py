from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import guard
import profile_scorer
import state_db
from config_loader import load_config


class GuardDecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(pathlib.Path(self.tmpdir.name) / "fleet.db")
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, load_config())
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

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def make_issue_old(self, issue_type: str) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.record_guard_issue(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "issue_type": issue_type,
                    "first_seen_utc": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(timespec="seconds"),
                    "payload": {},
                },
            )
            conn.commit()

    def test_guard_retargets_no_hash_after_grace(self) -> None:
        self.make_issue_old("no_hash")
        decisions = guard.enforce_issues(
            db_path=self.db_path,
            decision_price=0.64,
            apply=False,
            analysis={
                "fresh_workers": 3,
                "running_no_live_billable_slots": [
                    {"org": "kray", "slot": "prl-kray-roi-01", "cost_day": 1.0}
                ],
                "negative_slots": [],
            },
        )
        self.assertEqual(decisions[0]["action"], "retarget")
        self.assertNotEqual(decisions[0]["target_profile_key"], "3090:batch:2048")
        with state_db.connect(self.db_path) as conn:
            target = conn.execute(
                """
                SELECT profile_key, reason
                FROM slot_targets
                WHERE org_label = 'kray' AND slot_name = 'prl-kray-roi-01'
                """
            ).fetchone()
        self.assertEqual(target["profile_key"], decisions[0]["target_profile_key"])
        self.assertEqual(target["reason"], "guard_no_hash_retarget")

    def test_guard_uses_state_age_for_old_no_hash_issue(self) -> None:
        decisions = guard.enforce_issues(
            db_path=self.db_path,
            decision_price=0.64,
            apply=False,
            analysis={
                "fresh_workers": 3,
                "running_no_live_billable_slots": [
                    {
                        "org": "kray",
                        "slot": "prl-kray-roi-01",
                        "cost_day": 1.0,
                        "state_age_seconds": 600,
                    }
                ],
                "negative_slots": [],
            },
        )
        self.assertEqual(decisions[0]["action"], "retarget")
        self.assertEqual(decisions[0]["age_seconds"], 600)

    def test_guard_uses_probe_fallback_when_all_profiles_report_zero_availability(self) -> None:
        scores = profile_scorer.score_profiles(
            db_path=self.db_path,
            decision_price_usd=0.64,
            pearl_fee_rate=0.01,
            write=False,
        )
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            for row in scores:
                if row.get("eligible"):
                    state_db.upsert_profile_availability(
                        conn,
                        {
                            "org_label": "kray",
                            "profile_key": row["profile_key"],
                            "available_count": 0,
                            "ok": True,
                        },
                    )
            conn.commit()

        self.make_issue_old("no_hash")
        decisions = guard.enforce_issues(
            db_path=self.db_path,
            decision_price=0.64,
            apply=False,
            analysis={
                "fresh_workers": 3,
                "running_no_live_billable_slots": [
                    {"org": "kray", "slot": "prl-kray-roi-01", "cost_day": 1.0}
                ],
                "negative_slots": [],
            },
        )

        self.assertEqual(decisions[0]["action"], "retarget")
        self.assertIsNotNone(decisions[0]["target_profile_key"])
        self.assertIn("availability_probe_fallback", decisions[0]["target"]["reason"])

    def test_guard_stops_when_no_profitable_replacement_exists(self) -> None:
        self.make_issue_old("negative")
        decisions = guard.enforce_issues(
            db_path=self.db_path,
            decision_price=0.01,
            apply=False,
            analysis={
                "fresh_workers": 3,
                "running_no_live_billable_slots": [],
                "negative_slots": [
                    {
                        "org": "kray",
                        "slot": "prl-kray-roi-01",
                        "gpu": "3090",
                        "priority": "batch",
                        "profit_day": -1.0,
                    }
                ],
            },
        )
        self.assertEqual(decisions[0]["action"], "stop")
        self.assertIsNone(decisions[0]["target_profile_key"])

    def test_successful_apply_clears_slot_runtime_failure(self) -> None:
        self.make_issue_old("negative")
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.record_failure(
                conn,
                guard.guard_failure_component("kray", "prl-kray-roi-01", "negative"),
                severity="warning",
                error_type="TypeError",
                message="old apply failure",
            )
            conn.commit()

        with patch("guard.apply_guard_target", return_value={"action": "retarget", "applied": True}):
            decisions = guard.enforce_issues(
                db_path=self.db_path,
                decision_price=0.64,
                apply=True,
                analysis={
                    "fresh_workers": 3,
                    "running_no_live_billable_slots": [],
                    "negative_slots": [
                        {
                            "org": "kray",
                            "slot": "prl-kray-roi-01",
                            "gpu": "3090",
                            "priority": "batch",
                            "profit_day": -1.0,
                        }
                    ],
                },
            )

        self.assertEqual(decisions[0]["action"], "retarget")
        with state_db.connect(self.db_path) as conn:
            failures = conn.execute("SELECT COUNT(*) FROM runtime_failures").fetchone()[0]
            attempt = conn.execute("SELECT ok FROM attempts ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(failures, 0)
        self.assertEqual(attempt["ok"], 1)

    def test_failed_guard_patch_cooldowns_target_profile(self) -> None:
        self.make_issue_old("negative")
        analysis = {
            "fresh_workers": 3,
            "running_no_live_billable_slots": [],
            "negative_slots": [
                {
                    "org": "kray",
                    "slot": "prl-kray-roi-01",
                    "gpu": "3090",
                    "priority": "batch",
                    "profit_day": -1.0,
                }
            ],
        }

        with patch("guard.apply_guard_target", side_effect=RuntimeError("patch_slot returned false")):
            decisions = guard.enforce_issues(
                db_path=self.db_path,
                decision_price=0.64,
                apply=True,
                analysis=analysis,
            )

        failed_profile = decisions[0]["target_profile_key"]
        self.assertEqual(decisions[0]["action"], "retarget")
        self.assertEqual(decisions[0]["cooldown_profile_key"], failed_profile)
        self.assertGreaterEqual(decisions[0]["cooldown_seconds"], 60)
        with state_db.connect(self.db_path) as conn:
            cooldowns = state_db.active_search_cooldowns(conn)
        self.assertIn(("kray", "prl-kray-roi-01", failed_profile), cooldowns)

        retry_decisions = guard.enforce_issues(
            db_path=self.db_path,
            decision_price=0.64,
            apply=False,
            analysis=analysis,
        )
        self.assertEqual(retry_decisions[0]["action"], "retarget")
        self.assertNotEqual(retry_decisions[0]["target_profile_key"], failed_profile)

    def test_guard_applies_once_when_same_slot_has_multiple_issues(self) -> None:
        self.make_issue_old("no_hash")
        self.make_issue_old("negative")

        with patch("guard.apply_guard_target", return_value={"action": "retarget", "applied": True}) as apply_mock:
            decisions = guard.enforce_issues(
                db_path=self.db_path,
                decision_price=0.64,
                apply=True,
                analysis={
                    "fresh_workers": 3,
                    "running_no_live_billable_slots": [
                        {"org": "kray", "slot": "prl-kray-roi-01", "cost_day": 1.0}
                    ],
                    "negative_slots": [
                        {
                            "org": "kray",
                            "slot": "prl-kray-roi-01",
                            "gpu": "3090",
                            "priority": "batch",
                            "profit_day": -1.0,
                        }
                    ],
                },
            )

        self.assertEqual(apply_mock.call_count, 1)
        self.assertEqual([decision["action"] for decision in decisions], ["retarget", "skip_duplicate"])

    def test_repeated_no_hash_marks_target_for_forced_restart(self) -> None:
        self.make_issue_old("no_hash")
        with state_db.connect(self.db_path) as conn:
            state_db.increment_guard_issue_action(conn, "kray", "prl-kray-roi-01", "no_hash")
            conn.commit()

        with patch("guard.apply_guard_target", return_value={"action": "retarget", "applied": True}) as apply_mock:
            decisions = guard.enforce_issues(
                db_path=self.db_path,
                decision_price=0.64,
                apply=True,
                analysis={
                    "fresh_workers": 3,
                    "running_no_live_billable_slots": [
                        {"org": "kray", "slot": "prl-kray-roi-01", "cost_day": 1.0}
                    ],
                    "negative_slots": [],
                },
            )

        self.assertEqual(decisions[0]["action"], "retarget")
        target = apply_mock.call_args.args[0]
        self.assertEqual(target["force_restart_reason"], "repeated_no_hash_after_guard_action")

    def test_apply_guard_target_reallocates_pre_patch_and_snapshot_instances(self) -> None:
        class FakeCandidate:
            def __init__(self, label, priority, gpu_ids, memory):
                self.label = label
                self.priority = priority
                self.gpu_ids = gpu_ids
                self.memory = memory

        class FakeWatch:
            ORG = "kray"
            PROJECT = "default"
            Candidate = FakeCandidate

            def __init__(self) -> None:
                self.patch_calls = []
                self.reallocate_calls = []
                self.slot_state_calls = 0

            def slot_state(self, slot_name):
                self.slot_state_calls += 1
                if self.slot_state_calls == 1:
                    return {}, [{"id": "pre-patch-instance"}]
                return {}, []

            def patch_slot(self, slot_name, candidate, reason, *, start_after=True):
                self.patch_calls.append(
                    {
                        "slot_name": slot_name,
                        "candidate": candidate.label,
                        "reason": reason,
                        "start_after": start_after,
                    }
                )
                return True

            def reallocate(self, slot_name, instance_id, reason):
                self.reallocate_calls.append((slot_name, instance_id, reason))

        watch = FakeWatch()
        target = {
            "org_label": "kray",
            "slot_name": "prl-kray-roi-01",
            "profile_key": "5090:batch:2048",
            "gpu_key": "5090",
            "priority": "batch",
            "memory_mb": 2048,
            "label": "PearlFortune RTX 5090",
            "decision_price_usd": 0.64,
            "expected_profit_day": 2.0,
            "snapshot_instance_id": "snapshot-instance",
        }

        with (
            patch("guard.org_worker.load_watch_module", return_value=watch),
            patch("guard.org_worker.install_rate_limited_request"),
        ):
            result = guard.apply_guard_target(target, db_path=self.db_path, reason="guard_negative")

        self.assertEqual(result["reallocated_instances"], ["pre-patch-instance", "snapshot-instance"])
        self.assertEqual(result["pre_patch_instances"], 1)
        self.assertEqual(result["post_patch_instances"], 0)
        self.assertEqual(watch.patch_calls[0]["start_after"], False)
        self.assertEqual(
            watch.reallocate_calls,
            [
                ("prl-kray-roi-01", "pre-patch-instance", "guard_negative"),
                ("prl-kray-roi-01", "snapshot-instance", "guard_negative"),
            ],
        )

    def test_apply_guard_target_restarts_when_snapshot_instance_is_hidden_from_salad(self) -> None:
        class FakeCandidate:
            def __init__(self, label, priority, gpu_ids, memory):
                self.label = label
                self.priority = priority
                self.gpu_ids = gpu_ids
                self.memory = memory

        class FakeWatch:
            ORG = "kray"
            PROJECT = "default"
            Candidate = FakeCandidate

            def __init__(self) -> None:
                self.reallocate_calls = []
                self.request_calls = []
                self.start_calls = []

            def slot_state(self, slot_name):
                return {}, []

            def patch_slot(self, slot_name, candidate, reason, *, start_after=True):
                return True

            def reallocate(self, slot_name, instance_id, reason):
                self.reallocate_calls.append((slot_name, instance_id, reason))

            def request(self, method, path, *args, **kwargs):
                self.request_calls.append((method, path))
                return {}

            def start_slot(self, slot_name, reason):
                self.start_calls.append((slot_name, reason))

        watch = FakeWatch()
        target = {
            "org_label": "kray",
            "slot_name": "prl-kray-roi-01",
            "profile_key": "4090:batch:2048",
            "gpu_key": "4090",
            "priority": "batch",
            "memory_mb": 2048,
            "label": "PearlFortune RTX 4090",
            "decision_price_usd": 0.64,
            "expected_profit_day": 2.0,
            "snapshot_instance_id": "hidden-instance",
        }

        with (
            patch("guard.org_worker.load_watch_module", return_value=watch),
            patch("guard.org_worker.install_rate_limited_request"),
        ):
            result = guard.apply_guard_target(target, db_path=self.db_path, reason="guard_negative")

        self.assertEqual(result["reallocated_instances"], ["hidden-instance"])
        self.assertTrue(result["restart_requested"])
        self.assertEqual(result["restart_reason"], "snapshot_instance_without_salad_instances")
        self.assertEqual(
            watch.request_calls,
            [("POST", "/organizations/kray/projects/default/containers/prl-kray-roi-01/stop")],
        )
        self.assertEqual(
            watch.start_calls,
            [("prl-kray-roi-01", "guard_negative:snapshot_instance_without_salad_instances")],
        )

    def test_apply_guard_target_restarts_when_successful_patch_has_no_visible_instances(self) -> None:
        class FakeCandidate:
            def __init__(self, label, priority, gpu_ids, memory):
                self.label = label
                self.priority = priority
                self.gpu_ids = gpu_ids
                self.memory = memory

        class FakeWatch:
            ORG = "kray"
            PROJECT = "default"
            Candidate = FakeCandidate

            def __init__(self) -> None:
                self.request_calls = []
                self.start_calls = []

            def slot_state(self, slot_name):
                return {}, []

            def patch_slot(self, slot_name, candidate, reason, *, start_after=True):
                return True

            def request(self, method, path, *args, **kwargs):
                self.request_calls.append((method, path))
                return {}

            def start_slot(self, slot_name, reason):
                self.start_calls.append((slot_name, reason))

        watch = FakeWatch()
        target = {
            "org_label": "kray",
            "slot_name": "prl-kray-roi-01",
            "profile_key": "5090:low:2048",
            "gpu_key": "5090",
            "priority": "low",
            "memory_mb": 2048,
            "label": "PearlFortune RTX 5090 low",
            "decision_price_usd": 0.64,
            "expected_profit_day": 1.0,
            "snapshot_instance_id": None,
        }

        with (
            patch("guard.org_worker.load_watch_module", return_value=watch),
            patch("guard.org_worker.install_rate_limited_request"),
        ):
            result = guard.apply_guard_target(target, db_path=self.db_path, reason="guard_no_hash")

        self.assertEqual(result["reallocated_instances"], [])
        self.assertTrue(result["restart_requested"])
        self.assertEqual(result["restart_reason"], "retarget_without_visible_instances")
        self.assertEqual(
            watch.request_calls,
            [("POST", "/organizations/kray/projects/default/containers/prl-kray-roi-01/stop")],
        )
        self.assertEqual(
            watch.start_calls,
            [("prl-kray-roi-01", "guard_no_hash:retarget_without_visible_instances")],
        )

    def test_apply_guard_target_restarts_for_repeated_no_hash_even_with_visible_instance(self) -> None:
        class FakeCandidate:
            def __init__(self, label, priority, gpu_ids, memory):
                self.label = label
                self.priority = priority
                self.gpu_ids = gpu_ids
                self.memory = memory

        class FakeWatch:
            ORG = "kray"
            PROJECT = "default"
            Candidate = FakeCandidate

            def __init__(self) -> None:
                self.reallocate_calls = []
                self.request_calls = []
                self.start_calls = []

            def slot_state(self, slot_name):
                return {}, [{"id": "visible-instance"}]

            def patch_slot(self, slot_name, candidate, reason, *, start_after=True):
                return True

            def reallocate(self, slot_name, instance_id, reason):
                self.reallocate_calls.append((slot_name, instance_id, reason))

            def request(self, method, path, *args, **kwargs):
                self.request_calls.append((method, path))
                return {}

            def start_slot(self, slot_name, reason):
                self.start_calls.append((slot_name, reason))

        watch = FakeWatch()
        target = {
            "org_label": "kray",
            "slot_name": "prl-kray-roi-01",
            "profile_key": "5090:low:2048",
            "gpu_key": "5090",
            "priority": "low",
            "memory_mb": 2048,
            "label": "PearlFortune RTX 5090 low",
            "decision_price_usd": 0.64,
            "expected_profit_day": 1.0,
            "force_restart_reason": "repeated_no_hash_after_guard_action",
        }

        with (
            patch("guard.org_worker.load_watch_module", return_value=watch),
            patch("guard.org_worker.install_rate_limited_request"),
        ):
            result = guard.apply_guard_target(target, db_path=self.db_path, reason="guard_no_hash")

        self.assertEqual(result["reallocated_instances"], ["visible-instance"])
        self.assertTrue(result["restart_requested"])
        self.assertEqual(result["restart_reason"], "repeated_no_hash_after_guard_action")
        self.assertEqual(
            watch.request_calls,
            [("POST", "/organizations/kray/projects/default/containers/prl-kray-roi-01/stop")],
        )
        self.assertEqual(
            watch.start_calls,
            [("prl-kray-roi-01", "guard_no_hash:repeated_no_hash_after_guard_action")],
        )

    def test_apply_guard_target_reallocates_current_instance_when_patch_fails(self) -> None:
        class FakeCandidate:
            def __init__(self, label, priority, gpu_ids, memory):
                self.label = label
                self.priority = priority
                self.gpu_ids = gpu_ids
                self.memory = memory

        class FakeWatch:
            ORG = "kray"
            PROJECT = "default"
            Candidate = FakeCandidate

            def __init__(self) -> None:
                self.reallocate_calls = []

            def slot_state(self, slot_name):
                return {}, [{"id": "current-instance"}]

            def patch_slot(self, slot_name, candidate, reason, *, start_after=True):
                return False

            def reallocate(self, slot_name, instance_id, reason):
                self.reallocate_calls.append((slot_name, instance_id, reason))

        watch = FakeWatch()
        target = {
            "org_label": "kray",
            "slot_name": "prl-kray-roi-01",
            "profile_key": "5090:batch:2048",
            "gpu_key": "5090",
            "priority": "batch",
            "memory_mb": 2048,
            "label": "PearlFortune RTX 5090",
            "decision_price_usd": 0.64,
            "expected_profit_day": 2.0,
            "snapshot_instance_id": "snapshot-instance",
        }

        with (
            patch("guard.org_worker.load_watch_module", return_value=watch),
            patch("guard.org_worker.install_rate_limited_request"),
        ):
            result = guard.apply_guard_target(target, db_path=self.db_path, reason="guard_no_hash")

        self.assertEqual(result["action"], "retarget_fallback")
        self.assertTrue(result["patch_failed"])
        self.assertEqual(result["reallocated_instances"], ["current-instance", "snapshot-instance"])
        self.assertFalse(result["restart_requested"])
        self.assertEqual(
            watch.reallocate_calls,
            [
                ("prl-kray-roi-01", "current-instance", "guard_no_hash:patch_failed"),
                ("prl-kray-roi-01", "snapshot-instance", "guard_no_hash:patch_failed"),
            ],
        )

    def test_apply_guard_target_restarts_when_patch_fails_without_visible_instances(self) -> None:
        class FakeCandidate:
            def __init__(self, label, priority, gpu_ids, memory):
                self.label = label
                self.priority = priority
                self.gpu_ids = gpu_ids
                self.memory = memory

        class FakeWatch:
            ORG = "kray"
            PROJECT = "default"
            Candidate = FakeCandidate

            def __init__(self) -> None:
                self.request_calls = []
                self.start_calls = []

            def slot_state(self, slot_name):
                return {}, []

            def patch_slot(self, slot_name, candidate, reason, *, start_after=True):
                return False

            def request(self, method, path, *args, **kwargs):
                self.request_calls.append((method, path))
                return {}

            def start_slot(self, slot_name, reason):
                self.start_calls.append((slot_name, reason))

        watch = FakeWatch()
        target = {
            "org_label": "kray",
            "slot_name": "prl-kray-roi-01",
            "profile_key": "5090:batch:2048",
            "gpu_key": "5090",
            "priority": "batch",
            "memory_mb": 2048,
            "label": "PearlFortune RTX 5090",
            "decision_price_usd": 0.64,
            "expected_profit_day": 2.0,
            "snapshot_instance_id": None,
        }

        with (
            patch("guard.org_worker.load_watch_module", return_value=watch),
            patch("guard.org_worker.install_rate_limited_request"),
        ):
            result = guard.apply_guard_target(target, db_path=self.db_path, reason="guard_no_hash")

        self.assertEqual(result["action"], "retarget_fallback")
        self.assertTrue(result["patch_failed"])
        self.assertEqual(result["reallocated_instances"], [])
        self.assertTrue(result["restart_requested"])
        self.assertEqual(result["restart_reason"], "patch_failed_without_visible_instances")
        self.assertEqual(
            watch.request_calls,
            [("POST", "/organizations/kray/projects/default/containers/prl-kray-roi-01/stop")],
        )
        self.assertEqual(
            watch.start_calls,
            [("prl-kray-roi-01", "guard_no_hash:patch_failed_without_visible_instances")],
        )

    def test_guard_snapshot_updates_slot_hashrate_and_workers(self) -> None:
        fake_snapshot = {
            "live_market_prl_price": 0.64,
            "fresh_workers": 1,
            "running_no_live_billable_slots": [],
            "totals": {
                "th": 111.5,
                "cost_day": 2.16,
                "revenue_day": 3.0,
                "profit_day": 0.84,
            },
            "slots": [
                {
                    "worker": "kray-prl-roi-01-pearlfortune-inst-1",
                    "slot": "prl-kray-roi-01",
                    "org": "kray",
                    "gpu": "3090",
                    "priority": "batch",
                    "th": 111.5,
                    "cost_day": 2.16,
                    "profit_day": 0.84,
                    "last_stats_at": "2026-06-24T12:00:00+00:00",
                }
            ],
        }

        with patch("guard.snapshot.build_snapshot", return_value=fake_snapshot):
            payload = guard.run_once(db_path=self.db_path, price=0.64, apply=False)

        self.assertEqual(payload["issue_count"], 0)
        with state_db.connect(self.db_path) as conn:
            slot = conn.execute(
                """
                SELECT observed_status, observed_profile_key, live_hashrate_th, protected
                FROM slots
                WHERE org_label = 'kray' AND slot_name = 'prl-kray-roi-01'
                """
            ).fetchone()
            worker = conn.execute("SELECT * FROM workers").fetchone()
            snapshot = conn.execute(
                """
                SELECT profile_key, th
                FROM profit_snapshots
                WHERE scope = 'slot'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        self.assertEqual(slot["observed_status"], "running")
        self.assertEqual(slot["observed_profile_key"], "3090:batch:2048")
        self.assertEqual(slot["live_hashrate_th"], 111.5)
        self.assertEqual(slot["protected"], 1)
        self.assertEqual(worker["worker_name"], "kray-prl-roi-01-pearlfortune-inst-1")
        self.assertEqual(worker["org_label"], "kray")
        self.assertEqual(worker["slot_name"], "prl-kray-roi-01")
        self.assertEqual(worker["instance_id"], "inst-1")
        self.assertEqual(worker["gpu_key"], "3090")
        self.assertEqual(worker["reported_hashrate_th"], 111.5)
        self.assertEqual(worker["stale"], 0)
        self.assertEqual(snapshot["profile_key"], "3090:batch:2048")
        self.assertEqual(snapshot["th"], 111.5)

    def test_guard_uses_latest_risk_price_when_price_is_omitted(self) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.set_risk_mode(
                conn,
                {
                    "at_utc": "2026-06-24T12:00:00+00:00",
                    "mode": "boost_fill",
                    "decision_price_usd": 0.68,
                    "pearl_fee_rate": 0.01,
                    "reason": "test boost",
                },
            )
            conn.commit()

        fake_snapshot = {
            "live_market_prl_price": 0.71,
            "fresh_workers": 0,
            "running_no_live_billable_slots": [],
            "totals": {"th": 0, "cost_day": 0, "revenue_day": 0, "profit_day": 0},
            "slots": [],
        }

        with patch("guard.snapshot.build_snapshot", return_value=fake_snapshot) as build_snapshot:
            guard.run_once(db_path=self.db_path, apply=False)

        build_snapshot.assert_called_once_with(0.68)


if __name__ == "__main__":
    unittest.main()
