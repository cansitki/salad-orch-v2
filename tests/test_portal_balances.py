from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest


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

    def test_record_refresh_marks_missing_enabled_orgs_degraded(self) -> None:
        payload = {"status": 200, "checked_at_utc": "2026-06-25T00:00:00Z", "balances": [{"org": "kray"}]}

        result = portal_balances.record_refresh(
            db_path=self.db_path,
            payload=payload,
            balances={"kray": 7.41},
            balance_file=self.tmp_path / "state" / "salad_balances.json",
        )

        self.assertEqual(result["status"], "degraded")
        self.assertIn("kry1", result["missing_enabled_orgs"])
        with state_db.connect(self.db_path) as conn:
            heartbeat = conn.execute("SELECT * FROM heartbeats WHERE process_name = 'portal_balances'").fetchone()
        self.assertEqual(heartbeat["status"], "degraded")


if __name__ == "__main__":
    unittest.main()
