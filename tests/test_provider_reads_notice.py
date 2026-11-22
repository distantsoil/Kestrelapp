"""D-PRIV-795 (D-REL-41): the one-time notice about the provider reads, and the way back to the
updated disclosure.

Before 0.7.7 the saved online key went to the provider that issued it for balance and model-list
checks on page views. Installs that had online models switched on with a saved key are told once,
in the announcement slot on Today. While their acceptance predates the current disclosure (online
paused), the notice and the Models page link to Settings > Hosted, where the updated text is
ticked again. Every state is decided by the server from the stored rows, never from the request.
Cloud-safe: a migrated SQLite file, no network."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, reset_engine
from kestrel.llm import providers
from tests._crawl_support import stale_terms

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

# Invented: the OpenRouter prefix and characters that belong to no account.
FAKE_KEY = "sk-or-v1-" + "fedcba9876543210" * 4
ID = "provider-reads-0.7.7"
REVIEW = "/settings/hosted/review"


async def _prepare() -> None:
    await reset_engine()
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()


def _rows(rows: dict[str, object]) -> None:
    con = sqlite3.connect(paths.db_path())
    try:
        for k, v in rows.items():
            con.execute(
                "INSERT OR REPLACE INTO setting(key, value, updated_at) VALUES (?, ?, ?)",
                (k, json.dumps(v), "2026-11-04T00:00:00+00:00"),
            )
        con.commit()
    finally:
        con.close()


def _world(*, enabled: bool, key: bool, stale: bool = True, acked: bool = True) -> TestClient:
    """Seed the stored rows an install would have, then start the app on them."""
    from kestrel.config import HostedMode
    from kestrel.web.app import build_app

    asyncio.run(_prepare())
    rows: dict[str, object] = {"llm.hosted_mode.enabled": enabled}
    if acked:
        rows["llm.hosted_mode.acknowledged_at"] = "2026-10-15T00:00:00Z"
        rows["llm.hosted_mode.acknowledged_terms"] = (
            stale_terms() if stale else HostedMode().disclosure_terms())
    _rows(rows)
    if key:
        providers.write_secret("OPENROUTER_API_KEY", FAKE_KEY)
    return TestClient(build_app(worker=False))


def _notice(c: TestClient) -> str:
    r = c.get("/announcements")
    assert r.status_code == 200
    return r.text


def _shows(text: str) -> bool:
    return f'data-announcement-id="{ID}"' in text


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


# ------------------------------------------------------------------------------- who sees it
def test_online_on_with_a_saved_key_sees_the_notice_with_the_full_copy() -> None:
    c = _world(enabled=True, key=True)
    body = _notice(c)
    assert _shows(body)
    words = _text(body)
    for part in (
        "your saved key went to the provider that issued it",
        "balance and model-list checks",
        "on page load and about every 5 minutes while the app was open",
        "No job, resume or profile data was ever sent",
        "As of 0.7.7 these checks run only when online models are in use",
        "the disclosure now names them",
    ):
        assert part in words, part
    assert "—" not in body  # no em dashes in UI copy
    assert FAKE_KEY not in body and FAKE_KEY[-4:] not in body


def test_online_on_under_the_current_disclosure_still_sees_it_once() -> None:
    """Someone who already re-ticked was still affected before; they are told, without the
    re-accept button (nothing is paused for them)."""
    c = _world(enabled=True, key=True, stale=False)
    body = _notice(c)
    assert _shows(body)
    assert REVIEW not in body
    assert "paused" not in _text(body)


@pytest.mark.parametrize(
    ("enabled", "key", "acked"),
    [
        (True, False, True),    # online on, no saved key: nothing was ever sent
        (False, True, True),    # online switched off, key still saved
        (False, False, False),  # a fresh install
        (False, True, False),   # a key saved, online never set up
    ],
    ids=["no-key", "online-off", "fresh", "never-set-up"],
)
def test_everyone_else_does_not_see_it(enabled: bool, key: bool, acked: bool) -> None:
    c = _world(enabled=enabled, key=key, acked=acked)
    assert not _shows(_notice(c))
    assert not _shows(c.get("/").text)


def test_it_shows_once_and_never_after_got_it() -> None:
    c = _world(enabled=True, key=True)
    assert _shows(_notice(c))
    assert _shows(_notice(c))  # not dismissed by being shown; only "Got it" ends it
    r = c.post(f"/announcements/{ID}/dismiss")
    assert r.status_code == 200 and r.text == ""
    assert not _shows(_notice(c))
    # Still gone after the person re-ticks and online is in use again.
    from kestrel.config import HostedMode

    _rows({"llm.hosted_mode.acknowledged_terms": HostedMode().disclosure_terms()})
    assert not _shows(_notice(c))


# ---------------------------------------------------------------------------- the way back
def _links(html: str, testid: str) -> list[str]:
    return re.findall(rf'<a\b[^>]*data-testid="{testid}"[^>]*href="([^"]+)"', html) + re.findall(
        rf'<a\b[^>]*href="([^"]+)"[^>]*data-testid="{testid}"', html)


def _assert_review_page(c: TestClient, href: str) -> None:
    """The link resolves to Settings > Hosted with the updated disclosure and its tick."""
    from kestrel.config import HOSTED_PROVIDER_READS

    assert href == REVIEW
    page = c.get(href)
    assert page.status_code == 200
    html = page.text
    assert "<html" in html.lower()  # a whole page, not a bare fragment
    assert HOSTED_PROVIDER_READS in _text(html)
    form = re.search(r'<form[^>]*hx-post="/settings/hosted/mode"[^>]*>(.*?)</form>', html, re.S)
    assert form is not None, "the re-tick form is missing"
    assert 'name="ack"' in form.group(1) and 'value="on"' in form.group(1)
    assert 'data-testid="hosted-review"' in html
    # #224: the updated disclosure is open in its own section; Advanced stays closed by default.
    assert "<details" in html
    assert re.search(r'data-testid="hosted-disclosure-fold"[^>]*\bopen\b', html)
    assert re.search(r'id="hosted-advanced"[^>]*>', html)
    assert not re.search(r'id="hosted-advanced"[^>]*\bopen\b', html)


def test_the_notice_links_to_settings_hosted_while_online_is_paused() -> None:
    c = _world(enabled=True, key=True, stale=True)
    body = _notice(c)
    assert "Online scoring is paused" in _text(body)
    hrefs = _links(body, "announcement-link")
    assert hrefs == [REVIEW]
    assert "Review and re-accept" in body
    _assert_review_page(c, hrefs[0])


def test_the_models_page_links_there_whenever_online_is_paused() -> None:
    c = _world(enabled=True, key=True, stale=True)
    page = c.get("/models").text
    hrefs = _links(page, "mo-reaccept")
    assert hrefs == [REVIEW]
    _assert_review_page(c, hrefs[0])


@pytest.mark.parametrize(
    ("enabled", "stale", "acked"),
    [(True, False, True), (False, True, True), (False, False, False)],
    ids=["current", "off-with-old-tick", "never-set-up"],
)
def test_the_models_page_has_no_link_when_nothing_is_paused(
    enabled: bool, stale: bool, acked: bool
) -> None:
    c = _world(enabled=enabled, key=True, stale=stale, acked=acked)
    assert _links(c.get("/models").text, "mo-reaccept") == []


def test_the_pause_is_read_from_the_stored_rows_not_the_request() -> None:
    """A query string or header cannot make the link appear or vanish; the rows decide."""
    c = _world(enabled=True, key=True, stale=False)
    assert _links(c.get("/models?paused=1").text, "mo-reaccept") == []
    c = _world(enabled=True, key=True, stale=True)
    assert _links(c.get("/models?paused=0").text, "mo-reaccept") == [REVIEW]


def test_the_review_page_is_reachable_with_nothing_paused() -> None:
    """A link must point to a control that exists (KF-040): the page renders in every state."""
    for kwargs in ({"enabled": False, "key": False, "acked": False},
                   {"enabled": True, "key": True, "stale": False}):
        c = _world(**kwargs)
        r = c.get(REVIEW)
        assert r.status_code == 200 and 'id="hosted-panel"' in r.text
