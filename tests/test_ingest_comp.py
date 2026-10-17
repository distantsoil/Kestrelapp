"""Compensation parsing: accept real comp, and REJECT ordinary number pairs in prose (a false hit
would poison the comp corpus)."""

from __future__ import annotations

from kestrel.ingest.comp import parse_comp, to_annual


def test_parse_annual_range():
    c = parse_comp("Compensation: $210,000 - $245,000 per year.")
    assert (
        c and c.min == 210000 and c.max == 245000 and c.interval == "year" and c.currency == "USD"
    )


def test_parse_k_suffix():
    c = parse_comp("Base salary $180k-$220k.")
    assert c and c.min == 180000 and c.max == 220000 and c.interval == "year"


def test_parse_hourly_and_daily():
    assert parse_comp("Pay: $95/hr").interval == "hour"
    assert parse_comp("Rate $1,200/day").interval == "day"


def test_single_figure_requires_currency():
    assert parse_comp("200000") is None
    c = parse_comp("The salary is $200,000.")
    assert c and c.min == 200000 and c.interval == "year"


def test_reject_prose_number_pairs():
    # these must NOT be read as compensation
    assert parse_comp("Requires 3 to 5 years of experience.") is None
    assert parse_comp("Manage a team of 5 to 10 direct reports.") is None
    assert parse_comp("Available 2 to 3 days per week.") is None
    assert parse_comp("Grew revenue 2x to 3x over two years.") is None


def test_reject_implausible_magnitude():
    assert parse_comp("scored $5 to $9 on the rubric") is None  # too small to be annual pay


def test_currency_symbols():
    assert parse_comp("£90,000 - £110,000").currency == "GBP"
    assert parse_comp("€80,000 to €95,000").currency == "EUR"


def test_to_annual():
    assert to_annual(100, "hour") == 208000
    assert to_annual(1000, "day") == 260000
    assert to_annual(200000, "year") == 200000
