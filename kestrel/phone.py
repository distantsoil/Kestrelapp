"""One shared phone-number formatter (D-PHONE-1 to D-PHONE-5).

Pure and offline: the `phonenumbers` package carries its own metadata, and nothing here opens a
socket. Nothing is rejected: text that cannot be read as a phone number comes back exactly as
typed. Formatting changes separators only (D-PHONE-4).
"""

from __future__ import annotations

import locale
import os
import re
from dataclasses import dataclass

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat

DEFAULT_REGION = "US"
UNREADABLE_NOTE = "Kestrel could not read this as a phone number; it is kept as you typed it."
_NANP = 1  # the North American plan: US, Canada and territories share 555-555-0123
_MAX_LEN = 80  # a field this long is not a phone number; keep it as typed without parsing
MAX_LEN = _MAX_LEN  # public: anything read as a number, including a kept spelling, is bounded


@dataclass(frozen=True)
class PhoneResult:
    entered: str          # exactly what the person typed or pasted
    display: str          # what the field shows: formatted, or `entered` when unreadable
    e164: str             # "+15555550123", or "" when unreadable
    ext: str              # "42" or ""
    region: str           # the number's own country (GB for +44); the home country when
    # unreadable, empty when its calling code has no region
    readable: bool

    @property
    def note(self) -> str:
        return "" if self.readable or not self.entered.strip() else UNREADABLE_NOTE


def locale_region() -> str:
    """The country of this computer's locale (en_GB gives GB), else the default. Offline."""
    for raw in (os.environ.get("LC_ALL"), os.environ.get("LC_MESSAGES"), os.environ.get("LANG")):
        m = re.search(r"_([A-Za-z]{2})\b", raw or "")
        if m and m.group(1).upper() in phonenumbers.SUPPORTED_REGIONS:
            return m.group(1).upper()
    try:
        tag = locale.getlocale()[0] or ""
    except (ValueError, TypeError):
        tag = ""
    m = re.search(r"_([A-Za-z]{2})$", tag)
    if m and m.group(1).upper() in phonenumbers.SUPPORTED_REGIONS:
        return m.group(1).upper()
    return DEFAULT_REGION


def clean_region(value: object, fallback: str | None = None) -> str:
    code = str(value or "").strip().upper()
    if code in phonenumbers.SUPPORTED_REGIONS:
        return code
    return fallback if fallback in phonenumbers.SUPPORTED_REGIONS else locale_region()


def _unreadable(text: str, region: str) -> PhoneResult:
    return PhoneResult(entered=text, display=text, e164="", ext="", region=region, readable=False)


def _nanp(n: phonenumbers.PhoneNumber) -> str:
    nat = str(n.national_number)
    return f"{nat[:3]}-{nat[3:6]}-{nat[6:]}"


# What a phone number may contain besides its digits: separators and a plus sign, an optional
# leading "tel:", and one trailing extension. Anything else (a word, a vanity name such as
# 1-800-FLOWERS) is not read: libphonenumber would turn letters into digits and drop words, and
# formatting changes separators only (D-PHONE-4). Such text is kept exactly as typed.
_EXT = re.compile(
    r"(?:;ext=|[\s,]*(?:ext[.]?|extn[.]?|extension|x|#)\s*)\d{1,8}\s*$", re.IGNORECASE)
_SHAPE = re.compile(r"[\d+\-.()/\s\u00a0\u2010-\u2015]*")


def _plain_shape(source: str) -> bool:
    if len(source) > _MAX_LEN:  # bounds the _EXT scan below (CodeQL py/polynomial-redos)
        return False
    body = re.sub(r"^\s*tel:", "", source, flags=re.IGNORECASE)
    body = _EXT.sub("", body)
    return bool(_SHAPE.fullmatch(body))


def _possible(source: str, region: str | None) -> phonenumbers.PhoneNumber | None:
    """`source` read as a number of `region`, or None when it cannot be read or is impossible."""
    if not source or len(source) > _MAX_LEN or not _plain_shape(source):
        return None
    try:
        n = phonenumbers.parse(source, region)
    except NumberParseException:
        return None
    return n if phonenumbers.is_possible_number(n) else None


def _core(n: phonenumbers.PhoneNumber) -> tuple:
    """The number itself, without its extension (the extension is always the text's, D-PHONE-4)."""
    return (n.country_code, n.national_number, n.italian_leading_zero or False,
            n.number_of_leading_zeros or 1)


def hint_matches(text: object, hint: object, region: object) -> bool:
    """True when `hint` (an E.164) is the number `text` reads as when read under `region`, which
    is always the SERVER's region (the selector for new text, the stored home country for stored
    text), never one the hint or the page chose. An extension on the hint is not compared: the
    extension is whatever the text says. A stale, forged or mismatched hint fails and is ignored."""
    h = str(hint or "").strip().split(";", 1)[0]
    n = _possible(h, None) if h.startswith("+") else None
    t = _possible(str(text or "").strip(), clean_region(region))
    return n is not None and t is not None and _core(n) == _core(t)


