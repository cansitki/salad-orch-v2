from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
import os


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import state_db
import health
import org_worker
from config_loader import OrgConfig


class ApiBudgetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(pathlib.Path(self.tmpdir.name) / "fleet.db")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_rate_budget_waits_after_limit_until_window_resets(self) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            first = state_db.reserve_api_request(
                conn,
                "SALAD_API_KEY_SHARED",
                max_requests_per_minute=2,
                now_utc="2026-06-24T12:00:00+00:00",
            )
            second = state_db.reserve_api_request(
                conn,
                "SALAD_API_KEY_SHARED",
                max_requests_per_minute=2,
                now_utc="2026-06-24T12:00:01+00:00",
            )
            wait = state_db.reserve_api_request(
                conn,
                "SALAD_API_KEY_SHARED",
                max_requests_per_minute=2,
                now_utc="2026-06-24T12:00:02+00:00",
            )
            after_reset = state_db.reserve_api_request(
                conn,
                "SALAD_API_KEY_SHARED",
                max_requests_per_minute=2,
                now_utc="2026-06-24T12:01:01+00:00",
            )
        self.assertEqual(first, 0.0)
        self.assertEqual(second, 0.0)
        self.assertEqual(wait, 58.0)
        self.assertEqual(after_reset, 0.0)

    def test_rate_budget_is_per_api_key_env(self) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            self.assertEqual(
                state_db.reserve_api_request(
                    conn,
                    "SALAD_API_KEY_A",
                    max_requests_per_minute=1,
                    now_utc="2026-06-24T12:00:00+00:00",
                ),
                0.0,
            )
            self.assertEqual(
                state_db.reserve_api_request(
                    conn,
                    "SALAD_API_KEY_B",
                    max_requests_per_minute=1,
                    now_utc="2026-06-24T12:00:01+00:00",
                ),
                0.0,
            )

    def test_health_reports_api_rate_limits(self) -> None:
        with state_db.connect(self.db_path) as conn:
            state_db.init_db(conn)
            state_db.reserve_api_request(
                conn,
                "SALAD_API_KEY_SHARED",
                max_requests_per_minute=120,
                now_utc="2026-06-24T12:00:00+00:00",
            )
            conn.commit()
        payload = health.build_health(self.db_path)
        self.assertEqual(payload["api_rate_limits"][0]["api_key_env"], "SALAD_API_KEY_SHARED")

    def test_rate_limited_request_forwards_keyword_arguments(self) -> None:
        class DummyWatch:
            def __init__(self) -> None:
                self.calls = []

            def request(self, method, path, payload=None, *, patch=False):
                self.calls.append((method, path, payload, patch))
                return {"patch": patch}

        old_value = os.environ.get("PRL_SALAD_API_MAX_REQUESTS_PER_MINUTE")
        os.environ["PRL_SALAD_API_MAX_REQUESTS_PER_MINUTE"] = "120"
        try:
            watch = DummyWatch()
            org = OrgConfig(
                label="test",
                slug="test",
                api_key_env="SALAD_API_KEY_TEST",
                slot_prefix="prl-test-roi",
            )
            org_worker.install_rate_limited_request(watch, org, db_path=self.db_path)
            result = watch.request("PATCH", "/containers/test", {"x": 1}, patch=True)
        finally:
            if old_value is None:
                os.environ.pop("PRL_SALAD_API_MAX_REQUESTS_PER_MINUTE", None)
            else:
                os.environ["PRL_SALAD_API_MAX_REQUESTS_PER_MINUTE"] = old_value

        self.assertEqual(result, {"patch": True})
        self.assertEqual(watch.calls, [("PATCH", "/containers/test", {"x": 1}, True)])


if __name__ == "__main__":
    unittest.main()
