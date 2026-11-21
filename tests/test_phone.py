"""Phone formatting (#207, D-PHONE-1 to D-PHONE-5). Invented numbers only."""

from __future__ import annotations

import re
import socket

import pytest

from kestrel import phone as ph


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


@pytest.mark.parametrize("typed", [
    "5555550123", "+15555550123", "+1 555 555 0123", "(555) 555-0123", "555.555.0123",
    "  555 555 0123  ", "1-555-555-0123",
])
def test_us_ten_digits_with_and_without_country_code(typed):
    r = ph.format_phone(typed, "US")
    assert r.display == "555-555-0123" and r.e164 == "+15555550123" and r.readable
    assert r.entered == typed and r.region == "US" and r.note == ""


def test_pasted_text_with_separators_keeps_the_digits():
    typed = "Tel: (555) 555.0123"
    r = ph.format_phone(typed, "US")
    assert r.display == "555-555-0123" and _digits(r.display) == "5555550123"


def test_uk_and_german_numbers_use_their_national_format():
    uk = ph.format_phone("020 7946 0123", "GB")
    assert uk.display == "020 7946 0123" and uk.e164 == "+442079460123" and uk.region == "GB"
    de = ph.format_phone("+49 30 123456", "DE")
    assert de.display == "030 123456" and de.e164 == "+4930123456"


def test_a_number_from_another_country_shows_internationally():
    r = ph.format_phone("+44 20 7946 0123", "US")
    assert r.display == "+44 20 7946 0123" and r.region == "GB" and r.e164 == "+442079460123"


@pytest.mark.parametrize("typed,e164,shown", [
    ("+44 20 7946 0000", "+442079460000", "+44 20 7946 0000"),
    ("+39 06 6982 0123", "+390669820123", "+39 06 6982 0123"),
    ("+49 30 123456", "+4930123456", "+49 30 123456"),
])
def test_a_foreign_number_on_a_us_home_keeps_its_plus_and_every_digit(typed, e164, shown):
    r = ph.format_phone(typed, "US")
    assert r.display == shown and r.e164 == e164 and r.readable
    assert _digits(r.display) == _digits(r.e164)  # D-PHONE-4: a foreign number's digits are exact
    again = ph.format_phone(r.display, "US")  # saving the display never changes it
    assert again.display == r.display and again.e164 == r.e164


def test_a_us_number_on_a_uk_home_shows_internationally():
    r = ph.format_phone("+1 555 555 0123", "GB")
    assert r.display == "+1 555-555-0123" and r.e164 == "+15555550123"
    assert _digits(r.display) == _digits(r.e164)


def test_the_home_country_only_defaults_numbers_typed_without_a_plus():
    # With a plus the selector never reinterprets the number, only the display changes.
    assert ph.format_phone("+44 20 7946 0000", "GB").e164 == "+442079460000"
    assert ph.format_phone("+44 20 7946 0000", "GB").display == "020 7946 0000"
    assert ph.format_phone("+44 20 7946 0000", "US").e164 == "+442079460000"
    # Without a plus it does.
    assert ph.format_phone("030 123456", "DE").e164 == "+4930123456"
    assert ph.format_phone("020 7946 0000", "GB").e164 == "+442079460000"


def test_home_display_adds_only_the_home_trunk_prefix_to_the_e164_digits():
    for typed, home in [("020 7946 0000", "GB"), ("030 123456", "DE"), ("5555550123", "US")]:
        r = ph.format_phone(typed, home)
        nsn = _digits(r.e164)[len(str(__import__("phonenumbers").country_code_for_region(home))):]
        assert _digits(r.display) in (nsn, "0" + nsn), (typed, r.display, r.e164)


def test_changing_the_home_country_reformats_the_stored_number():
    r = ph.format_phone("020 7946 0000", "US", e164="+442079460000", read_as="GB")
    assert r.display == "+44 20 7946 0000"  # the text was written under GB, the home is now US
    r = ph.format_phone("+44 20 7946 0000", "GB", e164="+442079460000")
    assert r.display == "020 7946 0000"
    assert ph.format_phone("ask Dana", "GB", e164="").display == "ask Dana"


