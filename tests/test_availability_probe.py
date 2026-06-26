from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import availability_probe
import profit_model
import state_db
from config_loader import FleetConfig, OrgConfig


class FakeWatch:
    class Candidate:
        def __init__(self, label, priority, gpu_keys, memory):
            self.label = label
            self.priority = priority
            self.gpu_keys = gpu_keys
            self.memory = memory

    def candidate_availability(self, _slot_name, _candidate):
        return 1


class RecordingExecutor:
    last_max_workers = None

    def __init__(self, max_workers):
        type(self).last_max_workers = max_workers

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def map(self, func, items):
        return [func(item) for item in items]


class AvailabilityProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(pathlib.Path(self.tmpdir.name) / "fleet.db")
        self._balance_env = {
            "PRL_ORG_BALANCE_FILE": os.environ.get("PRL_ORG_BALANCE_FILE"),
            "PRL_BALANCE_FILE": os.environ.get("PRL_BALANCE_FILE"),
            "SALAD_BALANCE_FILE": os.environ.get("SALAD_BALANCE_FILE"),
        }
        os.environ.pop("PRL_ORG_BALANCE_FILE", None)
        os.environ.pop("SALAD_BALANCE_FILE", None)
        os.environ["PRL_BALANCE_FILE"] = str(pathlib.Path(self.tmpdir.name) / "balances.json")

    def tearDown(self) -> None:
        for key, value in self._balance_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmpdir.cleanup()

    def test_probe_installs_rate_limiter_for_org_watch(self) -> None:
        config = FleetConfig(
            organizations=(
                OrgConfig(
                    label="test",
                    slug="test",
                    api_key_env="SALAD_API_KEY_TEST",
                    slot_prefix="prl-test-roi",
                ),
            )
        )
        profile = profit_model.Profile(
            profile_key="4090:batch:2048",
            gpu_key="4090",
            gpu_id="gpu-4090",
            priority="batch",
            label="RTX 4090 batch",
            memory_mb=2048,
            expected_th=230.0,
            static_hourly_usd=0.16,
        )
        watch = FakeWatch()

        with (
            mock.patch.object(availability_probe, "load_config", return_value=config),
            mock.patch.object(availability_probe.profit_model, "load_profiles", return_value=[profile]),
            mock.patch.object(availability_probe, "load_watch_module", return_value=watch),
            mock.patch.object(availability_probe, "install_rate_limited_request") as install_limiter,
        ):
            payload = availability_probe.run_once(db_path=self.db_path, profile_limit=1)

        install_limiter.assert_called_once_with(watch, config.organizations[0], db_path=self.db_path)
        self.assertEqual(payload["probed"], 1)
        self.assertEqual(payload["by_profile"], {"4090:batch:2048": 1})

        with state_db.connect(self.db_path) as conn:
            heartbeat = conn.execute(
                "SELECT stale_after_seconds FROM heartbeats WHERE process_name = 'availability_probe'"
            ).fetchone()
        self.assertIsNotNone(heartbeat)
        self.assertEqual(heartbeat["stale_after_seconds"], 1800)

    def test_parallel_org_batches_do_not_share_api_key(self) -> None:
        tasks = [
            {
                "org": OrgConfig(
                    label="kray",
                    slug="kray",
                    api_key_env="SALAD_API_KEY_2",
                    slot_prefix="prl-kray-roi",
                )
            },
            {
                "org": OrgConfig(
                    label="kry1",
                    slug="kry1",
                    api_key_env="SALAD_API_KEY_KRY1",
                    slot_prefix="prl-kry1-roi",
                )
            },
            {
                "org": OrgConfig(
                    label="kray2",
                    slug="kray2",
                    api_key_env="SALAD_API_KEY_2",
                    slot_prefix="prl-kray2-roi",
                )
            },
            {
                "org": OrgConfig(
                    label="kray3",
                    slug="kray3",
                    api_key_env="SALAD_API_KEY_2",
                    slot_prefix="prl-kray3-roi",
                )
            },
        ]

        batches = availability_probe._batch_org_tasks(tasks, max_workers=4)

        self.assertEqual(
            [[task["org"].label for task in batch] for batch in batches],
            [["kray", "kry1"], ["kray2"], ["kray3"]],
        )

    def test_run_once_reports_selected_org_parallelism(self) -> None:
        config = FleetConfig(
            organizations=(
                OrgConfig(
                    label="test",
                    slug="test",
                    api_key_env="SALAD_API_KEY_TEST",
                    slot_prefix="prl-test-roi",
                ),
            )
        )
        profile = profit_model.Profile(
            profile_key="4090:batch:2048",
            gpu_key="4090",
            gpu_id="gpu-4090",
            priority="batch",
            label="RTX 4090 batch",
            memory_mb=2048,
            expected_th=230.0,
            static_hourly_usd=0.16,
        )

        with (
            mock.patch.object(availability_probe, "load_config", return_value=config),
            mock.patch.object(availability_probe.profit_model, "load_profiles", return_value=[profile]),
            mock.patch.object(availability_probe, "_probe_org_profiles", return_value=[]),
        ):
            payload = availability_probe.run_once(
                db_path=self.db_path,
                profile_limit=1,
                org_parallelism=3,
            )

        self.assertEqual(payload["org_parallelism"], 3)

    def test_run_once_reports_selected_profile_parallelism(self) -> None:
        config = FleetConfig(
            organizations=(
                OrgConfig(
                    label="test",
                    slug="test",
                    api_key_env="SALAD_API_KEY_TEST",
                    slot_prefix="prl-test-roi",
                ),
            )
        )
        profile = profit_model.Profile(
            profile_key="4090:batch:2048",
            gpu_key="4090",
            gpu_id="gpu-4090",
            priority="batch",
            label="RTX 4090 batch",
            memory_mb=2048,
            expected_th=230.0,
            static_hourly_usd=0.16,
        )

        with (
            mock.patch.object(availability_probe, "load_config", return_value=config),
            mock.patch.object(availability_probe.profit_model, "load_profiles", return_value=[profile]),
            mock.patch.object(availability_probe, "_probe_org_profiles", return_value=[]),
        ):
            payload = availability_probe.run_once(
                db_path=self.db_path,
                profile_limit=1,
                profile_parallelism=5,
            )

        self.assertEqual(payload["profile_parallelism"], 5)

    def test_run_once_skips_only_explicit_zero_balance_orgs(self) -> None:
        config = FleetConfig(
            organizations=(
                OrgConfig(
                    label="kray",
                    slug="kray",
                    api_key_env="SALAD_API_KEY_2",
                    slot_prefix="prl-kray-roi",
                ),
                OrgConfig(
                    label="kry1",
                    slug="kry1",
                    api_key_env="SALAD_API_KEY_KRY1",
                    slot_prefix="prl-kry1-roi",
                ),
            )
        )
        profile = profit_model.Profile(
            profile_key="4090:batch:2048",
            gpu_key="4090",
            gpu_id="gpu-4090",
            priority="batch",
            label="RTX 4090 batch",
            memory_mb=2048,
            expected_th=230.0,
            static_hourly_usd=0.16,
        )
        balance_file = pathlib.Path(self.tmpdir.name) / "balances.json"
        balance_file.write_text(json.dumps({"kray": 0.0}), encoding="utf-8")
        original_balance_file = os.environ.get("PRL_BALANCE_FILE")
        os.environ["PRL_BALANCE_FILE"] = str(balance_file)

        class QuotaOnlyWatch(FakeWatch):
            ORG = "kray"

            def request(self, method: str, path: str, _payload=None, **_kwargs):
                self.request_call = (method, path)
                return {
                    "container_groups_quotas": {
                        "container_replicas_quota": 10,
                        "container_replicas_used": 2,
                    },
                    "update_time": "2026-06-26T21:30:00+00:00",
                }

            def candidate_availability(self, _slot_name, _candidate):
                raise AssertionError("zero balance org should not probe profile availability")

        quota_watch = QuotaOnlyWatch()
        profile_watch = FakeWatch()
        try:
            with (
                mock.patch.object(availability_probe, "load_config", return_value=config),
                mock.patch.object(availability_probe.profit_model, "load_profiles", return_value=[profile]),
                mock.patch.object(availability_probe, "load_watch_module", side_effect=[quota_watch, profile_watch]) as load_watch,
                mock.patch.object(availability_probe, "install_rate_limited_request"),
            ):
                payload = availability_probe.run_once(db_path=self.db_path, profile_limit=1)
        finally:
            if original_balance_file is None:
                os.environ.pop("PRL_BALANCE_FILE", None)
            else:
                os.environ["PRL_BALANCE_FILE"] = original_balance_file

        self.assertEqual(payload["probed"], 1)
        self.assertEqual(payload["results"][0]["org_label"], "kry1")
        self.assertEqual([item["org_label"] for item in payload["skipped_zero_balance_orgs"]], ["kray"])
        self.assertEqual([item["org_label"] for item in payload["zero_balance_quota_refreshes"]], ["kray"])
        self.assertEqual(payload["zero_balance_quota_refreshes"][0]["quota"], 10)
        self.assertEqual(payload["zero_balance_quota_refreshes"][0]["used"], 2)
        self.assertEqual(quota_watch.request_call, ("GET", "/organizations/kray/quotas"))
        self.assertEqual(load_watch.call_args_list, [mock.call(config.organizations[0]), mock.call(config.organizations[1])])
        with state_db.connect(self.db_path) as conn:
            availability_orgs = [
                row["org_label"]
                for row in conn.execute("SELECT DISTINCT org_label FROM profile_availability ORDER BY org_label")
            ]
            quota = conn.execute("SELECT * FROM org_replica_quotas WHERE org_label='kray'").fetchone()
        self.assertEqual(availability_orgs, ["kry1"])
        self.assertEqual(quota["quota"], 10)
        self.assertEqual(quota["used"], 2)
        self.assertEqual(quota["source"], "availability_probe_zero_balance")

    def test_run_once_credit_probes_zero_balance_orgs_with_available_quota(self) -> None:
        config = FleetConfig(
            organizations=(
                OrgConfig(
                    label="kray",
                    slug="kray",
                    api_key_env="SALAD_API_KEY_2",
                    slot_prefix="prl-kray-roi",
                ),
            )
        )
        profile = profit_model.Profile(
            profile_key="4090:batch:2048",
            gpu_key="4090",
            gpu_id="gpu-4090",
            priority="batch",
            label="RTX 4090 batch",
            memory_mb=2048,
            expected_th=230.0,
            static_hourly_usd=0.16,
        )
        pathlib.Path(os.environ["PRL_BALANCE_FILE"]).write_text(json.dumps({"kray": 0.0}), encoding="utf-8")

        class QuotaOnlyWatch(FakeWatch):
            ORG = "kray"

            def request(self, method: str, path: str, _payload=None, **_kwargs):
                return {
                    "container_groups_quotas": {
                        "container_replicas_quota": 10,
                        "container_replicas_used": 0,
                    },
                    "update_time": "2026-06-26T21:30:00+00:00",
                }

            def candidate_availability(self, _slot_name, _candidate):
                raise AssertionError("zero balance org should not probe profile availability")

        worker_payload = {
            "targets": 10,
            "action_counts": {"create_failed": 1, "skip_no_credits": 9},
            "no_credits_skip": None,
        }
        with (
            mock.patch.dict(
                os.environ,
                {
                    "PRL_AVAILABILITY_ZERO_BALANCE_CREDIT_PROBE": "1",
                    "PRL_AVAILABILITY_ZERO_BALANCE_CREDIT_PROBE_COOLDOWN_SECONDS": "777",
                },
                clear=False,
            ),
            mock.patch.object(availability_probe, "load_config", return_value=config),
            mock.patch.object(availability_probe.profit_model, "load_profiles", return_value=[profile]),
            mock.patch.object(availability_probe, "load_watch_module", return_value=QuotaOnlyWatch()),
            mock.patch.object(availability_probe, "install_rate_limited_request"),
            mock.patch.object(availability_probe, "run_org_worker_once", return_value=worker_payload) as worker_mock,
        ):
            payload = availability_probe.run_once(db_path=self.db_path, profile_limit=1)

        worker_mock.assert_called_once()
        kwargs = worker_mock.call_args.kwargs
        self.assertEqual(kwargs["org_label"], "kray")
        self.assertEqual(kwargs["db_path"], self.db_path)
        self.assertTrue(kwargs["apply"])
        self.assertTrue(kwargs["schedule_if_empty"])
        self.assertTrue(kwargs["allow_pending_retarget"])
        self.assertEqual(payload["probed"], 0)
        self.assertEqual(
            payload["zero_balance_credit_probe"],
            [
                {
                    "org_label": "kray",
                    "ok": True,
                    "targets": 10,
                    "action_counts": {"create_failed": 1, "skip_no_credits": 9},
                    "no_credits_skip": None,
                }
            ],
        )
        with state_db.connect(self.db_path) as conn:
            heartbeat = conn.execute(
                "SELECT payload_json FROM heartbeats WHERE process_name='availability_probe'"
            ).fetchone()
        self.assertIn("zero_balance_credit_probe", heartbeat["payload_json"])

    def test_run_once_skips_active_no_credits_orgs(self) -> None:
        config = FleetConfig(
            organizations=(
                OrgConfig(
                    label="kray",
                    slug="kray",
                    api_key_env="SALAD_API_KEY_2",
                    slot_prefix="prl-kray-roi",
                ),
                OrgConfig(
                    label="kry1",
                    slug="kry1",
                    api_key_env="SALAD_API_KEY_KRY1",
                    slot_prefix="prl-kry1-roi",
                ),
            )
        )
        profile = profit_model.Profile(
            profile_key="4090:batch:2048",
            gpu_key="4090",
            gpu_id="gpu-4090",
            priority="batch",
            label="RTX 4090 batch",
            memory_mb=2048,
            expected_th=230.0,
            static_hourly_usd=0.16,
        )
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.record_search_state(
                conn,
                {
                    "org_label": "kry1",
                    "slot_name": "*",
                    "profile_key": "*",
                    "no_gpu_since_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                    "sleep_until_utc": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(timespec="seconds"),
                    "attempts": 1,
                    "reason": "http_400:no_credits_available",
                    "updated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                },
            )
            conn.commit()

        with (
            mock.patch.object(availability_probe, "load_config", return_value=config),
            mock.patch.object(availability_probe.profit_model, "load_profiles", return_value=[profile]),
            mock.patch.object(availability_probe, "load_watch_module", return_value=FakeWatch()) as load_watch,
            mock.patch.object(availability_probe, "install_rate_limited_request"),
        ):
            payload = availability_probe.run_once(db_path=self.db_path, profile_limit=1)

        self.assertEqual(payload["probed"], 1)
        self.assertEqual(payload["results"][0]["org_label"], "kray")
        self.assertEqual([item["org_label"] for item in payload["skipped_no_credits_orgs"]], ["kry1"])
        load_watch.assert_called_once_with(config.organizations[0])

    def test_run_once_clears_no_credits_cooldown_after_fresh_positive_balance(self) -> None:
        config = FleetConfig(
            organizations=(
                OrgConfig(
                    label="kray",
                    slug="kray",
                    api_key_env="SALAD_API_KEY_2",
                    slot_prefix="prl-kray-roi",
                ),
                OrgConfig(
                    label="kry1",
                    slug="kry1",
                    api_key_env="SALAD_API_KEY_KRY1",
                    slot_prefix="prl-kry1-roi",
                ),
            )
        )
        profile = profit_model.Profile(
            profile_key="4090:batch:2048",
            gpu_key="4090",
            gpu_id="gpu-4090",
            priority="batch",
            label="RTX 4090 batch",
            memory_mb=2048,
            expected_th=230.0,
            static_hourly_usd=0.16,
        )
        pathlib.Path(os.environ["PRL_BALANCE_FILE"]).write_text(json.dumps({"kry1": 1.25}), encoding="utf-8")
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.record_search_state(
                conn,
                {
                    "org_label": "kry1",
                    "slot_name": "*",
                    "profile_key": "*",
                    "no_gpu_since_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                    "sleep_until_utc": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(timespec="seconds"),
                    "attempts": 1,
                    "reason": "http_400:no_credits_available",
                    "updated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                },
            )
            conn.commit()

        with (
            mock.patch.object(availability_probe, "load_config", return_value=config),
            mock.patch.object(availability_probe.profit_model, "load_profiles", return_value=[profile]),
            mock.patch.object(availability_probe, "load_watch_module", return_value=FakeWatch()) as load_watch,
            mock.patch.object(availability_probe, "install_rate_limited_request"),
        ):
            payload = availability_probe.run_once(db_path=self.db_path, profile_limit=1, org_parallelism=1)

        self.assertEqual(payload["probed"], 2)
        self.assertEqual([row["org_label"] for row in payload["results"]], ["kray", "kry1"])
        self.assertEqual(payload["skipped_no_credits_orgs"], [])
        self.assertEqual([item["org_label"] for item in payload["cleared_no_credits_cooldowns"]], ["kry1"])
        self.assertEqual(load_watch.call_count, 2)
        with state_db.connect(self.db_path) as conn:
            cooldown = state_db.active_org_cooldown(conn, "kry1")
            cleared = conn.execute(
                """
                SELECT reason, sleep_until_utc
                FROM search_cooldowns
                WHERE org_label='kry1' AND slot_name='*' AND profile_key='*'
                """
            ).fetchone()
            heartbeat = conn.execute(
                "SELECT payload_json FROM heartbeats WHERE process_name='availability_probe'"
            ).fetchone()

        self.assertIsNone(cooldown)
        self.assertEqual(cleared["reason"], "positive_balance_restored")
        self.assertIsNone(cleared["sleep_until_utc"])
        self.assertIn("cleared_no_credits_cooldowns", heartbeat["payload_json"])

    def test_run_once_skips_zero_replica_quota_orgs(self) -> None:
        config = FleetConfig(
            organizations=(
                OrgConfig(
                    label="kray",
                    slug="kray",
                    api_key_env="SALAD_API_KEY_2",
                    slot_prefix="prl-kray-roi",
                ),
            )
        )
        profile = profit_model.Profile(
            profile_key="4090:batch:2048",
            gpu_key="4090",
            gpu_id="gpu-4090",
            priority="batch",
            label="RTX 4090 batch",
            memory_mb=2048,
            expected_th=230.0,
            static_hourly_usd=0.16,
        )

        class QuotaZeroWatch(FakeWatch):
            ORG = "kray"

            def request(self, method: str, path: str, _payload=None, **_kwargs):
                self.request_call = (method, path)
                return {
                    "container_groups_quotas": {
                        "container_replicas_quota": 0,
                        "container_replicas_used": 0,
                    },
                    "update_time": "2026-06-26T14:50:00+00:00",
                }

            def candidate_availability(self, _slot_name, _candidate):
                raise AssertionError("zero quota org should not probe profile availability")

        watch = QuotaZeroWatch()
        with (
            mock.patch.object(availability_probe, "load_config", return_value=config),
            mock.patch.object(availability_probe.profit_model, "load_profiles", return_value=[profile]),
            mock.patch.object(availability_probe, "load_watch_module", return_value=watch),
            mock.patch.object(availability_probe, "install_rate_limited_request"),
        ):
            payload = availability_probe.run_once(db_path=self.db_path, profile_limit=1)

        self.assertEqual(payload["probed"], 0)
        self.assertEqual(payload["results"], [])
        self.assertEqual(payload["by_profile"], {})
        self.assertEqual([item["org_label"] for item in payload["skipped_zero_replica_quota_orgs"]], ["kray"])
        self.assertEqual(watch.request_call, ("GET", "/organizations/kray/quotas"))
        with state_db.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM profile_availability").fetchone()[0]
            quota = conn.execute("SELECT * FROM org_replica_quotas WHERE org_label='kray'").fetchone()
        self.assertEqual(count, 0)
        self.assertEqual(quota["status"], "zero_quota")
        self.assertEqual(quota["quota"], 0)

    def test_quota_restored_positive_balance_orgs_requires_fresh_positive_balance(self) -> None:
        pathlib.Path(os.environ["PRL_BALANCE_FILE"]).write_text(
            json.dumps({"kray": 1.25, "kry1": 0.0}),
            encoding="utf-8",
        )

        restored = availability_probe.quota_restored_positive_balance_orgs(
            [
                {
                    "org_label": "kray",
                    "quota_transition": {"restored": True},
                },
                {
                    "org_label": "kry1",
                    "quota_transition": {"restored": True},
                },
                {
                    "org_label": "kray2",
                    "quota_transition": {"restored": False},
                },
                {
                    "org_label": "kray",
                    "quota_transition": {"restored": True},
                },
            ]
        )

        self.assertEqual(restored, ["kray"])

    def test_wake_rollout_on_quota_restore_can_be_disabled(self) -> None:
        with mock.patch.dict(os.environ, {"PRL_AVAILABILITY_WAKE_ROLLOUT_ON_QUOTA_RESTORE": "0"}, clear=False):
            payload = availability_probe.wake_rollout_on_quota_restore(
                db_path=self.db_path,
                restored_orgs=["kray"],
            )

        self.assertIsNone(payload)

    def test_wake_rollout_on_quota_restore_runs_existing_runtime_monitor_gates(self) -> None:
        monitor_payload = {
            "ok": True,
            "action": "all-orgs-pending",
            "shadow": {
                "ok": True,
                "health": "healthy",
                "live_hashing_gpus": 0,
                "no_hash": 0,
                "negative": 0,
                "stuck": 0,
            },
            "action_result": {"ok": True, "stage": "all-orgs"},
        }
        with (
            mock.patch.dict(
                os.environ,
                {
                    "PRL_AVAILABILITY_QUOTA_RESTORE_WAKE_FEE": "0.01",
                    "PRL_AVAILABILITY_QUOTA_RESTORE_WAKE_WORKER_PARALLELISM": "7",
                },
                clear=False,
            ),
            mock.patch("runtime_monitor.run_monitor_tick", return_value=monitor_payload) as monitor_mock,
        ):
            payload = availability_probe.wake_rollout_on_quota_restore(
                db_path=self.db_path,
                restored_orgs=["kray"],
            )

        monitor_mock.assert_called_once()
        kwargs = monitor_mock.call_args.kwargs
        self.assertEqual(kwargs["db_path"], self.db_path)
        self.assertEqual(kwargs["fee"], 0.01)
        self.assertTrue(kwargs["require_secrets"])
        self.assertTrue(kwargs["apply_all_orgs_pending"])
        self.assertTrue(kwargs["confirm_live_actions"])
        self.assertTrue(kwargs["guard_actionable_only"])
        self.assertTrue(kwargs["skip_shadow_workers"])
        self.assertEqual(kwargs["worker_parallelism"], 7)
        self.assertEqual(
            payload,
            {
                "ok": True,
                "action": "all-orgs-pending",
                "quota_restored_positive_balance_orgs": ["kray"],
                "shadow_ok": True,
                "shadow_health": "healthy",
                "action_ok": True,
                "action_stage": "all-orgs",
                "live_hashing_gpus": 0,
                "no_hash": 0,
                "negative": 0,
                "stuck": 0,
            },
        )

    def test_run_once_wakes_rollout_after_quota_restore_with_positive_balance(self) -> None:
        config = FleetConfig(
            organizations=(
                OrgConfig(
                    label="kray",
                    slug="kray",
                    api_key_env="SALAD_API_KEY_2",
                    slot_prefix="prl-kray-roi",
                ),
            )
        )
        profile = profit_model.Profile(
            profile_key="4090:batch:2048",
            gpu_key="4090",
            gpu_id="gpu-4090",
            priority="batch",
            label="RTX 4090 batch",
            memory_mb=2048,
            expected_th=230.0,
            static_hourly_usd=0.16,
        )
        pathlib.Path(os.environ["PRL_BALANCE_FILE"]).write_text(json.dumps({"kray": 1.25}), encoding="utf-8")
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.upsert_org_replica_quota(
                conn,
                {
                    "org_label": "kray",
                    "quota": 0,
                    "used": 0,
                    "available": 0,
                    "status": "zero_quota",
                    "reason": "container_replicas_quota_zero",
                    "source": "test",
                    "checked_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                },
            )
            conn.commit()

        class QuotaRestoredWatch(FakeWatch):
            ORG = "kray"

            def request(self, method: str, path: str, _payload=None, **_kwargs):
                self.request_call = (method, path)
                return {
                    "container_groups_quotas": {
                        "container_replicas_quota": 10,
                        "container_replicas_used": 0,
                    },
                    "update_time": "2026-06-26T21:30:00+00:00",
                }

        watch = QuotaRestoredWatch()
        wake_payload = {"ok": True, "action": "all-orgs-pending"}
        with (
            mock.patch.object(availability_probe, "load_config", return_value=config),
            mock.patch.object(availability_probe.profit_model, "load_profiles", return_value=[profile]),
            mock.patch.object(availability_probe, "load_watch_module", return_value=watch),
            mock.patch.object(availability_probe, "install_rate_limited_request"),
            mock.patch.object(
                availability_probe,
                "wake_rollout_on_quota_restore",
                return_value=wake_payload,
            ) as wake_mock,
        ):
            payload = availability_probe.run_once(db_path=self.db_path, profile_limit=1, org_parallelism=1)

        self.assertEqual(payload["probed"], 1)
        self.assertEqual(payload["quota_restored_positive_balance_orgs"], ["kray"])
        self.assertEqual(payload["quota_restore_rollout_wake"], wake_payload)
        wake_mock.assert_called_once_with(db_path=self.db_path, restored_orgs=["kray"])
        self.assertEqual(watch.request_call, ("GET", "/organizations/kray/quotas"))
        with state_db.connect(self.db_path) as conn:
            availability = conn.execute(
                "SELECT available_count FROM profile_availability WHERE org_label='kray'"
            ).fetchone()
            quota = conn.execute("SELECT * FROM org_replica_quotas WHERE org_label='kray'").fetchone()
            heartbeat = conn.execute(
                "SELECT payload_json FROM heartbeats WHERE process_name='availability_probe'"
            ).fetchone()
        self.assertEqual(availability["available_count"], 1)
        self.assertEqual(quota["quota"], 10)
        self.assertEqual(quota["available"], 10)
        self.assertIn("quota_restore_rollout_wake", heartbeat["payload_json"])

    def test_probe_org_profiles_uses_profile_parallelism(self) -> None:
        org = OrgConfig(
            label="test",
            slug="test",
            api_key_env="SALAD_API_KEY_TEST",
            slot_prefix="prl-test-roi",
        )
        profiles = [
            profit_model.Profile(
                profile_key=f"{gpu}:batch:2048",
                gpu_key=gpu,
                gpu_id=f"gpu-{gpu}",
                priority="batch",
                label=f"RTX {gpu} batch",
                memory_mb=2048,
                expected_th=230.0,
                static_hourly_usd=0.16,
            )
            for gpu in ("4090", "4080", "4070")
        ]

        RecordingExecutor.last_max_workers = None
        with (
            mock.patch.object(availability_probe, "load_watch_module", return_value=FakeWatch()),
            mock.patch.object(availability_probe, "install_rate_limited_request"),
            mock.patch.object(availability_probe.concurrent.futures, "ThreadPoolExecutor", RecordingExecutor),
        ):
            rows = availability_probe._probe_org_profiles(
                org,
                profiles,
                db_path=self.db_path,
                profile_parallelism=2,
            )

        self.assertEqual(RecordingExecutor.last_max_workers, 2)
        self.assertEqual([row["profile_key"] for row in rows], [profile.profile_key for profile in profiles])


if __name__ == "__main__":
    unittest.main()
