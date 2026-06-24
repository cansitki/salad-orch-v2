from __future__ import annotations

import pathlib
import sys
import unittest


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


class RuntimeMonitorTest(unittest.TestCase):
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
        self.assertTrue(calls[1]["allow_pending_retarget"])
        self.assertEqual(calls[1]["pending_retarget_after_seconds"], 75)

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

    def test_only_one_live_action_per_tick(self) -> None:
        with self.assertRaises(SystemExit):
            runtime_monitor.run_monitor_tick(
                apply_guard=True,
                apply_one_org=True,
                org="kry1",
                confirm_live_actions=True,
                runner=lambda **_: rollout_payload(stage="shadow"),
            )


if __name__ == "__main__":
    unittest.main()
