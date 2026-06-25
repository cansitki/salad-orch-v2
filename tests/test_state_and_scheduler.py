from __future__ import annotations

import json
import pathlib
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import config_loader
import fleet_scheduler
import org_worker
import profit_model
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

    def test_slot_observation_can_reset_age_after_pending_recycle(self) -> None:
        config = load_config()
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, config)
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "observed_profile_key": "4090:batch:2048",
                    "observed_status": "allocating",
                    "updated_at_utc": "2026-06-24T12:00:00+00:00",
                },
            )
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "observed_profile_key": "4090:batch:2048",
                    "observed_status": "allocating",
                    "updated_at_utc": "2026-06-24T12:05:00+00:00",
                    "reset_observed_age": True,
                },
            )
            row = conn.execute(
                """
                SELECT observed_profile_since_utc, observed_status_since_utc
                FROM slots
                WHERE org_label = 'kray' AND slot_name = 'prl-kray-roi-01'
                """
            ).fetchone()

        self.assertEqual(row["observed_profile_since_utc"], "2026-06-24T12:05:00+00:00")
        self.assertEqual(row["observed_status_since_utc"], "2026-06-24T12:05:00+00:00")

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

    def test_org_worker_run_once_can_write_non_staling_action_heartbeat(self) -> None:
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
            org_worker.run_once(
                org_label="kray",
                db_path=self.db_path,
                apply=False,
                schedule_if_empty=False,
                heartbeat_stale_after_seconds=0,
            )
        finally:
            org_worker.load_watch_module = original_load_watch_module
            org_worker.install_rate_limited_request = original_install_rate_limited_request

        with state_db.connect(self.db_path) as conn:
            heartbeat = conn.execute(
                "SELECT stale_after_seconds FROM heartbeats WHERE process_name = 'org_worker:kray'"
            ).fetchone()
        self.assertIsNotNone(heartbeat)
        self.assertEqual(heartbeat["stale_after_seconds"], 0)

    def test_org_worker_skips_live_actions_for_explicit_zero_balance(self) -> None:
        fleet_scheduler.schedule_once(db_path=self.db_path, price=0.64, fee=0.01, dry_run=False)
        balance_file = pathlib.Path(self.tmpdir.name) / "balances.json"
        balance_file.write_text(json.dumps({"kray": 0.0}), encoding="utf-8")

        original_balance_file = os.environ.get("PRL_BALANCE_FILE")
        original_skip = os.environ.get("PRL_SKIP_ZERO_BALANCE_ORGS")
        original_load_watch_module = org_worker.load_watch_module
        original_install_rate_limited_request = org_worker.install_rate_limited_request
        os.environ["PRL_BALANCE_FILE"] = str(balance_file)
        os.environ["PRL_SKIP_ZERO_BALANCE_ORGS"] = "1"
        org_worker.load_watch_module = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("zero balance org should not load Salad watcher")
        )
        org_worker.install_rate_limited_request = lambda *_args, **_kwargs: None
        try:
            payload = org_worker.run_once(
                org_label="kray",
                db_path=self.db_path,
                apply=True,
                schedule_if_empty=False,
                heartbeat_stale_after_seconds=0,
            )
        finally:
            if original_balance_file is None:
                os.environ.pop("PRL_BALANCE_FILE", None)
            else:
                os.environ["PRL_BALANCE_FILE"] = original_balance_file
            if original_skip is None:
                os.environ.pop("PRL_SKIP_ZERO_BALANCE_ORGS", None)
            else:
                os.environ["PRL_SKIP_ZERO_BALANCE_ORGS"] = original_skip
            org_worker.load_watch_module = original_load_watch_module
            org_worker.install_rate_limited_request = original_install_rate_limited_request

        self.assertEqual(payload["action_counts"], {"skip_zero_balance": 10})
        self.assertEqual(payload["targets"], 10)
        self.assertTrue(all(result["action"] == "skip_zero_balance" for result in payload["results"]))
        with state_db.connect(self.db_path) as conn:
            attempts = conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE org_label='kray' AND action='skip_zero_balance'"
            ).fetchone()[0]
            heartbeat = conn.execute(
                "SELECT payload_json FROM heartbeats WHERE process_name = 'org_worker:kray'"
            ).fetchone()
        self.assertEqual(attempts, 10)
        self.assertIn("zero_balance_skip", heartbeat["payload_json"])

    def test_zero_balance_skip_requires_explicit_fresh_org_balance(self) -> None:
        balance_file = pathlib.Path(self.tmpdir.name) / "balances.json"
        balance_file.write_text(json.dumps({"kray3": 0.0}), encoding="utf-8")

        self.assertIsNotNone(org_worker.explicit_zero_balance_skip("kray3", path=balance_file))
        self.assertIsNone(org_worker.explicit_zero_balance_skip("kry1", path=balance_file))

    def test_active_org_cooldown_uses_wildcard_search_cooldown(self) -> None:
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
            cooldown = state_db.active_org_cooldown(conn, "kry1")

        self.assertIsNotNone(cooldown)
        self.assertEqual(cooldown["reason"], "http_400:no_credits_available")

    def test_org_worker_skips_active_no_credits_cooldown_without_loading_watch(self) -> None:
        original_skip = os.environ.get("PRL_SKIP_ZERO_BALANCE_ORGS")
        os.environ["PRL_SKIP_ZERO_BALANCE_ORGS"] = "0"
        original_load_watch_module = org_worker.load_watch_module
        try:
            with state_db.connect(self.db_path) as conn:
                state_db.init_db(conn)
                state_db.sync_config(conn, load_config())
                state_db.upsert_gpu_profiles(conn, profit_model.load_profiles())
                state_db.set_slot_target(
                    conn,
                    {
                        "org_label": "kry1",
                        "slot_name": "prl-kry1-roi-01",
                        "profile_key": "4070tis:low:4096",
                        "mode": "risk_off",
                        "decision_price_usd": 0.64,
                        "expected_profit_day": 0.5,
                        "protected": False,
                        "reason": "test",
                    },
                )
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

            def fail_load_watch(*_args, **_kwargs):
                raise AssertionError("load_watch_module should not run during no-credits cooldown")

            org_worker.load_watch_module = fail_load_watch
            payload = org_worker.run_once(
                org_label="kry1",
                db_path=self.db_path,
                apply=True,
                schedule_if_empty=False,
            )
        finally:
            if original_skip is None:
                os.environ.pop("PRL_SKIP_ZERO_BALANCE_ORGS", None)
            else:
                os.environ["PRL_SKIP_ZERO_BALANCE_ORGS"] = original_skip
            org_worker.load_watch_module = original_load_watch_module

        self.assertEqual(payload["action_counts"], {"skip_no_credits": 1})
        self.assertEqual(payload["results"][0]["action"], "skip_no_credits")
        with state_db.connect(self.db_path) as conn:
            attempts = conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE org_label='kry1' AND action='skip_no_credits'"
            ).fetchone()[0]
            heartbeat = conn.execute(
                "SELECT payload_json FROM heartbeats WHERE process_name = 'org_worker:kry1'"
            ).fetchone()
        self.assertEqual(attempts, 1)
        self.assertIn("no_credits_skip", heartbeat["payload_json"])

    def test_org_worker_sets_no_credits_cooldown_and_skips_remaining_targets(self) -> None:
        original_skip = os.environ.get("PRL_SKIP_ZERO_BALANCE_ORGS")
        os.environ["PRL_SKIP_ZERO_BALANCE_ORGS"] = "0"
        original_load_watch_module = org_worker.load_watch_module
        original_install_rate_limited_request = org_worker.install_rate_limited_request

        class Watch:
            GPU = {"4070tis": "gpu-rtx-4070ti-super"}

            class Candidate:
                def __init__(self, label, priority, gpu_keys, memory):
                    self.label = label
                    self.priority = priority
                    self.gpu_keys = gpu_keys
                    self.memory = memory

            def __init__(self):
                self.start_calls = 0

            def slot_state(self, _slot_name):
                return (
                    {
                        "priority": "low",
                        "container": {
                            "resources": {
                                "gpu_classes": ["gpu-rtx-4070ti-super"],
                                "memory": 4096,
                            }
                        },
                        "current_state": {"instance_status_counts": {}},
                    },
                    [],
                )

            def start_slot(self, _slot_name, _reason):
                self.start_calls += 1
                return False

            def start_slot_error(self, _slot_name):
                return "http_400:no_credits_available"

        watch = Watch()
        try:
            with state_db.connect(self.db_path) as conn:
                state_db.init_db(conn)
                state_db.sync_config(conn, load_config())
                state_db.upsert_gpu_profiles(conn, profit_model.load_profiles())
                for index in (1, 2):
                    state_db.set_slot_target(
                        conn,
                        {
                            "org_label": "kry1",
                            "slot_name": f"prl-kry1-roi-{index:02d}",
                            "profile_key": "4070tis:low:4096",
                            "mode": "risk_off",
                            "decision_price_usd": 0.64,
                            "expected_profit_day": 0.5,
                            "protected": False,
                            "reason": "test",
                        },
                    )
                conn.commit()

            org_worker.load_watch_module = lambda *_args, **_kwargs: watch
            org_worker.install_rate_limited_request = lambda *_args, **_kwargs: None
            payload = org_worker.run_once(
                org_label="kry1",
                db_path=self.db_path,
                apply=True,
                schedule_if_empty=False,
            )
        finally:
            if original_skip is None:
                os.environ.pop("PRL_SKIP_ZERO_BALANCE_ORGS", None)
            else:
                os.environ["PRL_SKIP_ZERO_BALANCE_ORGS"] = original_skip
            org_worker.load_watch_module = original_load_watch_module
            org_worker.install_rate_limited_request = original_install_rate_limited_request

        self.assertEqual(watch.start_calls, 1)
        self.assertEqual(payload["action_counts"], {"start_failed": 1, "skip_no_credits": 1})
        self.assertEqual(payload["results"][0]["action"], "start_failed")
        self.assertEqual(payload["results"][1]["action"], "skip_no_credits")
        with state_db.connect(self.db_path) as conn:
            cooldown = state_db.active_org_cooldown(conn, "kry1")
        self.assertIsNotNone(cooldown)
        self.assertEqual(cooldown["reason"], "http_400:no_credits_available")

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

    def test_attempt_stats_excludes_iso_rows_older_than_24_hours(self) -> None:
        now = datetime.now(UTC)
        old = now - timedelta(hours=25)
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.record_attempt(
                conn,
                {
                    "at_utc": old.isoformat(timespec="seconds"),
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "action": "capacity_failure",
                    "profile_key": "4090:batch:2048",
                    "ok": False,
                },
            )
            state_db.record_attempt(
                conn,
                {
                    "at_utc": now.isoformat(timespec="seconds"),
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-02",
                    "action": "patch",
                    "profile_key": "4090:batch:2048",
                    "ok": True,
                },
            )
            stats = state_db.attempt_stats(conn)

        self.assertEqual(stats["4090:batch:2048"]["success"], 1)
        self.assertEqual(stats["4090:batch:2048"]["failure"], 0)
        self.assertEqual(stats["4090:batch:2048"]["capacity_failure"], 0)

    def test_profile_runtime_stats_excludes_iso_rows_older_than_24_hours(self) -> None:
        now = datetime.now(UTC)
        old = now - timedelta(hours=25)
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.record_attempt(
                conn,
                {
                    "at_utc": old.isoformat(timespec="seconds"),
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "action": "patch",
                    "profile_key": "3090:batch:2048",
                    "ok": True,
                },
            )
            state_db.record_profit_snapshot(
                conn,
                {
                    "at_utc": old.isoformat(timespec="seconds"),
                    "scope": "slot",
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "profile_key": "3080:batch:2048",
                    "decision_price_usd": 0.64,
                    "th": 0,
                    "cost_day": 1.44,
                    "revenue_day": 0,
                    "profit_day": -1.44,
                    "payload": {"gpu": "3080", "priority": "batch"},
                },
            )
            state_db.record_profit_snapshot(
                conn,
                {
                    "at_utc": now.isoformat(timespec="seconds"),
                    "scope": "slot",
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "profile_key": "3090:batch:2048",
                    "decision_price_usd": 0.64,
                    "th": 100,
                    "cost_day": 2.16,
                    "revenue_day": 3,
                    "profit_day": 0.84,
                    "payload": {"gpu": "3090", "priority": "batch"},
                },
            )
            stats = profile_scorer.profile_runtime_stats(conn)

        self.assertNotIn("3080:batch:2048", stats)
        self.assertEqual(stats["3090:batch:2048"]["live_hash_samples"], 1)
        self.assertEqual(stats["3090:batch:2048"]["no_hash_samples"], 0)
        self.assertEqual(stats["3090:batch:2048"]["time_to_hash_samples"], 0)

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

    def test_org_worker_treats_deploying_mismatch_as_pending(self) -> None:
        class Watch:
            def slot_state(self, _slot_name):
                return (
                    {
                        "priority": "batch",
                        "container": {"resources": {"gpu_classes": ["gpu-rtx-3070"], "memory": 4096}},
                        "current_state": {
                            "status": "Deploying",
                            "instance_status_counts": {},
                        },
                    },
                    [],
                )

            GPU = {"3070": "gpu-rtx-3070"}

        plan = org_worker.planned_action(
            Watch(),
            "prl-kray-roi-01",
            {
                "profile_key": "4090:batch:2048",
                "observed_profile_since_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            },
            protect_pending=False,
            pending_retarget_after_seconds=60,
        )

        self.assertEqual(plan["action"], "observe")
        self.assertEqual(plan["observed_status"], "deploying")
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

    def test_org_worker_uses_longer_default_for_pending_status_than_running_no_hash(self) -> None:
        class PendingWatch:
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

        class RunningWatch:
            def slot_state(self, _slot_name):
                return (
                    {
                        "priority": "low",
                        "container": {"resources": {"gpu_classes": ["gpu-rtx-4070tis"], "memory": 2048}},
                        "current_state": {"instance_status_counts": {"running_count": 1}},
                    },
                    [{"id": "running-1", "ready": True, "started": True}],
                )

            GPU = {"4070tis": "gpu-rtx-4070tis"}

        observed_at = (datetime.now(UTC) - timedelta(seconds=75)).isoformat(timespec="seconds")
        pending_plan = org_worker.planned_action(
            PendingWatch(),
            "prl-kray-roi-01",
            {
                "profile_key": "4090:batch:2048",
                "observed_status_since_utc": observed_at,
            },
            protect_pending=False,
            pending_retarget_after_seconds=60,
        )
        running_plan = org_worker.planned_action(
            RunningWatch(),
            "prl-kray-roi-01",
            {
                "profile_key": "5090:low:2048",
                "observed_profile_since_utc": observed_at,
                "live_worker_count": 0,
                "live_worker_th": 0,
            },
            protect_running=True,
            pending_retarget_after_seconds=60,
        )

        self.assertEqual(pending_plan["action"], "observe")
        self.assertIn("pending_profile_mismatch_wait", pending_plan["reason"])
        self.assertIn("lt_120", pending_plan["reason"])
        self.assertEqual(running_plan["action"], "patch")
        self.assertIn("stale_running_no_hash_profile_mismatch", running_plan["reason"])

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

            def patch_slot(self, _slot_name, _candidate, _reason, *, start_after=True):
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

    def test_org_worker_start_failure_is_reported(self) -> None:
        class Watch:
            class Candidate:
                def __init__(self, label, priority, gpu_keys, memory):
                    self.label = label
                    self.priority = priority
                    self.gpu_keys = gpu_keys
                    self.memory = memory

            def start_slot(self, _slot_name, _reason):
                return False

            def start_slot_error(self, _slot_name):
                return "http_400:no_credits_available"

        result = org_worker.execute_action(
            Watch(),
            {
                "slot_name": "prl-kry1-roi-01",
                "label": "RTX 4090 batch",
                "priority": "batch",
                "gpu_key": "4090",
                "memory_mb": 2048,
            },
            {
                "slot_name": "prl-kry1-roi-01",
                "action": "start",
                "reason": "target_stopped_or_empty",
                "target_profile_key": "4090:batch:2048",
                "current_profile_key": "4090:batch:2048",
                "observed_status": "stopped",
                "protected": False,
                "counts": {"allocating": 0, "creating": 0, "running": 0, "stopping": 0},
                "instance_count": 0,
            },
            apply=True,
        )

        self.assertFalse(result["ok"])
        self.assertFalse(result["applied"])
        self.assertEqual(result["action"], "start_failed")
        self.assertEqual(result["original_action"], "start")
        self.assertEqual(result["error"], "http_400:no_credits_available")

    def test_org_worker_stopped_patch_starts_separately_and_reports_failure(self) -> None:
        class Watch:
            class Candidate:
                def __init__(self, label, priority, gpu_keys, memory):
                    self.label = label
                    self.priority = priority
                    self.gpu_keys = gpu_keys
                    self.memory = memory

            def __init__(self):
                self.patch_start_after = None

            def patch_slot(self, _slot_name, _candidate, _reason, *, start_after=True):
                self.patch_start_after = start_after
                return True

            def start_slot(self, _slot_name, _reason):
                return False

            def start_slot_error(self, _slot_name):
                return "http_400:no_credits_available"

        watch = Watch()
        result = org_worker.execute_action(
            watch,
            {
                "slot_name": "prl-kry1-roi-01",
                "label": "RTX 5090 batch",
                "priority": "batch",
                "gpu_key": "5090",
                "memory_mb": 2048,
            },
            {
                "slot_name": "prl-kry1-roi-01",
                "action": "patch",
                "reason": "profile_mismatch:4090:batch:2048",
                "target_profile_key": "5090:batch:2048",
                "current_profile_key": "4090:batch:2048",
                "observed_status": "stopped",
                "protected": False,
                "counts": {"allocating": 0, "creating": 0, "running": 0, "stopping": 0},
                "instance_count": 0,
            },
            apply=True,
        )

        self.assertFalse(watch.patch_start_after)
        self.assertFalse(result["ok"])
        self.assertFalse(result["applied"])
        self.assertTrue(result["patched"])
        self.assertEqual(result["action"], "start_failed")
        self.assertEqual(result["original_action"], "patch")
        self.assertEqual(result["error"], "http_400:no_credits_available")

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

    def test_org_worker_recycles_stale_deploying_same_profile(self) -> None:
        class Watch:
            def slot_state(self, _slot_name):
                return (
                    {
                        "priority": "batch",
                        "container": {"resources": {"gpu_classes": ["gpu-rtx-4090"], "memory": 2048}},
                        "current_state": {
                            "status": "Deploying",
                            "instance_status_counts": {},
                        },
                    },
                    [{"id": "pending-1"}],
                )

            GPU = {"4090": "gpu-rtx-4090"}

        plan = org_worker.planned_action(
            Watch(),
            "prl-kray-roi-01",
            {
                "profile_key": "4090:batch:2048",
                "observed_profile_since_utc": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(
                    timespec="seconds"
                ),
            },
            protect_pending=False,
            pending_retarget_after_seconds=60,
        )

        self.assertEqual(plan["action"], "cooldown_pending")
        self.assertEqual(plan["observed_status"], "deploying")
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

    def test_org_worker_reallocates_running_no_hash_same_profile_instances(self) -> None:
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
                "slot_name": "prl-kray-roi-10",
                "label": "RTX 3090 batch",
                "priority": "batch",
                "gpu_key": "3090",
                "memory_mb": 2048,
            },
            {
                "slot_name": "prl-kray-roi-10",
                "action": "restart_no_hash",
                "reason": "stale_running_no_hash_same_profile:3090:batch:2048:age_300.0",
                "target_profile_key": "3090:batch:2048",
                "current_profile_key": "3090:batch:2048",
                "observed_status": "running",
                "protected": False,
                "counts": {"allocating": 0, "creating": 0, "running": 1, "stopping": 0},
                "instance_count": 1,
                "running_instance_ids": ["running-1"],
            },
            apply=True,
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["applied"])
        self.assertEqual(result["reallocated_instances"], ["running-1"])
        self.assertFalse(result["restart_requested"])
        self.assertEqual(watch.reallocate_calls, [("prl-kray-roi-10", "running-1", "running_no_hash_same_profile")])

    def test_org_worker_restarts_running_no_hash_same_profile_without_visible_instances(self) -> None:
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
                "slot_name": "prl-kray-roi-10",
                "label": "RTX 3090 batch",
                "priority": "batch",
                "gpu_key": "3090",
                "memory_mb": 2048,
            },
            {
                "slot_name": "prl-kray-roi-10",
                "action": "restart_no_hash",
                "reason": "stale_running_no_hash_same_profile:3090:batch:2048:age_300.0",
                "target_profile_key": "3090:batch:2048",
                "current_profile_key": "3090:batch:2048",
                "observed_status": "running",
                "protected": False,
                "counts": {"allocating": 0, "creating": 0, "running": 1, "stopping": 0},
                "instance_count": 0,
                "running_instance_ids": [],
            },
            apply=True,
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["applied"])
        self.assertEqual(result["reallocated_instances"], [])
        self.assertTrue(result["restart_requested"])
        self.assertEqual(result["restart_reason"], "running_no_hash_without_visible_instances")
        self.assertEqual(
            watch.request_calls,
            [("POST", "/organizations/kray/projects/default/containers/prl-kray-roi-10/stop")],
        )
        self.assertEqual(
            watch.start_calls,
            [("prl-kray-roi-10", "running_no_hash_same_profile:running_no_hash_without_visible_instances")],
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

    def test_org_worker_waits_before_patching_fresh_running_no_hash_mismatch(self) -> None:
        class Watch:
            def slot_state(self, _slot_name):
                return (
                    {
                        "priority": "low",
                        "container": {"resources": {"gpu_classes": ["gpu-rtx-4070tis"], "memory": 2048}},
                        "current_state": {"instance_status_counts": {"running_count": 1}},
                    },
                    [{"id": "running-1", "ready": True, "started": True}],
                )

            GPU = {"4070tis": "gpu-rtx-4070tis"}

        plan = org_worker.planned_action(
            Watch(),
            "prl-kray-roi-01",
            {
                "profile_key": "5090:low:2048",
                "observed_profile_since_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                "live_worker_count": 0,
                "live_worker_th": 0,
            },
            protect_running=True,
            pending_retarget_after_seconds=60,
        )

        self.assertEqual(plan["action"], "observe")
        self.assertIn("running_no_hash_profile_mismatch_wait", plan["reason"])
        self.assertFalse(plan["protected"])

    def test_org_worker_patches_stale_running_no_hash_mismatch(self) -> None:
        class Watch:
            def slot_state(self, _slot_name):
                return (
                    {
                        "priority": "low",
                        "container": {"resources": {"gpu_classes": ["gpu-rtx-4070tis"], "memory": 2048}},
                        "current_state": {"instance_status_counts": {"running_count": 1}},
                    },
                    [{"id": "running-1", "ready": True, "started": True}],
                )

            GPU = {"4070tis": "gpu-rtx-4070tis"}

        plan = org_worker.planned_action(
            Watch(),
            "prl-kray-roi-01",
            {
                "profile_key": "5090:low:2048",
                "observed_profile_since_utc": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(
                    timespec="seconds"
                ),
                "live_worker_count": 0,
                "live_worker_th": 0,
            },
            protect_running=True,
            pending_retarget_after_seconds=60,
        )

        self.assertEqual(plan["action"], "patch")
        self.assertIn("stale_running_no_hash_profile_mismatch", plan["reason"])
        self.assertFalse(plan["protected"])

    def test_org_worker_waits_before_restarting_fresh_running_no_hash_same_profile(self) -> None:
        class Watch:
            def slot_state(self, _slot_name):
                return (
                    {
                        "priority": "batch",
                        "container": {"resources": {"gpu_classes": ["gpu-rtx-3090"], "memory": 2048}},
                        "current_state": {"instance_status_counts": {"running_count": 1}},
                    },
                    [{"id": "running-1", "ready": True, "started": True}],
                )

            GPU = {"3090": "gpu-rtx-3090"}

        plan = org_worker.planned_action(
            Watch(),
            "prl-kray-roi-10",
            {
                "profile_key": "3090:batch:2048",
                "observed_profile_since_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                "live_worker_count": 0,
                "live_worker_th": 0,
            },
            protect_running=True,
            pending_retarget_after_seconds=60,
        )

        self.assertEqual(plan["action"], "observe")
        self.assertIn("running_no_hash_same_profile_wait", plan["reason"])
        self.assertFalse(plan["protected"])

    def test_org_worker_restarts_stale_running_no_hash_same_profile(self) -> None:
        class Watch:
            def slot_state(self, _slot_name):
                return (
                    {
                        "priority": "batch",
                        "container": {"resources": {"gpu_classes": ["gpu-rtx-3090"], "memory": 2048}},
                        "current_state": {"instance_status_counts": {"running_count": 1}},
                    },
                    [{"id": "running-1", "ready": True, "started": True}],
                )

            GPU = {"3090": "gpu-rtx-3090"}

        plan = org_worker.planned_action(
            Watch(),
            "prl-kray-roi-10",
            {
                "profile_key": "3090:batch:2048",
                "observed_profile_since_utc": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(
                    timespec="seconds"
                ),
                "live_worker_count": 0,
                "live_worker_th": 0,
            },
            protect_running=True,
            pending_retarget_after_seconds=60,
        )

        self.assertEqual(plan["action"], "restart_no_hash")
        self.assertIn("stale_running_no_hash_same_profile", plan["reason"])
        self.assertEqual(plan["running_instance_ids"], ["running-1"])
        self.assertFalse(plan["protected"])

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

    def test_scheduler_preserves_fresh_profitable_pending_slot_target(self) -> None:
        config = load_config()
        original = os.environ.get("PRL_PENDING_TARGET_PROTECT_SECONDS")
        os.environ["PRL_PENDING_TARGET_PROTECT_SECONDS"] = "300"
        try:
            with state_db.connect(self.db_path) as conn:
                state_db.init_db(conn)
                state_db.sync_config(conn, config)
                state_db.update_slot_observation(
                    conn,
                    {
                        "org_label": "kray",
                        "slot_name": "prl-kray-roi-01",
                        "observed_profile_key": "4090:batch:2048",
                        "observed_status": "allocating",
                        "updated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                    },
                )
                conn.commit()
            fleet_scheduler.schedule_once(db_path=self.db_path, price=0.64, fee=0.01, dry_run=False)
        finally:
            if original is None:
                os.environ.pop("PRL_PENDING_TARGET_PROTECT_SECONDS", None)
            else:
                os.environ["PRL_PENDING_TARGET_PROTECT_SECONDS"] = original

        with state_db.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT profile_key, protected, reason
                FROM slot_targets
                WHERE org_label = 'kray' AND slot_name = 'prl-kray-roi-01'
                """
            ).fetchone()

        self.assertEqual(row["profile_key"], "4090:batch:2048")
        self.assertEqual(row["protected"], 0)
        self.assertIn("protected_pending_observed_profile", row["reason"])

    def test_scheduler_default_pending_target_protection_is_120_seconds(self) -> None:
        config = load_config()
        original = os.environ.get("PRL_PENDING_TARGET_PROTECT_SECONDS")
        os.environ.pop("PRL_PENDING_TARGET_PROTECT_SECONDS", None)
        try:
            with state_db.connect(self.db_path) as conn:
                state_db.init_db(conn)
                state_db.sync_config(conn, config)
                observed_at = (datetime.now(UTC) - timedelta(seconds=130)).isoformat(timespec="seconds")
                state_db.update_slot_observation(
                    conn,
                    {
                        "org_label": "kray",
                        "slot_name": "prl-kray-roi-01",
                        "observed_profile_key": "3090:batch:2048",
                        "observed_status": "allocating",
                        "updated_at_utc": observed_at,
                    },
                )
                conn.commit()
            fleet_scheduler.schedule_once(db_path=self.db_path, price=0.64, fee=0.01, dry_run=False)
        finally:
            if original is None:
                os.environ.pop("PRL_PENDING_TARGET_PROTECT_SECONDS", None)
            else:
                os.environ["PRL_PENDING_TARGET_PROTECT_SECONDS"] = original

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

    def test_scheduler_keeps_profitable_stale_pending_when_replacement_is_weaker(self) -> None:
        config = load_config()
        observed_at = (datetime.now(UTC) - timedelta(minutes=5)).isoformat(timespec="seconds")
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, config)
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "observed_profile_key": "5090:batch:2048",
                    "observed_status": "allocating",
                    "updated_at_utc": observed_at,
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

        self.assertEqual(row["profile_key"], "5090:batch:2048")
        self.assertEqual(row["protected"], 0)
        self.assertIn("pending_observed_profile_recycle_first", row["reason"])

    def test_scheduler_can_prioritize_fill_over_recycling_profitable_stale_pending(self) -> None:
        config = load_config()
        original = os.environ.get("PRL_FILL_RECYCLE_CURRENT_PENDING_FIRST")
        os.environ["PRL_FILL_RECYCLE_CURRENT_PENDING_FIRST"] = "0"
        try:
            observed_at = (datetime.now(UTC) - timedelta(minutes=5)).isoformat(timespec="seconds")
            with state_db.connect(self.db_path) as conn:
                state_db.init_db(conn)
                state_db.sync_config(conn, config)
                state_db.update_slot_observation(
                    conn,
                    {
                        "org_label": "kray",
                        "slot_name": "prl-kray-roi-01",
                        "observed_profile_key": "5090:batch:2048",
                        "observed_status": "allocating",
                        "updated_at_utc": observed_at,
                    },
                )
                conn.commit()

            fleet_scheduler.schedule_once(db_path=self.db_path, price=0.64, fee=0.01, dry_run=False)
        finally:
            if original is None:
                os.environ.pop("PRL_FILL_RECYCLE_CURRENT_PENDING_FIRST", None)
            else:
                os.environ["PRL_FILL_RECYCLE_CURRENT_PENDING_FIRST"] = original

        with state_db.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT profile_key, protected, reason
                FROM slot_targets
                WHERE org_label = 'kray' AND slot_name = 'prl-kray-roi-01'
                """
            ).fetchone()

        self.assertNotEqual(row["profile_key"], "5090:batch:2048")
        self.assertEqual(row["protected"], 0)
        self.assertIn("replace_nohash_observed_profile", row["reason"])

    def test_scheduler_uses_fresh_existing_target_age_for_pending_protection(self) -> None:
        config = load_config()
        original = os.environ.get("PRL_PENDING_TARGET_PROTECT_SECONDS")
        os.environ.pop("PRL_PENDING_TARGET_PROTECT_SECONDS", None)
        try:
            now = datetime.now(UTC)
            observed_at = (now - timedelta(seconds=130)).isoformat(timespec="seconds")
            assigned_at = now.isoformat(timespec="seconds")
            with state_db.connect(self.db_path) as conn:
                state_db.init_db(conn)
                state_db.sync_config(conn, config)
                state_db.update_slot_observation(
                    conn,
                    {
                        "org_label": "kray",
                        "slot_name": "prl-kray-roi-01",
                        "observed_profile_key": "4090:batch:2048",
                        "observed_status": "allocating",
                        "updated_at_utc": observed_at,
                    },
                )
                state_db.set_slot_target(
                    conn,
                    {
                        "org_label": "kray",
                        "slot_name": "prl-kray-roi-01",
                        "profile_key": "4090:batch:2048",
                        "mode": "base_fill",
                        "decision_price_usd": 0.64,
                        "expected_profit_day": 1.0,
                        "protected": False,
                        "reason": "previous_assignment",
                        "assigned_at_utc": assigned_at,
                    },
                )
                conn.commit()
            fleet_scheduler.schedule_once(db_path=self.db_path, price=0.64, fee=0.01, dry_run=False)
        finally:
            if original is None:
                os.environ.pop("PRL_PENDING_TARGET_PROTECT_SECONDS", None)
            else:
                os.environ["PRL_PENDING_TARGET_PROTECT_SECONDS"] = original

        with state_db.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT profile_key, protected, reason
                FROM slot_targets
                WHERE org_label = 'kray' AND slot_name = 'prl-kray-roi-01'
                """
            ).fetchone()

        self.assertEqual(row["profile_key"], "4090:batch:2048")
        self.assertEqual(row["protected"], 0)
        self.assertIn("protected_pending_observed_profile", row["reason"])
        self.assertIn("lt_120", row["reason"])

    def test_scheduler_preserves_assigned_at_for_unchanged_profile_target(self) -> None:
        config = load_config()
        old_assigned_at = (datetime.now(UTC) - timedelta(minutes=10)).isoformat(timespec="seconds")
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, config)
            state_db.set_slot_target(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "profile_key": "4090:batch:2048",
                    "mode": "base_fill",
                    "decision_price_usd": 0.64,
                    "expected_profit_day": 1.0,
                    "protected": False,
                    "reason": "previous_assignment",
                    "assigned_at_utc": old_assigned_at,
                },
            )
            conn.commit()

        fleet_scheduler.schedule_once(db_path=self.db_path, price=0.64, fee=0.01, dry_run=False)

        with state_db.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT profile_key, assigned_at_utc
                FROM slot_targets
                WHERE org_label = 'kray' AND slot_name = 'prl-kray-roi-01'
                """
            ).fetchone()

        self.assertEqual(row["profile_key"], "4090:batch:2048")
        self.assertEqual(row["assigned_at_utc"], old_assigned_at)

    def test_scheduler_refreshes_assigned_at_when_profile_changes(self) -> None:
        config = load_config()
        old_assigned_at = "2026-01-01T00:00:00+00:00"
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, config)
            state_db.update_slot_observation(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "observed_profile_key": "4090:batch:2048",
                    "observed_status": "allocating",
                    "updated_at_utc": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(timespec="seconds"),
                },
            )
            state_db.record_search_state(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "profile_key": "4090:batch:2048",
                    "no_gpu_since_utc": old_assigned_at,
                    "sleep_until_utc": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(timespec="seconds"),
                    "attempts": 1,
                    "reason": "stale_pending_same_profile",
                },
            )
            state_db.set_slot_target(
                conn,
                {
                    "org_label": "kray",
                    "slot_name": "prl-kray-roi-01",
                    "profile_key": "4090:batch:2048",
                    "mode": "base_fill",
                    "decision_price_usd": 0.64,
                    "expected_profit_day": 1.0,
                    "protected": False,
                    "reason": "previous_assignment",
                    "assigned_at_utc": old_assigned_at,
                },
            )
            conn.commit()

        fleet_scheduler.schedule_once(db_path=self.db_path, price=0.64, fee=0.01, dry_run=False)

        with state_db.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT profile_key, assigned_at_utc
                FROM slot_targets
                WHERE org_label = 'kray' AND slot_name = 'prl-kray-roi-01'
                """
            ).fetchone()

        self.assertNotEqual(row["profile_key"], "4090:batch:2048")
        self.assertNotEqual(row["assigned_at_utc"], old_assigned_at)

    def test_scheduler_retargets_fresh_pending_profile_under_cooldown(self) -> None:
        config = load_config()
        original = os.environ.get("PRL_PENDING_TARGET_PROTECT_SECONDS")
        os.environ["PRL_PENDING_TARGET_PROTECT_SECONDS"] = "300"
        try:
            now = datetime.now(UTC)
            with state_db.connect(self.db_path) as conn:
                state_db.init_db(conn)
                state_db.sync_config(conn, config)
                state_db.update_slot_observation(
                    conn,
                    {
                        "org_label": "kray",
                        "slot_name": "prl-kray-roi-01",
                        "observed_profile_key": "4090:batch:2048",
                        "observed_status": "allocating",
                        "updated_at_utc": now.isoformat(timespec="seconds"),
                    },
                )
                state_db.record_search_state(
                    conn,
                    {
                        "org_label": "kray",
                        "slot_name": "prl-kray-roi-01",
                        "profile_key": "4090:batch:2048",
                        "no_gpu_since_utc": now.isoformat(timespec="seconds"),
                        "sleep_until_utc": (now + timedelta(minutes=10)).isoformat(timespec="seconds"),
                        "attempts": 1,
                        "reason": "stale_pending_same_profile:4090:batch:2048:age_60.0",
                    },
                )
                conn.commit()
            fleet_scheduler.schedule_once(db_path=self.db_path, price=0.64, fee=0.01, dry_run=False)
        finally:
            if original is None:
                os.environ.pop("PRL_PENDING_TARGET_PROTECT_SECONDS", None)
            else:
                os.environ["PRL_PENDING_TARGET_PROTECT_SECONDS"] = original

        with state_db.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT profile_key, protected, reason
                FROM slot_targets
                WHERE org_label = 'kray' AND slot_name = 'prl-kray-roi-01'
                """
            ).fetchone()

        self.assertNotEqual(row["profile_key"], "4090:batch:2048")
        self.assertEqual(row["protected"], 0)
        self.assertIn("replace_nohash_observed_profile", row["reason"])

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

    def test_scheduler_respects_availability_with_long_probe_stale_window(self) -> None:
        config = load_config()
        original = os.environ.get("PRL_AVAILABILITY_STALE_AFTER_SECONDS")
        os.environ["PRL_AVAILABILITY_STALE_AFTER_SECONDS"] = "1800"
        try:
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
                        "checked_at_utc": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(
                            timespec="seconds"
                        ),
                    },
                )
                conn.commit()
            payload = fleet_scheduler.schedule_once(db_path=self.db_path, price=0.64, fee=0.01, dry_run=False)
        finally:
            if original is None:
                os.environ.pop("PRL_AVAILABILITY_STALE_AFTER_SECONDS", None)
            else:
                os.environ["PRL_AVAILABILITY_STALE_AFTER_SECONDS"] = original

        kray_4090 = [
            target
            for target in payload["targets"]
            if target["org_label"] == "kray" and target["profile_key"] == "4090:batch:2048"
        ]
        self.assertEqual(kray_4090, [])

    def test_scheduler_prefers_reported_availability_outside_top_width_before_probe_fallback(self) -> None:
        config = config_loader.FleetConfig(
            organizations=(
                config_loader.OrgConfig(
                    label="kray",
                    slug="kray",
                    api_key_env="SALAD_API_KEY_TEST",
                    slot_prefix="prl-kray-roi",
                    slots=1,
                ),
            )
        )
        scores = [
            {
                "profile_key": "top1:batch:2048",
                "gpu_key": "top1",
                "priority": "batch",
                "memory_mb": 2048,
                "expected_profit_day": 1.0,
                "score": 100.0,
                "eligible": True,
            },
            {
                "profile_key": "top2:batch:2048",
                "gpu_key": "top2",
                "priority": "batch",
                "memory_mb": 2048,
                "expected_profit_day": 0.9,
                "score": 90.0,
                "eligible": True,
            },
            {
                "profile_key": "available:batch:2048",
                "gpu_key": "available",
                "priority": "batch",
                "memory_mb": 2048,
                "expected_profit_day": 0.2,
                "score": 10.0,
                "eligible": True,
            },
        ]

        targets = fleet_scheduler.build_targets(
            config,
            scores,
            mode="base_fill",
            decision_price_usd=0.64,
            width=2,
            availability={
                "kray": {
                    "top1:batch:2048": {"ok": True, "available_count": 0},
                    "top2:batch:2048": {"ok": True, "available_count": 0},
                    "available:batch:2048": {"ok": True, "available_count": 1},
                }
            },
        )

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["profile_key"], "available:batch:2048")
        self.assertNotIn("availability_probe_fallback", targets[0]["reason"])

    def test_scheduler_prefers_best_reported_available_profile_before_diversifying(self) -> None:
        config = config_loader.FleetConfig(
            organizations=(
                config_loader.OrgConfig(
                    label="kray",
                    slug="kray",
                    api_key_env="SALAD_API_KEY_TEST",
                    slot_prefix="prl-kray-roi",
                    slots=2,
                ),
            )
        )
        scores = [
            {
                "profile_key": "best:low:2048",
                "gpu_key": "best",
                "priority": "low",
                "memory_mb": 2048,
                "expected_profit_day": 2.0,
                "score": 200.0,
                "eligible": True,
            },
            {
                "profile_key": "second:low:2048",
                "gpu_key": "second",
                "priority": "low",
                "memory_mb": 2048,
                "expected_profit_day": 1.0,
                "score": 100.0,
                "eligible": True,
            },
        ]
        previous = os.environ.get("PRL_FILL_PREFER_REPORTED_AVAILABLE_SCORE_ORDER")
        os.environ["PRL_FILL_PREFER_REPORTED_AVAILABLE_SCORE_ORDER"] = "1"
        try:
            targets = fleet_scheduler.build_targets(
                config,
                scores,
                mode="base_fill",
                decision_price_usd=0.64,
                width=2,
                availability={
                    "kray": {
                        "best:low:2048": {"ok": True, "available_count": 2},
                        "second:low:2048": {"ok": True, "available_count": 2},
                    }
                },
            )
        finally:
            if previous is None:
                os.environ.pop("PRL_FILL_PREFER_REPORTED_AVAILABLE_SCORE_ORDER", None)
            else:
                os.environ["PRL_FILL_PREFER_REPORTED_AVAILABLE_SCORE_ORDER"] = previous

        self.assertEqual([target["profile_key"] for target in targets], ["best:low:2048", "best:low:2048"])

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
