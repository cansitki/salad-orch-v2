#!/usr/bin/env python3
from __future__ import annotations

import argparse
from typing import Any

import state_db
from config_loader import load_config
from fleet_common import json_dumps


BLOCKED_RISK_TIERS = {"negative", "marginal", "blocked_priority"}


def _slot_key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row["org_label"]), str(row["slot_name"])


def _target_rows(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT t.*, s.observed_profile_key, s.observed_status,
               s.live_hashrate_th, s.protected AS observed_protected,
               ps.risk_tier, ps.score, ps.scored_at_utc,
               sp.profit_day AS observed_profit_day
        FROM slot_targets t
        LEFT JOIN slots s ON s.org_label = t.org_label AND s.slot_name = t.slot_name
        LEFT JOIN profile_scores ps ON ps.profile_key = t.profile_key AND ps.mode = t.mode
        LEFT JOIN profit_snapshots sp
          ON sp.scope = 'slot'
         AND sp.org_label = t.org_label
         AND sp.slot_name = t.slot_name
         AND sp.at_utc = (
            SELECT at_utc
            FROM profit_snapshots
            WHERE scope = 'slot'
            ORDER BY at_utc DESC, id DESC
            LIMIT 1
         )
        ORDER BY t.org_label, t.slot_name
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _slot_rows(conn) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM slots ORDER BY org_label, slot_name").fetchall()
    return [dict(row) for row in rows]


def build_shadow_compare(db_path: str | None = None) -> dict[str, Any]:
    config = load_config()
    min_profit_by_mode = {
        "optimize": config.risk.optimize_min_profit_day,
        "base_fill": config.risk.fill_min_profit_day,
        "boost_fill": config.risk.fill_min_profit_day,
        "aggressive_boost": config.risk.fill_min_profit_day,
        "risk_off": config.risk.fill_min_profit_day,
    }
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        state_db.sync_config(conn, config)
        slots = _slot_rows(conn)
        targets = _target_rows(conn)
        conn.commit()

    target_by_slot = {_slot_key(row): row for row in targets}
    slot_by_key = {_slot_key(row): row for row in slots}
    expected_slot_keys = {
        (org.label, slot_name)
        for org in config.enabled_orgs()
        for slot_name in org.slot_names()
    }
    missing_targets = [
        {
            "org_label": org_label,
            "slot_name": slot_name,
            "observed_status": slot_by_key.get((org_label, slot_name), {}).get("observed_status"),
        }
        for org_label, slot_name in sorted(expected_slot_keys)
        if (org_label, slot_name) not in target_by_slot
    ]
    extra_targets = [
        {"org_label": org_label, "slot_name": slot_name, "profile_key": row.get("profile_key")}
        for (org_label, slot_name), row in sorted(target_by_slot.items())
        if (org_label, slot_name) not in expected_slot_keys
    ]
    unsafe_targets: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    for target in targets:
        mode = str(target["mode"])
        min_profit = float(min_profit_by_mode.get(mode, config.risk.fill_min_profit_day))
        expected_profit = float(target.get("expected_profit_day") or 0)
        risk_tier = target.get("risk_tier")
        observed_status = str(target.get("observed_status") or "")
        observed_protected = int(target.get("observed_protected") or 0) > 0
        observed_live_hashrate_th = float(target.get("live_hashrate_th") or 0)
        observed_profit_raw = target.get("observed_profit_day")
        observed_profit = float(observed_profit_raw) if observed_profit_raw is not None else None
        target_protected = int(target.get("protected") or 0) > 0
        profit_floor = observed_profit if observed_profit is not None else expected_profit
        protected_positive_fill = (
            mode != "optimize"
            and observed_status == "running"
            and observed_protected
            and observed_live_hashrate_th > 0
            and target_protected
            and profit_floor >= 0
        )
        if risk_tier is None:
            unsafe_targets.append(
                {
                    "org_label": target["org_label"],
                    "slot_name": target["slot_name"],
                    "profile_key": target["profile_key"],
                    "reason": "missing_profile_score",
                }
            )
        elif str(risk_tier) in BLOCKED_RISK_TIERS:
            payload = {
                "org_label": target["org_label"],
                "slot_name": target["slot_name"],
                "profile_key": target["profile_key"],
                "risk_tier": risk_tier,
                "reason": "blocked_risk_tier",
            }
            if observed_profit is not None:
                payload["observed_profit_day"] = observed_profit
            if protected_positive_fill:
                warnings.append({**payload, "reason": f"protected_positive_{risk_tier}"})
            else:
                unsafe_targets.append(payload)
        if expected_profit < min_profit:
            payload = {
                "org_label": target["org_label"],
                "slot_name": target["slot_name"],
                "profile_key": target["profile_key"],
                "expected_profit_day": expected_profit,
                "min_profit_day": min_profit,
                "reason": "below_min_profit",
            }
            if observed_profit is not None:
                payload["observed_profit_day"] = observed_profit
            if protected_positive_fill:
                warnings.append({**payload, "reason": "protected_positive_below_min_profit"})
            else:
                unsafe_targets.append(payload)
        observed_profile = target.get("observed_profile_key")
        if observed_profile and observed_profile != target["profile_key"]:
            severity = "info"
            if observed_status == "running" and observed_protected and mode != "optimize":
                severity = "warning"
            mismatches.append(
                {
                    "org_label": target["org_label"],
                    "slot_name": target["slot_name"],
                    "observed_profile_key": observed_profile,
                    "target_profile_key": target["profile_key"],
                    "observed_status": observed_status or None,
                    "observed_protected": observed_protected,
                    "target_mode": mode,
                    "severity": severity,
                    "reason": target.get("reason"),
                }
            )
    profile_counts: dict[str, int] = {}
    for target in targets:
        key = str(target["profile_key"])
        profile_counts[key] = profile_counts.get(key, 0) + 1
    target_count = len(targets)
    max_profile_count = max(profile_counts.values()) if profile_counts else 0
    top_profile_share = (max_profile_count / target_count) if target_count else 0.0
    diversification = {
        "unique_target_profiles": len(profile_counts),
        "max_profile_count": max_profile_count,
        "top_profile_share": round(top_profile_share, 4),
        "profile_counts": dict(sorted(profile_counts.items(), key=lambda item: item[1], reverse=True)),
    }
    gate_failures = []
    if missing_targets:
        gate_failures.append({"gate": "missing_targets", "count": len(missing_targets)})
    if extra_targets:
        gate_failures.append({"gate": "extra_targets", "count": len(extra_targets)})
    if unsafe_targets:
        gate_failures.append({"gate": "unsafe_targets", "count": len(unsafe_targets)})
    if target_count > 1 and len(profile_counts) <= 1:
        gate_failures.append({"gate": "diversification", "count": len(profile_counts)})
    warning_mismatches = [item for item in mismatches if item["severity"] == "warning"]
    if warning_mismatches:
        warnings.append({"gate": "protected_running_mismatch", "count": len(warning_mismatches)})
    return {
        "ok": not gate_failures,
        "target_slots": config.target_slot_count(),
        "assigned_targets": target_count,
        "observed_slots": len(slots),
        "missing_targets": missing_targets,
        "extra_targets": extra_targets,
        "unsafe_targets": unsafe_targets,
        "mismatches": mismatches,
        "diversification": diversification,
        "warnings": warnings,
        "gate_failures": gate_failures,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare shadow scheduler targets against observed Salad slot state.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    payload = build_shadow_compare(args.db)
    if args.json:
        print(json_dumps(payload))
        return
    print(
        f"shadow ok={payload['ok']} targets={payload['assigned_targets']}/{payload['target_slots']} "
        f"unsafe={len(payload['unsafe_targets'])} missing={len(payload['missing_targets'])} "
        f"mismatches={len(payload['mismatches'])}"
    )
    diversification = payload["diversification"]
    print(
        f"diversification unique={diversification['unique_target_profiles']} "
        f"top_share={float(diversification['top_profile_share']):.2%}"
    )
    for failure in payload["gate_failures"]:
        print(f"FAIL {failure['gate']}: {failure['count']}")
    for warning in payload["warnings"]:
        if "gate" in warning:
            print(f"WARN {warning['gate']}: {warning['count']}")
        else:
            print(f"WARN {warning['reason']}: {warning['org_label']} {warning['slot_name']} {warning['profile_key']}")


if __name__ == "__main__":
    main()
