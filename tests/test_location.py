from __future__ import annotations

import csv
from importlib.resources import files

import pytest

from kestrel.ingest.location import parse_location


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Dublin, ie", ("IE", None, "Dublin")),
        ("Seoul, kr", ("KR", None, "Seoul")),
        ("Staines, gb", ("GB", None, "Staines")),
        ("Amsterdam, nl", ("NL", None, "Amsterdam")),
        ("Søborg, dk", ("DK", None, "Søborg")),
        ("Singapore, Singapore, sg", ("SG", None, "Singapore")),
        ("London, GREATER LONDON, gb", ("GB", None, "London")),
        ("Toronto, Ontario, CA", ("CA", None, "Toronto")),
        ("Toronto, ON, ca", ("CA", None, "Toronto")),
        ("Bangalore, Karnataka, India", ("IN", None, "Bangalore")),
        ("Greater Noida, Uttar Pradesh, India", ("IN", None, "Greater Noida")),
        ("Turino, Italy", ("IT", None, "Turino")),
        ("Spain", ("ES", None, None)),
        ("India", ("IN", None, None)),
        ("Italy", ("IT", None, None)),
        ("United States - Remote", ("US", None, None)),
        ("Remote - United States", ("US", None, None)),
        ("Remote (US)", ("US", None, None)),
        ("US Remote", ("US", None, None)),
        ("United States of America", ("US", None, None)),
        ("Remote (U.S.)", ("US", None, None)),
        ("usa remote", ("US", None, None)),
        ("London, GBR", ("GB", None, "London")),
        ("Toronto, CAN", ("CA", None, "Toronto")),
        ("Dublin, 372", ("IE", None, "Dublin")),
        ("Tirana, 008", ("AL", None, "Tirana")),
        ("Berlin, DEU", ("DE", None, "Berlin")),
        ("Los Angeles, CA", ("US", "CA", "Los Angeles")),
        ("City, IN", ("US", "IN", "City")),
        ("City, DE", ("US", "DE", "City")),
        ("City, GA", ("US", "GA", "City")),
        ("City, CO", ("US", "CO", "City")),
        ("City, PA", ("US", "PA", "City")),
        ("City, MA", ("US", "MA", "City")),
        ("City, Region, IN", ("IN", None, "City")),
        ("City, Region, DE", ("DE", None, "City")),
        ("City, Region, GA", ("GA", None, "City")),
        ("City, Region, CO", ("CO", None, "City")),
        ("City, Region, PA", ("PA", None, "City")),
        ("City, Region, MA", ("MA", None, "City")),
        ("City, ca", ("CA", None, "City")),
        ("City, in", ("IN", None, "City")),
        ("City, de", ("DE", None, "City")),
        ("City, ga", ("GA", None, "City")),
        ("City, co", ("CO", None, "City")),
        ("City, pa", ("PA", None, "City")),
        ("City, ma", ("MA", None, "City")),
        ("Paris, Ile-de-France, France", ("FR", None, "Paris")),
        ("LA, France", ("FR", None, "LA")),
        ("Washington, United Kingdom", ("GB", None, "Washington")),
        ("Georgia", ("US", "GA", None)),
        ("Atlanta, Georgia", ("US", "GA", "Atlanta")),
        ("Tbilisi, GE", ("GE", None, "Tbilisi")),
        ("Palestine, State of", ("PS", None, None)),
        ("Ramallah, Palestine, State of", ("PS", None, "Ramallah")),
        ("Bonaire, Sint Eustatius and Saba", ("BQ", None, None)),
        ("Kralendijk, Bonaire, Sint Eustatius and Saba", ("BQ", None, "Kralendijk")),
        ("Seoul, Korea, Republic of", ("KR", None, "Seoul")),
    ],
)
def test_country_locations(raw: str, expected: tuple[str | None, str | None, str | None]) -> None:
    assert parse_location(raw) == expected


@pytest.mark.parametrize(
    "code",
    "AL AR AZ CA CO DE GA ID IL IN KY LA MA MD ME MN MO MS MT NC NE PA SC SD TN VA".split(),
)
def test_all_shared_state_and_country_codes(code: str) -> None:
    assert parse_location(f"City, {code}") == ("US", code, "City")
    assert parse_location(f"City, Region, {code}") == (code, None, "City")
    assert parse_location(f"City, {code.lower()}") == (code, None, "City")


