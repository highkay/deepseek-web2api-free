"""
Migrate accounts from ds2api's config.json into this project's account pool.

Reads /home/admin/ds2api/config.json, logs into each email/mobile account
against chat.deepseek.com to get a fresh token+cookies pair, then adds
each to this project's account store via the Admin API.

Usage:
    python3 scripts/migrate_from_ds2api.py [--admin-url URL] [--admin-token TOKEN]

Defaults:
    --admin-url     http://localhost:28080
    --admin-token   auto-detected by logging in (requires DEEPSEEK_ADMIN_PASSWORD in .env)
"""
import os
import sys
import time
import json
import random

BASE_URL = "https://chat.deepseek.com"

_LOGIN_HEADERS = {
    "User-Agent": "DeepSeek/1.8.0 Android/35",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "x-client-platform": "android",
    "x-client-version": "1.8.0",
    "x-client-locale": "zh_CN",
    "accept-charset": "UTF-8",
}


def _login(email: str, mobile: str, password: str) -> tuple[str, str]:
    import httpx
    payload: dict = {
        "password": password.strip(),
        "device_id": "deepseek_to_api",
        "os": "android",
    }
    if email:
        payload["email"] = email.strip()
    elif mobile:
        digits = "".join(c for c in mobile if c.isdigit())
        if (mobile.startswith("+") or digits.startswith("86")) and \
                digits.startswith("86") and len(digits) == 13:
            payload["mobile"] = digits[2:]
        else:
            payload["mobile"] = digits
        payload["area_code"] = None

    resp = httpx.post(f"{BASE_URL}/api/v0/users/login", json=payload,
                      headers=_LOGIN_HEADERS, timeout=30)
    data = resp.json()
    if data.get("code", -1) != 0:
        raise RuntimeError(f"login failed: {data.get('msg')}")
    biz = data.get("data", {}).get("biz_data", {})
    if biz.get("code", -1) != 0:
        raise RuntimeError(f"login failed: {biz.get('msg')}")
    user = biz.get("user", {})
    token = (user.get("token") or "").strip()
    if not token:
        raise RuntimeError("no token in login response")
    cookies = "; ".join(f"{n}={v}" for n, v in resp.cookies.items())
    return token, cookies


def _add_via_api(api_url: str, admin_token: str, email: str, mobile: str,
                 token: str, cookies: str, password: str) -> str | None:
    import httpx
    body = {
        "token": token,
        "cookies": cookies,
        "email": email or "",
        "password": password,
        "mobile": mobile or "",
    }
    resp = httpx.post(
        f"{api_url}/admin/api/accounts",
        json=body,
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=30,
    )
    if resp.status_code == 200:
        return None
    detail = resp.text[:200]
    if resp.status_code == 400 and "already exists" in detail:
        return "duplicate"
    return detail


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Migrate accounts from ds2api config.json")
    parser.add_argument("--admin-url", default="http://localhost:28080",
                        help="Target project admin API base URL (default: http://localhost:28080)")
    parser.add_argument("--admin-token", default="",
                        help="Admin API bearer token (if empty, reads DEEPSEEK_ADMIN_PASSWORD from .env and logs in)")
    parser.add_argument("--ds2api-config", default="/home/admin/ds2api/config.json",
                        help="Path to ds2api config.json")
    parser.add_argument("--delay", type=float, default=1.5,
                        help="Delay between account logins in seconds (default: 1.5)")
    parser.add_argument("--skip-existing", action="store_true", default=True,
                        help="Skip accounts that already exist in the pool (default: true)")
    args = parser.parse_args()

    admin_token = args.admin_token
    if not admin_token:
        from dotenv import load_dotenv
        load_dotenv()
        admin_pwd = os.environ.get("DEEPSEEK_ADMIN_PASSWORD", "")
        if not admin_pwd:
            print("ERROR: No --admin-token provided and DEEPSEEK_ADMIN_PASSWORD not set in .env")
            sys.exit(1)
        import httpx
        resp = httpx.post(
            f"{args.admin_url}/admin/api/login",
            json={"password": admin_pwd},
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"ERROR: Admin login failed (HTTP {resp.status_code}): {resp.text[:200]}")
            sys.exit(1)
        admin_token = resp.json()["token"]
        print(f"Admin login OK, token: {admin_token[:16]}...")

    cfg = json.load(open(args.ds2api_config))
    accounts = cfg.get("accounts", [])
    print(f"\nLoaded {len(accounts)} accounts from {args.ds2api_config}")

    results = {"ok": 0, "dup": 0, "fail_login": 0, "fail_api": 0, "errors": []}

    for i, acc in enumerate(accounts):
        email = acc.get("email", "").strip()
        mobile = acc.get("mobile", "").strip()
        password = acc.get("password", "").strip()
        if not password:
            print(f"  [{i+1}/{len(accounts)}] SKIP (no password): {email or mobile}")
            continue
        if not email and not mobile:
            print(f"  [{i+1}/{len(accounts)}] SKIP (no email/mobile)")
            continue

        label = email or mobile
        print(f"  [{i+1}/{len(accounts)}] {label} ... ", end="", flush=True)

        try:
            delay = args.delay + random.uniform(0, args.delay)
            time.sleep(delay)
            token, cookies = _login(email=email, mobile=mobile, password=password)
        except Exception as e:
            print(f"LOGIN FAIL: {e}")
            results["fail_login"] += 1
            results["errors"].append(f"{label}: login failed - {e}")
            continue

        err = _add_via_api(args.admin_url, admin_token, email, mobile,
                           token, cookies, password)
        if err is None:
            print("OK")
            results["ok"] += 1
        elif err == "duplicate":
            print("DUP (already exists)")
            results["dup"] += 1
        else:
            print(f"API FAIL: {err}")
            results["fail_api"] += 1
            results["errors"].append(f"{label}: api failed - {err}")

    print("\n" + "=" * 50)
    print(f"Results: {results['ok']} imported, {results['dup']} duplicate, "
          f"{results['fail_login']} login fail, {results['fail_api']} API fail")
    if results["errors"]:
        print("Errors:")
        for e in results["errors"]:
            print(f"  - {e}")
    print("Done.")


if __name__ == "__main__":
    main()