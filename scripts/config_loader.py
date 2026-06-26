#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from fleet_common import env_bool, env_float, json_dumps, load_env_file, read_json_env


@dataclass(frozen=True)
class OrgConfig:
    label: str
    slug: str
    api_key_env: str
    slot_prefix: str
    slots: int = 10
    enabled: bool = True
    worker_prefix: str | None = None
    worker_slot_prefix: str | None = None
    pool_worker_prefix: str | None = None
    display_prefix: str | None = None
    slot_name_overrides: tuple[str, ...] = ()

    def slot_names(self) -> list[str]:
        names = [f"{self.slot_prefix}-{index:02d}" for index in range(1, self.slots + 1)]
        for index, slot_name in enumerate(self.slot_name_overrides[: self.slots]):
            if slot_name:
                names[index] = slot_name
        return names

    def watch_env(self) -> dict[str, str]:
        label = self.label
        return {
            "PRL_WATCH_NAME": f"{label}-prl-watch",
            "PRL_WATCH_ORG": self.slug,
            "PRL_WATCH_PUBLIC_ORG": label,
            "PRL_WATCH_API_KEY_ENV": self.api_key_env,
            "PRL_WATCH_SLOTS": ",".join(self.slot_names()),
            "PRL_WATCH_WORKER_PREFIX": self.worker_prefix or f"{label}-prl",
            "PRL_WATCH_WORKER_SLOT_PREFIX": self.worker_slot_prefix or f"{label}-roi-",
            "PRL_WATCH_POOL_WORKER_PREFIX": self.pool_worker_prefix or f"{label}-prl-{label}",
            "PRL_WATCH_DISPLAY_PREFIX": self.display_prefix or f"PearlFortune {label.upper()}",
        }


@dataclass(frozen=True)
class RiskConfig:
    fleet_mode: str = "fill"
    base_decision_price: float = 0.64
    optimize_decision_price: float = 0.62
    boost_price_band: float = 0.02
    boost_trailing_min_30m: float = 0.70
    aggressive_trailing_min_1h: float = 0.72
    risk_off_trailing_min_15m: float = 0.68
    boost_min_window_seconds: int = 1200
    aggressive_min_window_seconds: int = 2700
    fill_min_profit_day: float = 0.05
    optimize_min_profit_day: float = 0.01
    optimize_min_upgrade_delta_day: float = 0.25
    pearl_fee_rate: float = 0.05
    temporary_pearl_fee_rate: float | None = None
    temporary_pearl_fee_until_utc: str | None = None
    base_allowed_priorities: tuple[str, ...] = ("batch", "low")
    boost_allowed_priorities: tuple[str, ...] = ("batch", "low")

    def effective_fee_rate(self, now: datetime | None = None) -> float:
        if self.temporary_pearl_fee_rate is None or not self.temporary_pearl_fee_until_utc:
            return self.pearl_fee_rate
        now = now or datetime.now(UTC)
        raw = self.temporary_pearl_fee_until_utc
        try:
            until = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return self.pearl_fee_rate
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        return self.temporary_pearl_fee_rate if now <= until else self.pearl_fee_rate

    def decision_price_for_mode(self, mode: str | None = None) -> float:
        selected = mode or self.fleet_mode
        if selected == "optimize":
            return self.optimize_decision_price
        return self.base_decision_price

    def min_profit_for_mode(self, mode: str | None = None) -> float:
        selected = mode or self.fleet_mode
        if selected == "optimize":
            return self.optimize_min_profit_day
        return self.fill_min_profit_day


@dataclass(frozen=True)
class MinerConfig:
    release_tag: str = "v.1.1.8"
    package_version: str = "v1.1.8"
    binary: str = "miner-cuda12"
    pool_proxy: str = "global.pearlfortune.org:443"


@dataclass(frozen=True)
class FleetConfig:
    organizations: tuple[OrgConfig, ...]
    risk: RiskConfig = field(default_factory=RiskConfig)
    miner: MinerConfig = field(default_factory=MinerConfig)
    project: str = "default"

    def enabled_orgs(self) -> list[OrgConfig]:
        return [org for org in self.organizations if org.enabled]

    def target_slot_count(self) -> int:
        return sum(org.slots for org in self.enabled_orgs())


