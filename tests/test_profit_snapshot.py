from __future__ import annotations

import pathlib
import tempfile
import time
import sys
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import salad_prl_profit_snapshot
import salad_prl_guard
import salad_prl_watch


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

    def test_wallet_observed_rewards_compares_realized_to_model(self) -> None:
        def fake_external_json(url: str):
            if "hourly-shares" in url:
                return {
                    "data": {
                        "credited_amount_by_window_atomic": {"h24": 3000000000},
                        "rolling_hashrates": [{"hours": 24, "hashrate": 2_000_000_000_000_000}],
                    }
                }
            return {
                "data": {
                    "pending_shares": {
                        "pending_estimate_by_window_atomic": {"h24": 1000000000},
                    }
                }
            }

        with patch.object(salad_prl_profit_snapshot, "external_json", side_effect=fake_external_json):
            observed = salad_prl_profit_snapshot.wallet_observed_rewards(0.025)

        self.assertIsNotNone(observed)
        assert observed is not None
        self.assertEqual(observed["credited_prl_24h"], 30)
        self.assertEqual(observed["pending_prl_24h"], 10)
        self.assertEqual(observed["total_prl_24h"], 40)
        self.assertEqual(observed["rolling_hashrate_th_24h"], 2000)
        self.assertEqual(observed["expected_prl_24h_at_rolling_hashrate"], 50)
        self.assertEqual(observed["observed_to_model_ratio_24h"], 0.8)

    def test_effective_prl_per_th_day_prefers_observed_wallet_yield(self) -> None:
        observed = {
            "observed_prl_per_th_day_24h": 0.02,
            "rolling_hashrate_th_24h": 2000,
        }

        with patch.dict("os.environ", {"PRL_USE_WALLET_OBSERVED_YIELD": "1"}, clear=False):
            value, source, fallback_reason = salad_prl_profit_snapshot.effective_prl_per_th_day(
                0.03,
                observed,
            )

        self.assertEqual(value, 0.02)
        self.assertEqual(source, "wallet_observed_24h")
        self.assertIsNone(fallback_reason)

    def test_effective_prl_per_th_day_falls_back_when_observed_hashrate_is_low(self) -> None:
        observed = {
            "observed_prl_per_th_day_24h": 0.02,
            "rolling_hashrate_th_24h": 10,
        }

        with patch.dict("os.environ", {"PRL_USE_WALLET_OBSERVED_YIELD": "1"}, clear=False):
            value, source, fallback_reason = salad_prl_profit_snapshot.effective_prl_per_th_day(
                0.03,
                observed,
            )

        self.assertEqual(value, 0.03)
        self.assertEqual(source, "pool_model")
        self.assertEqual(fallback_reason, "wallet_observed_low_hashrate")

    def test_estimated_cost_usd_from_snapshot_csv_integrates_24h_window(self) -> None:
        snapshot_at = datetime(2026, 6, 25, 12, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "snapshots.csv"
            path.write_text(
                "\n".join(
                    [
                        "at_utc,total_cost_day",
                        (snapshot_at - timedelta(hours=24)).isoformat() + ",24",
                        (snapshot_at - timedelta(hours=12)).isoformat() + ",48",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            result = salad_prl_profit_snapshot.estimated_cost_usd_from_snapshot_csv(
                path,
                snapshot_at,
                72,
            )

        self.assertEqual(result["coverage_hours"], 24)
        self.assertEqual(result["coverage_ratio"], 1)
        self.assertEqual(result["sample_count"], 3)
        self.assertEqual(result["estimated_cost_usd"], 48)

    def test_wallet_observed_economics_reports_realized_profit_and_break_even(self) -> None:
        snapshot_at = datetime(2026, 6, 25, 12, tzinfo=UTC)
        observed = {"total_prl_24h": 100}
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "snapshots.csv"
            path.write_text(
                "\n".join(
                    [
                        "at_utc,total_cost_day",
                        (snapshot_at - timedelta(hours=24)).isoformat() + ",24",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with patch.object(salad_prl_profit_snapshot, "snapshot_csv_path", return_value=path):
                result = salad_prl_profit_snapshot.wallet_observed_economics_24h(
                    observed,
                    snapshot_at=snapshot_at,
                    current_cost_day=24,
                    assumed_price=0.6,
                    market_price=0.7,
                )

        self.assertEqual(result["realized_prl_24h"], 100)
        self.assertEqual(result["estimated_cost_usd"], 24)
        self.assertEqual(result["profit_usd_at_assumed_price"], 36)
        self.assertEqual(result["profit_usd_at_market_price"], 46)
        self.assertEqual(result["break_even_price_usd"], 0.24)

    def test_wallet_observed_economics_skips_break_even_with_partial_cost_coverage(self) -> None:
        snapshot_at = datetime(2026, 6, 25, 12, tzinfo=UTC)
        observed = {"total_prl_24h": 100}
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "snapshots.csv"
            path.write_text(
                "\n".join(
                    [
                        "at_utc,total_cost_day",
                        (snapshot_at - timedelta(hours=1)).isoformat() + ",24",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with patch.object(salad_prl_profit_snapshot, "snapshot_csv_path", return_value=path):
                result = salad_prl_profit_snapshot.wallet_observed_economics_24h(
                    observed,
                    snapshot_at=snapshot_at,
                    current_cost_day=24,
                    assumed_price=0.6,
                    market_price=0.7,
                )

        self.assertEqual(result["coverage_hours"], 1)
        self.assertEqual(result["error"], "cost_coverage_incomplete")
        self.assertNotIn("break_even_price_usd", result)

    def test_append_snapshot_csv_rotates_mismatched_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "snapshots.csv"
            old_header = [field for field in salad_prl_profit_snapshot.CSV_FIELDS if not field.startswith("wallet_realized")]
            old_contents = ",".join(old_header) + "\n" + ",".join(salad_prl_profit_snapshot.CSV_FIELDS) + "\n"
            path.write_text(
                old_contents,
                encoding="utf-8",
            )
            snapshot = {
                "at_utc": "2026-06-25T12:00:00+00:00",
                "assumed_prl_price": 0.6,
                "live_market_prl_price": 0.7,
                "totals": {},
                "unmapped_totals": {},
                "wallet_observed_rewards": {},
                "wallet_observed_economics_24h": {},
                "pending_slots": [],
            }

            with patch.object(salad_prl_profit_snapshot, "snapshot_csv_path", return_value=path):
                salad_prl_profit_snapshot.append_snapshot_csv(snapshot)

            rows = path.read_text(encoding="utf-8").splitlines()
            backups = list(path.parent.glob("snapshots.schema-mismatch-*.csv"))

            self.assertEqual(rows[0], ",".join(salad_prl_profit_snapshot.CSV_FIELDS))
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), old_contents)

    def test_slot_action_detail_path_matches_existing_writers(self) -> None:
        org = "cantemir1"
        slot = "prl-cantemir1-roi-01"

        self.assertEqual(
            salad_prl_profit_snapshot.slot_action_detail_path(org, slot).name,
            salad_prl_guard.slot_action_detail_path(org, slot).name,
        )
        self.assertEqual(
            salad_prl_profit_snapshot.slot_action_detail_path(org, slot).name,
            salad_prl_watch.slot_action_detail_path(org, slot).name,
        )

    def test_effective_state_age_prefers_recent_slot_action(self) -> None:
        old_path = salad_prl_profit_snapshot.SLOT_ACTION_STATE_PATH
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                salad_prl_profit_snapshot.SLOT_ACTION_STATE_PATH = pathlib.Path(temp_dir) / "prl_slot_actions.json"
                detail_dir = salad_prl_profit_snapshot.SLOT_ACTION_STATE_PATH.parent / "prl_slot_actions.d"
                detail_dir.mkdir()
                detail_path = salad_prl_profit_snapshot.slot_action_detail_path("cantemir1", "prl-cantemir1-roi-01")
                detail_path.write_text(
                    '{"action":"patched","reason":"test","candidate":"RTX 4090 batch","at":'
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

    def test_effective_state_age_accepts_legacy_at_ts_slot_action(self) -> None:
        old_path = salad_prl_profit_snapshot.SLOT_ACTION_STATE_PATH
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                salad_prl_profit_snapshot.SLOT_ACTION_STATE_PATH = pathlib.Path(temp_dir) / "prl_slot_actions.json"
                salad_prl_profit_snapshot.SLOT_ACTION_STATE_PATH.write_text(
                    '{"cantemir1/prl-cantemir1-roi-01":{"action":"patched","at_ts":'
                    + str(time.time() - 60)
                    + "}}",
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
