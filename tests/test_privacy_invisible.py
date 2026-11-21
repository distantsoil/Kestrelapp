"""Confidential mode cannot be bypassed with characters that render as nothing (#210 review,
D-PRIV-1 amended). Every Unicode format character, every variation selector and the combining
grapheme joiner, anywhere inside a listed name, still match it, on every surface that uses
EmployerMap, and an online send's code restores exactly the text it replaced. Invented names."""

from __future__ import annotations

import sys
import unicodedata

from kestrel.privacy import Employer, EmployerMap

_MAP = EmployerMap([Employer(name="Bluefinch Bakery", shown_as="Current Employer")],
                   protected=["Bluefinch Labs"])


def _invisible() -> list[str]:
    cf = [chr(c) for c in range(sys.maxunicode + 1) if unicodedata.category(chr(c)) == "Cf"]
    return cf + ["͏", *map(chr, range(0xFE00, 0xFE10)), *map(chr, range(0xE0100, 0xE01F0))]


def test_every_invisible_character_inside_or_between_the_words_still_matches():
    chars = _invisible()
    assert len(chars) > 400  # the table, not a hand list: 170 or so Cf plus the selectors
    missed = []
    for ch in chars:
        for text in (f"Join Blue{ch}finch Bakery now", f"Join Bluefinch{ch} Bakery now",
                     f"Join Bluefinch {ch}Bakery now", f"Join B{ch}{ch}luefinch Baker{ch}y now",
                     f"Join BLUEFINCH BAK{ch}ERY now"):
            if _MAP.redact(text) != "Join Current Employer now":
                missed.append((hex(ord(ch)), text))
    assert missed == []


def test_compatibility_letters_read_as_the_name_and_only_the_span_changes():
    for text in ("Join \uff22\uff4c\uff55\uff45finch Bakery now",  # fullwidth "Blue"
                 "Join \U0001d401luefinch Bakery now",  # mathematical bold B
                 "Join Bluefinch\u00a0Bakery now", "Join Bluefinch\u2003Bakery now"):
        assert _MAP.redact(text) == "Join Current Employer now", text
    # Text outside the name is never rewritten, compatibility characters included.
    assert _MAP.redact("\uff21 \u2122 Bluefinch Bakery \ufb01le") == (
        "\uff21 \u2122 Current Employer \ufb01le")


def test_a_name_listed_in_compatibility_letters_matches_the_plain_text():
    emap = EmployerMap([Employer(name="\uff22luefinch\u00a0Bakery", shown_as="Current Employer")])
    assert emap.redact("Join Bluefinch Bakery now") == "Join Current Employer now"


def test_visible_spellings_stay_exact():
    # Operator-controlled and exact (D-PRIV-1): a visible change is a different spelling, which
    # the operator adds as its own chip; it is not guessed.
    for text in ("Bluefinch-Bakery", "Blue finch Bakery", "bluefinch bakery", "Bluefinchx Bakery"):
        assert _MAP.redact(text) == text


def test_a_protected_label_still_wins_with_invisible_characters():
    assert _MAP.redact("Bluefinch​ Labs and Bluefinch Bakery") == (
        "Bluefinch​ Labs and Current Employer")


def test_an_online_send_code_restores_exactly_the_text_it_replaced():
    book = _MAP.codebook()
    raw = "Join Blue‎finch­ Bakery today"
    sent = book.encode(raw)
    assert "Bluefinch" not in sent.replace("‎", "").replace("­", "")
    assert book.decode(sent) == raw


def test_the_posting_renderer_hides_a_name_written_with_an_entity_or_mark():
    from kestrel.web import posting_format as pf
    from kestrel.web.redact import redact_text

    def hide(t: str) -> str:
        return redact_text(t, _MAP, enabled=True) or ""

    for raw in ("Join Blue&#8206;finch Bakery", "Join Blue⁣finch Bakery",
                "Join Blue&shy;finch Bakery", "Join *Blue͏finch* Bakery"):
        seen = pf.plain_text(raw, hide)
        assert "Current Employer" in seen and "Bluefinch" not in "".join(
            ch for ch in seen if unicodedata.category(ch) != "Cf"), (raw, seen)
