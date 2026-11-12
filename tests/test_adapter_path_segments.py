"""D-SEC-10: a board id spliced into a URL path is one plain segment (greenhouse, lever, ashby,
smartrecruiters). The host is fixed for these four, so the risk is a path or query injected on the
same host; the check refuses it at config load. Invented ids only."""

from __future__ import annotations

import pytest

from kestrel.ingest.adapters.ashby import AshbyAdapter
from kestrel.ingest.adapters.greenhouse import GreenhouseAdapter
from kestrel.ingest.adapters.lever import LeverAdapter
from kestrel.ingest.adapters.smartrecruiters import SmartRecruitersAdapter
from kestrel.ingest.base import SourceConfigError

CASES = [
    (GreenhouseAdapter, "token"), (LeverAdapter, "company"), (AshbyAdapter, "token"),
    (SmartRecruitersAdapter, "company"),
]
BAD = ["../x", "a/b", "a?x=1", "a#b", "a@10.0.0.1", "a:443", "a b", "..", ".", "a%2e%2e"]
GOOD = ["acme", "Acme1", "acme-inc", "Acme_Inc", "acme.co", "A" * 100]


@pytest.mark.parametrize(("adapter", "key"), CASES)
@pytest.mark.parametrize("value", BAD)
def test_a_board_id_that_is_not_one_path_segment_is_refused(adapter, key, value):
    with pytest.raises(SourceConfigError):
        adapter().validate_config({key: value})


@pytest.mark.parametrize(("adapter", "key"), CASES)
@pytest.mark.parametrize("value", GOOD)
def test_an_ordinary_board_id_is_accepted(adapter, key, value):
    assert adapter().validate_config({key: value}) is None


@pytest.mark.parametrize(("adapter", "key"), CASES)
def test_a_missing_board_id_is_still_refused(adapter, key):
    with pytest.raises(SourceConfigError):
        adapter().validate_config({})


# ---- the older adapters: a trailing newline is not part of an id (re.fullmatch, not ^...$ match)
UUID = "12345678-1234-1234-1234-123456789abc"
OLDER = [
    ("kestrel.ingest.adapters.breezy", "BreezyAdapter", "slug", "acme"),
    ("kestrel.ingest.adapters.personio", "PersonioAdapter", "slug", "acme"),
    ("kestrel.ingest.adapters.recruitee", "RecruiteeAdapter", "slug", "acme"),
    ("kestrel.ingest.adapters.rippling", "RipplingAdapter", "slug", "acme"),
    ("kestrel.ingest.adapters.workable", "WorkableAdapter", "slug", "acme"),
    ("kestrel.ingest.adapters.paylocity", "PaylocityAdapter", "board_id", UUID),
    ("kestrel.ingest.adapters.adp", "AdpAdapter", "cid", UUID),
    ("kestrel.ingest.adapters.teamtailor", "TeamtailorAdapter", "domain", "jobs.example.com"),
]


@pytest.mark.parametrize(("module", "cls", "key", "good"), OLDER)
@pytest.mark.parametrize("tail", ["\n", "\r\n", " \n"])
def test_a_trailing_newline_is_not_a_valid_id(module, cls, key, good, tail):
    import importlib

    adapter = getattr(importlib.import_module(module), cls)()
    assert adapter.validate_config({key: good}) is None
    with pytest.raises(SourceConfigError):
        adapter.validate_config({key: good + tail})
