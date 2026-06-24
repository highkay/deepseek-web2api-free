"""
Admin API — authentication, statistics tracking, account pool management.
"""
import os
import secrets
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from account_pool import AccountPool

# ── Admin password (set DEEPSEEK_ADMIN_PASSWORD in .env) ──────
_ADMIN_PASSWORD = os.environ.get("DEEPSEEK_ADMIN_PASSWORD", "admin")

# ── Token management ──────────────────────────────────────────
_tokens: set[str] = set()


def _generate_token() -> str:
    return secrets.token_hex(32)


def _verify_token(token: str) -> bool:
    if not token:
        return False
    # Constant-time membership: compare against every stored token. The set is
    # small (one entry per active admin session) so the cost is negligible.
    snapshot = tuple(_tokens)
    matched = False
    for stored in snapshot:
        if secrets.compare_digest(token, stored):
            matched = True
    return matched


# ── Login throttling ──────────────────────────────────────────
# Per-IP sliding window: at most _LOGIN_MAX failures per _LOGIN_WINDOW seconds.
_LOGIN_MAX = 5
_LOGIN_WINDOW = 300
_login_attempts: dict[str, list[float]] = {}
_login_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    # Prefer X-Forwarded-For when behind a trusted proxy. The deployment guide
    # warns against exposing /admin/api/login publicly without TLS, so this is
    # best-effort identification, not a security boundary.
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _login_record_failure(ip: str) -> None:
    now = time.time()
    with _login_lock:
        attempts = _login_attempts.setdefault(ip, [])
        cutoff = now - _LOGIN_WINDOW
        attempts[:] = [t for t in attempts if t > cutoff]
        attempts.append(now)


def _login_check_over_limit(ip: str) -> None:
    """Raise 429 if this IP has too many recent failures.
    Call AFTER recording the failure so the current attempt is counted."""
    now = time.time()
    with _login_lock:
        attempts = _login_attempts.get(ip, [])
        cutoff = now - _LOGIN_WINDOW
        recent = [t for t in attempts if t > cutoff]
        if len(recent) >= _LOGIN_MAX:
            raise HTTPException(
                status_code=429,
                detail="Too many failed login attempts; try again later",
            )


def _login_clear(ip: str) -> None:
    with _login_lock:
        _login_attempts.pop(ip, None)


# ── Stats ─────────────────────────────────────────────────────
class StatsSnapshot:
    def __init__(self):
        self.reset()

    def reset(self):
        self.total_requests = 0
        self.success_requests = 0
        self.failed_requests = 0
        self.total_latency_ms = 0.0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.start_time = time.time()
        self.models: dict[str, dict] = {}

    def record(self, model: str, latency_ms: float,
               prompt_tokens: int = 0, completion_tokens: int = 0,
               success: bool = True):
        self.total_requests += 1
        if success:
            self.success_requests += 1
        else:
            self.failed_requests += 1
        self.total_latency_ms += latency_ms
        self.total_prompt_tokens += prompt_tokens
        self.total_completion_tokens += completion_tokens
        if model not in self.models:
            self.models[model] = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self.models[model]["requests"] += 1
        self.models[model]["prompt_tokens"] += prompt_tokens
        self.models[model]["completion_tokens"] += completion_tokens


_stats = StatsSnapshot()
_pool = AccountPool()


def get_pool() -> AccountPool:
    return _pool


def get_stats() -> StatsSnapshot:
    return _stats


# ── Pydantic models ───────────────────────────────────────────

class LoginRequest(BaseModel):
    password: str


class LoginResponse(BaseModel):
    token: str


class AccountAddRequest(BaseModel):
    token: str
    cookies: str
    email: str | None = ""


class AccountUpdateRequest(BaseModel):
    token: str | None = None
    cookies: str | None = None
    email: str | None = None


class AccountReloginResponse(BaseModel):
    ok: bool
    message: str


# ── Router ────────────────────────────────────────────────────

router = APIRouter(prefix="/admin/api")


def _check_auth(request: Request):
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if not _verify_token(token):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _pool_error(e: Exception):
    if isinstance(e, KeyError):
        raise HTTPException(status_code=404, detail=str(e).strip("'"))
    if isinstance(e, PermissionError):
        raise HTTPException(status_code=400, detail=str(e))
    if isinstance(e, RuntimeError):
        raise HTTPException(status_code=409, detail=str(e))
    if isinstance(e, ValueError):
        raise HTTPException(status_code=400, detail=str(e))
    raise e


@router.post("/login")
async def login(req: LoginRequest, request: Request):
    ip = _client_ip(request)
    # Constant-time compare so a wrong password and a right one take the
    # same time. Throttle is checked AFTER the compare so a legitimate
    # password resets the counter even if the IP was previously cooling
    # down — typical SSH/GitHub semantics, prevents lockout-of-self.
    correct = secrets.compare_digest(req.password or "", _ADMIN_PASSWORD or "")
    if correct:
        _login_clear(ip)
        token = _generate_token()
        _tokens.add(token)
        return {"token": token}
    _login_record_failure(ip)
    _login_check_over_limit(ip)
    raise HTTPException(status_code=403, detail="Invalid password")


@router.post("/logout")
async def logout(request: Request):
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if token:
        _tokens.discard(token)
    return {"ok": True}


@router.get("/stats")
async def stats(request: Request):
    _check_auth(request)
    s = _stats
    uptime = int(time.time() - s.start_time)
    avg_latency = int(s.total_latency_ms / s.total_requests) if s.total_requests > 0 else 0
    return {
        "total_requests": s.total_requests,
        "success_requests": s.success_requests,
        "failed_requests": s.failed_requests,
        "avg_latency_ms": avg_latency,
        "total_prompt_tokens": s.total_prompt_tokens,
        "total_completion_tokens": s.total_completion_tokens,
        "uptime_secs": uptime,
        "models": s.models,
    }


@router.get("/accounts")
async def list_accounts(request: Request):
    _check_auth(request)
    pool = get_pool()
    return {
        "accounts": pool.get_all(),
        **pool.stats(),
    }


@router.post("/accounts")
async def add_account(req: AccountAddRequest, request: Request):
    _check_auth(request)
    pool = get_pool()
    try:
        acct = pool.add(token=req.token, cookies=req.cookies, email=req.email or "")
    except Exception as e:
        _pool_error(e)
    return {"ok": True, "account": acct.to_dict()}


@router.put("/accounts/{account_id}")
async def update_account(account_id: str, req: AccountUpdateRequest, request: Request):
    _check_auth(request)
    pool = get_pool()
    try:
        acct = pool.update(account_id, token=req.token, cookies=req.cookies, email=req.email)
    except Exception as e:
        _pool_error(e)
    return {"ok": True, "account": acct.to_dict()}


@router.delete("/accounts/{account_id}")
async def remove_account(account_id: str, request: Request):
    _check_auth(request)
    pool = get_pool()
    try:
        ok = pool.remove_by_id(account_id)
    except Exception as e:
        _pool_error(e)
    if not ok:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"ok": True}


@router.post("/accounts/{account_id}/relogin")
async def relogin_account(account_id: str, request: Request) -> AccountReloginResponse:
    _check_auth(request)
    pool = get_pool()
    ok, msg = pool.relogin_by_id(account_id)
    return AccountReloginResponse(ok=ok, message=msg)
