from __future__ import annotations

import pathlib
import json
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


if __name__ == "__main__":
    unittest.main()
