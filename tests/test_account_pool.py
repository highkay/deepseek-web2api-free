"""Unit tests for AccountPool: env fallback, acquire priority, persistence.

Each test runs against an isolated store path (tmp_path) and a clean env
so no real credentials or the real data/accounts.json are touched.
"""
import pytest

from account_pool import AccountPool


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Point the store at a temp file and clear all DeepSeek env vars."""
    monkeypatch.setenv("ACCOUNT_STORE_PATH", str(tmp_path / "accounts.json"))
    for name in ["DEEPSEEK_TOKEN", "DEEPSEEK_COOKIES", "DEEPSEEK_EMAIL",
                 "DEEPSEEK_PROXY", "DEEPSEEK_JITTER_SECS",
                 "DEEPSEEK_RATE_LIMIT_RETRY_DELAYS"]:
        monkeypatch.delenv(name, raising=False)
    for i in range(1, 6):
        for suffix in ["TOKEN", "COOKIES", "EMAIL", "PROXY"]:
            monkeypatch.delenv(f"DEEPSEEK_{suffix}_{i}", raising=False)
    return monkeypatch


def _pool():
    return AccountPool()


# ── env fallback loading ──────────────────────────────────────

def test_no_env_credentials_no_fallback(clean_env):
    pool = _pool()
    assert pool._env_fallback is None
    assert len(pool._accounts) == 0
    assert pool.stats()["total"] == 0


def test_legacy_env_fallback_loaded(clean_env):
    clean_env.setenv("DEEPSEEK_TOKEN", "legacy-token")
    clean_env.setenv("DEEPSEEK_COOKIES", "smidV2=legacy-cookie")
    pool = _pool()
    assert pool._env_fallback is not None
    assert pool._env_fallback.id == "env-default"
    assert pool._env_fallback.source == "env"
    assert pool._env_fallback.token == "legacy-token"
    # Not in the pool itself
    assert len(pool._accounts) == 0


def test_numbered_fallback_preferred_over_legacy(clean_env):
    clean_env.setenv("DEEPSEEK_TOKEN", "legacy-token")
    clean_env.setenv("DEEPSEEK_COOKIES", "legacy-cookie")
    clean_env.setenv("DEEPSEEK_TOKEN_1", "numbered-token")
    clean_env.setenv("DEEPSEEK_COOKIES_1", "numbered-cookie")
    clean_env.setenv("DEEPSEEK_EMAIL_1", "acct-1")
    pool = _pool()
    assert pool._env_fallback is not None
    assert pool._env_fallback.id == "env-1"
    assert pool._env_fallback.email == "acct-1"


def test_incomplete_env_credentials_skipped(clean_env):
    clean_env.setenv("DEEPSEEK_TOKEN", "token-only")  # no cookies
    clean_env.setenv("DEEPSEEK_COOKIES_1", "cookie-only")  # no token
    pool = _pool()
    assert pool._env_fallback is None


# ── acquire priority ──────────────────────────────────────────

def test_acquire_empty_pool_uses_fallback(clean_env):
    clean_env.setenv("DEEPSEEK_TOKEN", "legacy-token")
    clean_env.setenv("DEEPSEEK_COOKIES", "legacy-cookie")
    pool = _pool()
    acct = pool.acquire()
    assert acct is not None
    assert acct.id == "env-default"
    assert acct.state == "busy"
    pool.release(acct)
    assert pool._env_fallback.state == "idle"


def test_acquire_prefers_panel_account(clean_env):
    clean_env.setenv("DEEPSEEK_TOKEN", "legacy-token")
    clean_env.setenv("DEEPSEEK_COOKIES", "legacy-cookie")
    pool = _pool()
    panel = pool.add(token="panel-token", cookies="panel-cookie", email="panel-1")
    acct = pool.acquire()
    assert acct is not None
    assert acct.id == panel.id  # panel wins over fallback
    pool.release(acct)


def test_acquire_empty_pool_no_fallback_returns_none(clean_env):
    pool = _pool()
    assert pool.acquire() is None


# ── get_all / stats / remove ──────────────────────────────────

def test_get_all_and_stats_include_fallback(clean_env):
    clean_env.setenv("DEEPSEEK_TOKEN", "legacy-token")
    clean_env.setenv("DEEPSEEK_COOKIES", "legacy-cookie")
    pool = _pool()
    pool.add(token="panel-token", cookies="panel-cookie", email="panel-1")
    all_accounts = pool.get_all()
    ids = [a["id"] for a in all_accounts]
    assert "env-default" in ids
    assert any(a["source"] == "env" and a["read_only"] for a in all_accounts)
    stats = pool.stats()
    assert stats["total"] == 2  # panel + fallback


def test_remove_by_id_cannot_remove_fallback(clean_env):
    clean_env.setenv("DEEPSEEK_TOKEN", "legacy-token")
    clean_env.setenv("DEEPSEEK_COOKIES", "legacy-cookie")
    pool = _pool()
    # Fallback never enters the pool, so removal by id returns False.
    assert pool.remove_by_id("env-default") is False
    assert pool._env_fallback is not None


# ── panel account persistence ─────────────────────────────────

def test_panel_account_persists_across_pool_reload(clean_env):
    pool = _pool()
    acct = pool.add(token="panel-token", cookies="panel-cookie", email="panel-1")
    # A brand-new pool instance reads the same store file.
    pool2 = _pool()
    ids = [a["id"] for a in pool2.get_all()]
    assert acct.id in ids
    # Remove persists too.
    assert pool2.remove_by_id(acct.id) is True
    pool3 = _pool()
    assert acct.id not in [a["id"] for a in pool3.get_all()]
