"""Settings on the new design system (D-UI-149): the page, all six sections, and that every
fragment the page loads renders. Cloud-safe: TestClient on the real app, a throwaway data
directory, no model and no network."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kestrel.db import migrate
from kestrel.db.engine import forget_engine
from kestrel.web.app import build_app

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def _client() -> TestClient:
    migrate.run_migrations()
    forget_engine()
    return TestClient(build_app(worker=False))


FRAGMENTS = (
    "/settings/section/roles", "/settings/section/scoring", "/settings/section/computer",
    "/settings/section/privacy", "/settings/section/data", "/settings/section/preferences",
    "/settings/computer/checklist", "/settings/roles/rules", "/settings/roles/cutoffs",
    "/settings/roles/hidden", "/settings/scoring/weights", "/settings/scoring/learned",
    "/settings/data/contacts", "/settings/data/applied", "/settings/privacy/access",
    "/settings/privacy/mcp", "/settings/computer/mode",
)


def test_the_page_renders_with_six_sections_and_two_headings():
    with _client() as c:
        r = c.get("/settings")
    assert r.status_code == 200, r.text[:2000]
    for sid in ("computer", "roles", "scoring", "privacy", "data", "preferences"):
        assert f'data-testid="st-nav-{sid}"' in r.text
    assert "SET UP" in r.text and "YOU" in r.text
    assert 'id="your-data"' in r.text  # the anchor the Company page links to


@pytest.mark.parametrize("url", FRAGMENTS)
def test_every_fragment_renders(url):
    with _client() as c:
        r = c.get(url)
    assert r.status_code == 200, (url, r.text[:1500])
    assert r.text.strip()
