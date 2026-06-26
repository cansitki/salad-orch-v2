#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import time
from dataclasses import dataclass
from typing import Any

import portal_balances
import state_db
from config_loader import load_config
from fleet_common import env_bool, env_float, env_int, json_dumps, load_env_file, safe_public_payload, utc_now


DEFAULT_BALANCE_FILE = pathlib.Path("state/salad_balances.json")
DEFAULT_ACCOUNT_STATE_DIR = pathlib.Path("state/portal_balance_accounts")
DEFAULT_PASSWORD_ENV = "SALAD_PORTAL_PASSWORD"


def default_interval_seconds() -> int:
    return max(1, int(os.environ.get("PRL_PORTAL_BALANCE_INTERVAL_SECONDS", "60")))


@dataclass(frozen=True)
class PortalAccount:
    label: str
    email: str
    password_env: str = DEFAULT_PASSWORD_ENV
    cookie_jar: pathlib.Path | None = None
    balance_file: pathlib.Path | None = None


def safe_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9]+", "_", value.strip().lower()).strip("_")
    return label or "account"


def default_cookie_jar(label: str, state_dir: pathlib.Path = DEFAULT_ACCOUNT_STATE_DIR) -> pathlib.Path:
    return state_dir / f"{label}_cookies.txt"


def default_account_balance_file(label: str, state_dir: pathlib.Path = DEFAULT_ACCOUNT_STATE_DIR) -> pathlib.Path:
    return state_dir / f"{label}_balances.json"


def account_from_payload(payload: str | dict[str, Any]) -> PortalAccount:
    if isinstance(payload, str):
        email = payload.strip()
        label = safe_label(email)
        return PortalAccount(label=label, email=email)
    email = str(payload["email"]).strip()
    label = safe_label(str(payload.get("label") or email))
    cookie_jar = pathlib.Path(str(payload["cookie_jar"])) if payload.get("cookie_jar") else None
    balance_file = pathlib.Path(str(payload["balance_file"])) if payload.get("balance_file") else None
    return PortalAccount(
        label=label,
        email=email,
        password_env=str(payload.get("password_env") or DEFAULT_PASSWORD_ENV),
        cookie_jar=cookie_jar,
        balance_file=balance_file,
    )


def email_from_account_label(label: str) -> str | None:
    parts = [part for part in label.split("_") if part]
    if len(parts) < 3:
        return None
    domain = ".".join(parts[-2:])
    local = "_".join(parts[:-2])
    if not local or not domain:
        return None
    return f"{local}@{domain}"


def discover_accounts_from_state_dir(state_dir: pathlib.Path = DEFAULT_ACCOUNT_STATE_DIR) -> list[PortalAccount]:
    if not state_dir.exists():
        return []
    accounts: list[PortalAccount] = []
    seen: set[str] = set()
    for cookie_jar in sorted(state_dir.glob("*_cookies.txt")):
        label = cookie_jar.name[: -len("_cookies.txt")]
        if not label or label in seen:
            continue
        email = email_from_account_label(label)
        if not email:
            continue
        seen.add(label)
        accounts.append(
            PortalAccount(
                label=label,
                email=email,
                cookie_jar=cookie_jar,
                balance_file=default_account_balance_file(label, state_dir),
            )
        )
    return accounts


def load_accounts(
    *,
    accounts_json: str | None = None,
    emails: str | None = None,
    account_state_dir: pathlib.Path = DEFAULT_ACCOUNT_STATE_DIR,
) -> list[PortalAccount]:
    raw_json = accounts_json or os.environ.get("SALAD_PORTAL_BALANCE_ACCOUNTS_JSON")
    if raw_json:
        payload = json.loads(raw_json)
        if isinstance(payload, dict):
            payload = payload.get("accounts") or []
        return [account_from_payload(item) for item in payload]
    raw_emails = emails or os.environ.get("SALAD_PORTAL_BALANCE_EMAILS")
    if raw_emails:
        return [account_from_payload(email) for email in raw_emails.split(",") if email.strip()]
    discovered_accounts = discover_accounts_from_state_dir(account_state_dir)
    if discovered_accounts:
        return discovered_accounts
    fallback_email = os.environ.get("SALAD_PORTAL_EMAIL")
    return [account_from_payload(fallback_email)] if fallback_email else []


