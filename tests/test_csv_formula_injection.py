"""D-SEC-10: a CSV the operator opens in a spreadsheet never carries a live formula. A posting
title, a company name or a note comes from the web, so a cell that starts with = + - @ tab or CR
gets one leading single quote. Numbers stay numbers. Invented text only."""

from __future__ import annotations

import csv
import io

import pytest

from kestrel.services.csv_safe import safe_cell, safe_record, safe_rows

PAYLOADS = ['=HYPERLINK("http://x.invalid/","go")', "+1+1", "-2+3", "@SUM(A1)", "\t=1+1",
            "\r=1+1", "=cmd|' /C calc'!A0"]


@pytest.mark.parametrize("cell", PAYLOADS)
def test_a_cell_that_starts_like_a_formula_gets_a_leading_quote(cell):
    assert safe_cell(cell) == "'" + cell


@pytest.mark.parametrize("cell", ["Engineering Manager", "", " =x", "a=b", "100", "x@y.example"])
def test_an_ordinary_cell_is_untouched(cell):
    assert safe_cell(cell) == cell


def test_numbers_and_none_are_untouched():
    assert safe_row_values([5, -3, 2.5, None, True]) == [5, -3, 2.5, None, True]


def safe_row_values(row):
    return safe_rows([row])[0]


def test_a_record_is_cleaned_cell_by_cell():
    assert safe_record({"title": "=A1", "score": -4}) == {"title": "'=A1", "score": -4}


def test_the_analytics_csv_response_is_safe_for_a_spreadsheet():
    from kestrel.web.pages.analytics import _csv_response

    rows = [["Role", "Company"], ["=HYPERLINK(\"http://x.invalid/\")", "@Acme"], ["Fine", "+Co"]]
    r = _csv_response(rows, "x.csv")
    got = list(csv.reader(io.StringIO(r.body.decode("utf-8").lstrip("﻿"))))
    assert got[1] == ["'=HYPERLINK(\"http://x.invalid/\")", "'@Acme"]
    assert got[2] == ["Fine", "'+Co"]
    assert got[0] == ["Role", "Company"]