@pytest.mark.parametrize(
    ("name", "country"),
    [
        ("USA", "US"),
        ("U.S.", "US"),
        ("United States", "US"),
        ("UK", "GB"),
        ("Great Britain", "GB"),
        ("United Kingdom", "GB"),
        ("Republic of Korea", "KR"),
        ("South Korea", "KR"),
        ("Czechia", "CZ"),
        ("Czech Republic", "CZ"),
        ("Türkiye", "TR"),
        ("Turkey", "TR"),
        ("Ivory Coast", "CI"),
        ("Vietnam", "VN"),
        ("Russia", "RU"),
        ("Laos", "LA"),
        ("Swaziland", "SZ"),
        ("Cape Verde", "CV"),
    ],
)
def test_country_alternative_names(name: str, country: str) -> None:
    assert parse_location(name) == (country, None, None)
    assert parse_location(f"City, {name}") == (country, None, "City")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, (None, None, None)),
        ("", (None, None, None)),
        ("   , , ", (None, None, None)),
        ("Remote", (None, None, None)),
        ("2 Locations", (None, None, "2 Locations")),
        ("Multiple locations", (None, None, "Multiple locations")),
        ("Nowhere, ZZ", (None, None, "Nowhere")),
        ("Wakanda", (None, None, "Wakanda")),
        ("Columbus", (None, None, "Columbus")),
        ("Remote (business)", (None, None, None)),
        ("Austin, TX", ("US", "TX", "Austin")),
        ("Austin, tx", ("US", "TX", "Austin")),
        ("Austin, TX, USA", ("US", "TX", "Austin")),
        ("Austin, Texas, US", ("US", "TX", "Austin")),
        ("US, Austin, TX", ("US", "TX", "Austin")),
        ("UK, London", ("GB", None, "London")),
        ("Canada, Toronto", ("CA", None, "Toronto")),
        ("North Carolina", ("US", "NC", None)),
        ("Raleigh, North Carolina", ("US", "NC", "Raleigh")),
        ("Washington, DC", ("US", "DC", "Washington")),
        ("Washington, D.C.", ("US", "DC", "Washington")),
        ("Washington, District of Columbia", ("US", "DC", "Washington")),
        ("DC", ("US", "DC", None)),
        ("D.C.", ("US", "DC", "D.C.")),
        ("CA", ("US", "CA", None)),
        ("Remote, US", ("US", None, None)),
        ("New York - Remote", (None, None, None)),
    ],
)
def test_existing_and_unknown_locations(
    raw: str | None, expected: tuple[str | None, str | None, str | None]
) -> None:
    assert parse_location(raw) == expected


def test_iso_table_is_complete_and_codes_are_unique() -> None:
    with (
        files("kestrel.resources")
        .joinpath("iso3166.csv")
        .open("r", encoding="utf-8", newline="") as country_file
    ):
        reader = csv.DictReader(country_file)
        rows = list(reader)
    assert reader.fieldnames == ["alpha_2", "alpha_3", "numeric", "name", "aliases"]
    assert len(rows) == 249
    for column, width in (("alpha_2", 2), ("alpha_3", 3), ("numeric", 3)):
        values = [row[column] for row in rows]
        assert len(set(values)) == 249
        assert all(len(value) == width for value in values)
        assert all(
            value.isdecimal() if column == "numeric" else value.isupper() for value in values
        )
    for row in rows:
        assert set(row) == set(reader.fieldnames)
        country = row["alpha_2"]
        for column in ("alpha_2", "alpha_3", "numeric"):
            assert parse_location(f"City, Region, {row[column]}") == (country, None, "City")
        if country != "GE":
            assert parse_location(row["name"]) == (country, None, None)
            assert parse_location(f"City, {row['name']}") == (country, None, "City")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("US, Los Angeles, CA", ("US", "CA", "Los Angeles")),
        ("United States, Indianapolis, IN", ("US", "IN", "Indianapolis")),
        ("USA, Austin, TX", ("US", "TX", "Austin")),
        ("Canada, Toronto, ON", ("CA", None, "Toronto")),
        ("GB, London", ("GB", None, "London")),
        # A first part that is also a US state stays a state.
        ("Georgia, Atlanta", ("US", "GA", None)),
        ("CA, Los Angeles", ("US", "CA", None)),
    ],
)
def test_a_country_named_first_wins(raw, expected):
    """R3 finding 1: a leading country beat by a trailing two-letter code dropped US roles."""
    assert parse_location(raw) == expected


@pytest.mark.parametrize(
    ("raw", "outside"),
    [
        ("New York, NY; London, GB", False),  # one place is in the area: kept
        ("London, GB | Remote", False),  # a place with no known country keeps it
        ("London, GB; Dublin, ie", True),  # every place is outside: dropped
        ("Austin, TX", False),
        ("", False),
    ],
)
def test_a_role_in_several_places_stays_when_any_is_in_the_area(raw, outside):
    """Owner ruling D-SRC-13: only a role clearly outside the area is dropped."""
    from kestrel.ingest.runner import _outside_area

    assert _outside_area(raw, ["US"]) is outside
