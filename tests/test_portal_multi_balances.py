from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import portal_multi_balances
import state_db
from config_loader import load_config


class PortalMultiBalancesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self.tmpdir.name)
        self.db_path = str(self.tmp_path / "fleet.db")
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.sync_config(conn, load_config())
            conn.commit()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_default_interval_prefers_fast_refill_detection(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(portal_multi_balances.default_interval_seconds(), 60)

    def test_default_interval_can_be_overridden(self) -> None:
        with mock.patch.dict(os.environ, {"PRL_PORTAL_BALANCE_INTERVAL_SECONDS": "120"}, clear=True):
            self.assertEqual(portal_multi_balances.default_interval_seconds(), 120)

    def test_load_accounts_from_email_csv(self) -> None:
        accounts = portal_multi_balances.load_accounts(emails="a@example.com, b@example.com")

        self.assertEqual([account.label for account in accounts], ["a_example_com", "b_example_com"])
        self.assertEqual([account.email for account in accounts], ["a@example.com", "b@example.com"])

    def test_load_accounts_discovers_cookie_jars_before_single_email_fallback(self) -> None:
        state_dir = self.tmp_path / "accounts"
        state_dir.mkdir()
        (state_dir / "bcansitki_gmail_com_cookies.txt").write_text("", encoding="utf-8")
        (state_dir / "sal2_loot_md_cookies.txt").write_text("", encoding="utf-8")

        with mock.patch.dict(
            os.environ,
            {"SALAD_PORTAL_EMAIL": "single@example.com"},
            clear=True,
        ):
            accounts = portal_multi_balances.load_accounts(account_state_dir=state_dir)

        self.assertEqual([account.label for account in accounts], ["bcansitki_gmail_com", "sal2_loot_md"])
        self.assertEqual([account.email for account in accounts], ["bcansitki@gmail.com", "sal2@loot.md"])
        self.assertEqual(accounts[0].cookie_jar, state_dir / "bcansitki_gmail_com_cookies.txt")
        self.assertEqual(accounts[0].balance_file, state_dir / "bcansitki_gmail_com_balances.json")

    def test_load_accounts_explicit_email_csv_overrides_cookie_discovery(self) -> None:
        state_dir = self.tmp_path / "accounts"
        state_dir.mkdir()
        (state_dir / "bcansitki_gmail_com_cookies.txt").write_text("", encoding="utf-8")

        accounts = portal_multi_balances.load_accounts(
            emails="explicit@example.com",
            account_state_dir=state_dir,
        )

        self.assertEqual([account.email for account in accounts], ["explicit@example.com"])

    def test_restored_positive_balance_orgs_detects_zero_to_positive(self) -> None:
        restored = portal_multi_balances.restored_positive_balance_orgs(
            {"kray": 0.0, "kray2": 1.0, "kry1": 0.01},
            {"kray": 2.5, "kray2": 1.25, "kry1": 0.01, "kr1": 4.0},
            threshold=0.0,
        )

        self.assertEqual(restored, ["kr1", "kray"])

    def test_run_once_merges_account_balances(self) -> None:
        balance_file = self.tmp_path / "state" / "salad_balances.json"
        account_state_dir = self.tmp_path / "state" / "accounts"

        with (
            mock.patch.dict(os.environ, {"SALAD_PORTAL_PASSWORD": "secret"}, clear=False),
            mock.patch.object(
                portal_multi_balances,
                "refresh_account",
                side_effect=[
                    {"status": "ok", "balances": {"kray": 7.9, "kray2": 8.15}},
                    {"status": "ok", "balances": {"kry1": 0.0, "kry2": 7.42}},
                ],
            ) as refresh_mock,
            mock.patch.object(
                portal_multi_balances,
                "wake_availability_on_balance_restore",
                return_value={"ok": True, "probed": 2},
            ) as wake_mock,
        ):
            payload = portal_multi_balances.run_once(
                db_path=self.db_path,
                balance_file=balance_file,
                account_state_dir=account_state_dir,
                emails="bcansitki@example.com,can@example.com",
                cwd=self.tmp_path,
                force_login=True,
            )

        self.assertEqual(
            json.loads(balance_file.read_text(encoding="utf-8")),
            {"kray": 7.9, "kray2": 8.15, "kry1": 0.0, "kry2": 7.42},
        )
        self.assertEqual(payload["org_count"], 4)
        self.assertEqual(refresh_mock.call_count, 2)
        self.assertTrue(refresh_mock.call_args_list[0].kwargs["force_login"])
        wake_mock.assert_called_once_with(
            db_path=self.db_path,
            restored_orgs=["kray", "kray2", "kry2"],
        )
        self.assertEqual(payload["restored_positive_balance_orgs"], ["kray", "kray2", "kry2"])
        self.assertEqual(payload["availability_wake"], {"ok": True, "probed": 2})

    def test_run_once_preserves_existing_balances_on_account_failure(self) -> None:
        balance_file = self.tmp_path / "state" / "salad_balances.json"
        balance_file.parent.mkdir(parents=True, exist_ok=True)
        balance_file.write_text('{"kray":7.9,"kry1":0.0}\n', encoding="utf-8")

        with (
            mock.patch.dict(os.environ, {"SALAD_PORTAL_PASSWORD": "secret"}, clear=False),
            mock.patch.object(
                portal_multi_balances,
                "refresh_account",
                side_effect=[RuntimeError("network down"), {"status": "ok", "balances": {"kry2": 7.42}}],
            ),
            mock.patch.object(
                portal_multi_balances,
                "wake_availability_on_balance_restore",
                return_value={"ok": True, "probed": 1},
            ) as wake_mock,
        ):
            payload = portal_multi_balances.run_once(
                db_path=self.db_path,
                balance_file=balance_file,
                account_state_dir=self.tmp_path / "accounts",
                emails="first@example.com,second@example.com",
                cwd=self.tmp_path,
            )

        self.assertEqual(
            json.loads(balance_file.read_text(encoding="utf-8")),
            {"kray": 7.9, "kry1": 0.0, "kry2": 7.42},
        )
        self.assertEqual(payload["failed_accounts"], ["first_example_com"])
        self.assertEqual(payload["status"], "degraded")
        wake_mock.assert_called_once_with(db_path=self.db_path, restored_orgs=["kry2"])
        self.assertEqual(payload["restored_positive_balance_orgs"], ["kry2"])

    def test_wake_availability_can_be_disabled(self) -> None:
        with mock.patch.dict(os.environ, {"PRL_PORTAL_BALANCE_WAKE_AVAILABILITY": "0"}, clear=False):
            self.assertIsNone(
                portal_multi_balances.wake_availability_on_balance_restore(
                    db_path=self.db_path,
                    restored_orgs=["kray"],
                )
            )


if __name__ == "__main__":
    unittest.main()
