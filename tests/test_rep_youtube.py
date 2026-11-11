import sqlite3

import pytest

from src import rep_youtube
from src.rep_record import ensure_schema

REP = {"name": "James Manning Jr.", "youtube_slugs": ["manning"]}


def _video(folder, name, title, private="False", url="https://youtu.be/x", words=320):
    body = " ".join(f"w{i}" for i in range(words))
    (folder / name).write_text(
        f"---\ntitle: {title}\nurl: {url}\npublished: 2025-03-04\nprivate: {private}\nfiltered: False\n---\n\n# {title}\n\n{body}\n",
        encoding="utf-8")


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:"); ensure_schema(c); yield c; c.close()


def test_parse_and_skip(tmp_path):
    _video(tmp_path, "a.md", "Manning on housing")
    _video(tmp_path, "b.md", "Private one", private="True")
    _video(tmp_path, "c.md", "No url", url="")
    (tmp_path / "d.md").write_text("no frontmatter", encoding="utf-8")
    parsed = rep_youtube.parse_transcript(tmp_path / "a.md")
    assert parsed["title"] == "Manning on housing" and parsed["published"] == "2025-03-04"
    assert parsed["text"].startswith("w0 ")
    assert all(rep_youtube.parse_transcript(tmp_path / n) is None for n in ("b.md", "c.md", "d.md"))


def test_segments_sizes_tail_and_cap():
    text = " ".join(str(i) for i in range(330))
    assert [len(s.split()) for s in rep_youtube.segments(text)] == [150, 150]  # 30-word tail dropped
    assert len(rep_youtube.segments(" ".join("x" for _ in range(1000)), words=50, max_segments=3)) == 3


def test_collect_verified_by_title_and_dedupe(conn, tmp_path):
    folder = tmp_path / "manning"; folder.mkdir()
    _video(folder, "a.md", "Sen. Manning on housing")
    _video(folder, "b.md", "Committee hearing", url="https://youtu.be/y")
    first = rep_youtube.collect_youtube(conn, REP, tmp_path)
    assert first == {"inserted": 4, "verified": 2, "videos": 2}
    assert rep_youtube.collect_youtube(conn, REP, tmp_path)["inserted"] == 0
    note = conn.execute("SELECT note FROM backfill_progress WHERE source='statements:youtube'").fetchone()[0]
    assert "no timestamps" in note
    assert rep_youtube.collect_youtube(conn, {"name": "X"}, tmp_path)["inserted"] == 0


def test_main_requires_root(monkeypatch):
    monkeypatch.delenv("RESISTOR_YOUTUBE_ROOT", raising=False)
    with pytest.raises(SystemExit) as exc:
        rep_youtube.main([])
    assert exc.value.code == 2
