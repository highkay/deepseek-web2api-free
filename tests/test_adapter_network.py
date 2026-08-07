"""Adapter network-level tests: trust_env disable, proxy resolution,
transport-fault classification, network retry, and circuit breaker.

No real network is touched — curl_cffi.Session and the transport calls are
monkeypatched / faked, mirroring the style of tests/test_adapter.py.
"""
import os

import pytest

import adapter as A
from adapter import DeepSeekAdapter, UpstreamNetworkError
from curl_cffi.requests import exceptions as _cffi_exc


def _adapter():
    return DeepSeekAdapter(token="test-token", cookies="test-cookie")


# ── trust_env / proxy resolution ──────────────────────────────

class TestSessionConfig:
    def test_client_created_with_trust_env_false(self, monkeypatch):
        captured = {}

        class _FakeSession:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.cookies = {}

        monkeypatch.setattr(A.cffi_requests, "Session", _FakeSession)
        ad = _adapter()
        assert captured.get("trust_env") is False
        assert ad._trust_env is False

    def test_proxy_arg_wins_over_env(self, monkeypatch):
        captured = {}

        class _FakeSession:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.cookies = {}

        monkeypatch.setattr(A.cffi_requests, "Session", _FakeSession)
        monkeypatch.setenv("DEEPSEEK_PROXY", "http://env:pass@envhost:1")
        ad = DeepSeekAdapter(token="t", cookies="c", proxy="http://arg:pass@arghost:2")
        assert ad.proxy == "http://arg:pass@arghost:2"
        assert captured["proxies"] == {"all": "http://arg:pass@arghost:2"}

    def test_proxy_none_falls_back_to_env(self, monkeypatch):
        captured = {}

        class _FakeSession:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.cookies = {}

        monkeypatch.setattr(A.cffi_requests, "Session", _FakeSession)
        monkeypatch.setenv("DEEPSEEK_PROXY", "http://env:pass@envhost:9")
        ad = _adapter()
        assert ad.proxy == "http://env:pass@envhost:9"
        assert captured["proxies"] == {"all": "http://env:pass@envhost:9"}

    def test_no_proxy_resolves_none(self, monkeypatch):
        captured = {}

        class _FakeSession:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.cookies = {}

        monkeypatch.setattr(A.cffi_requests, "Session", _FakeSession)
        monkeypatch.delenv("DEEPSEEK_PROXY", raising=False)
        ad = _adapter()
        assert ad.proxy is None
        assert captured.get("proxies") is None
        assert captured.get("trust_env") is False

    def test_no_explicit_proxy_strips_container_proxy_env(self, monkeypatch):
        # curl_cffi's trust_env arg doesn't stop libcurl from reading
        # HTTP(S)_PROXY env vars, so with no explicit proxy we must strip
        # them from the process env (verified root cause of the 500s).
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
        monkeypatch.delenv("DEEPSEEK_PROXY", raising=False)
        _adapter()
        assert os.environ.get("HTTP_PROXY") is None
        assert os.environ.get("HTTPS_PROXY") is None

    def test_explicit_proxy_keeps_container_proxy_env(self, monkeypatch):
        # With an explicit proxy the strip is skipped; other subsystems may
        # legitimately still want the env proxy.
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
        monkeypatch.setenv("DEEPSEEK_PROXY", "http://user:pass@host:1")
        DeepSeekAdapter(token="t", cookies="c",
                        proxy="http://arg:pass@arghost:2")
        assert os.environ.get("HTTP_PROXY") == "http://127.0.0.1:7890"


# ── transport fault classification ─────────────────────────────

