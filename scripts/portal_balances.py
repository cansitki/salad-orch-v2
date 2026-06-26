#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

import state_db
from config_loader import load_config
from fleet_common import compact_json, json_dumps, load_env_file, safe_public_payload, utc_now


PORTAL_API = "https://portal-api.salad.com/api/portal"
DEFAULT_SESSION = "salad-login"
DEFAULT_BALANCE_FILE = pathlib.Path("state/salad_balances.json")
DEFAULT_COOKIE_JAR = pathlib.Path("state/portal_cookies.txt")
DEFAULT_PORTAL_EMAIL_ENV = "SALAD_PORTAL_EMAIL"
DEFAULT_PORTAL_PASSWORD_ENV = "SALAD_PORTAL_PASSWORD"


def default_interval_seconds() -> int:
    return max(1, int(os.environ.get("PRL_PORTAL_BALANCE_INTERVAL_SECONDS", "60")))


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


def load_cookie_jar(path: pathlib.Path) -> http.cookiejar.MozillaCookieJar:
    jar = http.cookiejar.MozillaCookieJar(str(path))
    if path.exists():
        jar.load(ignore_discard=True, ignore_expires=True)
    return jar


def save_cookie_jar(jar: http.cookiejar.MozillaCookieJar, path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    jar.save(ignore_discard=True, ignore_expires=True)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def http_json_request(
    *,
    url: str,
    jar: http.cookiejar.MozillaCookieJar,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Origin": "https://portal.salad.com",
        "Referer": "https://portal.salad.com/",
        "User-Agent": "salad-orch-v2/portal-balances",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        with opener.open(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        status = int(exc.code)
    except urllib.error.URLError as exc:
        raise PortalBalanceError(f"portal HTTP request failed: {type(exc.reason).__name__}") from exc

    try:
        payload = json.loads(text) if text else {}
    except json.JSONDecodeError:
        payload = {"raw": text[:400]}
    if not isinstance(payload, dict):
        payload = {"items": payload}
    payload["ok"] = 200 <= status < 300
    payload["status"] = status
    return payload


def fetch_portal_balances_http(
    *,
    cookie_jar: pathlib.Path,
    email: str | None = None,
    password: str | None = None,
    force_login: bool = False,
    timeout: int = 30,
) -> dict[str, Any]:
    jar = load_cookie_jar(cookie_jar)
    if force_login:
        if not email or not password:
            raise PortalBalanceError("portal force-login requested without credentials")
        login_payload = http_json_request(
            url=f"{PORTAL_API}/users/login",
            jar=jar,
            method="POST",
            body={"email": email, "password": password},
            timeout=timeout,
        )
        if not login_payload.get("ok"):
            raise PortalBalanceError(f"portal login failed status {login_payload.get('status')}")
        save_cookie_jar(jar, cookie_jar)
    org_payload = http_json_request(url=f"{PORTAL_API}/organizations", jar=jar, timeout=timeout)
    if int(org_payload.get("status") or 0) in {401, 403} and email and password:
        login_payload = http_json_request(
            url=f"{PORTAL_API}/users/login",
            jar=jar,
            method="POST",
            body={"email": email, "password": password},
            timeout=timeout,
        )
        if not login_payload.get("ok"):
            raise PortalBalanceError(f"portal login failed status {login_payload.get('status')}")
        save_cookie_jar(jar, cookie_jar)
        org_payload = http_json_request(url=f"{PORTAL_API}/organizations", jar=jar, timeout=timeout)
    if not org_payload.get("ok"):
        raise PortalBalanceError(f"portal organizations status {org_payload.get('status')}")

    orgs = org_payload.get("items") if isinstance(org_payload.get("items"), list) else []
    balances = []
    for org in orgs:
        if not isinstance(org, dict):
            continue
        name = org.get("name") or org.get("display_name") or org.get("id")
        if not name:
            continue
        balance_payload = http_json_request(
            url=f"{PORTAL_API}/organizations/{urllib.parse.quote(str(name), safe='')}/billing-profile/credits-balance",
            jar=jar,
            timeout=timeout,
        )
        amount_cents = balance_payload.get("amount")
        amount_cents = amount_cents if isinstance(amount_cents, (int, float)) else None
        balances.append(
            {
                "org": name,
                "org_id": org.get("id"),
                "display_name": org.get("display_name"),
                "status": balance_payload.get("status"),
                "ok": bool(balance_payload.get("ok")),
                "amount_cents": amount_cents,
                "balance_usd": amount_cents / 100 if amount_cents is not None else None,
                "error": None if balance_payload.get("ok") else str(balance_payload)[:400],
            }
        )
    save_cookie_jar(jar, cookie_jar)
    return {"ok": True, "status": org_payload.get("status"), "checked_at_utc": utc_now(), "balances": balances}


def fetch_portal_balances(
    *,
    session: str = DEFAULT_SESSION,
    cwd: pathlib.Path | None = None,
    timeout: int = 90,
    cookie_jar: pathlib.Path | None = None,
    portal_email: str | None = None,
    portal_password: str | None = None,
    force_login: bool = False,
) -> dict[str, Any]:
    run_cwd = cwd or pathlib.Path.cwd()
    if cookie_jar is not None:
        return fetch_portal_balances_http(
            cookie_jar=cookie_jar,
            email=portal_email,
            password=portal_password,
            force_login=force_login,
            timeout=min(timeout, 30),
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
    if isinstance(payload, dict) and payload.get("ok"):
        return payload
    if isinstance(payload, dict) and int(payload.get("status") or 0) not in {401, 403}:
        return payload
    run_agent_browser(
        ["--session", session, "open", "https://portal.salad.com/organizations"],
        cwd=run_cwd,
        timeout=timeout,
    )
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


def load_existing_balance_file(path: pathlib.Path) -> dict[str, float]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    balances: dict[str, float] = {}
    for org, value in raw.items():
        try:
            balances[str(org)] = round(float(value), 2)
        except (TypeError, ValueError):
            continue
    return balances


def merge_existing_balances_for_partial_failures(
    *,
    payload: dict[str, Any],
    balances: dict[str, float],
    balance_file: pathlib.Path,
) -> tuple[dict[str, float], list[str]]:
    previous = load_existing_balance_file(balance_file)
    merged = dict(balances)
    stale_orgs: list[str] = []
    for row in payload.get("balances") or []:
        if not isinstance(row, dict) or row.get("ok"):
            continue
        org = str(row.get("org") or "").strip()
        if org and org not in merged and org in previous:
            merged[org] = previous[org]
            stale_orgs.append(org)
    return merged, sorted(stale_orgs)


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
    stale_balance_orgs: list[str] | None = None,
) -> dict[str, Any]:
    config = load_config()
    enabled_orgs = [org.label for org in config.enabled_orgs()]
    missing_enabled_orgs = [org for org in enabled_orgs if org not in balances]
    stale_balance_orgs = stale_balance_orgs or []
    status = "degraded" if missing_enabled_orgs or stale_balance_orgs else "ok"
    public_payload = {
        "org_count": len(balances),
        "portal_org_count": len(payload.get("balances") or []),
        "balance_file": str(balance_file),
        "missing_enabled_orgs": missing_enabled_orgs,
        "stale_balance_orgs": stale_balance_orgs,
        "portal_status": payload.get("status"),
        "checked_at_utc": payload.get("checked_at_utc"),
    }
    with state_db.connect(db_path) as conn:
        state_db.init_db(conn)
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
    cookie_jar: pathlib.Path | None = None,
    portal_email: str | None = None,
    portal_password: str | None = None,
    force_login: bool = False,
) -> dict[str, Any]:
    payload = fetch_portal_balances(
        session=session,
        cwd=cwd,
        timeout=timeout,
        cookie_jar=cookie_jar,
        portal_email=portal_email,
        portal_password=portal_password,
        force_login=force_login,
    )
    balances = normalize_balances(payload)
    balances, stale_balance_orgs = merge_existing_balances_for_partial_failures(
        payload=payload,
        balances=balances,
        balance_file=balance_file,
    )
    write_balance_file(balance_file, balances)
    return record_refresh(
        db_path=db_path,
        payload=payload,
        balances=balances,
        balance_file=balance_file,
        stale_balance_orgs=stale_balance_orgs,
    )


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser(description="Refresh private Salad portal balance file for fleet audits.")
    parser.add_argument("--db", default=None)
    parser.add_argument("--balance-file", default=str(DEFAULT_BALANCE_FILE))
    parser.add_argument("--session", default=DEFAULT_SESSION)
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--cookie-jar", default=os.environ.get("SALAD_PORTAL_COOKIE_JAR"))
    parser.add_argument("--portal-email-env", default=DEFAULT_PORTAL_EMAIL_ENV)
    parser.add_argument("--portal-password-env", default=DEFAULT_PORTAL_PASSWORD_ENV)
    parser.add_argument("--portal-credentials-stdin", action="store_true")
    parser.add_argument("--force-login", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=default_interval_seconds())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    portal_email = os.environ.get(args.portal_email_env)
    portal_password = os.environ.get(args.portal_password_env)
    if args.portal_credentials_stdin:
        lines = sys.stdin.read().splitlines()
        if len(lines) < 2:
            raise SystemExit("--portal-credentials-stdin requires email and password lines")
        portal_email = lines[0].strip()
        portal_password = lines[1]

    while True:
        try:
            payload = run_once(
                db_path=args.db,
                balance_file=pathlib.Path(args.balance_file),
                session=args.session,
                cwd=pathlib.Path(args.cwd),
                timeout=args.timeout,
                cookie_jar=pathlib.Path(args.cookie_jar) if args.cookie_jar else None,
                portal_email=portal_email,
                portal_password=portal_password,
                force_login=args.force_login,
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
