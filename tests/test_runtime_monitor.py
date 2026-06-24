from __future__ import annotations

import os
import pathlib
import sys
import time
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import runtime_monitor


def rollout_payload(*, stage: str, ok: bool = True) -> dict:
    return {
        "stage": stage,
        "gates": {
            "ok": ok,
            "coverage": {"assigned_targets": 40, "target_slots": 40},
            "failed": [] if ok else [{"gate": "shadow_compare"}],
            "warnings": [{"gate": "no_hash"}],
        },
        "report": {
            "live_hashing_gpus": 10,
            "no_hash": 1,
            "negative": 0,
            "stuck": 0,
        },
        "health": {"health": "healthy"},
        "shadow_compare": {"ok": ok},
    }


def rollout_payload_without_issues(*, stage: str, ok: bool = True) -> dict:
    payload = rollout_payload(stage=stage, ok=ok)
    payload["report"] = {
        "live_hashing_gpus": 10,
        "no_hash": 0,
        "negative": 0,
        "stuck": 0,
    }
    return payload


class RuntimeMonitorTest(unittest.TestCase):
    def test_guard_due_skips_initial_fill_ticks(self) -> None:
        self.assertFalse(runtime_monitor._guard_due(0, 3))
        self.assertFalse(runtime_monitor._guard_due(1, 3))
        self.assertTrue(runtime_monitor._guard_due(2, 3))
        self.assertFalse(runtime_monitor._guard_due(3, 3))
        self.assertFalse(runtime_monitor._guard_due(0, 0))

    def test_read_only_tick_runs_shadow_only(self) -> None:
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            return rollout_payload(stage=kwargs["stage"])

        payload = runtime_monitor.run_monitor_tick(price=0.64, fee=0.01, runner=runner)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "none")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["stage"], "shadow")
        self.assertFalse(calls[0]["apply_workers"])
        self.assertFalse(calls[0]["apply_guard"])
        self.assertTrue(calls[0]["skip_guard"])
        self.assertFalse(calls[0]["skip_workers"])

    def test_live_action_requires_confirmation(self) -> None:
        with self.assertRaises(SystemExit):
            runtime_monitor.run_monitor_tick(apply_guard=True, runner=lambda **_: rollout_payload(stage="shadow"))

    def test_guard_apply_runs_after_passing_shadow(self) -> None:
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            return rollout_payload(stage=kwargs["stage"])

        payload = runtime_monitor.run_monitor_tick(
            apply_guard=True,
            confirm_live_actions=True,
            require_secrets=True,
            runner=runner,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "guard-apply")
        self.assertEqual([call["stage"] for call in calls], ["shadow", "guard-apply"])
        self.assertTrue(calls[0]["skip_guard"])
        self.assertNotIn("skip_guard", calls[1])
        self.assertTrue(calls[1]["apply_guard"])
        self.assertTrue(calls[1]["require_secrets"])

    def test_allow_degraded_shadow_is_limited_to_preflight(self) -> None:
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            return rollout_payload(stage=kwargs["stage"])

        payload = runtime_monitor.run_monitor_tick(
            apply_guard=True,
            confirm_live_actions=True,
            allow_degraded_shadow=True,
            runner=runner,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual([call["stage"] for call in calls], ["shadow", "guard-apply"])
        self.assertTrue(calls[0]["allow_degraded"])
        self.assertNotIn("allow_degraded", calls[1])

    def test_one_org_apply_requires_org(self) -> None:
        with self.assertRaises(SystemExit):
            runtime_monitor.run_monitor_tick(
                apply_one_org=True,
                confirm_live_actions=True,
                runner=lambda **_: rollout_payload(stage="shadow"),
            )

    def test_one_org_apply_can_allow_pending_retarget(self) -> None:
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            return rollout_payload(stage=kwargs["stage"])

        payload = runtime_monitor.run_monitor_tick(
            apply_one_org=True,
            org="kry1",
            confirm_live_actions=True,
            allow_pending_retarget=True,
            pending_retarget_after_seconds=75,
            runner=runner,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual([call["stage"] for call in calls], ["shadow", "one-org"])
        self.assertTrue(calls[0]["skip_guard"])
        self.assertTrue(calls[1]["skip_guard"])
        self.assertTrue(calls[1]["allow_pending_retarget"])
        self.assertEqual(calls[1]["pending_retarget_after_seconds"], 75)

    def test_all_orgs_pending_apply_uses_pending_retarget_only(self) -> None:
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            return rollout_payload(stage=kwargs["stage"])

        payload = runtime_monitor.run_monitor_tick(
            apply_all_orgs_pending=True,
            confirm_live_actions=True,
            require_secrets=True,
            pending_retarget_after_seconds=75,
            runner=runner,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "all-orgs-pending")
        self.assertEqual([call["stage"] for call in calls], ["shadow", "all-orgs"])
        self.assertTrue(calls[0]["skip_guard"])
        self.assertTrue(calls[1]["skip_guard"])
        self.assertTrue(calls[1]["apply_workers"])
        self.assertTrue(calls[1]["confirm_all_orgs"])
        self.assertTrue(calls[1]["allow_pending_retarget"])
        self.assertNotIn("allow_live_retarget", calls[1])
        self.assertEqual(calls[1]["pending_retarget_after_seconds"], 75)
        self.assertTrue(calls[1]["require_secrets"])

    def test_pending_retarget_sets_scheduler_protection_and_default_profile_cooldown(self) -> None:
        calls = []
        original_protect = os.environ.get("PRL_PENDING_TARGET_PROTECT_SECONDS")
        original_cooldown = os.environ.get("PRL_PENDING_PROFILE_COOLDOWN_SECONDS")
        os.environ["PRL_PENDING_TARGET_PROTECT_SECONDS"] = "999"
        os.environ.pop("PRL_PENDING_PROFILE_COOLDOWN_SECONDS", None)

        def runner(**kwargs):
            calls.append(
                {
                    "stage": kwargs["stage"],
                    "protect_seconds": os.environ.get("PRL_PENDING_TARGET_PROTECT_SECONDS"),
                    "cooldown_seconds": os.environ.get("PRL_PENDING_PROFILE_COOLDOWN_SECONDS"),
                }
            )
            return rollout_payload(stage=kwargs["stage"])

        try:
            payload = runtime_monitor.run_monitor_tick(
                apply_all_orgs_pending=True,
                confirm_live_actions=True,
                pending_retarget_after_seconds=60,
                runner=runner,
            )

            self.assertTrue(payload["ok"])
            self.assertEqual(
                calls,
                [
                    {"stage": "shadow", "protect_seconds": "60", "cooldown_seconds": "60"},
                    {"stage": "all-orgs", "protect_seconds": "60", "cooldown_seconds": "60"},
                ],
            )
            self.assertEqual(os.environ.get("PRL_PENDING_TARGET_PROTECT_SECONDS"), "999")
            self.assertIsNone(os.environ.get("PRL_PENDING_PROFILE_COOLDOWN_SECONDS"))
        finally:
            if original_protect is None:
                os.environ.pop("PRL_PENDING_TARGET_PROTECT_SECONDS", None)
            else:
                os.environ["PRL_PENDING_TARGET_PROTECT_SECONDS"] = original_protect
            if original_cooldown is None:
                os.environ.pop("PRL_PENDING_PROFILE_COOLDOWN_SECONDS", None)
            else:
                os.environ["PRL_PENDING_PROFILE_COOLDOWN_SECONDS"] = original_cooldown

    def test_pending_retarget_respects_explicit_profile_cooldown_override(self) -> None:
        calls = []
        original_protect = os.environ.get("PRL_PENDING_TARGET_PROTECT_SECONDS")
        original_cooldown = os.environ.get("PRL_PENDING_PROFILE_COOLDOWN_SECONDS")
        os.environ.pop("PRL_PENDING_TARGET_PROTECT_SECONDS", None)
        os.environ["PRL_PENDING_PROFILE_COOLDOWN_SECONDS"] = "240"

        def runner(**kwargs):
            calls.append(
                {
                    "stage": kwargs["stage"],
                    "protect_seconds": os.environ.get("PRL_PENDING_TARGET_PROTECT_SECONDS"),
                    "cooldown_seconds": os.environ.get("PRL_PENDING_PROFILE_COOLDOWN_SECONDS"),
                }
            )
            return rollout_payload(stage=kwargs["stage"])

        try:
            payload = runtime_monitor.run_monitor_tick(
                apply_all_orgs_pending=True,
                confirm_live_actions=True,
                pending_retarget_after_seconds=60,
                runner=runner,
            )

            self.assertTrue(payload["ok"])
            self.assertEqual(
                calls,
                [
                    {"stage": "shadow", "protect_seconds": "60", "cooldown_seconds": "240"},
                    {"stage": "all-orgs", "protect_seconds": "60", "cooldown_seconds": "240"},
                ],
            )
            self.assertIsNone(os.environ.get("PRL_PENDING_TARGET_PROTECT_SECONDS"))
            self.assertEqual(os.environ.get("PRL_PENDING_PROFILE_COOLDOWN_SECONDS"), "240")
        finally:
            if original_protect is None:
                os.environ.pop("PRL_PENDING_TARGET_PROTECT_SECONDS", None)
            else:
                os.environ["PRL_PENDING_TARGET_PROTECT_SECONDS"] = original_protect
            if original_cooldown is None:
                os.environ.pop("PRL_PENDING_PROFILE_COOLDOWN_SECONDS", None)
            else:
                os.environ["PRL_PENDING_PROFILE_COOLDOWN_SECONDS"] = original_cooldown

    def test_guard_on_issues_runs_guard_instead_of_fill_when_due(self) -> None:
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            return rollout_payload(stage=kwargs["stage"])

        payload = runtime_monitor.run_monitor_tick(
            apply_all_orgs_pending=True,
            guard_on_issues=True,
            guard_due=True,
            confirm_live_actions=True,
            runner=runner,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "guard-apply")
        self.assertEqual([call["stage"] for call in calls], ["shadow", "guard-apply"])
        self.assertTrue(calls[0]["skip_guard"])
        self.assertTrue(calls[1]["apply_guard"])

    def test_guard_actionable_only_uses_fill_when_guard_decisions_wait(self) -> None:
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            return rollout_payload(stage=kwargs["stage"])

        with patch(
            "runtime_monitor.guard_status.run_once",
            return_value={
                "issue_count": 2,
                "decisions": [
                    {"action": "wait", "issue_type": "no_hash"},
                    {"action": "wait", "issue_type": "negative"},
                ],
            },
        ) as guard_mock:
            payload = runtime_monitor.run_monitor_tick(
                apply_all_orgs_pending=True,
                guard_on_issues=True,
                guard_due=True,
                guard_actionable_only=True,
                confirm_live_actions=True,
                runner=runner,
            )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "all-orgs-pending")
        self.assertEqual([call["stage"] for call in calls], ["shadow", "all-orgs"])
        self.assertEqual(payload["guard_probe"]["actionable"], 0)
        guard_mock.assert_called_once_with(db_path=None, price=None, apply=False)

    def test_guard_actionable_only_runs_guard_when_decision_is_actionable(self) -> None:
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            return rollout_payload(stage=kwargs["stage"])

        with patch(
            "runtime_monitor.guard_status.run_once",
            return_value={
                "issue_count": 1,
                "decisions": [
                    {"action": "retarget", "issue_type": "negative"},
                ],
            },
        ):
            payload = runtime_monitor.run_monitor_tick(
                apply_all_orgs_pending=True,
                guard_on_issues=True,
                guard_due=True,
                guard_actionable_only=True,
                confirm_live_actions=True,
                runner=runner,
            )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "guard-apply")
        self.assertEqual([call["stage"] for call in calls], ["shadow", "guard-apply"])
        self.assertEqual(payload["guard_probe"]["actionable"], 1)

    def test_guard_on_issues_uses_fill_when_not_due(self) -> None:
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            return rollout_payload(stage=kwargs["stage"])

        payload = runtime_monitor.run_monitor_tick(
            apply_all_orgs_pending=True,
            guard_on_issues=True,
            guard_due=False,
            confirm_live_actions=True,
            runner=runner,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "all-orgs-pending")
        self.assertEqual([call["stage"] for call in calls], ["shadow", "all-orgs"])

    def test_guard_on_issues_uses_fill_when_shadow_has_no_issues(self) -> None:
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            return rollout_payload_without_issues(stage=kwargs["stage"])

        payload = runtime_monitor.run_monitor_tick(
            apply_all_orgs_pending=True,
            guard_on_issues=True,
            guard_due=True,
            confirm_live_actions=True,
            runner=runner,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "all-orgs-pending")
        self.assertEqual([call["stage"] for call in calls], ["shadow", "all-orgs"])

    def test_guard_actionable_only_checks_guard_even_when_shadow_has_no_issues(self) -> None:
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            return rollout_payload_without_issues(stage=kwargs["stage"])

        with patch(
            "runtime_monitor.guard_status.run_once",
            return_value={
                "issue_count": 1,
                "decisions": [
                    {"action": "retarget", "issue_type": "negative"},
                ],
            },
        ) as guard_mock:
            payload = runtime_monitor.run_monitor_tick(
                apply_all_orgs_pending=True,
                guard_on_issues=True,
                guard_due=True,
                guard_actionable_only=True,
                confirm_live_actions=True,
                runner=runner,
            )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "guard-apply")
        self.assertEqual([call["stage"] for call in calls], ["shadow", "guard-apply"])
        self.assertEqual(payload["guard_probe"]["actionable"], 1)
        guard_mock.assert_called_once_with(db_path=None, price=None, apply=False)

    def test_guard_actionable_only_fills_when_shadow_and_guard_are_clean(self) -> None:
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            return rollout_payload_without_issues(stage=kwargs["stage"])

        with patch(
            "runtime_monitor.guard_status.run_once",
            return_value={"issue_count": 0, "decisions": []},
        ):
            payload = runtime_monitor.run_monitor_tick(
                apply_all_orgs_pending=True,
                guard_on_issues=True,
                guard_due=True,
                guard_actionable_only=True,
                confirm_live_actions=True,
                runner=runner,
            )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "all-orgs-pending")
        self.assertEqual([call["stage"] for call in calls], ["shadow", "all-orgs"])
        self.assertEqual(payload["guard_probe"]["actionable"], 0)

    def test_guard_on_issues_requires_all_orgs_pending_mode(self) -> None:
        with self.assertRaises(SystemExit):
            runtime_monitor.run_monitor_tick(
                guard_on_issues=True,
                guard_due=True,
                confirm_live_actions=True,
                runner=lambda **_: rollout_payload(stage="shadow"),
            )

    def test_worker_parallelism_is_passed_to_shadow_and_action(self) -> None:
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            return rollout_payload(stage=kwargs["stage"])

        payload = runtime_monitor.run_monitor_tick(
            apply_all_orgs_pending=True,
            confirm_live_actions=True,
            worker_parallelism=4,
            runner=runner,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual([call["worker_parallelism"] for call in calls], [4, 4])

    def test_skip_shadow_workers_only_affects_shadow_preflight(self) -> None:
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            return rollout_payload(stage=kwargs["stage"])

        payload = runtime_monitor.run_monitor_tick(
            apply_all_orgs_pending=True,
            confirm_live_actions=True,
            skip_shadow_workers=True,
            worker_parallelism=4,
            runner=runner,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual([call["stage"] for call in calls], ["shadow", "all-orgs"])
        self.assertTrue(calls[0]["skip_workers"])
        self.assertNotIn("skip_workers", calls[1])
        self.assertEqual([call["worker_parallelism"] for call in calls], [4, 4])

    def test_live_action_is_skipped_when_shadow_fails(self) -> None:
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            return rollout_payload(stage=kwargs["stage"], ok=False)

        payload = runtime_monitor.run_monitor_tick(
            apply_guard=True,
            confirm_live_actions=True,
            runner=runner,
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["action"], "none")
        self.assertTrue(payload["skipped_live_action"])
        self.assertEqual(len(calls), 1)

    def test_shadow_timeout_returns_failed_tick(self) -> None:
        def runner(**_kwargs):
            time.sleep(0.2)
            return rollout_payload(stage="shadow")

        payload = runtime_monitor.run_monitor_tick(
            runner=runner,
            runner_timeout_seconds=0.01,
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["action"], "none")
        self.assertIn("monitor_timeout", payload["shadow"]["failed_gates"])
        self.assertIn("TimeoutError", payload["shadow"]["error"])

    def test_shadow_hard_timeout_returns_failed_tick(self) -> None:
        def runner(**_kwargs):
            time.sleep(1.0)
            return rollout_payload(stage="shadow")

        started = time.monotonic()
        payload = runtime_monitor.run_monitor_tick(
            runner=runner,
            runner_timeout_seconds=0.05,
            hard_runner_timeout=True,
        )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.8)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["action"], "none")
        self.assertIn("monitor_timeout", payload["shadow"]["failed_gates"])
        self.assertIn("TimeoutError", payload["shadow"]["error"])

    def test_hard_timeout_runner_returns_large_payload_without_queue_deadlock(self) -> None:
        result = runtime_monitor._call_with_process_timeout(
            lambda: {"blob": "x" * 1_000_000},
            timeout_seconds=2.0,
        )

        self.assertEqual(len(result["blob"]), 1_000_000)

    def test_shadow_runner_error_uses_db_fallback_status(self) -> None:
        def runner(**_kwargs):
            raise RuntimeError("salad api timed out")

        with (
            patch(
                "runtime_monitor.reporter.build_report",
                return_value={
                    "assigned_targets": 40,
                    "target_slots": 40,
                    "live_hashing_gpus": 12,
                    "running_no_live_billable_slots": [{}],
                    "negative_slots": [],
                    "stuck_slots": [{}, {}],
                },
            ) as report_mock,
            patch(
                "runtime_monitor.health_status.build_health",
                return_value={"health": "degraded", "target_count": 40, "slot_count": 40},
            ) as health_mock,
        ):
            payload = runtime_monitor.run_monitor_tick(db_path="/tmp/fleet.db", runner=runner)

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["shadow"]["failed_gates"], ["monitor_runner_error"])
        self.assertEqual(payload["shadow"]["targets"], 40)
        self.assertEqual(payload["shadow"]["target_slots"], 40)
        self.assertEqual(payload["shadow"]["health"], "degraded")
        self.assertEqual(payload["shadow"]["live_hashing_gpus"], 12)
        self.assertEqual(payload["shadow"]["no_hash"], 1)
        self.assertEqual(payload["shadow"]["stuck"], 2)
        self.assertEqual(payload["shadow"]["fallback_source"], "db")
        report_mock.assert_called_once_with("/tmp/fleet.db")
        health_mock.assert_called_once_with("/tmp/fleet.db")

    def test_shadow_runner_error_reports_unavailable_fallback(self) -> None:
        def runner(**_kwargs):
            raise RuntimeError("salad api timed out")

        with patch("runtime_monitor.reporter.build_report", side_effect=RuntimeError("db locked")):
            payload = runtime_monitor.run_monitor_tick(runner=runner)

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["shadow"]["fallback_source"], "unavailable")
        self.assertIn("db locked", payload["shadow"]["fallback_error"])

    def test_action_timeout_returns_failed_action_result(self) -> None:
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            if kwargs["stage"] == "guard-apply":
                time.sleep(0.2)
            return rollout_payload(stage=kwargs["stage"])

        payload = runtime_monitor.run_monitor_tick(
            apply_guard=True,
            confirm_live_actions=True,
            runner=runner,
            runner_timeout_seconds=0.01,
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["action"], "guard-apply")
        self.assertEqual([call["stage"] for call in calls], ["shadow", "guard-apply"])
        self.assertIn("monitor_timeout", payload["action_result"]["failed_gates"])
        self.assertIn("TimeoutError", payload["action_result"]["error"])

    def test_only_one_live_action_per_tick(self) -> None:
        with self.assertRaises(SystemExit):
            runtime_monitor.run_monitor_tick(
                apply_guard=True,
                apply_one_org=True,
                org="kry1",
                confirm_live_actions=True,
                runner=lambda **_: rollout_payload(stage="shadow"),
            )

    def test_all_orgs_pending_is_mutually_exclusive_with_other_live_actions(self) -> None:
        with self.assertRaises(SystemExit):
            runtime_monitor.run_monitor_tick(
                apply_guard=True,
                apply_all_orgs_pending=True,
                confirm_live_actions=True,
                runner=lambda **_: rollout_payload(stage="shadow"),
            )


if __name__ == "__main__":
    unittest.main()
