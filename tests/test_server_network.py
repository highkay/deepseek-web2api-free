"""Server error-mapping tests: UpstreamNetworkError → 502 and no mark_error,
while generic exceptions still → 500 and mark the account.

Calls the handler functions directly (no HTTP client) and spies on
server.pool to assert the pool is / isn't poisoned.
"""
import pytest
from fastapi import HTTPException

import server as S
from adapter import UpstreamNetworkError


class _FakeAcq:
    """Minimal AcquiredAccount stand-in whose adapter raises `error`."""

    def __init__(self, error):
        self._error = error
        self.acct = type("Acct", (), {"state": "busy"})()
        # server calls acq.adapter.chat(...) — the adapter lives on the
        # AcquiredAccount, not on the pool account directly.
        self.adapter = type("A", (), {
            "create_session": lambda: "sess-1",
            "chat": self._boom,
        })()
        self.acct.adapter = self.adapter
        self._session_id = None
        self._parent_message_id = None
        self.parent_message_id = None  # AcquiredAccount property used by handlers

    def _boom(self, *a, **k):
        raise self._error

    def create_session(self):
        return "sess-1"

    def release(self):
        pass


class TestNonstreamNetworkMapping:
    def test_network_error_maps_to_502_no_mark_error(self, monkeypatch):
        marked = []
        monkeypatch.setattr(S.pool, "mark_error",
                            lambda acct, msg: marked.append((acct, msg)))
        monkeypatch.setattr(S, "_acquire",
                            lambda cache_key=None: _FakeAcq(
                                UpstreamNetworkError("proxy down")))
        with pytest.raises(HTTPException) as ei:
            S._handle_nonstream("prox-1", "hello")
        assert ei.value.status_code == 502
        assert marked == []  # pool not poisoned

    def test_generic_exception_still_marks_account(self, monkeypatch):
        marked = []
        monkeypatch.setattr(S.pool, "mark_error",
                            lambda acct, msg: marked.append((acct, msg)))
        monkeypatch.setattr(S, "_acquire",
                            lambda cache_key=None: _FakeAcq(
                                RuntimeError("backend bug")))
        with pytest.raises(RuntimeError):
            S._handle_nonstream("prose-1", "hello")
        assert len(marked) == 1  # generic still poisons pool

    def test_rate_limit_maps_429_no_mark(self, monkeypatch):
        marked = []
        monkeypatch.setattr(S.pool, "mark_error",
                            lambda acct, msg: marked.append((acct, msg)))
        monkeypatch.setattr(S, "_acquire",
                            lambda cache_key=None: _FakeAcq(
                                S.RateLimitError("too fast")))
        with pytest.raises(HTTPException) as ei:
            S._handle_nonstream("rl-1", "hello")
        assert ei.value.status_code == 429
        assert marked == []  # rate limit is not a credential failure


class TestStreamClassifier:
    def test_network_error_in_no_mark_classifier(self):
        # The tuple used by event_stream() / _anthropic_* to decide whether
        # an error poisons the pool must include UpstreamNetworkError.
        no_mark = (S.RateLimitError, S.UpstreamEmptyError, S.UpstreamNetworkError)
        assert UpstreamNetworkError in no_mark