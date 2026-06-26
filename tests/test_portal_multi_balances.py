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

    def test_load_accounts_from_email_csv(self) -> None:
        accounts = portal_multi_balances.load_accounts(emails="a@example.com, b@example.com")

        self.assertEqual([account.label for account in accounts], ["a_example_com", "b_example_com"])
        self.assertEqual([account.email for account in accounts], ["a@example.com", "b@example.com"])

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


if __name__ == "__main__":
    unittest.main()
