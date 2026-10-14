"""regulations.gov client tests against captured fixtures (no network)."""

import inspect
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src import regsgov
from src.db import get_connection

FIXTURES = Path(__file__).parent / "fixtures"
REQUESTER = {"name": "Test Person", "city": "Eugene", "state": "OR"}


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_get(url, params=None, ttl_seconds=0, as_text=False):
        seen.append((url, dict(params or {}), ttl_seconds))
        if url.endswith("/documents"):
            return _load("regs_documents.json")
        if "/documents/" in url:
            return _load("regs_document_detail.json")
        if "/dockets/" in url:
            return _load("regs_docket.json")
        raise AssertionError(url)

    monkeypatch.setenv("CONGRESS_API_KEY", "test-key")
    monkeypatch.setattr(regsgov, "cached_get", fake_get)
    return seen


def test_no_write_methods_in_client():
    source = inspect.getsource(regsgov)
    for verb in ("requests.post", "requests.put", "requests.patch", ".post(", "/comments\""):
        assert verb not in source


def test_find_open_comment_periods_filters_and_caches(calls):
    results = regsgov.find_open_comment_periods("water", today=date(2026, 10, 13))
    assert [r["id"] for r in results][:3] == [
        "DRBC-2026-0166-0001", "EPA_FRDOC_0001-33165", "EPA-HQ-OW-2026-6965-0001"]
    url, params, ttl = calls[0]
    assert params["filter[documentType]"] == "Proposed Rule"
    assert params["filter[commentEndDate][ge]"] == "2026-10-13"
    assert params["filter[searchTerm]"] == "water"
    assert params["api_key"] == "test-key"
    assert ttl == regsgov.SEARCH_TTL
    assert results[0]["url"] == "https://www.regulations.gov/document/DRBC-2026-0166-0001"


def test_get_docket(calls):
    d = regsgov.get_docket("EPA-HQ-OW-2026-6965")
    assert d["rin"] == "2040-AG56"
    assert d["agency"] == "EPA"
    assert calls[0][2] == regsgov.DETAIL_TTL


def test_get_document_has_fr_cite_fields(calls):
    doc = regsgov.get_document("EPA-HQ-OW-2026-6965-0001")
    assert doc["docket_id"] == "EPA-HQ-OW-2026-6965"
    assert doc["fr_volume"] == "91"
    assert doc["fr_pages"].startswith("56819")
    assert doc["comment_end"] == "2026-10-20T03:59:59Z"


def test_days_left():
    now = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
    assert regsgov.days_left("2026-10-20T03:59:59Z", now) == 6
    assert regsgov.days_left("2026-10-01T00:00:00Z", now) < 0
    assert regsgov.days_left(None, now) is None


def test_draft_comment_is_unsent_scaffold(calls):
    result = regsgov.draft_comment(
        "EPA-HQ-OW-2026-6965-0001", "I oppose narrowing the definition.",
        defects=["The proposal does not analyze effects on Oregon estuaries."],
        requester=REQUESTER)
    body = result["body"]
    assert body.startswith(regsgov.DRAFT_HEADER)
    assert "91 FR 56819" in body
    assert "5 U.S.C. 553(c)" in body
    assert "Oregon estuaries" in body
    assert "Do not submit until filled in" in body
    assert "—" not in body

    conn = get_connection()
    row = conn.execute("SELECT status, document_id FROM regs_comments WHERE id=?",
                       (result["draft_id"],)).fetchone()
    conn.close()
    assert row["status"] == "draft_unsent"
    assert row["document_id"] == "EPA-HQ-OW-2026-6965-0001"


def test_missing_key_raises(monkeypatch):
    monkeypatch.setattr(regsgov, "get_env", lambda k, d=None: None)
    with pytest.raises(RuntimeError):
        regsgov.get_docket("X")