DEFAULT_ORGS = (
    OrgConfig(
        label="kray",
        slug="kray",
        api_key_env="SALAD_API_KEY_2",
        slot_prefix="prl-kray-roi",
        worker_prefix="kray-prl",
        worker_slot_prefix="kray-roi-",
        pool_worker_prefix="kray-prl-kray",
        display_prefix="PearlFortune KRAY",
    ),
    OrgConfig(
        label="kry1",
        slug="kry1",
        api_key_env="SALAD_API_KEY_KRY1",
        slot_prefix="prl-kry1-roi",
        worker_prefix="kry1-prl",
        worker_slot_prefix="kry1-roi-",
        pool_worker_prefix="kry1-prl-kry1",
        display_prefix="PearlFortune KRY1",
    ),
    OrgConfig(
        label="kray2",
        slug="kray2",
        api_key_env="SALAD_API_KEY_2",
        slot_prefix="prl-kray2-roi",
        worker_prefix="kray2-prl",
        worker_slot_prefix="kray2-roi-",
        pool_worker_prefix="kray2-prl-kray2",
        display_prefix="PearlFortune KRAY2",
    ),
    OrgConfig(
        label="kray3",
        slug="kray3",
        api_key_env="SALAD_API_KEY_2",
        slot_prefix="prl-kray3-roi",
        worker_prefix="kray3-prl",
        worker_slot_prefix="kray3-roi-",
        pool_worker_prefix="kray3-prl-kray3",
        display_prefix="PearlFortune KRAY3",
    ),
)


def _org_from_dict(payload: dict[str, Any]) -> OrgConfig:
    slot_name_overrides = payload.get("slot_name_overrides") or payload.get("slot_names") or ()
    return OrgConfig(
        label=str(payload["label"]),
        slug=str(payload.get("slug") or payload["label"]),
        api_key_env=str(payload["api_key_env"]),
        slot_prefix=str(payload["slot_prefix"]),
        slots=int(payload.get("slots", 10)),
        enabled=bool(payload.get("enabled", True)),
        worker_prefix=payload.get("worker_prefix"),
        worker_slot_prefix=payload.get("worker_slot_prefix"),
        pool_worker_prefix=payload.get("pool_worker_prefix"),
        display_prefix=payload.get("display_prefix"),
        slot_name_overrides=tuple(str(item or "") for item in slot_name_overrides),
    )


