from __future__ import annotations

import pathlib
import tempfile
import time
import sys
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import salad_prl_profit_snapshot


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

    def test_effective_state_age_prefers_recent_slot_action(self) -> None:
        old_path = salad_prl_profit_snapshot.SLOT_ACTION_STATE_PATH
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                salad_prl_profit_snapshot.SLOT_ACTION_STATE_PATH = pathlib.Path(temp_dir) / "prl_slot_actions.json"
                detail_dir = salad_prl_profit_snapshot.SLOT_ACTION_STATE_PATH.parent / "prl_slot_actions.d"
                detail_dir.mkdir()
                detail_path = salad_prl_profit_snapshot.slot_action_detail_path("cantemir1", "prl-cantemir1-roi-01")
                detail_path.write_text(
                    '{"action":"patched","reason":"test","candidate":"RTX 4090 batch","at_ts":'
                    + str(time.time() - 60)
                    + "}",
                    encoding="utf-8",
                )

                age, action = salad_prl_profit_snapshot.effective_state_age_seconds(
                    "cantemir1", "prl-cantemir1-roi-01", 7200.0
                )
        finally:
            salad_prl_profit_snapshot.SLOT_ACTION_STATE_PATH = old_path

        self.assertIsNotNone(action)
        self.assertLess(age or 0, 120.0)

    def test_effective_state_age_keeps_observed_age_without_recent_action(self) -> None:
        old_path = salad_prl_profit_snapshot.SLOT_ACTION_STATE_PATH
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                salad_prl_profit_snapshot.SLOT_ACTION_STATE_PATH = pathlib.Path(temp_dir) / "missing.json"
                age, action = salad_prl_profit_snapshot.effective_state_age_seconds(
                    "cantemir1", "prl-cantemir1-roi-01", 7200.0
                )
        finally:
            salad_prl_profit_snapshot.SLOT_ACTION_STATE_PATH = old_path

        self.assertIsNone(action)
        self.assertEqual(age, 7200.0)


if __name__ == "__main__":
    unittest.main()