def restored_positive_balance_orgs(
    previous: dict[str, float],
    current: dict[str, float],
    *,
    threshold: float | None = None,
) -> list[str]:
    selected_threshold = env_float("PRL_PORTAL_BALANCE_RESTORE_THRESHOLD_USD", 0.0) if threshold is None else threshold
    restored = []
    for org, value in sorted(current.items()):
        try:
            current_value = float(value)
            previous_value = float(previous.get(org, 0.0))
        except (TypeError, ValueError):
            continue
        if previous_value <= selected_threshold < current_value:
            restored.append(str(org))
    return restored


def wake_availability_on_balance_restore(
    *,
    db_path: str | None,
    restored_orgs: list[str],
) -> dict[str, Any] | None:
    if not restored_orgs or not env_bool("PRL_PORTAL_BALANCE_WAKE_AVAILABILITY", True):
        return None
    priorities = tuple(
        item.strip().lower()
        for item in os.environ.get("PRL_PORTAL_BALANCE_WAKE_PRIORITIES", "batch,low").split(",")
        if item.strip()
    )
    try:
        import availability_probe

        payload = availability_probe.run_once(
            db_path=db_path,
            priorities=priorities or ("batch", "low"),
            org_parallelism=env_int("PRL_PORTAL_BALANCE_WAKE_ORG_PARALLELISM", 2),
            profile_parallelism=env_int("PRL_PORTAL_BALANCE_WAKE_PROFILE_PARALLELISM", 4),
        )
    except Exception as exc:
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc)[:240],
            "restored_positive_balance_orgs": restored_orgs,
        }
    return {
        "ok": True,
        "restored_positive_balance_orgs": restored_orgs,
        "probed": int(payload.get("probed") or 0),
        "by_profile": payload.get("by_profile") or {},
        "skipped_zero_balance_count": len(payload.get("skipped_zero_balance_orgs") or []),
        "skipped_zero_quota_count": len(payload.get("skipped_zero_replica_quota_orgs") or []),
        "cleared_no_credits_count": len(payload.get("cleared_no_credits_cooldowns") or []),
    }


def refresh_account(
    account: PortalAccount,
    *,
    cwd: pathlib.Path,
    timeout: int,
    force_login: bool,
    account_state_dir: pathlib.Path,
) -> dict[str, Any]:
    password = os.environ.get(account.password_env)
    if not password:
        raise portal_balances.PortalBalanceError(f"missing password env {account.password_env}")
    label = account.label
    balance_file = account.balance_file or default_account_balance_file(label, account_state_dir)
    payload = portal_balances.fetch_portal_balances(
        session=f"salad-login-{label}",
        cwd=cwd,
        timeout=timeout,
        cookie_jar=account.cookie_jar or default_cookie_jar(label, account_state_dir),
        portal_email=account.email,
        portal_password=password,
        force_login=force_login,
    )
    balances = portal_balances.normalize_balances(payload)
    balances, stale_balance_orgs = portal_balances.merge_existing_balances_for_partial_failures(
        payload=payload,
        balances=balances,
        balance_file=balance_file,
    )
    portal_balances.write_balance_file(balance_file, balances)
    return {
        "status": "degraded" if stale_balance_orgs else "ok",
        "org_count": len(balances),
        "portal_org_count": len(payload.get("balances") or []),
        "checked_at_utc": payload.get("checked_at_utc"),
        "balances": balances,
        "stale_balance_orgs": stale_balance_orgs,
    }