def test_extension_is_kept():
    for typed in ("555-555-0123 ext. 42", "555-555-0123 x42", "555-555-0123;ext=42"):
        r = ph.format_phone(typed, "US")
        assert r.display == "555-555-0123 x42" and r.ext == "42" and r.e164 == "+15555550123"


@pytest.mark.parametrize("typed", ["call me", "12", "ask for Dana", "+++", "555", "x" * 200])
def test_unparseable_is_kept_exactly_and_never_rejected(typed):
    r = ph.format_phone(typed, "US")
    assert not r.readable and r.display == typed and r.e164 == "" and r.entered == typed
    assert r.note == ph.UNREADABLE_NOTE


def test_empty_has_no_note():
    r = ph.format_phone("", "US")
    assert r.display == "" and r.note == "" and not r.readable


@pytest.mark.parametrize("typed,region,digits", [
    ("5555550123", "US", "5555550123"),
    ("020 7946 0123", "GB", "02079460123"),   # the home trunk prefix is the one addition
    ("+1 555 555 0123", "US", "5555550123"),  # the matching country code is the one omission
])
def test_digits_change_only_by_the_matching_country_code_or_the_home_trunk_prefix(
        typed, region, digits):
    assert _digits(ph.format_phone(typed, region).display) == digits


def test_unknown_country_falls_back_not_fails():
    assert ph.format_phone("5555550123", "ZZ").readable


