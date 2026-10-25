"""The source catalog, and the About page facts that used to sit beside it on Help.

The catalog's point is that it cannot drift from the registries: every registered adapter and
signal source is listed and described, no description survives for a kind that no longer exists,
and the example config per kind passes the exact validator the Sources page runs before saving.
/about is its own page since D-UI-6; Help (D-UI-124) holds articles, not the catalog.
docs/SOURCES.md must name every kind too."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kestrel.db.enums import SOURCE_KINDS
from kestrel.ingest.adapters import known_kinds
from kestrel.ingest.catalog import (
    REGION_NOTE,
    SourceKindInfo,
    described_kinds,
    source_catalog,
)
from kestrel.ingest.signal_sources import known_signal_kinds

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

_REGISTERED = set(known_kinds()) | set(known_signal_kinds())


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _by_kind() -> dict[str, SourceKindInfo]:
    return {k.kind: k for k in source_catalog()}


# ------------------------------------------------------------------------------------- catalog
def test_catalog_lists_every_registered_kind_and_every_table_kind():
    kinds = set(_by_kind())
    assert _REGISTERED <= kinds, _REGISTERED - kinds
    assert set(SOURCE_KINDS) <= kinds, set(SOURCE_KINDS) - kinds
    assert len(source_catalog()) == len(kinds)  # no duplicate rows


def test_every_listed_kind_is_documented():
    undocumented = [k.kind for k in source_catalog() if not k.documented]
    assert undocumented == [], f"add a description in kestrel/ingest/catalog.py for {undocumented}"


def test_no_description_outlives_its_kind():
    stale = described_kinds() - _REGISTERED - set(SOURCE_KINDS)
    assert stale == frozenset(), stale


def test_registered_kinds_are_scheduled_and_addable_on_sources_page():
    rows = _by_kind()
    for kind in _REGISTERED:
        assert rows[kind].scheduled, kind
        assert rows[kind].can_add_on_sources_page, kind
        assert "Sources > Add source" in rows[kind].how_to_add, kind
    # The two table-only kinds exist for capture and dedup; nothing scans them and the catalog
    # says so rather than inviting the operator to add one.
    for kind in set(SOURCE_KINDS) - _REGISTERED:
        assert not rows[kind].scheduled, kind
        assert "Not added by hand" in rows[kind].how_to_add, kind


def test_config_examples_pass_the_sources_page_validator(monkeypatch):
    """`how_to_add` promises the example keys pass; prove it with the Sources page's own check."""
    from kestrel.web.pages.sources import _validate_source_config

    monkeypatch.setenv("SAM_GOV_API_KEY", "test-key-for-validation-only")
    for row in source_catalog():
        if not row.scheduled:
            continue
        assert row.config_example, f"{row.kind} needs a config_example"
        assert _validate_source_config(row.kind, dict(row.config_example)) is None, row.kind


def test_region_note_states_the_us_default_and_the_worldwide_path():
    assert "US-centric" in REGION_NOTE
    assert "USA" in REGION_NOTE and "SEC EDGAR" in REGION_NOTE and "SAM.gov" in REGION_NOTE
    assert "Texas" not in REGION_NOTE and "example profile is a US city" in REGION_NOTE
    assert "worldwide" in REGION_NOTE
    for row in source_catalog():
        assert row.region, f"{row.kind} has no region note"
        assert row.rate_limit, f"{row.kind} has no rate limit note"


# ------------------------------------------------------------------------- the about page
def test_about_is_its_own_page_and_links_to_help():
    """/about used to alias Help; it is now the About page (D-UI-6) and points at Help."""
    with _client() as c:
        about = c.get("/about")
        helped = c.get("/help")
    assert about.status_code == 200
    assert "<h1>About</h1>" in about.text
    assert "<h1>Help</h1>" not in about.text
    assert 'href="/help"' in about.text and "<h1>Help</h1>" in helped.text


def test_about_shows_the_hardware_profile_only_when_one_was_chosen():
    """The hardware profile `kestrel setup` chose moved from Help to About's component versions
    (with the port Kestrel is serving on); with no profile there is no row."""
    from kestrel.llm import profiles

    with _client() as c:
        r = c.get("/about")
        assert "Hardware profile" not in r.text
        assert "Kestrel is serving on port" in r.text
        profiles.save_active_profile("rtx-16gb", "16 GB VRAM detected")
        r2 = c.get("/about")
    assert "Hardware profile" in r2.text
    assert "rtx-16gb" in r2.text and "16 GB VRAM detected" in r2.text


# ---------------------------------------------------------------------------------------- docs
def test_docs_sources_md_names_every_kind():
    doc = Path(__file__).resolve().parents[1] / "docs" / "SOURCES.md"
    text = doc.read_text(encoding="utf-8")
    for row in source_catalog():
        assert f"`{row.kind}`" in text, row.kind
    assert "## Adding an adapter" in text
    assert "validate_config" in text and "SourceConfigError" in text
    assert "tests/fixtures/" in text
