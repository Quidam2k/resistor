"""TTL cache: hits skip the network, the API key never reaches disk."""

import pytest
import requests

from src import http_cache


class _Resp:
    def __init__(self, body):
        self.content = body.encode("utf-8")

    def raise_for_status(self):
        pass


@pytest.fixture
def counter(monkeypatch, tmp_path):
    monkeypatch.setattr(http_cache, "CACHE_DIR", tmp_path)
    hits = []

    def fake_get(url, params=None, timeout=None):
        hits.append(params)
        return _Resp('{"name": "Niño"}')

    monkeypatch.setattr(http_cache.requests, "get", fake_get)
    return hits


def test_second_call_is_cached_and_key_not_written(counter, tmp_path):
    first = http_cache.cached_get("https://x/y", {"api_key": "SECRET", "q": 1})
    second = http_cache.cached_get("https://x/y", {"api_key": "OTHER", "q": 1})
    assert first == second == {"name": "Niño"}
    assert len(counter) == 1
    for f in tmp_path.iterdir():
        assert "SECRET" not in f.name and "SECRET" not in f.read_text(encoding="utf-8")


def test_expired_entry_refetches(counter):
    http_cache.cached_get("https://x/z")
    http_cache.cached_get("https://x/z", ttl_seconds=0)
    assert len(counter) == 2


def test_stale_entry_served_when_network_fails(counter, monkeypatch):
    http_cache.cached_get("https://x/s")

    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(http_cache.requests, "get", boom)
    assert http_cache.cached_get("https://x/s", ttl_seconds=0) == {"name": "Niño"}