def test_locale_region_from_environment(monkeypatch):
    for var in ("LC_ALL", "LC_MESSAGES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "en_GB.UTF-8")
    assert ph.locale_region() == "GB"
    monkeypatch.setenv("LANG", "C")
    monkeypatch.setattr(ph.locale, "getlocale", lambda: (None, None))
    assert ph.locale_region() == "US"


def test_display_only_leaves_unreadable_contact_text_alone():
    assert ph.display_only("5555550123", "US") == "555-555-0123"
    assert ph.display_only("ask the front desk", "US") == "ask the front desk"


def test_formatting_uses_no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("phone handling touched the network")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    assert ph.format_phone("+44 20 7946 0123", "US").readable
    assert ph.format_phone("5555550123", "US").display == "555-555-0123"


def test_a_number_saved_without_e164_is_resolved_once_then_never_re_regioned():
    """Round 3: 555-555-0123 saved on a US home became +445555550123 when home changed to GB."""
    pinned = ph.pin("555-555-0123", "", "US")
    assert pinned == "+15555550123"
    # The home country changed to GB; the text was written under US, which the server remembers.
    after = ph.format_phone("555-555-0123", "GB", pinned, read_as="US")
    assert after.e164 == "+15555550123" and after.display == "+1 555-555-0123"
    assert ph.pin("555-555-0123 x42", "", "US") == "+15555550123;ext=42"
    assert ph.pin("ask Dana", "", "US") == ""  # an unreadable entry has no number to pin
    assert ph.pin("020 7946 0000", "+442079460000", "GB") == "+442079460000"  # still its number
    assert ph.pin("5555550123", "+442079460000", "US") == "+15555550123"  # old number, new text


def test_the_extension_travels_beside_the_number_and_is_not_part_of_e164():
    assert ph.source_of("+15555550123", "42") == "+15555550123;ext=42"
    assert ph.source_of("+15555550123;ext=42", "42") == "+15555550123;ext=42"  # never twice
    r = ph.format_phone("+1 555-555-0123 x42", "GB", "+15555550123;ext=42")
    assert r.e164 == "+15555550123" and r.ext == "42" and r.display == "+1 555-555-0123 x42"
    assert _digits(r.display) == _digits(r.e164) + r.ext  # D-PHONE-4: extension digits follow


def test_a_number_reports_its_own_country_not_the_home_country():
    r = ph.format_phone("+1 555 555 0123 x42", "GB")
    assert r.region == "US" and r.display == "+1 555-555-0123 x42"


def test_a_shared_numbering_plan_counts_as_the_same_country():
    """D-PHONE-5: national display compares calling codes, so Canada on a US home and Guernsey on
    a UK home show nationally."""
    assert ph.format_phone("+1 416 555 0123", "US").display == "416-555-0123"
    assert ph.format_phone("+44 1481 700000", "GB").display == "01481 700000"


def test_a_hint_that_is_not_the_number_the_text_reads_as_is_ignored():
    """Round 4: the client's E.164 is only a hint. Mismatched, invalid or stale ones lose."""
    # A valid number that is not the one typed.
    r = ph.format_phone("555-555-0123", "US", "+447911123456")
    assert r.e164 == "+15555550123" and r.display == "555-555-0123"
    # An invalid E.164 falls back to the text, it never makes the entry unreadable.
    r = ph.format_phone("555-555-0123", "US", "+1555")
    assert r.readable and r.e164 == "+15555550123"
    # The old number beside new text.
    r = ph.format_phone("020 7946 0000", "GB", "+15555550123")
    assert r.e164 == "+442079460000"
    assert ph.hint_matches("555-555-0123", "+15555550123", "US")
    assert not ph.hint_matches("555-555-0123", "+447911123456", "US")
    assert not ph.hint_matches("555-555-0123", "+1555", "US")
    assert not ph.hint_matches("555-555-0123", "5555550123", "US")  # not an E.164 at all
    # The region is the server's: a hint cannot pick it (round 5).
    assert ph.hint_matches("020 7946 0000", "+442079460000", "GB")
    assert not ph.hint_matches("020 7946 0000", "+442079460000", "US")
    r = ph.format_phone("020 7946 0000", "US", "+442079460000")  # forged: text read under US
    assert r.e164 != "+442079460000"
    # The hint's extension is not compared; the extension is the text's own.
    assert ph.hint_matches("555-555-0123", "+15555550123;ext=42", "US")
    assert ph.format_phone("555-555-0123", "US", "+15555550123;ext=42").ext == ""
    assert ph.pin("555-555-0123", "+15555550123;ext=42", "US") == "+15555550123"


def test_a_pinned_number_is_read_against_the_country_its_text_was_written_under():
    r = ph.format_phone("020 7946 0000", "US", "+442079460000", read_as="GB")
    assert r.display == "+44 20 7946 0000" and r.e164 == "+442079460000"
    # Under the wrong country the same hint does not match, and is ignored.
    assert ph.format_phone("020 7946 0000", "US", "+442079460000", read_as="US").e164 != (
        "+442079460000")


@pytest.mark.parametrize("typed", [
    "1-800-FLOWERS", "Contact (555) 555.0123", "call 555-555-0123", "555-555-0123 ask for Dana",
    "555 555 0123 or 555 555 0199", "five five five 555 0123",
])
def test_words_and_letters_are_never_read_as_digits(typed):
    """Round 6 (4): libphonenumber turns 1-800-FLOWERS into 1-800-356-9377 and drops leading
    words; that changes digits and text. Such entries are kept exactly as typed."""
    r = ph.format_phone(typed, "US")
    assert not r.readable and r.display == typed and r.e164 == ""


@pytest.mark.parametrize("typed", [
    "tel:+1-555-555-0123", "TEL: (555) 555.0123", "555-555-0123 ext. 42", "555-555-0123 x42",
    "555-555-0123;ext=42", "555-555-0123 extension 42", "555-555-0123 #42",
    "\uff15\uff15\uff15-\uff15\uff15\uff15-\uff10\uff11\uff12\uff13",  # fullwidth digits
])
def test_labels_extensions_and_other_scripts_digits_still_read(typed):
    r = ph.format_phone(typed, "US")
    assert r.readable and r.e164 == "+15555550123"


def test_the_shape_check_refuses_text_past_the_length_cap():
    """CodeQL py/polynomial-redos: the extension scan only ever sees a field of at most _MAX_LEN."""
    from kestrel import phone

    assert phone._plain_shape("+1 555 010 0199 ext. 12")
    assert not phone._plain_shape("1" + " " * phone._MAX_LEN + "x1")
    assert not phone._plain_shape(" " * 20_000 + "ext 1")