def _split_csv(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        return default
    return tuple(item.strip().lower() for item in value.split(",") if item.strip())


def _load_orgs_from_env() -> tuple[OrgConfig, ...]:
    payload = read_json_env("SALAD_FLEET_ORGS_JSON") or read_json_env("PRL_FLEET_ORGS_JSON")
    if payload is None:
        return DEFAULT_ORGS
    if isinstance(payload, dict):
        payload = payload.get("organizations") or []
    return tuple(_org_from_dict(item) for item in payload)


def _extra_orgs_from_env() -> tuple[OrgConfig, ...]:
    payload = read_json_env("SALAD_FLEET_EXTRA_ORGS_JSON") or read_json_env("PRL_FLEET_EXTRA_ORGS_JSON")
    if payload is None:
        return ()
    if isinstance(payload, dict):
        payload = payload.get("organizations") or []
    return tuple(_org_from_dict(item) for item in payload)


def _slot_name_overrides_from_env() -> dict[str, Any]:
    payload = read_json_env("SALAD_SLOT_NAME_OVERRIDES_JSON") or read_json_env("PRL_SLOT_NAME_OVERRIDES_JSON")
    return payload if isinstance(payload, dict) else {}


def _apply_slot_name_overrides(organizations: tuple[OrgConfig, ...]) -> tuple[OrgConfig, ...]:
    payload = _slot_name_overrides_from_env()
    if not payload:
        return organizations
    updated: list[OrgConfig] = []
    for org in organizations:
        raw = payload.get(org.label, payload.get(org.slug))
        if raw is None:
            updated.append(org)
            continue
        overrides = list(org.slot_name_overrides[: org.slots])
        if len(overrides) < org.slots:
            overrides.extend([""] * (org.slots - len(overrides)))
        base_names = [f"{org.slot_prefix}-{index:02d}" for index in range(1, org.slots + 1)]
        if isinstance(raw, list):
            for index, value in enumerate(raw[: org.slots]):
                if value:
                    overrides[index] = str(value)
        elif isinstance(raw, dict):
            for key, value in raw.items():
                if not value:
                    continue
                key_text = str(key)
                index: int | None = None
                if key_text.isdigit():
                    index = int(key_text) - 1
                elif key_text in base_names:
                    index = base_names.index(key_text)
                elif key_text in {name.rsplit("-", 1)[-1] for name in base_names}:
                    for candidate_index, name in enumerate(base_names):
                        if name.rsplit("-", 1)[-1] == key_text:
                            index = candidate_index
                            break
                if index is not None and 0 <= index < org.slots:
                    overrides[index] = str(value)
        updated.append(OrgConfig(**{**asdict(org), "slot_name_overrides": tuple(overrides)}))
    return tuple(updated)


def validate_config(config: FleetConfig, *, require_secrets: bool = False) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    seen_labels: set[str] = set()
    seen_slugs: set[str] = set()
    seen_slot_prefixes: set[str] = set()
    seen_slots: set[str] = set()
    for org in config.organizations:
        if not org.label:
            issues.append({"level": "error", "field": "label", "message": "organization label is empty"})
        if org.label in seen_labels:
            issues.append({"level": "error", "field": "label", "message": f"duplicate org label {org.label}"})
        seen_labels.add(org.label)
        if org.slug in seen_slugs:
            issues.append({"level": "error", "field": "slug", "message": f"duplicate org slug {org.slug}"})
        seen_slugs.add(org.slug)
        if org.slot_prefix in seen_slot_prefixes:
            issues.append({"level": "error", "field": "slot_prefix", "message": f"duplicate slot prefix {org.slot_prefix}"})
        seen_slot_prefixes.add(org.slot_prefix)
        if org.slots <= 0:
            issues.append({"level": "error", "field": "slots", "message": f"{org.label} has non-positive slot count"})
        if org.slots != 10:
            issues.append({"level": "warning", "field": "slots", "message": f"{org.label} uses {org.slots} slots instead of the default 10"})
        if not org.api_key_env:
            issues.append({"level": "error", "field": "api_key_env", "message": f"{org.label} has no API key env var"})
        elif require_secrets and org.enabled and not os.environ.get(org.api_key_env):
            issues.append({"level": "error", "field": "api_key_env", "message": f"{org.label} missing env var {org.api_key_env}"})
        for slot_name in org.slot_names():
            if slot_name in seen_slots:
                issues.append({"level": "error", "field": "slot_name", "message": f"duplicate slot name {slot_name}"})
            seen_slots.add(slot_name)
    return issues


def load_config() -> FleetConfig:
    load_env_file()
    config_payload = (
        read_json_env("SALAD_FLEET_CONFIG_PATH")
        or read_json_env("PRL_FLEET_CONFIG_PATH")
        or read_json_env("SALAD_FLEET_CONFIG_JSON")
    )

    if config_payload:
        org_payload = config_payload.get("organizations") or []
        organizations = tuple(_org_from_dict(item) for item in org_payload)
    else:
        organizations = _load_orgs_from_env()

    extra_orgs = _extra_orgs_from_env()
    if extra_orgs:
        organizations = (*organizations, *extra_orgs)
    organizations = _apply_slot_name_overrides(organizations)

    enabled_filter = {item.strip() for item in os.environ.get("PRL_ENABLED_ORGS", "").split(",") if item.strip()}
    if enabled_filter:
        organizations = tuple(
            OrgConfig(**{**asdict(org), "enabled": org.enabled and org.label in enabled_filter})
            for org in organizations
        )

    risk = RiskConfig(
        fleet_mode=os.environ.get("PRL_FLEET_MODE", "fill"),
        base_decision_price=env_float("PRL_FILL_FIXED_DECISION_PRICE_USD", 0.64),
        optimize_decision_price=env_float("PRL_OPTIMIZE_FIXED_DECISION_PRICE_USD", 0.62),
        boost_price_band=env_float("PRL_PRICE_BAND_USD", 0.02),
        boost_trailing_min_30m=env_float("PRL_BOOST_TRAILING_MIN_30M_USD", 0.70),
        aggressive_trailing_min_1h=env_float("PRL_AGGRESSIVE_TRAILING_MIN_1H_USD", 0.72),
        risk_off_trailing_min_15m=env_float("PRL_RISK_OFF_TRAILING_MIN_15M_USD", 0.68),
        boost_min_window_seconds=int(env_float("PRL_BOOST_MIN_WINDOW_SECONDS", 1200)),
        aggressive_min_window_seconds=int(env_float("PRL_AGGRESSIVE_MIN_WINDOW_SECONDS", 2700)),
        fill_min_profit_day=env_float("PRL_FILL_MIN_PROFIT_USD_DAY", 0.05),
        optimize_min_profit_day=env_float("PRL_OPTIMIZE_MIN_PROFIT_USD_DAY", 0.01),
        optimize_min_upgrade_delta_day=env_float("PRL_OPTIMIZE_MIN_UPGRADE_DELTA_USD_DAY", 0.25),
        pearl_fee_rate=env_float("PRL_PEARL_FEE_RATE", 0.05),
        temporary_pearl_fee_rate=(
            env_float("PRL_TEMP_PEARL_FEE_RATE", 0.0)
            if os.environ.get("PRL_TEMP_PEARL_FEE_RATE")
            else None
        ),
        temporary_pearl_fee_until_utc=os.environ.get("PRL_TEMP_PEARL_FEE_UNTIL_UTC"),
        base_allowed_priorities=_split_csv(os.environ.get("PRL_BASE_ALLOWED_PRIORITIES"), ("batch", "low")),
        boost_allowed_priorities=_split_csv(os.environ.get("PRL_BOOST_ALLOWED_PRIORITIES"), ("batch", "low")),
    )

    miner = MinerConfig(
        release_tag=os.environ.get("PRL_WATCH_MINER_RELEASE_TAG", "v.1.1.8"),
        package_version=os.environ.get("PRL_WATCH_MINER_PACKAGE_VERSION", "v1.1.8"),
        binary=os.environ.get("PRL_WATCH_MINER_BINARY", "miner-cuda12"),
        pool_proxy=os.environ.get("PRL_POOL_PROXY", "global.pearlfortune.org:443"),
    )

    return FleetConfig(organizations=organizations, risk=risk, miner=miner)


def public_config_dict(config: FleetConfig) -> dict[str, Any]:
    return {
        "project": config.project,
        "target_slot_count": config.target_slot_count(),
        "organizations": [asdict(org) for org in config.organizations],
        "risk": {
            **asdict(config.risk),
            "effective_pearl_fee_rate": config.risk.effective_fee_rate(),
        },
        "miner": asdict(config.miner),
        "secrets_present": {
            org.label: bool(os.environ.get(org.api_key_env))
            for org in config.organizations
        },
        "validation": validate_config(config),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Load the public Salad PRL fleet config.")
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    parser.add_argument("--check-secrets", action="store_true", help="Exit non-zero if an enabled org key is missing.")
    parser.add_argument("--validate", action="store_true", help="Validate org config and exit non-zero on errors.")
    args = parser.parse_args()

    config = load_config()
    payload = public_config_dict(config)
    issues = validate_config(config, require_secrets=args.check_secrets)
    if args.check_secrets:
        missing = [
            org.api_key_env
            for org in config.enabled_orgs()
            if not os.environ.get(org.api_key_env)
        ]
        if missing:
            raise SystemExit(f"missing env vars: {', '.join(sorted(set(missing)))}")
    if args.validate:
        errors = [issue for issue in issues if issue["level"] == "error"]
        if args.json:
            print(json_dumps({**payload, "validation": issues}))
        else:
            for issue in issues:
                print(f"{issue['level']}: {issue['field']}: {issue['message']}")
            if not issues:
                print("config valid")
        if errors:
            raise SystemExit(2)
        return
    if args.json:
        print(json_dumps({**payload, "validation": issues}))
    else:
        for org in config.enabled_orgs():
            print(f"{org.label}: slug={org.slug} slots={org.slots} key_env={org.api_key_env}")
        print(f"target_slots={config.target_slot_count()}")
        print(f"effective_pearl_fee_rate={config.risk.effective_fee_rate():.4f}")
        if issues:
            print("validation:")
            for issue in issues:
                print(f"  {issue['level']}: {issue['field']}: {issue['message']}")


if __name__ == "__main__":
    main()
