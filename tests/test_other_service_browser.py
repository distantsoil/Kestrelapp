"""Other service in Settings and Models, using mocked metadata only, in all desktop themes."""

from __future__ import annotations

import os

import pytest

from tests.browser import launch_sync
from tests.test_other_service import KEY, MODELS, URL
from tests.test_settings_browser import _KIDS, _LINES, _overlaps

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)


@pytest.fixture
def live(monkeypatch):
    from playwright.sync_api import sync_playwright

    from kestrel.db import migrate
    from kestrel.llm import providers as prov
    from kestrel.web.serve import running_server

    async def verify(backend):
        assert backend.base_url == URL and backend.provider == "generic"
        assert (backend.api_key or prov.secret_value(backend.api_key_env)) == KEY
        return prov.KeyStatus(True, "generic", "key accepted")

    async def models(backend):
        return [prov.ModelInfo(id=m["id"]) for m in MODELS["data"]]

    monkeypatch.setattr(prov, "verify_key", verify)
    monkeypatch.setattr(prov, "list_models", models)
    migrate.run_migrations()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


def _layout(page, theme, width):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (theme, width)
    for el in page.locator(
        "#st-mode h3, #st-mode .st-mc__t b, #st-mode .k-btn, #st-mode .st-svc-label"
    ).all():
        if el.is_visible():
            assert el.evaluate(_LINES) <= 1, (theme, width, el.inner_text())
    for el in page.locator("#st-mode .st-modes, #st-mode .st-tiles, #st-mode .st-keyrow").all():
        kids = el.evaluate(_KIDS)
        for i, a in enumerate(kids):
            for b in kids[i + 1 :]:
                assert not _overlaps(a, b), (theme, width, a, b)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1280, 1680])
def test_other_test_save_reload_and_approve(live, theme, width):
    from playwright.sync_api import expect

    from kestrel import paths

    base, browser = live
    ctx = browser.new_context(viewport={"width": width, "height": 1000}, color_scheme=theme)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        page.goto(base + "/settings#computer")
        page.get_by_test_id("st-mode-online").click()
        expect(page.locator("#st-online")).to_be_visible()
        page.get_by_test_id("st-svc-other").click()
        expect(page.get_by_test_id("st-base-url")).to_be_visible()
        page.get_by_test_id("st-base-url").fill(URL)
        page.get_by_test_id("st-key").fill(KEY)
        page.get_by_test_id("st-key-show").click()
        expect(page.get_by_test_id("st-key")).to_have_attribute("type", "text")
        page.get_by_test_id("st-key-show").click()
        expect(page.get_by_test_id("st-key")).to_have_attribute("type", "password")
        _layout(page, theme, width)
        page.get_by_test_id("st-key-test").click()
        expect(page.get_by_test_id("st-keyres")).to_contain_text("connection works")
        expect(page.get_by_test_id("st-base-url")).to_have_value(URL)
        expect(page.get_by_test_id("st-key")).to_have_value("")
        assert not paths.dotenv_path().exists()
        page.get_by_test_id("st-key").fill(KEY)
        page.get_by_test_id("st-key-save").click()
        expect(page.get_by_test_id("st-keyres")).to_contain_text("Service address and key saved")
        expect(page.locator("#st-mode")).to_have_count(1)
        expect(page.get_by_test_id("st-key")).to_have_value("")
        page.reload()
        page.get_by_test_id("st-svc-other").click()
        expect(page.get_by_test_id("st-base-url")).to_have_value(URL)
        _layout(page, theme, width)
        page.goto(base + "/models")
        page.get_by_test_id("mo-online-manage").click()
        expect(page.get_by_test_id("mo-other-connect")).to_be_visible()
        expect(page.locator('#mo-online-body input[name="ack"]')).not_to_be_checked()
        page.get_by_test_id("mo-other-approve").click()
        expect(page.get_by_test_id("mo-online-message")).to_contain_text("nothing was saved")
        page.locator('#mo-online-body input[name="ack"]').check()
        page.get_by_test_id("mo-other-approve").click()
        expect(page.get_by_test_id("mo-online-message")).to_contain_text(
            "Connected to Other service"
        )
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        for el in page.locator("#mo-online-body button").all():
            if el.is_visible():
                assert el.evaluate(_LINES) <= 1
        assert not errors, errors
    finally:
        ctx.close()
