from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import availability_probe
import profit_model
from config_loader import FleetConfig, OrgConfig


class FakeWatch:
    class Candidate:
        def __init__(self, label, priority, gpu_keys, memory):
            self.label = label
            self.priority = priority
            self.gpu_keys = gpu_keys
            self.memory = memory

    def candidate_availability(self, _slot_name, _candidate):
        return 1


class AvailabilityProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(pathlib.Path(self.tmpdir.name) / "fleet.db")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_probe_installs_rate_limiter_for_org_watch(self) -> None:
        config = FleetConfig(
            organizations=(
                OrgConfig(
                    label="test",
                    slug="test",
                    api_key_env="SALAD_API_KEY_TEST",
                    slot_prefix="prl-test-roi",
                ),
            )
        )
        profile = profit_model.Profile(
            profile_key="4090:batch:2048",
            gpu_key="4090",
            gpu_id="gpu-4090",
            priority="batch",
            label="RTX 4090 batch",
            memory_mb=2048,
            expected_th=230.0,
            static_hourly_usd=0.16,
        )
        watch = FakeWatch()

        with (
            mock.patch.object(availability_probe, "load_config", return_value=config),
            mock.patch.object(availability_probe.profit_model, "load_profiles", return_value=[profile]),
            mock.patch.object(availability_probe, "load_watch_module", return_value=watch),
            mock.patch.object(availability_probe, "install_rate_limited_request") as install_limiter,
        ):
            payload = availability_probe.run_once(db_path=self.db_path, profile_limit=1)

        install_limiter.assert_called_once_with(watch, config.organizations[0], db_path=self.db_path)
        self.assertEqual(payload["probed"], 1)
        self.assertEqual(payload["by_profile"], {"4090:batch:2048": 1})


if __name__ == "__main__":
    unittest.main()
