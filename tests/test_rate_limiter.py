"""Unit tests for rate_limiter."""
import pytest

from rate_limiter import RateLimiter, _Window


def test_window_allows_under_limit():
    w = _Window(capacity=3, window_secs=60)
    for _ in range(3):
        ok, remaining, limit, _ = w.check()
        assert ok is True
        assert limit == 3
    ok, remaining, _, _ = w.check()
    assert ok is False
    assert remaining == 0


def test_window_unlimited_when_capacity_zero():
    w = _Window(capacity=0, window_secs=60)
    for _ in range(100):
        ok, remaining, limit, _ = w.check()
        assert ok is True
        assert limit == 0
        assert remaining == -1


def test_limiter_both_dimensions_must_pass():
    rl = RateLimiter(per_key=2, per_ip=10, enabled=True)
    rl._per_key = 2
    rl._per_ip = 10
    # Use up the key quota; IP still has room.
    assert rl.check("k1", "1.1.1.1")[0] is True
    assert rl.check("k1", "1.1.1.1")[0] is True
    ok, headers = rl.check("k1", "1.1.1.1")
    assert ok is False
    assert "Retry-After" in headers


def test_limiter_disabled_allows_everything():
    rl = RateLimiter(per_key=1, per_ip=1, enabled=False)
    for _ in range(100):
        ok, headers = rl.check("k", "ip")
        assert ok is True
        assert headers == {}


def test_limiter_ip_dimension():
    rl = RateLimiter(per_key=0, per_ip=2, enabled=True)
    rl._per_key = 0
    rl._per_ip = 2
    assert rl.check("k1", "ip1")[0] is True
    assert rl.check("k2", "ip1")[0] is True
    assert rl.check("k3", "ip1")[0] is False