def format_phone(text: object, country: object = None, e164: object = None,
                 *, read_as: object = None) -> PhoneResult:
    """Read `text` and format it for a person whose home country is `country` (the selector).

    The home country does two things only: it is the default region for a number typed WITHOUT a
    plus sign, and it decides the display. A number with a plus sign is never reinterpreted by the
    selector, and typing a foreign number never changes the home country (D-PHONE-5):
    - the number's country calling code equals the home country's: national format. A shared
      numbering plan counts as the same country (Canada on a US home, Guernsey on a UK home,
      D-PHONE-5);
    - otherwise: international format (`+44 20 7946 0000`).
    `e164` (stored or hinted, one rule for both) is the number only when `text` reads as that
    number under `read_as` (the country the text was written under: the stored home country for
    stored text, else the selector). Then a change of home country reformats the pinned number and
    never re-reads it. Otherwise the hint is ignored and the text is read afresh under the
    selector. The extension is always the text's own.
    """
    entered = str(text or "")
    home = clean_region(country)
    stripped = entered.strip()
    hint = str(e164 or "").strip()
    n = None
    if hint and hint_matches(stripped, hint, read_as if read_as else home):
        n = _possible(stripped, clean_region(read_as if read_as else home))
    if n is None:
        n = _possible(stripped, home)
    if n is None:
        return _unreadable(entered, home)
    ext = n.extension or ""
    bare = phonenumbers.PhoneNumber(
        country_code=n.country_code, national_number=n.national_number,
        italian_leading_zero=n.italian_leading_zero,
        number_of_leading_zeros=n.number_of_leading_zeros,
    )
    if n.country_code == phonenumbers.country_code_for_region(home):
        body = _nanp(n) if n.country_code == _NANP else (
            phonenumbers.format_number(bare, PhoneNumberFormat.NATIONAL))
    else:
        body = phonenumbers.format_number(bare, PhoneNumberFormat.INTERNATIONAL)
    display = f"{body} x{ext}" if ext else body
    main = phonenumbers.region_code_for_country_code(n.country_code or 0)
    num_region = phonenumbers.region_code_for_number(n) or (
        main if main in phonenumbers.SUPPORTED_REGIONS else "")  # "" when the code has no region
    return PhoneResult(
        entered=entered, display=display,
        e164=phonenumbers.format_number(bare, PhoneNumberFormat.E164), ext=ext,
        region=num_region, readable=True,
    )


def display_only(text: object, home: object = None) -> str:
    """A stored contact's phone for display: formatted when readable, else exactly as stored."""
    res = format_phone(text, home)
    return res.display if res.readable else str(text or "")


# Names for the country selector (D-PHONE-5). Any region libphonenumber knows is accepted when
# typed as a +code; this list is only what the dropdown offers, plus the saved choice.
_NAMES = {
    "US": "United States", "CA": "Canada", "GB": "United Kingdom", "IE": "Ireland",
    "AU": "Australia", "NZ": "New Zealand", "DE": "Germany", "FR": "France", "ES": "Spain",
    "IT": "Italy", "NL": "Netherlands", "BE": "Belgium", "CH": "Switzerland", "AT": "Austria",
    "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland", "PL": "Poland",
    "PT": "Portugal", "IN": "India", "SG": "Singapore", "HK": "Hong Kong", "JP": "Japan",
    "KR": "South Korea", "AE": "United Arab Emirates", "SA": "Saudi Arabia", "IL": "Israel",
    "ZA": "South Africa", "NG": "Nigeria", "KE": "Kenya", "EG": "Egypt", "BR": "Brazil",
    "MX": "Mexico", "AR": "Argentina", "CL": "Chile", "CO": "Colombia", "PH": "Philippines",
    "MY": "Malaysia", "ID": "Indonesia", "TH": "Thailand", "VN": "Vietnam", "PK": "Pakistan",
    "CN": "China", "TR": "Turkey", "UA": "Ukraine", "CZ": "Czechia", "RO": "Romania",
}


def region_options(selected: object = None) -> list[tuple[str, str]]:
    """(code, label) for the selector, United States first, the saved choice always present."""
    chosen = clean_region(selected)
    codes = ["US"] + sorted((c for c in _NAMES if c != "US"), key=lambda c: _NAMES[c])
    if chosen not in codes:
        codes.insert(1, chosen)
    return [
        (c, f"{_NAMES.get(c, c)} (+{phonenumbers.country_code_for_region(c)})") for c in codes
    ]


def source_of(e164: object, ext: object = "") -> str:
    """The pinned number the page carries beside the field: E.164, plus `;ext=N` when the number
    has an extension (the extension is not part of E.164, D-PHONE-4)."""
    base = str(e164 or "").strip()
    tail = str(ext or "").strip()
    if ";ext=" in base:  # already carries its extension
        return base
    return f"{base};ext={tail}" if base and tail else base


def pin(text: object, e164: object, country: object) -> str:
    """The pinned source to carry beside a field, under the server's region `country` (the stored
    home for stored text). A number saved before E.164 was kept is resolved ONCE under that home
    and carried as its E.164, so a later change of home country never re-regions it (D-PHONE-5).
    An E.164 that is not the number `text` reads as (the old number beside newly typed text) is
    dropped and the text is resolved afresh. The extension is the text's own."""
    res = format_phone(text, country, e164)
    return source_of(res.e164, res.ext) if res.readable else ""
