from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import config_loader


class ConfigLoaderTest(unittest.TestCase):
    def test_default_fill_priorities_include_low_for_profitable_fill(self) -> None:
        with patch.object(config_loader, "load_env_file", lambda: None), patch.dict(
            config_loader.os.environ,
            {},
            clear=True,
        ):
            config = config_loader.load_config()

        self.assertEqual(config.risk.base_allowed_priorities, ("batch", "low"))

    def test_extra_orgs_append_to_defaults(self) -> None:
        extra = [
            {
                "label": "kray4",
                "slug": "kray4",
                "api_key_env": "SALAD_API_KEY_KRAY4",
                "slot_prefix": "prl-kray4-roi",
                "slots": 10,
            }
        ]
        with patch.object(config_loader, "load_env_file", lambda: None), patch.dict(
            config_loader.os.environ,
            {"SALAD_FLEET_EXTRA_ORGS_JSON": json.dumps(extra)},
            clear=True,
        ):
            config = config_loader.load_config()
        self.assertEqual(config.target_slot_count(), 50)
        self.assertIn("kray4", [org.label for org in config.enabled_orgs()])

    def test_extra_orgs_accept_requested_expansion_inline_json(self) -> None:
        extra = [
            {
                "label": "kry2",
                "slug": "kry2",
                "api_key_env": "SALAD_API_KEY_KRY2",
                "slot_prefix": "prl-kry2-roi",
                "worker_prefix": "kry2-prl",
                "worker_slot_prefix": "kry2-roi-",
                "pool_worker_prefix": "kry2-prl-kry2",
                "display_prefix": "PearlFortune KRY2",
                "slots": 10,
            },
            {
                "label": "kr1",
                "slug": "kr1",
                "api_key_env": "SALAD_API_KEY_KR1",
                "slot_prefix": "prl-kr1-roi",
                "worker_prefix": "kr1-prl",
                "worker_slot_prefix": "kr1-roi-",
                "pool_worker_prefix": "kr1-prl-kr1",
                "display_prefix": "PearlFortune KR1",
                "slots": 10,
            },
            {
                "label": "kr2",
                "slug": "kr2",
                "api_key_env": "SALAD_API_KEY_KR1",
                "slot_prefix": "prl-kr2-roi",
                "worker_prefix": "kr2-prl",
                "worker_slot_prefix": "kr2-roi-",
                "pool_worker_prefix": "kr2-prl-kr2",
                "display_prefix": "PearlFortune KR2",
                "slots": 10,
            },
            {
                "label": "kr3",
                "slug": "kr3",
                "api_key_env": "SALAD_API_KEY_KR1",
                "slot_prefix": "prl-kr3-roi",
                "worker_prefix": "kr3-prl",
                "worker_slot_prefix": "kr3-roi-",
                "pool_worker_prefix": "kr3-prl-kr3",
                "display_prefix": "PearlFortune KR3",
                "slots": 10,
            },
            {
                "label": "alpha1",
                "slug": "alpha1",
                "api_key_env": "SALAD_API_KEY_ALPHA",
                "slot_prefix": "prl-alpha1-roi",
                "worker_prefix": "alpha1-prl",
                "worker_slot_prefix": "alpha1-roi-",
                "pool_worker_prefix": "alpha1-prl-alpha1",
                "display_prefix": "PearlFortune ALPHA1",
                "slots": 10,
            },
            {
                "label": "alpha2",
                "slug": "alpha2",
                "api_key_env": "SALAD_API_KEY_ALPHA",
                "slot_prefix": "prl-alpha2-roi",
                "worker_prefix": "alpha2-prl",
                "worker_slot_prefix": "alpha2-roi-",
                "pool_worker_prefix": "alpha2-prl-alpha2",
                "display_prefix": "PearlFortune ALPHA2",
                "slots": 10,
            },
        ]
        with patch.object(config_loader, "load_env_file", lambda: None), patch.dict(
            config_loader.os.environ,
            {"SALAD_FLEET_EXTRA_ORGS_JSON": json.dumps(extra)},
            clear=True,
        ):
            config = config_loader.load_config()

        self.assertEqual(config.target_slot_count(), 100)
        self.assertEqual(
            [org.label for org in config.enabled_orgs()[-6:]],
            ["kry2", "kr1", "kr2", "kr3", "alpha1", "alpha2"],
        )

    def test_config_path_loads_public_fleet_config(self) -> None:
        payload = {
            "organizations": [
                {
                    "label": "path-org",
                    "slug": "path-org",
                    "api_key_env": "SALAD_API_KEY_PATH",
                    "slot_prefix": "prl-path-org-roi",
                    "worker_prefix": "path-org-prl",
                    "worker_slot_prefix": "path-org-roi-",
                    "pool_worker_prefix": "path-org-prl-path-org",
                    "display_prefix": "PearlFortune PATH",
                    "slots": 10,
                    "enabled": True,
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = pathlib.Path(tmpdir) / "fleet.json"
            config_path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(config_loader, "load_env_file", lambda: None), patch.dict(
                config_loader.os.environ,
                {"SALAD_FLEET_CONFIG_PATH": str(config_path)},
                clear=True,
            ):
                config = config_loader.load_config()

        self.assertEqual(config.target_slot_count(), 10)
        self.assertEqual([org.label for org in config.enabled_orgs()], ["path-org"])

    def test_validate_config_catches_duplicate_slot_prefix(self) -> None:
        orgs = (
            config_loader.OrgConfig(
                label="a",
                slug="a",
                api_key_env="SALAD_API_KEY_A",
                slot_prefix="prl-dup-roi",
            ),
            config_loader.OrgConfig(
                label="b",
                slug="b",
                api_key_env="SALAD_API_KEY_B",
                slot_prefix="prl-dup-roi",
            ),
        )
        config = config_loader.FleetConfig(organizations=orgs)
        issues = config_loader.validate_config(config)
        self.assertTrue(any(issue["field"] == "slot_prefix" and issue["level"] == "error" for issue in issues))

    def test_validate_config_can_require_enabled_org_secrets(self) -> None:
        orgs = (
            config_loader.OrgConfig(
                label="a",
                slug="a",
                api_key_env="SALAD_API_KEY_A",
                slot_prefix="prl-a-roi",
                enabled=True,
            ),
            config_loader.OrgConfig(
                label="b",
                slug="b",
                api_key_env="SALAD_API_KEY_B",
                slot_prefix="prl-b-roi",
                enabled=False,
            ),
        )
        config = config_loader.FleetConfig(organizations=orgs)
        with patch.dict(config_loader.os.environ, {}, clear=True):
            issues = config_loader.validate_config(config, require_secrets=True)
        messages = [issue["message"] for issue in issues]
        self.assertIn("a missing env var SALAD_API_KEY_A", messages)
        self.assertNotIn("b missing env var SALAD_API_KEY_B", messages)

    def test_slot_name_overrides_replace_one_slot_without_changing_capacity(self) -> None:
        overrides = {"kray2": {"05": "prl-kray2-roi-05b"}}
        with patch.object(config_loader, "load_env_file", lambda: None), patch.dict(
            config_loader.os.environ,
            {"PRL_SLOT_NAME_OVERRIDES_JSON": json.dumps(overrides)},
            clear=True,
        ):
            config = config_loader.load_config()

        kray2 = next(org for org in config.organizations if org.label == "kray2")
        self.assertEqual(config.target_slot_count(), 40)
        self.assertIn("prl-kray2-roi-05b", kray2.slot_names())
        self.assertNotIn("prl-kray2-roi-05", kray2.slot_names())


if __name__ == "__main__":
    unittest.main()
