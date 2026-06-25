#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import time
from typing import Any

import state_db
from config_loader import load_config
from fleet_common import compact_json, json_dumps, safe_public_payload, utc_now


PORTAL_API = "https://portal-api.salad.com/api/portal"
DEFAULT_SESSION = "salad-login"
DEFAULT_BALANCE_FILE = pathlib.Path("state/salad_balances.json")


class PortalBalanceError(RuntimeError):
    pass


def agent_browser_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("AGENT_BROWSER_ALLOWED_DOMAINS", "salad.com,*.salad.com")
    env.setdefault("AGENT_BROWSER_CONTENT_BOUNDARIES", "1")
    return env


def extract_json_from_output(output: str) -> Any:
    lines = output.splitlines()
    for start_index, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith(("{", "[")):
            continue
        collected: list[str] = []
        for line in lines[start_index:]:
            if line.startswith("--- END_AGENT_BROWSER_PAGE_CONTENT"):
                break
            collected.append(line)
            candidate = "\n".join(collected).strip()
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    raise PortalBalanceError("agent-browser output did not contain JSON")


def run_agent_browser(args: list[str], *, cwd: pathlib.Path, timeout: int = 90) -> str:
    result = subprocess.run(
        ["npx", "agent-browser", *args],
        cwd=str(cwd),
        env=agent_browser_env(),
        text=True,
        input=None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise PortalBalanceError(f"agent-browser failed rc={result.returncode}: {result.stdout[-600:]}")
    return result.stdout


def portal_eval(js: str, *, session: str, cwd: pathlib.Path, timeout: int = 90) -> Any:
    result = subprocess.run(
        ["npx", "agent-browser", "--session", session, "eval", "--stdin"],
        cwd=str(cwd),
        env=agent_browser_env(),
        text=True,
        input=js,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise PortalBalanceError(f"agent-browser eval failed rc={result.returncode}: {result.stdout[-600:]}")
    return extract_json_from_output(result.stdout)


def fetch_portal_balances(
    *,
    session: str = DEFAULT_SESSION,
    cwd: pathlib.Path | None = None,
    timeout: int = 90,
) -> dict[str, Any]:
    run_cwd = cwd or pathlib.Path.cwd()
    run_agent_browser(
        ["--session", session, "open", "https://portal.salad.com/organizations"],
        cwd=run_cwd,
        timeout=timeout,
    )
    js = f"""
(async () => {{
  const orgResponse = await fetch('{PORTAL_API}/organizations', {{ credentials: 'include' }});
  const orgText = await orgResponse.text();
  if (!orgResponse.ok) {{
    return {{ ok: false, status: orgResponse.status, error: orgText.slice(0, 400), balances: [] }};
  }}
  let orgPayload = {{}};
  try {{ orgPayload = JSON.parse(orgText); }} catch (err) {{
    return {{ ok: false, status: orgResponse.status, error: 'invalid organizations json', balances: [] }};
  }}
  const orgs = Array.isArray(orgPayload.items) ? orgPayload.items : [];
  const balances = [];
  for (const org of orgs) {{
    const name = org.name || org.display_name || org.id;
    const url = `{PORTAL_API}/organizations/${{encodeURIComponent(name)}}/billing-profile/credits-balance`;
    const response = await fetch(url, {{ credentials: 'include' }});
    const text = await response.text();
    let parsed = {{}};
    try {{ parsed = JSON.parse(text); }} catch (_err) {{}}
    const amountCents = Number.isFinite(parsed.amount) ? parsed.amount : null;
    balances.push({{
      org: name,
      org_id: org.id || null,
      display_name: org.display_name || null,
      status: response.status,
      ok: response.ok,
      amount_cents: amountCents,
      balance_usd: Number.isFinite(amountCents) ? amountCents / 100 : null,
      error: response.ok ? null : text.slice(0, 400)
    }});
  }}
  return {{ ok: true, status: orgResponse.status, checked_at_utc: new Date().toISOString(), balances }};
}})()
"""
    payload = portal_eval(js, session=session, cwd=run_cwd, timeout=timeout)
    if not isinstance(payload, dict):
        raise PortalBalanceError("portal response was not a JSON object")
    return payload


def normalize_balances(payload: dict[str, Any]) -> dict[str, float]:
    if not payload.get("ok"):
        raise PortalBalanceError(str(payload.get("error") or f"portal status {payload.get('status')}"))
    balances: dict[str, float] = {}
    for row in payload.get("balances") or []:
        if not isinstance(row, dict) or not row.get("ok"):
            continue
        org = str(row.get("org") or "").strip()
        value = row.get("balance_usd")
        if not org or value is None:
            continue
        balances[org] = round(float(value), 2)
    return balances


def write_balance_file(path: pathlib.Path, balances: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(compact_json(dict(sorted(balances.items()))) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def record_refresh(
    *,
    db_path: str | None,
    payload: dict[str, Any],
    balances: dict[str, float],
    balance_file: pathlib.Path,
) -> dict[str, Any]:
    config = load_config()
    enabled_orgs = [org.label for org in config.enabled_orgs()]
    missing_enabled_orgs = [org for org in enabled_orgs if org not in balances]
    status = "degraded" if missing_enabled_orgs else "ok"
    public_payload = {
        "org_count": len(balances),
        "portal_org_count": len(payload.get("balances") or []),
        "balance_file": str(balance_file),
        "missing_enabled_orgs": missing_enabled_orgs,
        "portal_status": payload.get("status"),
        "checked_at_utc": payload.get("checked_at_utc"),
    }
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
        state_db.write_heartbeat(
            conn,
            "portal_balances",
            status=status,
            stale_after_seconds=1800,
            payload=public_payload,
        )
        state_db.record_event(
            conn,
            "portal_balances_refreshed",
            source="portal_balances",
            level="warning" if missing_enabled_orgs else "info",
            message="Salad portal balances refreshed",
            payload=public_payload,
        )
        conn.commit()
    return {"status": status, **public_payload, "balances": balances}


def record_failure(db_path: str | None, exc: Exception) -> None:
    try:
        with state_db.connect(db_path) as conn:
            state_db.init_db(conn)
            state_db.record_failure(
                conn,
                "portal_balances",
                severity="warning",
                error_type=type(exc).__name__,
                message=str(exc)[:300],
                payload={},
            )
            state_db.write_heartbeat(
                conn,
                "portal_balances",
                status="degraded",
                stale_after_seconds=1800,
                payload={"error_type": type(exc).__name__, "message": str(exc)[:300], "at_utc": utc_now()},
            )
            conn.commit()
    except Exception:
        return


def run_once(
    *,
    db_path: str | None = None,
    balance_file: pathlib.Path = DEFAULT_BALANCE_FILE,
    session: str = DEFAULT_SESSION,
    cwd: pathlib.Path | None = None,
    timeout: int = 90,
) -> dict[str, Any]:
    payload = fetch_portal_balances(session=session, cwd=cwd, timeout=timeout)
    balances = normalize_balances(payload)
    write_balance_file(balance_file, balances)
    return record_refresh(db_path=db_path, payload=payload, balances=balances, balance_file=balance_file)


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh private Salad portal balance file for fleet audits.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--balance-file", default=str(DEFAULT_BALANCE_FILE))
    parser.add_argument("--session", default=DEFAULT_SESSION)
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=900)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    while True:
        try:
            payload = run_once(
                db_path=args.db,
                balance_file=pathlib.Path(args.balance_file),
                session=args.session,
                cwd=pathlib.Path(args.cwd),
                timeout=args.timeout,
            )
        except Exception as exc:
            record_failure(args.db, exc)
            if args.json:
                print(json_dumps({"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:300]}), flush=True)
            else:
                print(f"portal_balances failed error={type(exc).__name__}: {str(exc)[:180]}", flush=True)
            if args.once or not args.loop:
                raise SystemExit(1) from exc
            time.sleep(max(1, args.interval))
            continue

        public_payload = safe_public_payload({"ok": True, **payload})
        if args.json:
            print(json_dumps(public_payload), flush=True)
        else:
            print(
                f"portal_balances status={payload['status']} orgs={payload['org_count']} "
                f"missing={','.join(payload['missing_enabled_orgs']) or '-'}",
                flush=True,
            )
        if args.once or not args.loop:
            break
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    main()
