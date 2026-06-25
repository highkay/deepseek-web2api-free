"""Unit tests for ip_utils."""
import pytest

from ip_utils import is_trusted_proxy, get_real_client_ip, get_trusted_networks
import ip_utils as iu


def reset_cache():
    iu._cached_entries = None


def test_empty_trusted_proxies_blocks_xff(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "")
    reset_cache()
    headers = {"X-Forwarded-For": "8.8.8.8"}
    ip = get_real_client_ip(headers, "127.0.0.1")
    # Peer is loopback, but the env is empty — XFF is NOT trusted.
    assert ip == "127.0.0.1"


def test_trusted_loopback_allows_xff(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "127.0.0.1")
    reset_cache()
    headers = {"X-Forwarded-For": "8.8.8.8, 1.1.1.1"}
    ip = get_real_client_ip(headers, "127.0.0.1")
    assert ip == "8.8.8.8"


def test_cidr_match(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.0/8")
    reset_cache()
    headers = {"X-Forwarded-For": "8.8.8.8"}
    ip = get_real_client_ip(headers, "10.5.6.7")
    assert ip == "8.8.8.8"


def test_untrusted_peer_falls_back_to_peer(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.0/8")
    reset_cache()
    headers = {"X-Forwarded-For": "8.8.8.8"}
    ip = get_real_client_ip(headers, "1.2.3.4")
    assert ip == "1.2.3.4"


def test_is_trusted_proxy_invalid_ip():
    assert is_trusted_proxy(None) is False
    assert is_trusted_proxy("not-an-ip") is False
