from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import portal_balances
import state_db
from config_loader import load_config


class PortalBalancesTest(unittest.TestCase):
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

    def test_extract_json_from_agent_browser_boundaries(self) -> None:
        output = """noise
--- AGENT_BROWSER_PAGE_CONTENT nonce=x origin=https://portal.salad.com ---
{
  "ok": true,
  "balances": [{"org": "kray", "balance_usd": 7.41}]
}
--- END_AGENT_BROWSER_PAGE_CONTENT nonce=x ---
"""
        payload = portal_balances.extract_json_from_output(output)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["balances"][0]["org"], "kray")

    def test_normalize_balances_keeps_successful_org_values(self) -> None:
        payload = {
            "ok": True,
            "balances": [
                {"org": "kray", "ok": True, "balance_usd": 7.414},
                {"org": "kry1", "ok": False, "balance_usd": None},
                {"org": "", "ok": True, "balance_usd": 1.0},
            ],
        }

        balances = portal_balances.normalize_balances(payload)

        self.assertEqual(balances, {"kray": 7.41})

    def test_write_balance_file_is_plain_org_map(self) -> None:
        path = self.tmp_path / "state" / "salad_balances.json"

        portal_balances.write_balance_file(path, {"kray2": 6.0, "kray": 7.41})

        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"kray": 7.41, "kray2": 6.0})

    def test_partial_org_balance_failure_preserves_previous_value_as_stale(self) -> None:
        path = self.tmp_path / "state" / "salad_balances.json"
        portal_balances.write_balance_file(path, {"kray": 7.41, "kray2": 6.0})
        payload = {
            "ok": True,
            "balances": [
                {"org": "kray", "ok": False, "balance_usd": None},
                {"org": "kray2", "ok": True, "balance_usd": 5.5},
            ],
        }
        balances = portal_balances.normalize_balances(payload)

        merged, stale_orgs = portal_balances.merge_existing_balances_for_partial_failures(
            payload=payload,
            balances=balances,
            balance_file=path,
        )

        self.assertEqual(merged, {"kray": 7.41, "kray2": 5.5})
        self.assertEqual(stale_orgs, ["kray"])

    def test_fetch_portal_balances_uses_eval_without_open_when_authenticated(self) -> None:
        expected = {"ok": True, "status": 200, "balances": []}

        with (
            mock.patch.object(portal_balances, "portal_eval", return_value=expected) as eval_mock,
            mock.patch.object(portal_balances, "run_agent_browser") as open_mock,
        ):
            payload = portal_balances.fetch_portal_balances(cwd=self.tmp_path)

        self.assertEqual(payload, expected)
        eval_mock.assert_called_once()
        open_mock.assert_not_called()

    def test_fetch_portal_balances_prefers_http_cookie_jar_when_configured(self) -> None:
        expected = {"ok": True, "status": 200, "balances": []}

        with (
            mock.patch.object(portal_balances, "fetch_portal_balances_http", return_value=expected) as http_mock,
            mock.patch.object(portal_balances, "portal_eval") as eval_mock,
        ):
            payload = portal_balances.fetch_portal_balances(
                cwd=self.tmp_path,
                cookie_jar=self.tmp_path / "portal_cookies.txt",
                portal_email="user@example.com",
                portal_password="secret",
            )

        self.assertEqual(payload, expected)
        http_mock.assert_called_once()
        eval_mock.assert_not_called()

    def test_fetch_portal_balances_http_can_force_login(self) -> None:
        calls = []

        def fake_http_json_request(**kwargs):
            calls.append(kwargs)
            if kwargs["url"].endswith("/users/login"):
                return {"ok": True, "status": 200}
            if kwargs["url"].endswith("/organizations"):
                return {"ok": True, "status": 200, "items": []}
            raise AssertionError(kwargs["url"])

        with mock.patch.object(portal_balances, "http_json_request", side_effect=fake_http_json_request):
            payload = portal_balances.fetch_portal_balances_http(
                cookie_jar=self.tmp_path / "portal_cookies.txt",
                email="user@example.com",
                password="test-password",
                force_login=True,
            )

        self.assertTrue(payload["ok"])
        self.assertTrue(calls[0]["url"].endswith("/users/login"))
        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(calls[0]["body"], {"email": "user@example.com", "password": "test-password"})
        self.assertTrue(calls[1]["url"].endswith("/organizations"))

    def test_fetch_portal_balances_opens_portal_after_unauthorized_eval(self) -> None:
        expected = {"ok": True, "status": 200, "balances": []}

        with (
            mock.patch.object(
                portal_balances,
                "portal_eval",
                side_effect=[{"ok": False, "status": 401, "balances": []}, expected],
            ) as eval_mock,
            mock.patch.object(portal_balances, "run_agent_browser") as open_mock,
        ):
            payload = portal_balances.fetch_portal_balances(cwd=self.tmp_path)

        self.assertEqual(payload, expected)
        self.assertEqual(eval_mock.call_count, 2)
        open_mock.assert_called_once()

    def test_main_loads_env_before_reading_default_cookie_jar(self) -> None:
        cookie_path = self.tmp_path / "portal_cookies.txt"
        balance_file = self.tmp_path / "balances.json"

        def fake_load_env_file() -> None:
            os.environ["SALAD_PORTAL_COOKIE_JAR"] = str(cookie_path)

        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(sys, "argv", ["portal_balances.py", "--once", "--balance-file", str(balance_file)]),
            mock.patch.object(portal_balances, "load_env_file", side_effect=fake_load_env_file),
            mock.patch.object(
                portal_balances,
                "run_once",
                return_value={"status": "ok", "org_count": 0, "missing_enabled_orgs": [], "balances": {}},
            ) as run_mock,
            mock.patch("builtins.print"),
        ):
            os.environ.pop("SALAD_PORTAL_COOKIE_JAR", None)
            portal_balances.main()

        self.assertEqual(run_mock.call_args.kwargs["cookie_jar"], cookie_path)

    def test_main_can_read_portal_credentials_from_stdin(self) -> None:
        balance_file = self.tmp_path / "balances.json"

        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "portal_balances.py",
                    "--once",
                    "--balance-file",
                    str(balance_file),
                    "--force-login",
                    "--portal-credentials-stdin",
                ],
            ),
            mock.patch.object(sys, "stdin", io.StringIO("user@example.com\ntest-password\n")),
            mock.patch.object(
                portal_balances,
                "run_once",
                return_value={"status": "ok", "org_count": 0, "missing_enabled_orgs": [], "balances": {}},
            ) as run_mock,
            mock.patch("builtins.print"),
        ):
            portal_balances.main()

        self.assertEqual(run_mock.call_args.kwargs["portal_email"], "user@example.com")
        self.assertEqual(run_mock.call_args.kwargs["portal_password"], "test-password")
        self.assertTrue(run_mock.call_args.kwargs["force_login"])

    def test_record_refresh_marks_missing_enabled_orgs_degraded(self) -> None:
        payload = {"status": 200, "checked_at_utc": "2026-06-25T00:00:00Z", "balances": [{"org": "kray"}]}
        with state_db.connect(self.db_path) as conn:
            state_db.record_failure(
                conn,
                "portal_balances",
                severity="warning",
                error_type="TimeoutExpired",
                message="stale portal timeout",
            )
            conn.commit()

        result = portal_balances.record_refresh(
            db_path=self.db_path,
            payload=payload,
            balances={"kray": 7.41},
            balance_file=self.tmp_path / "state" / "salad_balances.json",
            stale_balance_orgs=["kray"],
        )

        self.assertEqual(result["status"], "degraded")
        self.assertIn("kry1", result["missing_enabled_orgs"])
        self.assertEqual(result["stale_balance_orgs"], ["kray"])
        with state_db.connect(self.db_path) as conn:
            heartbeat = conn.execute("SELECT * FROM heartbeats WHERE process_name = 'portal_balances'").fetchone()
            failures = conn.execute("SELECT COUNT(*) FROM runtime_failures WHERE component = 'portal_balances'").fetchone()[0]
        self.assertEqual(heartbeat["status"], "degraded")
        self.assertEqual(failures, 0)


if __name__ == "__main__":
    unittest.main()
