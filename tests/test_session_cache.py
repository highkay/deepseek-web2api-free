"""Unit tests for session_cache."""
import time

import pytest

from session_cache import SessionCache, ChatSession


def test_get_returns_none_when_disabled():
    cache = SessionCache(ttl=0)
    cache.put("k", ChatSession(chat_session_id="s1"))
    assert cache.get("k") is None


def test_put_then_get_within_ttl():
    cache = SessionCache(ttl=10)
    cache.put("k", ChatSession(chat_session_id="s1"))
    s = cache.get("k")
    assert s is not None
    assert s.chat_session_id == "s1"


def test_expired_entry_returned_as_none():
    cache = SessionCache(ttl=0.05)
    cache.put("k", ChatSession(chat_session_id="s1"))
    time.sleep(0.1)
    assert cache.get("k") is None


def test_lru_eviction():
    cache = SessionCache(ttl=60, max_size=2)
    cache.put("a", ChatSession(chat_session_id="sa"))
    cache.put("b", ChatSession(chat_session_id="sb"))
    cache.put("c", ChatSession(chat_session_id="sc"))  # evicts "a"
    assert cache.get("a") is None
    assert cache.get("b") is not None
    assert cache.get("c") is not None


def test_invalidate_removes_entry():
    cache = SessionCache(ttl=10)
    cache.put("k", ChatSession(chat_session_id="s1"))
    cache.invalidate("k")
    assert cache.get("k") is None


def test_chat_session_message_counter():
    s = ChatSession(chat_session_id="x")
    assert s.parent_message_id == 0
    m1 = s.next_message_id()
    m2 = s.next_message_id()
    m3 = s.next_message_id()
    assert m1 == 1
    assert m2 == 2
    assert m3 == 3
    assert s.parent_message_id == 3


def test_derive_conversation_id_from_user_field():
    msgs = [{"role": "user", "content": "hi", "user": "alice"}]
    cid = SessionCache.derive_conversation_id(None, msgs, None)
    # The helper DOES honor the "user" key in the message dict (it's the
    # canonical OpenAI field). When the field is present, we get the raw
    # value as the conversation id.
    assert cid == "alice"


def test_derive_conversation_id_from_metadata():
    cid = SessionCache.derive_conversation_id(None, [], {"user_id": "bob"})
    assert cid == "bob"


def test_derive_conversation_id_anon():
    cid = SessionCache.derive_conversation_id(None, [], None)
    assert cid == "anon"