def record_multi_refresh(
    *,
    db_path: str | None,
    balance_file: pathlib.Path,
    balances: dict[str, float],
    account_results: list[dict[str, Any]],
    account_errors: list[dict[str, str]],
    restored_positive_balance_orgs: list[str] | None = None,
    availability_wake: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = load_config()
    enabled_orgs = [org.label for org in config.enabled_orgs()]
    missing_enabled_orgs = [org for org in enabled_orgs if org not in balances]
    status = "degraded" if missing_enabled_orgs or account_errors else "ok"
    public_payload = {
        "account_count": len(account_results) + len(account_errors),
        "successful_accounts": [row["label"] for row in account_results],
        "failed_accounts": [row["label"] for row in account_errors],
        "org_count": len(balances),
        "balance_file": str(balance_file),
        "missing_enabled_orgs": missing_enabled_orgs,
        "checked_at_utc": utc_now(),
        "restored_positive_balance_orgs": restored_positive_balance_orgs or [],
        "availability_wake": availability_wake,
    }
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        if account_errors:
            state_db.record_failure(
                conn,
                "portal_balances",
                severity="warning",
                error_type="PortalMultiBalancePartialFailure",
                message=f"{len(account_errors)} portal balance account refreshes failed",
                payload={**public_payload, "errors": account_errors},
            )
        else:
            state_db.clear_failure(conn, "portal_balances")
        state_db.write_heartbeat(
            conn,
            "portal_balances",
            status=status,
            stale_after_seconds=1800,
            payload=public_payload,
        )
        state_db.record_event(
            conn,
            "portal_multi_balances_refreshed",
            source="portal_multi_balances",
            level="warning" if status != "ok" else "info",
            message="Salad portal balances refreshed across accounts",
            payload=public_payload,
        )
        conn.commit()
    return {"status": status, **public_payload, "balances": balances, "account_errors": account_errors}


def run_once(
    *,
    db_path: str | None = None,
    balance_file: pathlib.Path = DEFAULT_BALANCE_FILE,
    account_state_dir: pathlib.Path = DEFAULT_ACCOUNT_STATE_DIR,
    accounts_json: str | None = None,
    emails: str | None = None,
    cwd: pathlib.Path | None = None,
    timeout: int = 90,
    force_login: bool = False,
    preserve_existing_on_failure: bool = True,
) -> dict[str, Any]:
    accounts = load_accounts(accounts_json=accounts_json, emails=emails, account_state_dir=account_state_dir)
    if not accounts:
        raise portal_balances.PortalBalanceError("no portal balance accounts configured")
    account_state_dir.mkdir(parents=True, exist_ok=True)
    run_cwd = cwd or pathlib.Path.cwd()
    previous_balances = portal_balances.load_existing_balance_file(balance_file)
    balances = dict(previous_balances) if preserve_existing_on_failure else {}
    account_results: list[dict[str, Any]] = []
    account_errors: list[dict[str, str]] = []
    for account in accounts:
        try:
            payload = refresh_account(
                account,
                cwd=run_cwd,
                timeout=timeout,
                force_login=force_login,
                account_state_dir=account_state_dir,
            )
        except Exception as exc:
            account_errors.append(
                {
                    "label": account.label,
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:240],
                }
            )
            continue
        account_balances = {
            str(org): round(float(value), 2)
            for org, value in (payload.get("balances") or {}).items()
            if value is not None
        }
        balances.update(account_balances)
        account_results.append(
            {
                "label": account.label,
                "org_count": len(account_balances),
                "status": str(payload.get("status") or "unknown"),
            }
        )
    portal_balances.write_balance_file(balance_file, balances)
    restored_orgs = restored_positive_balance_orgs(previous_balances, balances)
    availability_wake = wake_availability_on_balance_restore(
        db_path=db_path,
        restored_orgs=restored_orgs,
    )
    return record_multi_refresh(
        db_path=db_path,
        balance_file=balance_file,
        balances=balances,
        account_results=account_results,
        account_errors=account_errors,
        restored_positive_balance_orgs=restored_orgs,
        availability_wake=availability_wake,
    )


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser(description="Refresh and merge Salad portal balances across accounts.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--balance-file", default=str(DEFAULT_BALANCE_FILE))
    parser.add_argument("--account-state-dir", default=str(DEFAULT_ACCOUNT_STATE_DIR))
    parser.add_argument("--accounts-json", default=None)
    parser.add_argument("--emails", default=None, help="Comma-separated portal account emails.")
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--force-login", action="store_true")
    parser.add_argument("--no-preserve-existing-on-failure", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=default_interval_seconds())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    while True:
        try:
            payload = run_once(
                db_path=args.db,
                balance_file=pathlib.Path(args.balance_file),
                account_state_dir=pathlib.Path(args.account_state_dir),
                accounts_json=args.accounts_json,
                emails=args.emails,
                cwd=pathlib.Path(args.cwd),
                timeout=args.timeout,
                force_login=args.force_login,
                preserve_existing_on_failure=not args.no_preserve_existing_on_failure,
            )
        except Exception as exc:
            if args.json:
                print(json_dumps({"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:300]}), flush=True)
            else:
                print(f"portal_multi_balances failed error={type(exc).__name__}: {str(exc)[:180]}", flush=True)
            if args.once or not args.loop:
                raise SystemExit(1) from exc
            time.sleep(max(1, args.interval))
            continue
        public_payload = safe_public_payload({"ok": True, **payload})
        if args.json:
            print(json_dumps(public_payload), flush=True)
        else:
            print(
                f"portal_multi_balances status={payload['status']} accounts={payload['account_count']} "
                f"orgs={payload['org_count']} missing={','.join(payload['missing_enabled_orgs']) or '-'}",
                flush=True,
            )
        if args.once or not args.loop:
            break
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    main()
