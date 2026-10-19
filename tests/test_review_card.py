from src.review_card import build_card, verify_card

HEAD = "A Constituent\nSpringfield, ST 00000\n\nOctober 19, 2026"
SIGN = "Sincerely,\nA Constituent"
LETTERS = [
    {"id": 50, "recipient": "Ron Wyden", "channel": "fax",
     "body": f"{HEAD}\n\nDear Senator Wyden,\n\nShared argument.\n\nWyden ask.\n\n{SIGN}"},
    {"id": 51, "recipient": "Val Hoyle", "channel": "mail",
     "body": f"{HEAD}\n\nDear Representative Hoyle,\n\nShared argument.\n\nHoyle ask.\n\n{SIGN}"},
]


def test_shared_text_shown_once_and_reassembles():
    card = build_card(LETTERS, "# Batch")
    assert card.count("Shared argument.") == 1
    assert "Wyden ask." in card and "Hoyle ask." in card
    assert "**This letter is:** S1-S2 + its greeting + S3 + its own paragraph + S4" in card
    assert verify_card(card, LETTERS) == []


def test_one_changed_character_fails():
    card = build_card(LETTERS).replace("Hoyle ask.", "Hoyle ask!")
    assert verify_card(card, LETTERS) == ["#51: reassembled text differs from the queued body"]


def test_no_shared_paragraphs_still_reassembles():
    solo = [dict(LETTERS[0], body="Dear X,\n\nOnly mine."), dict(LETTERS[1], body="Dear Y,\n\nAlso mine.")]
    card = build_card(solo)
    assert "Shared text" not in card
    assert verify_card(card, solo) == []


def test_card_has_no_raw_html_besides_markers():
    import re
    card = build_card(LETTERS)
    rest = re.sub(r"^<!-- (?:p:\w+|assemble #\d+: [\w ]+) -->$", "", card, flags=re.M)
    assert "<" not in rest
