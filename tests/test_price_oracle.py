from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import price_oracle
import state_db
from config_loader import load_config


class PriceOracleTest(unittest.TestCase):
    def test_pool_summary_sums_hourly_rewards_into_daily_rate(self) -> None:
        payload = {
            "data": {
                "pool_stats": {
                    "hourly_stats": [
                        {"pool_reward": 100, "pool_hashrate": 10e12},
                        {"pool_reward": 50, "pool_hashrate": 5e12},
                    ]
                }
            }
        }
        with patch.object(price_oracle, "external_json", return_value=payload):
            value, points = price_oracle.fetch_gross_prl_per_th_day(hours=24)
        self.assertEqual(points, 2)
        self.assertEqual(value, 20)

    def test_sample_price_applies_reward_calibration_to_gross_prl(self) -> None:
        def fake_external_json(url: str):
            if "market/price" in url:
                return {"data": {"price_usd": 0.66}}
            if "safe.trade" in url:
                return {"ticker": {"last": 0.65, "buy": 0.64, "sell": 0.66}}
            if "pool-fee-rate" in url:
                return {"data": {"pool_fee_rate": 0.01}}
            return {
                "data": {
                    "pool_stats": {
                        "hourly_stats": [
                            {"pool_reward": 100, "pool_hashrate": 10e12},
                            {"pool_reward": 50, "pool_hashrate": 5e12},
                        ]
                    }
                }
            }

        with patch.dict("os.environ", {"PRL_REWARD_CALIBRATION_FACTOR": "0.92"}, clear=False):
            with patch.object(price_oracle, "external_json", side_effect=fake_external_json):
                sample = price_oracle.sample_price(0.01)
        self.assertEqual(sample["raw_gross_prl_per_th_day"], 20)
        self.assertAlmostEqual(sample["gross_prl_per_th_day"], 18.4)
        self.assertEqual(sample["reward_calibration_factor"], 0.92)

    def test_one_high_price_sample_does_not_enable_boost(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(pathlib.Path(tmpdir) / "oracle.db")
            with state_db.connect(db_path) as conn:
                state_db.init_db(conn)
                sample = {
                    "sampled_at_utc": "2026-06-24T11:00:00+00:00",
                    "selected_price_usd": 0.71,
                    "configured_pearl_fee_rate": 0.01,
                }
                state_db.insert_price_sample(conn, sample)
                with patch.dict("os.environ", {"PRL_FILL_FIXED_DECISION_PRICE_USD": "0.64"}, clear=False):
                    risk = price_oracle.compute_risk_mode(conn, sample, load_config())
        self.assertEqual(risk["mode"], "base_fill")
        self.assertEqual(risk["decision_price_usd"], 0.64)

    def test_low_price_sample_caps_base_decision_price(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(pathlib.Path(tmpdir) / "oracle.db")
            with state_db.connect(db_path) as conn:
                state_db.init_db(conn)
                sample = {
                    "sampled_at_utc": "2026-06-24T11:00:00+00:00",
                    "selected_price_usd": 0.41,
                    "configured_pearl_fee_rate": 0.01,
                }
                state_db.insert_price_sample(conn, sample)
                with patch.dict("os.environ", {"PRL_FILL_FIXED_DECISION_PRICE_USD": "0.64"}, clear=False):
                    risk = price_oracle.compute_risk_mode(conn, sample, load_config())
        self.assertEqual(risk["decision_price_usd"], 0.41)


if __name__ == "__main__":
    unittest.main()
