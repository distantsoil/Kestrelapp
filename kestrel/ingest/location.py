"""Location parsing and remote classification (SPEC.md 5.4). location_raw is always retained by the
caller. Remote type defaults to `unknown` and is NEVER guessed as `onsite`."""

from __future__ import annotations

import csv
import re
from importlib.resources import files

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
# Full state names, so "North Carolina" parses as NC, not as a city (owner round 14).
_STATE_NAMES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA",
    "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE", "FLORIDA": "FL", "GEORGIA": "GA",
    "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT",
    "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM", "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND",
    "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT",
    "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI", "WYOMING": "WY", "DISTRICT OF COLUMBIA": "DC",
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


def _country_key(text: str) -> str:
    return " ".join(text.split()).casefold()


def _load_country_codes() -> dict[str, str]:
    codes: dict[str, str] = {}
    with (
        files("kestrel.resources")
        .joinpath("iso3166.csv")
        .open("r", encoding="utf-8", newline="") as country_file
    ):
        for row in csv.DictReader(country_file):
            tokens = [row["alpha_2"], row["alpha_3"], row["numeric"], row["name"]]
            tokens.extend(row["aliases"].split("|"))
            for token in tokens:
                if token:
                    codes[_country_key(token)] = row["alpha_2"]
    return codes


_COUNTRY_CODES = _load_country_codes()


def parse_location(location_raw: str | None) -> tuple[str | None, str | None, str | None]:
    """Return (country, state, city). Best-effort; unknown parts are None."""
    if not location_raw:
        return (None, None, None)
    text = location_raw.strip()
    remote_match = re.fullmatch(
        r"(.+?)(?:\s*-\s*|\s+)remote|remote(?:\s*-\s*|\s*\(\s*|\s+)(.+?)\)?",
        text,
        flags=re.IGNORECASE,
    )
    if remote_match:
        remote_country = remote_match.group(1) or remote_match.group(2)
        if _COUNTRY_CODES.get(_country_key(remote_country)) == "US":
            return ("US", None, None)
    parts = [p.strip() for p in text.split(",") if p.strip()]
    # A country named first wins ("US, Los Angeles, CA", "Canada, Toronto, ON"), unless that first
    # part is also a US state ("Georgia, Atlanta", "CA, Los Angeles"), which stays a state.
    whole = _COUNTRY_CODES.get(_country_key(text))
    if whole and len(parts) >= 2:  # a country name with a comma: "Palestine, State of"
        return (whole, None, None)
    if len(parts) >= 2:
        lead = parts[0]
        lead_code = _COUNTRY_CODES.get(_country_key(lead))
        if lead_code and lead.upper() not in _US_STATES and lead.upper() not in _STATE_NAMES:
            if lead_code == "US":
                _, rest_state, rest_city = parse_location(", ".join(parts[1:]))
                return ("US", rest_state, rest_city)
            return (lead_code, None, parts[1])
    country: str | None = None
    state: str | None = None
    city: str | None = None

    country_start: int | None = None
    for index in range(len(parts)):
        suffix = ", ".join(parts[index:])
        if suffix.upper() in _STATE_NAMES:
            continue
        if suffix.upper() in _US_STATES and not suffix.islower() and len(parts) < 3:
            continue
        if code := _COUNTRY_CODES.get(_country_key(suffix)):
            country, country_start = code, index
            break
    if country is None:
        parts_upper = [p.upper() for p in parts]
        for token, code in _COUNTRY_TOKENS.items():
            if token in parts_upper:
                country = code
                break
    if country and country != "US":
        if (
            country_start != 0
            and parts
            and parts[0].upper() not in _COUNTRY_TOKENS
            and "remote" not in parts[0].lower()
        ):
            city = parts[0]
        return (country, None, city)

    named = None  # the comma-part that was a full state name
    for p in parts:
        code = p.upper().replace(".", "")  # "D.C." is DC
        if code in _US_STATES:
            state = code
            country = country or "US"
    if state is None:  # a code wins: "Washington, DC" is the city of Washington in DC
        # The last state name wins: "Washington, District of Columbia" is DC.
        for p in parts:
            if p.upper() in _STATE_NAMES:
                state, named = _STATE_NAMES[p.upper()], p
                country = country or "US"

    if (
        parts
        and country_start != 0
        and parts[0].upper() not in _US_STATES
        and parts[0].upper() not in _COUNTRY_TOKENS
        and parts[0] is not named
    ):
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
