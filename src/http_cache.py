"""On-disk TTL cache for read-only GETs.

regulations.gov and Congress.gov share one api.data.gov key (CONGRESS_API_KEY),
and regulations.gov allows only 1,000 requests/hour on it. Every read from the
new FOIA/regs/committee modules goes through here so a session never burns the
quota the existing Congress features depend on.
"""

import hashlib
import json
import time
from pathlib import Path

import requests

from src.config import DATA_DIR

CACHE_DIR = DATA_DIR / "cache"

# Params never part of the cache key (and never written to disk).
_SECRET_PARAMS = {"api_key"}


def _key(url: str, params: dict) -> str:
    public = {k: v for k, v in sorted(params.items()) if k not in _SECRET_PARAMS}
    raw = url + "?" + json.dumps(public, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def cached_get(url: str, params: dict = None, ttl_seconds: int = 6 * 3600,
               as_text: bool = False):
    """GET url, returning the cached body if younger than ttl_seconds.

    Returns parsed JSON, or the raw text when as_text=True. A failed request
    falls back to a stale cache entry when one exists.
    """
    params = params or {}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / (_key(url, params) + (".txt" if as_text else ".json"))

    if path.exists() and time.time() - path.stat().st_mtime < ttl_seconds:
        body = path.read_text(encoding="utf-8")
        return body if as_text else json.loads(body)

    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
    except requests.RequestException:
        if path.exists():
            body = path.read_text(encoding="utf-8")
            return body if as_text else json.loads(body)
        raise

    # All three sources serve UTF-8; some omit the charset, which makes
    # requests fall back to ISO-8859-1 and mangle names.
    body = response.content.decode("utf-8")
    path.write_text(body, encoding="utf-8")
    return body if as_text else json.loads(body)
