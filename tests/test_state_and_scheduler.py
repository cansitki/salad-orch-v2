from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import config_loader
import fleet_scheduler
import org_worker
import profile_scorer
import state_db
from config_loader import load_config


class StateAndSchedulerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(pathlib.Path(self.tmpdir.name) / "fleet.db")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_state_db_syncs_default_orgs_as_ten_slot_units(self) -> None:
        config = load_config()
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, config)
            conn.commit()
            orgs = conn.execute("SELECT COUNT(*) FROM organizations WHERE enabled = 1").fetchone()[0]
            slots = conn.execute("SELECT COUNT(*) FROM slots").fetchone()[0]
        self.assertEqual(orgs, 4)
        self.assertEqual(slots, 40)
        self.assertEqual(config.target_slot_count(), 40)

    def test_state_db_sync_prunes_slot_names_removed_by_override(self) -> None:
        org = config_loader.OrgConfig(
            label="kray2",
            slug="kray2",
            api_key_env="SALAD_API_KEY_2",
            slot_prefix="prl-kray2-roi",
        )
        initial = config_loader.FleetConfig(organizations=(org,))
        updated = config_loader.FleetConfig(
            organizations=(
                config_loader.OrgConfig(
                    **{
                        **config_loader.asdict(org),
                        "slot_name_overrides": ("", "", "", "", "prl-kray2-roi-05b"),
                    }
                ),
            )
        )
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, initial)
            state_db.set_slot_target(
                conn,
                {
                    "org_label": "kray2",
                    "slot_name": "prl-kray2-roi-05",
                    "profile_key": "4090:batch:2048",
                    "mode": "base_fill",
                    "decision_price_usd": 0.64,
                    "expected_profit_day": 1.0,
                    "protected": False,
                    "reason": "test",
                    "assigned_at_utc": "2026-06-24T12:00:00+00:00",
                },
            )
            state_db.sync_config(conn, updated)
            slots = [
                row[0]
                for row in conn.execute(
                    "SELECT slot_name FROM slots WHERE org_label='kray2' ORDER BY slot_index"
                ).fetchall()
            ]
            targets = [
                row[0]
                for row in conn.execute(
                    "SELECT slot_name FROM slot_targets WHERE org_label='kray2' ORDER BY slot_name"
                ).fetchall()
            ]
        self.assertEqual(len(slots), 10)
        self.assertIn("prl-kray2-roi-05b", slots)
        self.assertNotIn("prl-kray2-roi-05", slots)
        self.assertEqual(targets, [])

    def test_slot_observation_tracks_status_and_profile_since(self) -> None:
        config = load_config()
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, config)
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "observed_profile_key": "3090:batch:2048",
                    "observed_status": "allocating",
                    "updated_at_utc": "2026-06-24T12:00:00+00:00",
                },
            )
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "observed_profile_key": "3090:batch:2048",
                    "observed_status": "allocating",
                    "updated_at_utc": "2026-06-24T12:01:00+00:00",
                },
            )
            row = conn.execute(
                """
                SELECT observed_profile_since_utc, observed_status_since_utc
                FROM slots
                WHERE org_label = 'kray' AND slot_name = 'prl-kray-roi-01'
                """
            ).fetchone()
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "observed_profile_key": "4070ti:batch:2048",
                    "observed_status": "running",
                    "updated_at_utc": "2026-06-24T12:02:00+00:00",
                },
            )
            changed = conn.execute(
                """
                SELECT observed_profile_since_utc, observed_status_since_utc
                FROM slots
                WHERE org_label = 'kray' AND slot_name = 'prl-kray-roi-01'
                """
            ).fetchone()
        self.assertEqual(row["observed_profile_since_utc"], "2026-06-24T12:00:00+00:00")
        self.assertEqual(row["observed_status_since_utc"], "2026-06-24T12:00:00+00:00")
        self.assertEqual(changed["observed_profile_since_utc"], "2026-06-24T12:02:00+00:00")
        self.assertEqual(changed["observed_status_since_utc"], "2026-06-24T12:02:00+00:00")

    def test_worker_sync_marks_missing_workers_stale(self) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, load_config())
            state_db.sync_worker_rows(
                conn,
                [
                    {
                        "worker_name": "kray-prl-test-pearlfortune-inst-1",
                        "org_label": "kray",
                        "slot_name": "prl-kray-roi-01",
                        "instance_id": "inst-1",
                        "gpu_key": "3090",
                        "reported_hashrate_th": 101.5,
                        "last_stats_at": "2026-06-24T12:00:00+00:00",
                    }
                ],
            )
            live = conn.execute("SELECT stale, reported_hashrate_th FROM workers").fetchone()
            state_db.sync_worker_rows(conn, [])
            stale = conn.execute("SELECT stale, reported_hashrate_th FROM workers").fetchone()
        self.assertEqual(live["stale"], 0)
        self.assertEqual(live["reported_hashrate_th"], 101.5)
        self.assertEqual(stale["stale"], 1)
        self.assertEqual(stale["reported_hashrate_th"], 101.5)

    def test_org_worker_heartbeat_allows_long_live_ticks(self) -> None:
        class FakeWatch:
            ORG = "kray"
            PROJECT = "default"

            def slot_state(self, _slot_name: str) -> dict[str, object]:
                return {"counts": {"running": 0, "creating": 0, "allocating": 0, "stopping": 0}}

        original_load_watch_module = org_worker.load_watch_module
        original_install_rate_limited_request = org_worker.install_rate_limited_request
        org_worker.load_watch_module = lambda *_args, **_kwargs: FakeWatch()
        org_worker.install_rate_limited_request = lambda *_args, **_kwargs: None
        try:
            org_worker.run_once(org_label="kray", db_path=self.db_path, apply=False, schedule_if_empty=False)
        finally:
            org_worker.load_watch_module = original_load_watch_module
            org_worker.install_rate_limited_request = original_install_rate_limited_request

        with state_db.connect(self.db_path) as conn:
            heartbeat = conn.execute(
                "SELECT stale_after_seconds FROM heartbeats WHERE process_name = 'org_worker:kray'"
            ).fetchone()
        self.assertIsNotNone(heartbeat)
        self.assertEqual(heartbeat["stale_after_seconds"], 300)

    def test_slot_observation_preserves_hashrate_when_omitted(self) -> None:
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
                    "live_hashrate_th": 111.5,
                    "protected": True,
                },
            )
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
            preserved = conn.execute(
                """
                SELECT live_hashrate_th
                FROM slots
                WHERE org_label = 'kray' AND slot_name = 'prl-kray-roi-01'
                """
            ).fetchone()
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "observed_profile_key": "3090:batch:2048",
                    "observed_status": "running",
                    "live_hashrate_th": 0,
                    "protected": True,
                },
            )
            cleared = conn.execute(
                """
                SELECT live_hashrate_th
                FROM slots
                WHERE org_label = 'kray' AND slot_name = 'prl-kray-roi-01'
                """
            ).fetchone()

        self.assertEqual(preserved["live_hashrate_th"], 111.5)
        self.assertEqual(cleared["live_hashrate_th"], 0)

    def test_profile_scorer_uses_runtime_profit_snapshot_history(self) -> None:
        now = datetime.now(UTC)
        earlier = now - timedelta(minutes=2)
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, load_config())
            state_db.record_profit_snapshot(
                conn,
                {
                    "at_utc": now.isoformat(timespec="seconds"),
                    "scope": "slot",
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "decision_price_usd": 0.64,
                    "th": 0,
                    "cost_day": 1.44,
                    "revenue_day": 0,
                    "profit_day": -1.44,
                    "payload": {"gpu": "3080", "priority": "batch", "worker": "NO_POOL_HASHRATE"},
                },
            )
            state_db.record_attempt(
                conn,
                {
                    "at_utc": earlier.isoformat(timespec="seconds"),
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-02",
                    "action": "patch",
                    "profile_key": "3090:batch:2048",
                    "ok": True,
                },
            )
            state_db.record_profit_snapshot(
                conn,
                {
                    "at_utc": now.isoformat(timespec="seconds"),
                    "scope": "slot",
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-02",
                    "decision_price_usd": 0.64,
                    "th": 100,
                    "cost_day": 2.16,
                    "revenue_day": 3,
                    "profit_day": 0.84,
                    "payload": {"gpu": "3090", "priority": "batch", "worker": "kray-prl-test"},
                },
            )
            conn.commit()

        rows = profile_scorer.score_profiles(
            db_path=self.db_path,
            mode="base_fill",
            decision_price_usd=0.64,
            pearl_fee_rate=0.01,
            write=False,
        )
        by_profile = {row["profile_key"]: row for row in rows}
        self.assertEqual(by_profile["3080:batch:2048"]["reason"]["no_hash"], 1.0)
        self.assertEqual(by_profile["3080:batch:2048"]["reason"]["negative"], 1.0)
        self.assertEqual(by_profile["3080:batch:2048"]["reason"]["no_hash_sample_rate"], 1.0)
        self.assertEqual(by_profile["3090:batch:2048"]["reason"]["live_hash_samples"], 1.0)
        self.assertEqual(by_profile["3090:batch:2048"]["reason"]["avg_time_to_hash_seconds"], 120.0)

    def test_org_worker_waits_before_retargeting_fresh_pending_mismatch(self) -> None:
        class Watch:
            def slot_state(self, _slot_name):
                return (
                    {
                        "priority": "batch",
                        "container": {"resources": {"gpu_classes": ["gpu-rtx-3070"], "memory": 4096}},
                        "current_state": {"instance_status_counts": {"allocating_count": 1}},
                    },
                    [],
                )

            GPU = {"3070": "gpu-rtx-3070"}

        plan = org_worker.planned_action(
            Watch(),
            "prl-kray-roi-01",
            {
                "profile_key": "4090:batch:2048",
                "observed_status_since_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            },
            protect_pending=False,
            pending_retarget_after_seconds=45,
        )
        self.assertEqual(plan["action"], "observe")
        self.assertIn("pending_profile_mismatch_wait", plan["reason"])

    def test_org_worker_patches_stale_pending_mismatch_when_allowed(self) -> None:
        class Watch:
            def slot_state(self, _slot_name):
                return (
                    {
                        "priority": "batch",
                        "container": {"resources": {"gpu_classes": ["gpu-rtx-3070"], "memory": 4096}},
                        "current_state": {"instance_status_counts": {"allocating_count": 1}},
                    },
                    [],
                )

            GPU = {"3070": "gpu-rtx-3070"}

        plan = org_worker.planned_action(
            Watch(),
            "prl-kray-roi-01",
            {
                "profile_key": "4090:batch:2048",
                "observed_status_since_utc": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(timespec="seconds"),
            },
            protect_pending=False,
            pending_retarget_after_seconds=45,
        )
        self.assertEqual(plan["action"], "patch")
        self.assertIn("stale_pending_profile_mismatch", plan["reason"])

    def test_org_worker_waits_for_fresh_pending_profile_even_when_status_is_old(self) -> None:
        class Watch:
            def slot_state(self, _slot_name):
                return (
                    {
                        "priority": "batch",
                        "container": {"resources": {"gpu_classes": ["gpu-rtx-3070"], "memory": 4096}},
                        "current_state": {"instance_status_counts": {"allocating_count": 1}},
                    },
                    [],
                )

            GPU = {"3070": "gpu-rtx-3070"}

        plan = org_worker.planned_action(
            Watch(),
            "prl-kray-roi-01",
            {
                "profile_key": "4090:batch:2048",
                "observed_status_since_utc": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(
                    timespec="seconds"
                ),
                "observed_profile_since_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            },
            protect_pending=False,
            pending_retarget_after_seconds=60,
        )
        self.assertEqual(plan["action"], "observe")
        self.assertIn("pending_profile_mismatch_wait", plan["reason"])

    def test_org_worker_records_target_profile_after_successful_patch(self) -> None:
        observed = org_worker.observed_profile_key_for_result(
            {"profile_key": "4090:batch:2048"},
            {
                "action": "patch",
                "applied": True,
                "current_profile_key": "3070:batch:4096",
            },
            apply=True,
        )

        self.assertEqual(observed, "4090:batch:2048")

    def test_org_worker_cooldowns_stale_pending_source_profile_after_patch(self) -> None:
        profile_key = org_worker.cooldown_profile_key_for_result(
            {"profile_key": "4090:batch:2048"},
            {
                "action": "patch",
                "reason": "stale_pending_profile_mismatch:4080:batch:2048:age_300.0",
                "current_profile_key": "4080:batch:2048",
            },
        )

        self.assertEqual(profile_key, "4080:batch:2048")

    def test_org_worker_cooldowns_pending_target_profile(self) -> None:
        profile_key = org_worker.cooldown_profile_key_for_result(
            {"profile_key": "4090:batch:2048"},
            {
                "action": "cooldown_pending",
                "reason": "stale_pending_same_profile:4090:batch:2048:age_300.0",
                "current_profile_key": "4090:batch:2048",
            },
        )

        self.assertEqual(profile_key, "4090:batch:2048")

    def test_org_worker_does_not_cooldown_normal_patch(self) -> None:
        profile_key = org_worker.cooldown_profile_key_for_result(
            {"profile_key": "4090:batch:2048"},
            {
                "action": "patch",
                "reason": "missing_or_empty",
                "current_profile_key": "4080:batch:2048",
            },
        )

        self.assertIsNone(profile_key)

    def test_org_worker_failed_patch_becomes_profile_cooldown_action(self) -> None:
        class Watch:
            class Candidate:
                def __init__(self, label, priority, gpu_keys, memory):
                    self.label = label
                    self.priority = priority
                    self.gpu_keys = gpu_keys
                    self.memory = memory

            def patch_slot(self, _slot_name, _candidate, _reason):
                return False

        result = org_worker.execute_action(
            Watch(),
            {
                "slot_name": "prl-kray-roi-01",
                "label": "RTX 4090 batch",
                "priority": "batch",
                "gpu_key": "4090",
                "memory_mb": 2048,
            },
            {
                "slot_name": "prl-kray-roi-01",
                "action": "patch",
                "reason": "stale_pending_profile_mismatch:4080:batch:2048:age_300.0",
                "target_profile_key": "4090:batch:2048",
                "current_profile_key": "4080:batch:2048",
                "observed_status": "allocating",
                "protected": False,
                "counts": {"allocating": 1, "creating": 0, "running": 0, "stopping": 0},
                "instance_count": 0,
            },
            apply=True,
        )

        self.assertTrue(result["ok"])
        self.assertFalse(result["applied"])
        self.assertEqual(result["action"], "cooldown_failed_patch")
        self.assertEqual(result["original_action"], "patch")

    def test_org_worker_cooldowns_stale_pending_same_profile(self) -> None:
        class Watch:
            def slot_state(self, _slot_name):
                return (
                    {
                        "priority": "batch",
                        "container": {"resources": {"gpu_classes": ["gpu-rtx-4090"], "memory": 2048}},
                        "current_state": {"instance_status_counts": {"allocating_count": 1}},
                    },
                    [{"id": "pending-1"}],
                )

            GPU = {"4090": "gpu-rtx-4090"}

        plan = org_worker.planned_action(
            Watch(),
            "prl-kray-roi-01",
            {
                "profile_key": "4090:batch:2048",
                "observed_status_since_utc": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(timespec="seconds"),
            },
            protect_pending=False,
            pending_retarget_after_seconds=60,
        )
        self.assertEqual(plan["action"], "cooldown_pending")
        self.assertIn("stale_pending_same_profile", plan["reason"])
        self.assertEqual(plan["pending_instance_ids"], ["pending-1"])

    def test_org_worker_recycles_stale_pending_same_profile_instances(self) -> None:
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
                self.reallocate_calls = []

            def reallocate(self, slot_name, instance_id, reason):
                self.reallocate_calls.append((slot_name, instance_id, reason))

        watch = Watch()
        result = org_worker.execute_action(
            watch,
            {
                "slot_name": "prl-kray-roi-01",
                "label": "RTX 4090 batch",
                "priority": "batch",
                "gpu_key": "4090",
                "memory_mb": 2048,
            },
            {
                "slot_name": "prl-kray-roi-01",
                "action": "cooldown_pending",
                "reason": "stale_pending_same_profile:4090:batch:2048:age_300.0",
                "target_profile_key": "4090:batch:2048",
                "current_profile_key": "4090:batch:2048",
                "observed_status": "allocating",
                "protected": False,
                "counts": {"allocating": 1, "creating": 0, "running": 0, "stopping": 0},
                "instance_count": 1,
                "pending_instance_ids": ["pending-1"],
            },
            apply=True,
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["applied"])
        self.assertEqual(result["recycled_pending_instances"], ["pending-1"])
        self.assertFalse(result["restart_requested"])
        self.assertEqual(watch.reallocate_calls, [("prl-kray-roi-01", "pending-1", "stale_pending_same_profile")])

    def test_org_worker_restarts_hidden_stale_pending_same_profile(self) -> None:
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
                self.request_calls = []
                self.start_calls = []

            def request(self, method, path):
                self.request_calls.append((method, path))
                return {}

            def start_slot(self, slot_name, reason):
                self.start_calls.append((slot_name, reason))

        watch = Watch()
        result = org_worker.execute_action(
            watch,
            {
                "slot_name": "prl-kray-roi-01",
                "label": "RTX 4090 batch",
                "priority": "batch",
                "gpu_key": "4090",
                "memory_mb": 2048,
            },
            {
                "slot_name": "prl-kray-roi-01",
                "action": "cooldown_pending",
                "reason": "stale_pending_same_profile:4090:batch:2048:age_300.0",
                "target_profile_key": "4090:batch:2048",
                "current_profile_key": "4090:batch:2048",
                "observed_status": "allocating",
                "protected": False,
                "counts": {"allocating": 1, "creating": 0, "running": 0, "stopping": 0},
                "instance_count": 0,
                "pending_instance_ids": [],
            },
            apply=True,
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["applied"])
        self.assertEqual(result["recycled_pending_instances"], [])
        self.assertTrue(result["restart_requested"])
        self.assertEqual(result["restart_reason"], "stale_pending_without_visible_instances")
        self.assertEqual(
            watch.request_calls,
            [("POST", "/organizations/kray/projects/default/containers/prl-kray-roi-01/stop")],
        )
        self.assertEqual(
            watch.start_calls,
            [("prl-kray-roi-01", "stale_pending_same_profile:stale_pending_without_visible_instances")],
        )

    def test_org_worker_waits_on_fresh_pending_same_profile(self) -> None:
        class Watch:
            def slot_state(self, _slot_name):
                return (
                    {
                        "priority": "batch",
                        "container": {"resources": {"gpu_classes": ["gpu-rtx-4090"], "memory": 2048}},
                        "current_state": {"instance_status_counts": {"allocating_count": 1}},
                    },
                    [],
                )

            GPU = {"4090": "gpu-rtx-4090"}

        plan = org_worker.planned_action(
            Watch(),
            "prl-kray-roi-01",
            {
                "profile_key": "4090:batch:2048",
                "observed_status_since_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            },
            protect_pending=False,
            pending_retarget_after_seconds=60,
        )
        self.assertEqual(plan["action"], "observe")
        self.assertIn("target_pending_wait", plan["reason"])

    def test_org_worker_waits_on_fresh_pending_profile_same_target_even_when_status_is_old(self) -> None:
        class Watch:
            def slot_state(self, _slot_name):
                return (
                    {
                        "priority": "batch",
                        "container": {"resources": {"gpu_classes": ["gpu-rtx-4090"], "memory": 2048}},
                        "current_state": {"instance_status_counts": {"allocating_count": 1}},
                    },
                    [],
                )

            GPU = {"4090": "gpu-rtx-4090"}

        plan = org_worker.planned_action(
            Watch(),
            "prl-kray-roi-01",
            {
                "profile_key": "4090:batch:2048",
                "observed_status_since_utc": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(
                    timespec="seconds"
                ),
                "observed_profile_since_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            },
            protect_pending=False,
            pending_retarget_after_seconds=60,
        )
        self.assertEqual(plan["action"], "observe")
        self.assertIn("target_pending_wait", plan["reason"])

    def test_org_worker_skips_live_hashing_target_without_guard_issue(self) -> None:
        target = {
            "slot_name": "prl-kray-roi-01",
            "profile_key": "3090:batch:2048",
            "slot_observed_profile_key": "3090:batch:2048",
            "slot_observed_status": "running",
            "live_worker_count": 1,
            "live_worker_th": 111.5,
            "active_guard_issues": 0,
        }

        self.assertTrue(org_worker.should_skip_live_hashing_target(target, apply=True, allow_live_retarget=False))
        result = org_worker.skipped_live_hashing_result(target)

        self.assertEqual(result["action"], "skip_live_hashing")
        self.assertEqual(result["current_profile_key"], "3090:batch:2048")
        self.assertEqual(result["observed_status"], "running")
        self.assertTrue(result["protected"])

    def test_org_worker_does_not_skip_live_hashing_target_with_guard_issue(self) -> None:
        target = {
            "slot_observed_status": "running",
            "live_worker_count": 1,
            "live_worker_th": 111.5,
            "active_guard_issues": 1,
        }

        self.assertFalse(org_worker.should_skip_live_hashing_target(target, apply=True, allow_live_retarget=False))

    def test_scheduler_assigns_diversified_profitable_batch_targets(self) -> None:
        payload = fleet_scheduler.schedule_once(
            db_path=self.db_path,
            price=0.64,
            fee=0.01,
            dry_run=False,
            width=10,
        )
        self.assertEqual(payload["assigned_targets"], 40)
        self.assertEqual(len(payload["profile_counts"]), 10)
        self.assertTrue(all(":low:" not in key for key in payload["profile_counts"]))
        with state_db.connect(self.db_path) as conn:
            target_count = conn.execute("SELECT COUNT(*) FROM slot_targets").fetchone()[0]
            profile_count = conn.execute("SELECT COUNT(*) FROM gpu_profiles").fetchone()[0]
        self.assertEqual(target_count, 40)
        self.assertGreaterEqual(profile_count, 10)

    def test_scheduler_preserves_protected_running_slot_target(self) -> None:
        config = load_config()
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, config)
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "observed_profile_key": "3090:batch:2048",
                    "observed_status": "running",
                    "live_hashrate_th": 111.5,
                    "protected": True,
                },
            )
            conn.commit()
        fleet_scheduler.schedule_once(db_path=self.db_path, price=0.64, fee=0.01, dry_run=False)
        with state_db.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT profile_key, protected, reason
                FROM slot_targets
                WHERE org_label = 'kray' AND slot_name = 'prl-kray-roi-01'
                """
            ).fetchone()
        self.assertEqual(row["profile_key"], "3090:batch:2048")
        self.assertEqual(row["protected"], 1)
        self.assertIn("protected_observed_profile", row["reason"])

    def test_scheduler_replaces_protected_running_slot_without_live_hashrate(self) -> None:
        config = load_config()
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, config)
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "observed_profile_key": "3090:batch:2048",
                    "observed_status": "running",
                    "live_hashrate_th": 0,
                    "protected": True,
                },
            )
            conn.commit()
        fleet_scheduler.schedule_once(db_path=self.db_path, price=0.64, fee=0.01, dry_run=False)
        with state_db.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT profile_key, protected, reason
                FROM slot_targets
                WHERE org_label = 'kray' AND slot_name = 'prl-kray-roi-01'
                """
            ).fetchone()
        self.assertNotEqual(row["profile_key"], "3090:batch:2048")
        self.assertEqual(row["protected"], 0)
        self.assertIn("replace_nohash_observed_profile", row["reason"])

    def test_scheduler_replaces_protected_negative_running_slot_target(self) -> None:
        config = load_config()
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, config)
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": "kry1",
                    "slot_name": "prl-kry1-roi-09",
                    "observed_profile_key": "4080:low:2048",
                    "observed_status": "running",
                    "protected": True,
                },
            )
            conn.commit()
        fleet_scheduler.schedule_once(db_path=self.db_path, price=0.64, fee=0.01, dry_run=False)
        with state_db.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT profile_key, protected, reason, expected_profit_day
                FROM slot_targets
                WHERE org_label = 'kry1' AND slot_name = 'prl-kry1-roi-09'
                """
            ).fetchone()
        self.assertNotEqual(row["profile_key"], "4080:low:2048")
        self.assertEqual(row["protected"], 0)
        self.assertGreaterEqual(row["expected_profit_day"], 0.05)
        self.assertIn("replace_negative_observed_profile", row["reason"])

    def test_scheduler_preserves_active_guard_target(self) -> None:
        config = load_config()
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, config)
            state_db.set_slot_target(
                conn,
                {
                    "org_label": "kry1",
                    "slot_name": "prl-kry1-roi-07",
                    "profile_key": "5090:batch:2048",
                    "mode": "base_fill",
                    "decision_price_usd": 0.64,
                    "expected_profit_day": 1.09,
                    "protected": False,
                    "reason": "guard_negative_retarget",
                    "assigned_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                },
            )
            state_db.record_guard_issue(
                conn,
                {
                    "org_label": "kry1",
                    "slot_name": "prl-kry1-roi-07",
                    "issue_type": "negative",
                    "payload": {"gpu": "5090laptop"},
                },
            )
            conn.commit()

        fleet_scheduler.schedule_once(db_path=self.db_path, price=0.64, fee=0.01, dry_run=False)

        with state_db.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT profile_key, protected, reason
                FROM slot_targets
                WHERE org_label = 'kry1' AND slot_name = 'prl-kry1-roi-07'
                """
            ).fetchone()

        self.assertEqual(row["profile_key"], "5090:batch:2048")
        self.assertEqual(row["protected"], 0)
        self.assertEqual(row["reason"], "guard_negative_retarget")

    def test_optimize_mode_can_upgrade_protected_running_slot_when_delta_is_large(self) -> None:
        config = load_config()
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, config)
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "observed_profile_key": "3060ti:batch:2048",
                    "observed_status": "running",
                    "live_hashrate_th": 111.5,
                    "protected": True,
                },
            )
            conn.commit()
        fleet_scheduler.schedule_once(
            db_path=self.db_path,
            mode="optimize",
            price=0.64,
            fee=0.01,
            dry_run=False,
        )
        with state_db.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT profile_key, protected, reason
                FROM slot_targets
                WHERE org_label = 'kray' AND slot_name = 'prl-kray-roi-01'
                """
            ).fetchone()
        self.assertNotEqual(row["profile_key"], "3060ti:batch:2048")
        self.assertEqual(row["protected"], 0)
        self.assertIn("optimize:upgrade_from_3060ti:batch:2048", row["reason"])

    def test_scheduler_respects_recent_zero_availability_for_org_profile(self) -> None:
        config = load_config()
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, config)
            state_db.upsert_profile_availability(
                conn,
                {
                    "org_label": "kray",
                    "profile_key": "4090:batch:2048",
                    "available_count": 0,
                    "ok": True,
                },
            )
            conn.commit()
        payload = fleet_scheduler.schedule_once(db_path=self.db_path, price=0.64, fee=0.01, dry_run=False)
        kray_4090 = [
            target
            for target in payload["targets"]
            if target["org_label"] == "kray" and target["profile_key"] == "4090:batch:2048"
        ]
        self.assertEqual(kray_4090, [])

    def test_scheduler_uses_probe_fallback_to_keep_org_filled_when_all_profiles_report_zero(self) -> None:
        config = load_config()
        scores = profile_scorer.score_profiles(
            db_path=self.db_path,
            decision_price_usd=0.64,
            pearl_fee_rate=0.01,
            write=False,
        )
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, config)
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

        payload = fleet_scheduler.schedule_once(db_path=self.db_path, price=0.64, fee=0.01, dry_run=False)
        kray_targets = [target for target in payload["targets"] if target["org_label"] == "kray"]

        self.assertEqual(payload["assigned_targets"], 40)
        self.assertEqual(len(kray_targets), 10)
        self.assertTrue(all("availability_probe_fallback" in target["reason"] for target in kray_targets))

    def test_scheduler_respects_active_wildcard_search_cooldown(self) -> None:
        config = load_config()
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, config)
            state_db.record_search_state(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "*",
                    "profile_key": "4090:batch:2048",
                    "sleep_until_utc": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(timespec="seconds"),
                    "attempts": 20,
                    "reason": "availability_zero",
                },
            )
            conn.commit()
        payload = fleet_scheduler.schedule_once(db_path=self.db_path, price=0.64, fee=0.01, dry_run=False)
        self.assertFalse(
            any(
                target["org_label"] == "kray" and target["profile_key"] == "4090:batch:2048"
                for target in payload["targets"]
            )
        )


if __name__ == "__main__":
    unittest.main()
