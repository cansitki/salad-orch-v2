from __future__ import annotations

import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import profit_model


class ProfitModelTest(unittest.TestCase):
    def profiles(self) -> dict[str, profit_model.Profile]:
        return {profile.profile_key: profile for profile in profit_model.load_profiles()}

    def estimate(self, profile_key: str, price: float, fee: float):
        return profit_model.expected_profit(
            self.profiles()[profile_key],
            decision_price_usd=price,
            gross_prl_per_th_day=profit_model.DEFAULT_GROSS_PRL_PER_TH_DAY,
            pearl_fee_rate=fee,
            min_profit_day=0.05,
        )

    def test_base_price_with_five_percent_fee_keeps_weak_batch_out(self) -> None:
        self.assertGreater(self.estimate("4090:batch:2048", 0.64, 0.05).profit_day, 0.05)
        self.assertLess(self.estimate("3080:batch:2048", 0.64, 0.05).profit_day, 0.0)
        self.assertLess(self.estimate("5080:batch:2048", 0.64, 0.05).profit_day, 0.05)

    def test_boost_price_with_five_percent_fee_adds_more_batch_profiles(self) -> None:
        self.assertGreater(self.estimate("3080:batch:2048", 0.70, 0.05).profit_day, 0.05)
        self.assertGreater(self.estimate("5080:batch:2048", 0.70, 0.05).profit_day, 0.05)

    def test_one_percent_fee_for_current_window_improves_margin(self) -> None:
        five_percent = self.estimate("5080:batch:2048", 0.64, 0.05).profit_day
        one_percent = self.estimate("5080:batch:2048", 0.64, 0.01).profit_day
        self.assertGreater(one_percent, five_percent)
        self.assertGreater(one_percent, 0.05)


if __name__ == "__main__":
    unittest.main()
