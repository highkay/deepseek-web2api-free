"""
Account pool — multi-account management for DeepSeek Chat proxy.

Manages multiple DeepSeek accounts, tracks their states (idle/busy/error),
provides credential health checking, session lifecycle, env bootstrapping,
and persistent panel-managed accounts.
"""
import hashlib
import json
import os
import secrets
import tempfile
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from adapter import DeepSeekAdapter

load_dotenv()


STORE_VERSION = 1
DEFAULT_STORE_PATH = Path(__file__).resolve().parent / "data" / "accounts.json"


def _now() -> int:
    return int(time.time())


def _account_id(prefix: str = "acc") -> str:
    return f"{prefix}_{secrets.token_urlsafe(9)}"


def _credential_fingerprint(token: str, cookies: str) -> str:
    return hashlib.sha256(f"{token}\0{cookies}".encode()).hexdigest()


def _mask_secret(value: str, start: int = 6, end: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= start + end:
        return "*" * len(value)
    return f"{value[:start]}...{value[-end:]}"


def _cookie_names(cookies: str, limit: int = 6) -> str:
    names = []
    for part in cookies.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        names.append(part.split("=", 1)[0].strip())
    if not names:
        return _mask_secret(cookies, 12, 4)
    suffix = "" if len(names) <= limit else f", +{len(names) - limit} more"
    return ", ".join(names[:limit]) + suffix


@dataclass
class Account:
    """A single DeepSeek account with credentials and runtime state."""
    token: str
    cookies: str
    email: str = ""
    id: str = field(default_factory=_account_id)
    source: str = "file"       # file | env
    proxy: str = ""             # per-account upstream proxy (optional)
    created_at: int = field(default_factory=_now)
    updated_at: int = field(default_factory=_now)
    state: str = "idle"        # idle | busy | error
    error_count: int = 0
    last_error: str = ""
    last_used: float = 0.0
    _adapter: Optional[DeepSeekAdapter] = field(default=None, repr=False)

    @property
    def adapter(self) -> DeepSeekAdapter:
        if self._adapter is None:
            self._adapter = DeepSeekAdapter(
                token=self.token,
                cookies=self.cookies,
                proxy=self.proxy or None,
            )
        return self._adapter

    @property
    def fingerprint(self) -> str:
        return _credential_fingerprint(self.token, self.cookies)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "source": self.source,
            "state": self.state,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "last_used": int(self.last_used),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "token_preview": _mask_secret(self.token),
            "cookies_preview": _cookie_names(self.cookies),
            "credential_fingerprint": self.fingerprint[:12],
            "read_only": self.source == "env",
        }

    def to_store_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "token": self.token,
            "cookies": self.cookies,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class AccountPool:
    """Thread-safe pool of DeepSeek accounts with round-robin selection."""

    def __init__(self):
        self._lock = threading.Lock()
        self._accounts: list[Account] = []
        self._next_idx = 0
        configured_store = Path(os.environ.get("ACCOUNT_STORE_PATH", str(DEFAULT_STORE_PATH)))
        if not configured_store.is_absolute():
            configured_store = Path(__file__).resolve().parent / configured_store
        self._store_path = configured_store
        self._load_persisted_accounts()
        self._load_env_accounts()

    # ── Loading / persistence ────────────────────────────────────

    def _append_loaded(self, acct: Account):
        if any(a.fingerprint == acct.fingerprint for a in self._accounts):
            print(f"WARNING: Skipping duplicate DeepSeek account {acct.email or acct.id}")
            return
        self._accounts.append(acct)

    def _load_persisted_accounts(self):
        path = self._store_path
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"WARNING: Failed to load account store {path}: {e}")
            return

        for item in data.get("accounts", []):
            token = str(item.get("token") or "").strip()
            cookies = str(item.get("cookies") or "").strip()
            if not token or not cookies:
                print("WARNING: Skipping persisted account with missing token/cookies")
                continue
            created_at = int(item.get("created_at") or _now())
            updated_at = int(item.get("updated_at") or created_at)
            self._append_loaded(Account(
                id=str(item.get("id") or _account_id()),
                email=str(item.get("email") or ""),
                token=token,
                cookies=cookies,
                source="file",
                created_at=created_at,
                updated_at=updated_at,
            ))

    def _load_env_accounts(self):
        """Load legacy and numbered accounts from env vars."""
        for i in range(1, 101):
            token = os.environ.get(f"DEEPSEEK_TOKEN_{i}", "").strip()
            cookies = os.environ.get(f"DEEPSEEK_COOKIES_{i}", "").strip()
            email = os.environ.get(f"DEEPSEEK_EMAIL_{i}", "").strip() or f"env-{i}"
            proxy = os.environ.get(f"DEEPSEEK_PROXY_{i}", "").strip()
            if not token and not cookies:
                continue
            if not token or not cookies:
                print(f"WARNING: Skipping env account {i}: token/cookies must both be set")
                continue
            self._append_loaded(Account(
                id=f"env-{i}",
                email=email,
                token=token,
                cookies=cookies,
                proxy=proxy,
                source="env",
            ))

        token = os.environ.get("DEEPSEEK_TOKEN", "").strip()
        cookies = os.environ.get("DEEPSEEK_COOKIES", "").strip()
        email = os.environ.get("DEEPSEEK_EMAIL", "").strip() or "env-default"
        proxy = os.environ.get("DEEPSEEK_PROXY", "").strip()
        if token and cookies:
            self._append_loaded(Account(
                id="env-default",
                email=email,
                token=token,
                cookies=cookies,
                proxy=proxy,
                source="env",
            ))
        elif token or cookies:
            print("WARNING: Skipping legacy env account: DEEPSEEK_TOKEN and DEEPSEEK_COOKIES must both be set")

    def _ensure_store_dir(self):
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self._store_path.parent, 0o700)

    def _save_persisted_accounts_locked(self):
        self._ensure_store_dir()
        data = {
            "version": STORE_VERSION,
            "accounts": [a.to_store_dict() for a in self._accounts if a.source == "file"],
        }
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"

        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self._store_path.name}.",
            suffix=".tmp",
            dir=str(self._store_path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self._store_path)
            os.chmod(self._store_path, 0o600)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    # ── CRUD ───────────────────────────────────────────────────

    def add(self, token: str, cookies: str, email: str = "", persist: bool = True) -> Account:
        token = (token or "").strip()
        cookies = (cookies or "").strip()
        email = (email or "").strip()
        if not token or not cookies:
            raise ValueError("Token and cookies are required")

        with self._lock:
            fp = _credential_fingerprint(token, cookies)
            if any(a.fingerprint == fp for a in self._accounts):
                raise ValueError("Account already exists")
            if not email:
                email = f"acc-{len(self._accounts) + 1}"
            now = _now()
            acct = Account(
                id=_account_id(),
                token=token,
                cookies=cookies,
                email=email,
                source="file" if persist else "memory",
                created_at=now,
                updated_at=now,
            )
            self._accounts.append(acct)
            if persist:
                self._save_persisted_accounts_locked()
            return acct

    def get_by_id(self, account_id: str) -> Optional[Account]:
        with self._lock:
            return next((a for a in self._accounts if a.id == account_id), None)

    def update(self, account_id: str, token: str | None = None,
               cookies: str | None = None, email: str | None = None) -> Account:
        with self._lock:
            acct = next((a for a in self._accounts if a.id == account_id), None)
            if acct is None:
                raise KeyError("Account not found")
            if acct.source == "env":
                raise PermissionError("Environment accounts are read-only; edit .env and restart the service")
            new_token = acct.token if token is None or token == "" else token.strip()
            new_cookies = acct.cookies if cookies is None or cookies == "" else cookies.strip()
            new_email = acct.email if email is None else email.strip()
            if not new_token or not new_cookies:
                raise ValueError("Token and cookies cannot be empty")

            new_fp = _credential_fingerprint(new_token, new_cookies)
            if any(a.id != account_id and a.fingerprint == new_fp for a in self._accounts):
                raise ValueError("Account already exists")

            credentials_changed = new_token != acct.token or new_cookies != acct.cookies
            acct.token = new_token
            acct.cookies = new_cookies
            acct.email = new_email or acct.email
            acct.updated_at = _now()
            if credentials_changed:
                acct._adapter = None
                acct.state = "idle"
                acct.error_count = 0
                acct.last_error = ""
            self._save_persisted_accounts_locked()
            return acct

    def remove_by_id(self, account_id: str) -> bool:
        with self._lock:
            for idx, acct in enumerate(self._accounts):
                if acct.id != account_id:
                    continue
                if acct.source == "env":
                    raise PermissionError("Environment accounts are read-only; edit .env and restart the service")
                self._accounts.pop(idx)
                if self._next_idx >= len(self._accounts):
                    self._next_idx = 0
                self._save_persisted_accounts_locked()
                return True
            return False

    # Backward-compatible index removal for callers that have not migrated.
    def remove(self, index: int) -> bool:
        with self._lock:
            if not (0 <= index < len(self._accounts)):
                return False
            account_id = self._accounts[index].id
        return self.remove_by_id(account_id)

    def get_all(self) -> list[dict]:
        with self._lock:
            return [a.to_dict() for a in self._accounts]

    def count(self) -> int:
        with self._lock:
            return len(self._accounts)

    # ── Selection ──────────────────────────────────────────────

    def acquire(self) -> Optional[Account]:
        """Get the next idle account (round-robin), or None if all busy."""
        with self._lock:
            if not self._accounts:
                return None
            n = len(self._accounts)
            for _ in range(n):
                idx = self._next_idx % n
                self._next_idx += 1
                acct = self._accounts[idx]
                if acct.state == "idle":
                    acct.state = "busy"
                    acct.last_used = time.time()
                    return acct
            return None

    def release(self, acct: Account):
        """Mark account back to idle unless it was already marked as error."""
        with self._lock:
            if acct.state == "busy":
                acct.state = "idle"

    def mark_error(self, acct: Account, error_msg: str = ""):
        with self._lock:
            acct.state = "error"
            acct.error_count += 1
            acct.last_error = error_msg

    # ── Health check / relogin ─────────────────────────────────

    def check_health(self, acct: Account) -> bool:
        """Test if account credentials are valid by creating a session."""
        try:
            adapter = DeepSeekAdapter(token=acct.token, cookies=acct.cookies)
            adapter.create_session()
            return True
        except Exception as e:
            with self._lock:
                acct.state = "error"
                acct.error_count += 1
                acct.last_error = str(e)
            return False

    def relogin_by_id(self, account_id: str) -> tuple[bool, str]:
        """Attempt to heal an error account by testing credentials."""
        with self._lock:
            acct = next((a for a in self._accounts if a.id == account_id), None)
            if acct is None:
                return False, "Account not found"
            if acct.state != "error":
                return False, f"Account is {acct.state}, not error"

        ok = self.check_health(acct)
        if ok:
            with self._lock:
                acct.state = "idle"
                acct.error_count = 0
                acct.last_error = ""
                acct._adapter = None
            return True, "ok"
        return False, acct.last_error or "unknown error"

    def relogin(self, index: int) -> tuple[bool, str]:
        with self._lock:
            if not (0 <= index < len(self._accounts)):
                return False, "Account not found"
            account_id = self._accounts[index].id
        return self.relogin_by_id(account_id)

    # ── Stats ──────────────────────────────────────────────────

    def stats(self) -> dict:
        with self._lock:
            total = len(self._accounts)
            idle = sum(1 for a in self._accounts if a.state == "idle")
            busy = sum(1 for a in self._accounts if a.state == "busy")
            error = sum(1 for a in self._accounts if a.state == "error")
            return {
                "total": total,
                "idle": idle,
                "busy": busy,
                "error": error,
            }
