"""Location parsing and remote classification (SPEC.md 5.4). location_raw is always retained by the
caller. Remote type defaults to `unknown` and is NEVER guessed as `onsite`."""

from __future__ import annotations

_US_STATES = {
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "DC",
}
_COUNTRY_TOKENS = {
    "USA": "US",
    "US": "US",
    "U.S.": "US",
    "UNITED STATES": "US",
    "UK": "GB",
    "UNITED KINGDOM": "GB",
    "CANADA": "CA",
}


def parse_location(location_raw: str | None) -> tuple[str | None, str | None, str | None]:
    """Return (country, state, city). Best-effort; unknown parts are None."""
    if not location_raw:
        return (None, None, None)
    text = location_raw.strip()
    parts = [p.strip() for p in text.split(",") if p.strip()]
    country: str | None = None
    state: str | None = None
    city: str | None = None

    # Country only when a whole comma-part equals a country token (never a substring: "Columbus"
    # and "Cyprus" must not become US).
    parts_upper = [p.upper() for p in parts]
    for token, code in _COUNTRY_TOKENS.items():
        if token in parts_upper:
            country = code
            break

    for p in parts:
        if p.upper() in _US_STATES:
            state = p.upper()
            country = country or "US"

    if parts and parts[0].upper() not in _US_STATES and parts[0].upper() not in _COUNTRY_TOKENS:
        if "remote" not in parts[0].lower():
            city = parts[0]
    return (country, state, city)


def classify_remote(
    *,
    title: str | None = None,
    description: str | None = None,
    hint: str | None = None,
    is_remote: bool | None = None,
) -> str:
    """remote | hybrid | onsite | unknown. Bias to unknown; only an explicit onsite signal yields
    onsite."""
    if is_remote is True:
        return "remote"
    if hint:
        h = hint.lower()
        if "remote" in h:
            return "remote"
        if "hybrid" in h:
            return "hybrid"
        if "onsite" in h or "on-site" in h or "in office" in h or "in-office" in h:
            return "onsite"

    blob = f"{title or ''} {(description or '')[:500]}".lower()
    if "hybrid" in blob:
        return "hybrid"
    if (
        "fully remote" in blob
        or "100% remote" in blob
        or "remote-first" in blob
        or "remote first" in blob
    ):
        return "remote"
    if "remote" in blob:
        return "remote"
    if "on-site" in blob or "onsite" in blob or "in-office" in blob or "in office" in blob:
        return "onsite"
    return "unknown"
