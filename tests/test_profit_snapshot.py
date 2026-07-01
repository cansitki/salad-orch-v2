from __future__ import annotations

import pathlib
import json
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import salad_prl_profit_snapshot
import state_db
from config_loader import load_config


class ProfitSnapshotTest(unittest.TestCase):
    def test_zero_fixed_price_env_falls_back_to_live_market_price(self) -> None:
        env = {
            "PRL_SNAPSHOT_PRICE_USD": "0",
            "PRL_FIXED_DECISION_PRICE_USD": "0",
            "PRL_WATCH_FIXED_DECISION_PRICE_USD": "0",
            "PRL_FILL_FIXED_DECISION_PRICE_USD": "0",
            "PRL_NOHASH_FALLBACK_PRICE": "0",
        }
        with patch.dict("os.environ", env, clear=False):
            with patch.object(salad_prl_profit_snapshot, "market_prl_price_usd", return_value=0.66):
                self.assertEqual(salad_prl_profit_snapshot.default_snapshot_price(), 0.66)

    def test_positive_snapshot_price_env_wins_over_market_price(self) -> None:
        with patch.dict("os.environ", {"PRL_SNAPSHOT_PRICE_USD": "0.70"}, clear=False):
            with patch.object(salad_prl_profit_snapshot, "market_prl_price_usd", return_value=0.66):
                self.assertEqual(salad_prl_profit_snapshot.default_snapshot_price(), 0.70)

    def test_configured_accounts_preserves_known_org_api_keys_with_fleet_filter(self) -> None:
        with patch.dict("os.environ", {"PRL_FLEET_ORGS": "kray,kray2,kray3"}, clear=True):
            accounts = salad_prl_profit_snapshot.configured_accounts()
        self.assertEqual([row[0] for row in accounts], ["kray", "kray2", "kray3"])
        self.assertEqual({row[2] for row in accounts}, {"SALAD_API_KEY_2"})
        kray2_slots = accounts[1][3]
        self.assertIn("prl-kray2-roi-05b", kray2_slots)
        self.assertNotIn("prl-kray2-roi-05", kray2_slots)

    def test_configured_accounts_uses_extra_org_key_env_for_unknown_fleet_org(self) -> None:
        extra = [
            {
                "label": "kry2",
                "slug": "kry2",
                "api_key_env": "SALAD_API_KEY_KRY1",
                "slot_prefix": "prl-kry2-roi",
                "slots": 10,
            }
        ]
        with patch.dict(
            "os.environ",
            {
                "PRL_FLEET_ORGS": "kry2",
                "PRL_FLEET_EXTRA_ORGS_JSON": json.dumps(extra),
                "PRL_WATCH_DEFAULT_API_KEY_ENV": "SALAD_API_KEY_2",
            },
            clear=True,
        ):
            accounts = salad_prl_profit_snapshot.configured_accounts()

        self.assertEqual(
            accounts,
            [
                (
                    "kry2",
                    "kry2",
                    "SALAD_API_KEY_KRY1",
                    [f"prl-kry2-roi-{index:02d}" for index in range(1, 11)],
                )
            ],
        )

    def test_pool_prl_per_th_day_applies_reward_calibration(self) -> None:
        payloads = {
            "https://pearlfortune.org/api/v1/stats/pool-fee-rate": {"data": {"pool_fee_rate": 0.01}},
            "https://pearlfortune.org/api/v1/summary?hours=24": {
                "data": {
                    "pool_stats": {
                        "hourly_stats": [
                            {"pool_reward": 100, "pool_hashrate": 10e12},
                            {"pool_reward": 50, "pool_hashrate": 5e12},
                        ]
                    }
                }
            },
        }
        old_factor = salad_prl_profit_snapshot.REWARD_CALIBRATION_FACTOR
        try:
            salad_prl_profit_snapshot.REWARD_CALIBRATION_FACTOR = 0.92
            with patch.object(salad_prl_profit_snapshot, "external_json", side_effect=lambda url: payloads[url]):
                value, points, fee = salad_prl_profit_snapshot.pool_prl_per_th_day()
        finally:
            salad_prl_profit_snapshot.REWARD_CALIBRATION_FACTOR = old_factor
        self.assertEqual(points, 2)
        self.assertEqual(fee, 0.01)
        self.assertAlmostEqual(value, 20 * 0.99 * 0.92)

    def test_write_snapshot_db_persists_live_workers_and_profit_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(pathlib.Path(tmpdir) / "fleet.db")
            with state_db.connect(db_path) as conn:
                state_db.init_db(conn)
                state_db.sync_config(conn, load_config())
                conn.commit()

            snapshot = {
                "at_utc": "2026-07-01T12:00:00+00:00",
                "assumed_prl_price": 0.55,
                "live_market_prl_price": 0.47,
                "fresh_workers": 1,
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
                        "revenue_day": 3.0,
                        "profit_day": 0.84,
                        "last_stats_at": "2026-07-01T11:59:00+00:00",
                    }
                ],
            }

            salad_prl_profit_snapshot.write_snapshot_db(snapshot, db_path=db_path, decision_price=0.55)

            with state_db.connect(db_path) as conn:
                slot = conn.execute(
                    """
                    SELECT observed_status, observed_profile_key, live_hashrate_th, protected
                    FROM slots
                    WHERE org_label = 'kray' AND slot_name = 'prl-kray-roi-01'
                    """
                ).fetchone()
                worker = conn.execute("SELECT * FROM workers").fetchone()
                rows = conn.execute(
                    """
                    SELECT scope, org_label, slot_name, profile_key, th, revenue_day
                    FROM profit_snapshots
                    ORDER BY id
                    """
                ).fetchall()

            self.assertEqual(slot["observed_status"], "running")
            self.assertEqual(slot["observed_profile_key"], "3090:batch:2048")
            self.assertEqual(slot["live_hashrate_th"], 111.5)
            self.assertEqual(slot["protected"], 1)
            self.assertEqual(worker["worker_name"], "kray-prl-roi-01-pearlfortune-inst-1")
            self.assertEqual(worker["reported_hashrate_th"], 111.5)
            self.assertEqual(worker["stale"], 0)
            self.assertEqual([row["scope"] for row in rows], ["fleet", "slot"])
            self.assertEqual(rows[1]["profile_key"], "3090:batch:2048")
            self.assertEqual(rows[1]["revenue_day"], 3.0)

    def test_build_snapshot_falls_back_to_static_prices_when_catalog_fails(self) -> None:
        group = {
            "priority": "batch",
            "container": {"resources": {"gpu_classes": [salad_prl_profit_snapshot.GPU_IDS["3090"]]}},
            "current_state": {
                "status": "running",
                "start_time": "2026-07-01T11:50:00+00:00",
                "instance_status_counts": {"running_count": 1},
            },
        }

        def fake_salad_json(path: str, _api_key: str) -> dict:
            if path.endswith("/instances"):
                return {"items": [{"id": "inst-1"}]}
            return group

        with (
            patch.object(
                salad_prl_profit_snapshot,
                "configured_accounts",
                return_value=[("kray", "kray", "SALAD_API_KEY", ["prl-kray-roi-01"])],
            ),
            patch.dict("os.environ", {"SALAD_API_KEY": "test", "PRL_WALLET": "prl-test"}, clear=False),
            patch.object(salad_prl_profit_snapshot, "price_catalog", side_effect=RuntimeError("catalog down")),
            patch.object(salad_prl_profit_snapshot, "salad_json", side_effect=fake_salad_json),
            patch.object(salad_prl_profit_snapshot, "pool_prl_per_th_day", return_value=(0.04, 24, 0.01)),
            patch.object(salad_prl_profit_snapshot, "market_prl_price_usd", return_value=0.47),
            patch.object(
                salad_prl_profit_snapshot,
                "parse_workers",
                return_value=[
                    {
                        "worker": "prl-kray-roi-01-pearlfortune-inst-1",
                        "slot": None,
                        "named_slot": None,
                        "gpu": "RTX 3090",
                        "gpu_id": salad_prl_profit_snapshot.GPU_IDS["3090"],
                        "gpu_token": "3090",
                        "th": 100.0,
                        "stale": False,
                        "last_stats_at": "2026-07-01T11:59:00+00:00",
                    }
                ],
            ),
        ):
            snapshot = salad_prl_profit_snapshot.build_snapshot(0.55)

        self.assertEqual(snapshot["catalog_errors"], [{"org": "kray", "error_type": "RuntimeError"}])
        self.assertEqual(snapshot["fresh_workers"], 1)
        self.assertEqual(snapshot["totals"]["cost_day"], 2.16)
        self.assertEqual(snapshot["slots"][0]["slot"], "prl-kray-roi-01")


if __name__ == "__main__":
    unittest.main()
