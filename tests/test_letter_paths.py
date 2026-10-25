"""Redrafts with the same date/recipient/topic prefix get their own file."""

from src import letter


def test_redraft_does_not_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(letter, "LETTERS_DIR", tmp_path)
    topic = "Ethan's Law hearing after Bend (S. 726)"
    v2 = letter.write_letter_markdown("Ron Wyden", topic + " v2", "v2 body", "fax")
    v3 = letter.write_letter_markdown("Ron Wyden", topic + " v3", "v3 body", "fax")
    assert v2 != v3
    assert v3.name.endswith("-2.md")
    letter.update_markdown_status(v2, "rejected")
    assert "v2 body" in v2.read_text(encoding="utf-8")
    assert "**Status:** draft" in v3.read_text(encoding="utf-8")


def test_colon_in_topic_writes_a_real_file(tmp_path, monkeypatch):  # #9387
    monkeypatch.setattr(letter, "LETTERS_DIR", tmp_path)
    path = letter.write_letter_markdown(
        "Val Hoyle", "Vote against, not for: back a method", "the body", "mail")
    assert ":" not in path.name and path.suffix == ".md"
    assert "the body" in path.read_text(encoding="utf-8")