class TestTransportClassification:
    def test_transport_faults_map_to_upstream_network_error(self, monkeypatch):
        for exc_name in ("ConnectionError", "Timeout", "ProxyError",
                         "SSLError", "DNSError", "RetryError", "ConnectTimeout"):
            exc_cls = getattr(_cffi_exc, exc_name)

            def fail_post(*a, **k):
                raise exc_cls(f"boom {exc_name}")

            ad = _adapter()
            monkeypatch.setattr(ad, "_client",
                                type("C", (), {"post": fail_post, "get": fail_post})())
            with pytest.raises(UpstreamNetworkError):
                ad._request("post", "http://x")

    def test_http_error_not_network(self, monkeypatch):
        # HTTPError from raise_for_status must NOT become UpstreamNetworkError.
        def fail_post(self, *a, **k):
            raise _cffi_exc.HTTPError("500")
        ad = _adapter()
        monkeypatch.setattr(ad, "_client", type("C", (), {"post": fail_post})())
        with pytest.raises(_cffi_exc.HTTPError):
            ad._request("post", "http://x")


# ── network retry in chat() ────────────────────────────────────

class TestChatNetworkRetry:
    def test_chat_retries_network_error_then_succeeds(self, monkeypatch):
        ad = _adapter()
        calls = {"n": 0}
        monkeypatch.setattr(A, "NETWORK_RETRY_DELAYS", [0.0, 0.0])

        def fake_once(*a, **k):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise UpstreamNetworkError("transient")
            return ("ok", "")

        monkeypatch.setattr(ad, "_chat_once", fake_once)
        monkeypatch.setattr(ad, "create_session", lambda: "sess-new")
        monkeypatch.setattr(A.time, "sleep", lambda s: None)
        content, thinking = ad.chat("sess-1", "prompt")
        assert content == "ok"
        assert calls["n"] == 3

    def test_chat_raises_after_network_retries_exhausted(self, monkeypatch):
        ad = _adapter()
        calls = {"n": 0}
        monkeypatch.setattr(A, "NETWORK_RETRY_DELAYS", [0.0, 0.0])

        def doomed(*a, **k):
            calls["n"] += 1
            raise UpstreamNetworkError("dead")

        monkeypatch.setattr(ad, "_chat_once", doomed)
        monkeypatch.setattr(ad, "create_session", lambda: "sess-new")
        monkeypatch.setattr(A.time, "sleep", lambda s: None)
        with pytest.raises(UpstreamNetworkError):
            ad.chat("sess-1", "prompt")
        # 1 initial + len(NETWORK_RETRY_DELAYS)=2 retries = 3 attempts
        assert calls["n"] == 3


# ── circuit breaker ────────────────────────────────────────────

class TestBreaker:
    def test_short_circuits_after_threshold(self, monkeypatch):
        ad = _adapter()
        ad._breaker_failures = 2  # threshold=3, so next failure opens
        ad._breaker_open_until = 0.0  # not yet open
        # Trigger one more failure → opens the breaker.
        ad._breaker_on_failure()
        assert ad._breaker_open_until > 0
        assert ad._breaker_allow() is False  # open → short-circuit

        called = {"chat_once": 0}

        def spy(*a, **k):
            called["chat_once"] += 1
            raise UpstreamNetworkError("x")

        monkeypatch.setattr(ad, "_chat_once", spy)
        monkeypatch.setattr(ad, "create_session", lambda a: "s")
        with pytest.raises(UpstreamNetworkError) as ei:
            ad.chat("s", "p")
        assert "breaker" in str(ei.value)
        assert called["chat_once"] == 0  # did NOT attempt

    def test_half_open_probe_allowed_after_cooldown(self, monkeypatch):
        monkeypatch.setattr(A.time, "monotonic", lambda: 1000.0)
        ad = _adapter()
        ad._breaker_failures = A.BREAKER_ERROR_THRESHOLD
        ad._breaker_open_until = 1005.0  # open until t=1005
        assert ad._breaker_allow() is False
        # advance clock past cooldown
        monkeypatch.setattr(A.time, "monotonic", lambda: 1006.0)
        assert ad._breaker_allow() is True  # half-open allowed probe

    def test_success_resets_breaker(self, monkeypatch):
        ad = _adapter()
        ad._breaker_failures = 2
        ad._breaker_open_until = 1234.0
        ad._breaker_on_success()
        assert ad._breaker_failures == 0
        assert ad._breaker_open_until == 0.0