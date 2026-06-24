#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from datetime import UTC, datetime
from typing import Any

import profile_scorer
import state_db
from config_loader import FleetConfig, load_config
from fleet_common import env_int, json_dumps, utc_now


def _scheduler_mode(config: FleetConfig, db_mode: str | None) -> str:
    if db_mode:
        return db_mode
    if config.risk.fleet_mode == "optimize":
        return "optimize"
    return "base_fill"


def _top_eligible_profiles(scores: list[dict[str, Any]], *, width: int) -> list[dict[str, Any]]:
    eligible = [row for row in scores if row.get("eligible")]
    eligible.sort(key=lambda item: (float(item["score"]), float(item["expected_profit_day"])), reverse=True)
    return eligible[: max(1, min(width, len(eligible)))]


def _age_seconds(value: Any) -> float | None:
    if not value:
        return None
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - at).total_seconds())


def build_targets(
    config: FleetConfig,
    scores: list[dict[str, Any]],
    *,
    mode: str,
    decision_price_usd: float,
    width: int = 10,
    slot_rows: dict[tuple[str, str], dict[str, Any]] | None = None,
    availability: dict[str, dict[str, dict[str, Any]]] | None = None,
    cooldowns: set[tuple[str, str, str]] | None = None,
    guard_targets: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    profiles = _top_eligible_profiles(scores, width=width)
    if not profiles:
        return []
    slot_rows = slot_rows or {}
    availability = availability or {}
    cooldowns = cooldowns or set()
    guard_targets = guard_targets or {}
    scores_by_key = {str(score["profile_key"]): score for score in scores}
    pending_target_protect_seconds = max(0, env_int("PRL_PENDING_TARGET_PROTECT_SECONDS", 180))
    min_profit_day = config.risk.min_profit_for_mode("optimize" if mode == "optimize" else "fill")
    assigned_by_org_profile: dict[tuple[str, str], int] = {}
    targets: list[dict[str, Any]] = []
    assigned_at = utc_now()
    enabled_orgs = sorted(
        config.enabled_orgs(),
        key=lambda org: sum(
            1
            for slot_name in org.slot_names()
            if (slot_rows.get((org.label, slot_name), {}).get("observed_status") or "")
            in {"running", "creating", "allocating"}
        ),
    )

    def has_capacity(
        org_label: str,
        slot_name: str,
        profile_key: str,
        *,
        allow_availability_probe: bool = False,
    ) -> bool:
        if (org_label, slot_name, profile_key) in cooldowns:
            return False
        if (org_label, "*", profile_key) in cooldowns:
            return False
        org_availability = availability.get(org_label, {})
        if profile_key not in org_availability:
            return True
        row = org_availability[profile_key]
        if not row.get("ok"):
            return True
        available_count = int(row.get("available_count") or 0)
        used = assigned_by_org_profile.get((org_label, profile_key), 0)
        if used < available_count:
            return True
        return allow_availability_probe

    def is_in_cooldown(org_label: str, slot_name: str, profile_key: str) -> bool:
        return (org_label, slot_name, profile_key) in cooldowns or (org_label, "*", profile_key) in cooldowns

    def slot_sort_key(org_label: str, slot_name: str) -> tuple[int, str]:
        row = slot_rows.get((org_label, slot_name), {})
        status = str(row.get("observed_status") or "unknown")
        protected = int(row.get("protected") or 0) > 0
        if protected:
            return (3, slot_name)
        if status in {"missing", "stopped", "unknown", ""}:
            return (0, slot_name)
        if status in {"creating", "allocating"}:
            return (1, slot_name)
        return (2, slot_name)

    def diversified_candidate(
        org_label: str,
        slot_name: str,
        slot_index: int,
        org_index: int,
        *,
        skip_profile_key: str | None = None,
        min_profit_day: float | None = None,
        allow_availability_probe: bool = False,
    ) -> tuple[int, dict[str, Any]] | None:
        for offset in range(len(profiles)):
            profile_index = (slot_index - 1 + org_index * 3 + offset) % len(profiles)
            candidate = profiles[profile_index]
            candidate_key = str(candidate["profile_key"])
            if skip_profile_key and candidate_key == skip_profile_key:
                continue
            if min_profit_day is not None and float(candidate["expected_profit_day"]) < min_profit_day:
                continue
            if has_capacity(
                org_label,
                slot_name,
                candidate_key,
                allow_availability_probe=allow_availability_probe,
            ):
                return profile_index, candidate
        return None

    def fill_candidate(
        org_label: str,
        slot_name: str,
        slot_index: int,
        org_index: int,
        *,
        skip_profile_key: str | None = None,
        min_profit_day: float | None = None,
    ) -> tuple[int, dict[str, Any], bool] | None:
        selected = diversified_candidate(
            org_label,
            slot_name,
            slot_index,
            org_index,
            skip_profile_key=skip_profile_key,
            min_profit_day=min_profit_day,
        )
        if selected is not None:
            profile_index, profile = selected
            return profile_index, profile, False
        selected = diversified_candidate(
            org_label,
            slot_name,
            slot_index,
            org_index,
            skip_profile_key=skip_profile_key,
            min_profit_day=min_profit_day,
            allow_availability_probe=True,
        )
        if selected is None:
            return None
        profile_index, profile = selected
        return profile_index, profile, True

    for org_index, org in enumerate(enabled_orgs):
        ordered_slots = sorted(org.slot_names(), key=lambda slot_name: slot_sort_key(org.label, slot_name))
        for slot_index, slot_name in enumerate(ordered_slots, start=1):
            slot_row = slot_rows.get((org.label, slot_name), {})
            guard_target = guard_targets.get((org.label, slot_name))
            if guard_target:
                profile_key = str(guard_target["profile_key"])
                score = scores_by_key.get(profile_key, {})
                assigned_by_org_profile[(org.label, profile_key)] = (
                    assigned_by_org_profile.get((org.label, profile_key), 0) + 1
                )
                targets.append(
                    {
                        "org_label": org.label,
                        "slot_name": slot_name,
                        "slot_index": slot_index,
                        "profile_key": profile_key,
                        "gpu_key": guard_target["gpu_key"],
                        "priority": guard_target["priority"],
                        "memory_mb": guard_target["memory_mb"],
                        "mode": guard_target["mode"],
                        "decision_price_usd": decision_price_usd,
                        "expected_profit_day": float(
                            score.get("expected_profit_day", guard_target["expected_profit_day"])
                        ),
                        "protected": False,
                        "reason": guard_target["reason"],
                        "assigned_at_utc": assigned_at,
                    }
                )
                continue
            observed_profile = slot_row.get("observed_profile_key")
            observed_status = str(slot_row.get("observed_status") or "")
            protected = int(slot_row.get("protected") or 0) > 0
            live_hashrate_th = float(slot_row.get("live_hashrate_th") or 0)
            protected_live_hashing = protected and live_hashrate_th > 0
            pending_observed = observed_status in {"creating", "allocating", "deploying"}
            pending_age = _age_seconds(
                slot_row.get("observed_profile_since_utc") or slot_row.get("observed_status_since_utc")
            )
            pending_protect = (
                pending_observed
                and mode != "optimize"
                and pending_target_protect_seconds > 0
                and not is_in_cooldown(org.label, slot_name, str(observed_profile))
                and (pending_age is None or pending_age < pending_target_protect_seconds)
            )
            if (protected or pending_observed) and observed_profile and observed_profile in scores_by_key:
                current = scores_by_key[str(observed_profile)]
                selected = None
                current_profit = float(current["expected_profit_day"])
                if current_profit < 0:
                    selected = fill_candidate(
                        org.label,
                        slot_name,
                        slot_index,
                        org_index,
                        skip_profile_key=str(observed_profile),
                    )
                    if selected is not None:
                        profile_index, profile, used_probe_fallback = selected
                        protected = False
                        reason = f"{mode}:replace_negative_observed_profile:{observed_profile}"
                        if used_probe_fallback:
                            reason += ":availability_probe_fallback"
                    else:
                        profile = current
                        profile_index = 0
                        reason = f"{mode}:negative_observed_profile_no_replacement"
                elif pending_protect and current_profit >= min_profit_day:
                    profile = current
                    profile_index = 0
                    protected = False
                    age_text = "unknown" if pending_age is None else f"{pending_age:.1f}"
                    reason = (
                        f"{mode}:protected_pending_observed_profile:{observed_profile}:"
                        f"age_{age_text}_lt_{pending_target_protect_seconds}"
                    )
                elif not protected_live_hashing:
                    selected = fill_candidate(
                        org.label,
                        slot_name,
                        slot_index,
                        org_index,
                        skip_profile_key=str(observed_profile),
                    )
                    if selected is not None:
                        profile_index, profile, used_probe_fallback = selected
                        protected = False
                        reason = f"{mode}:replace_nohash_observed_profile:{observed_profile}"
                        if used_probe_fallback:
                            reason += ":availability_probe_fallback"
                    else:
                        profile = current
                        profile_index = 0
                        protected = False
                        reason = f"{mode}:nohash_observed_profile_no_replacement"
                elif mode == "optimize":
                    min_upgrade_profit = (
                        current_profit + float(config.risk.optimize_min_upgrade_delta_day)
                    )
                    selected = diversified_candidate(
                        org.label,
                        slot_name,
                        slot_index,
                        org_index,
                        skip_profile_key=str(observed_profile),
                        min_profit_day=min_upgrade_profit,
                    )
                    if selected is not None:
                        profile_index, profile = selected
                        protected = False
                        delta = float(profile["expected_profit_day"]) - current_profit
                        reason = f"{mode}:upgrade_from_{observed_profile}:delta_{delta:.3f}"
                    else:
                        profile = current
                        profile_index = 0
                        reason = f"{mode}:protected_observed_profile"
                else:
                    profile = current
                    profile_index = 0
                    reason = f"{mode}:protected_observed_profile"
            else:
                selected = fill_candidate(org.label, slot_name, slot_index, org_index)
                if selected is None:
                    continue
                profile_index, profile, used_probe_fallback = selected
                reason = f"{mode}:diversified_rank_{profile_index + 1}_of_{len(profiles)}"
                if used_probe_fallback:
                    reason += ":availability_probe_fallback"
            assigned_by_org_profile[(org.label, str(profile["profile_key"]))] = (
                assigned_by_org_profile.get((org.label, str(profile["profile_key"])), 0) + 1
            )
            targets.append(
                {
                    "org_label": org.label,
                    "slot_name": slot_name,
                    "slot_index": slot_index,
                    "profile_key": profile["profile_key"],
                    "gpu_key": profile["gpu_key"],
                    "priority": profile["priority"],
                    "memory_mb": profile["memory_mb"],
                    "mode": mode,
                    "decision_price_usd": decision_price_usd,
                    "expected_profit_day": float(profile["expected_profit_day"]),
                    "protected": protected,
                    "reason": reason,
                    "assigned_at_utc": assigned_at,
                }
            )
    return targets


def active_guard_targets(conn) -> dict[tuple[str, str], dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT t.*, p.gpu_key, p.priority, p.memory_mb, p.label
        FROM slot_targets t
        JOIN guard_issues g ON g.org_label = t.org_label AND g.slot_name = t.slot_name
        LEFT JOIN gpu_profiles p ON p.profile_key = t.profile_key
        WHERE t.reason LIKE 'guard_%'
        ORDER BY t.org_label, t.slot_name
        """
    ).fetchall()
    targets = {}
    for row in rows:
        target = dict(row)
        parts = str(target["profile_key"]).split(":")
        if not target.get("gpu_key") and parts:
            target["gpu_key"] = parts[0]
        if not target.get("priority") and len(parts) > 1:
            target["priority"] = parts[1]
        if not target.get("memory_mb") and len(parts) > 2:
            target["memory_mb"] = int(parts[2])
        if not target.get("label"):
            target["label"] = str(target["profile_key"])
        targets[(str(row["org_label"]), str(row["slot_name"]))] = target
    return targets


def schedule_once(
    *,
    db_path: str | None = None,
    mode: str | None = None,
    price: float | None = None,
    fee: float | None = None,
    gross_prl_per_th_day: float | None = None,
    dry_run: bool = False,
    width: int = 10,
) -> dict[str, Any]:
    config = load_config()
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        state_db.sync_config(conn, config)
        risk = state_db.latest_risk_mode(conn)
        slot_rows = {
            (str(row["org_label"]), str(row["slot_name"])): dict(row)
            for row in conn.execute("SELECT * FROM slots").fetchall()
        }
        availability = state_db.latest_profile_availability(conn)
        cooldowns = state_db.active_search_cooldowns(conn)
        guard_targets = active_guard_targets(conn)
        db_mode = str(risk["mode"]) if risk else None
        db_price = float(risk["decision_price_usd"]) if risk else config.risk.decision_price_for_mode()
        selected_mode = _scheduler_mode(config, mode or db_mode)
        decision_price = price if price is not None else db_price
        selected_fee = fee if fee is not None else (float(risk["pearl_fee_rate"]) if risk else config.risk.effective_fee_rate())

    scores = profile_scorer.score_profiles(
        db_path=db_path,
        mode=selected_mode,
        decision_price_usd=decision_price,
        gross_prl_per_th_day=gross_prl_per_th_day,
        pearl_fee_rate=selected_fee,
        write=not dry_run,
    )
    targets = build_targets(
        config,
        scores,
        mode=selected_mode,
        decision_price_usd=decision_price,
        width=width,
        slot_rows=slot_rows,
        availability=availability,
        cooldowns=cooldowns,
        guard_targets=guard_targets,
    )

    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        if not dry_run:
            conn.execute("DELETE FROM slot_targets")
            for target in targets:
                state_db.set_slot_target(conn, target)
            state_db.write_heartbeat(
                conn,
                "fleet_scheduler",
                payload={
                    "mode": selected_mode,
                    "decision_price_usd": decision_price,
                    "targets": len(targets),
                    "target_slots": config.target_slot_count(),
                },
            )
            state_db.record_event(
                conn,
                "slot_targets_assigned",
                source="fleet_scheduler",
                message="central scheduler assigned slot targets",
                payload={
                    "mode": selected_mode,
                    "targets": len(targets),
                    "profiles": sorted({target["profile_key"] for target in targets}),
                    "dry_run": dry_run,
                },
            )
            conn.commit()

    profile_counts: dict[str, int] = {}
    for target in targets:
        profile_counts[target["profile_key"]] = profile_counts.get(target["profile_key"], 0) + 1
    return {
        "mode": selected_mode,
        "decision_price_usd": decision_price,
        "pearl_fee_rate": selected_fee,
        "target_slots": config.target_slot_count(),
        "assigned_targets": len(targets),
        "dry_run": dry_run,
        "profile_counts": profile_counts,
        "targets": targets,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Central deterministic scheduler for Salad PRL fleet targets.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mode", default=None)
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument("--fee", type=float, default=None)
    parser.add_argument("--gross-prl-per-th-day", type=float, default=None)
    parser.add_argument("--width", type=int, default=10, help="Number of top profiles to diversify across.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    def run_and_print() -> None:
        payload = schedule_once(
            db_path=args.db,
            mode=args.mode,
            price=args.price,
            fee=args.fee,
            gross_prl_per_th_day=args.gross_prl_per_th_day,
            dry_run=args.dry_run,
            width=args.width,
        )
        if args.json:
            print(json_dumps(payload))
            return
        print(
            f"mode={payload['mode']} targets={payload['assigned_targets']}/{payload['target_slots']} "
            f"price={float(payload['decision_price_usd']):.4f} fee={float(payload['pearl_fee_rate']):.2%}"
        )
        for profile_key, count in sorted(payload["profile_counts"].items(), key=lambda item: item[1], reverse=True):
            print(f"{count:>3} {profile_key}")

    if args.loop:
        while True:
            run_and_print()
            time.sleep(args.interval)
    else:
        run_and_print()


if __name__ == "__main__":
    main()
