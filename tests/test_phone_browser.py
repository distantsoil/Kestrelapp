"""#207: the phone field in a real browser. Formats on leaving the field and on paste, keeps an
unreadable entry as typed with the gentle note, and nothing overlaps at 390 wide, light or dark.
Gated: KESTREL_LIVE_BROWSER=1. KESTREL_SHOT_DIR also writes screenshots."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests import corrections_seed
from tests.browser import launch_sync, wait_for_layout
from tests.browser_plugin import browser_artifacts  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("KESTREL_LIVE_BROWSER") != "1",
    reason="launches a real browser; set KESTREL_LIVE_BROWSER=1",
)

NOTE = "Kestrel could not read this as a phone number; it is kept as you typed it."


@pytest.fixture
def live():
    from playwright.sync_api import sync_playwright

    from kestrel.web.serve import running_server

    corrections_seed.seed()
    with running_server(worker=False) as base, sync_playwright() as p:
        browser = launch_sync(p)
        yield base, browser
        browser.close()


def _shot(page, name: str, field: str = "#pa-phone") -> None:
    """The phone field in view, in the viewport: a full-page capture stretches the fixed setup
    panels over the form at narrow widths."""
    out = os.environ.get("KESTREL_SHOT_DIR")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        page.locator(field).scroll_into_view_if_needed()
        handle = page.locator(field).element_handle()
        page.evaluate("f => f.scrollIntoView({block: 'center'})", handle)
        page.screenshot(path=str(Path(out) / f"{name}.png"))


def _field(page):
    return page.locator("#pa-phone")


def test_leaving_the_field_formats_and_an_unreadable_entry_is_kept(live):
    base, browser = live
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(f"{base}/profile?tab=about&track=employment")
    wait_for_layout(page)
    f = _field(page)
    f.fill("5555550123")
    page.locator("#pa-loc").focus()  # leaves the phone field
    page.wait_for_function("document.querySelector('#pa-phone').value === '555-555-0123'")
    assert page.locator("[data-phone-entered]").input_value() == "5555550123"
    assert not page.locator("[data-phone-note]").evaluate("e => e.classList.contains('ph-show')")
    f.fill("ask for Dana")
    page.locator("#pa-loc").focus()
    page.wait_for_function(
        "document.querySelector('[data-phone-note]').classList.contains('ph-show')")
    assert f.input_value() == "ask for Dana"
    assert page.locator("[data-phone-note]").inner_text() == NOTE
    _shot(page, "profile-phone-unreadable-light")
    # Paste: the text lands, then it is formatted without leaving the field.
    f.fill("")
    f.focus()
    page.evaluate("""() => { const f = document.querySelector('#pa-phone');
      f.value = '(555) 555.0123'; f.dispatchEvent(new Event('paste', {bubbles: true})); }""")
    page.wait_for_function("document.querySelector('#pa-phone').value === '555-555-0123'")
    assert page.input_value("#pa-phone") == "555-555-0123"
    page.close()


def test_the_country_selector_changes_the_format_and_nothing_overlaps_at_390(live):
    base, browser = live
    for scheme in ("light", "dark"):
        page = browser.new_page(viewport={"width": 390, "height": 844}, color_scheme=scheme)
        page.goto(f"{base}/profile?tab=about&track=employment")
        wait_for_layout(page)
        page.locator("#pa-phone").fill("020 7946 0123")
        page.locator("[data-phone-country]").select_option("GB")
        page.wait_for_function("document.querySelector('#pa-phone').value === '020 7946 0123'")
        input_box = page.locator("#pa-phone").bounding_box()
        sel_box = page.locator("[data-phone-country]").bounding_box()
        assert input_box["x"] >= 0 and sel_box["x"] + sel_box["width"] <= 390
        assert not (input_box["x"] < sel_box["x"] + sel_box["width"] - 1
                    and sel_box["x"] < input_box["x"] + input_box["width"] - 1
                    and input_box["y"] < sel_box["y"] + sel_box["height"] - 1
                    and sel_box["y"] < input_box["y"] + input_box["height"] - 1)
        assert page.evaluate("document.documentElement.scrollWidth <= 390")
        _shot(page, f"profile-phone-390-{scheme}")
        page.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_profile_phone_fits_at_1440(live, scheme):
    base, browser = live
    page = browser.new_page(viewport={"width": 1440, "height": 900}, color_scheme=scheme)
    page.goto(f"{base}/profile?tab=about&track=employment")
    wait_for_layout(page)
    page.locator("#pa-phone").fill("+44 20 7946 0123")
    page.locator("#pa-loc").focus()
    page.wait_for_function("document.querySelector('#pa-phone').value === '+44 20 7946 0123'")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    _shot(page, f"profile-phone-1440-{scheme}")
    page.close()


def test_reduced_motion_turns_the_note_transition_off(live):
    base, browser = live
    page = browser.new_page(viewport={"width": 1280, "height": 900}, reduced_motion="reduce")
    page.goto(f"{base}/profile?tab=about&track=employment")
    dur = page.locator("[data-phone-note]").evaluate("e => getComputedStyle(e).transitionDuration")
    assert set(dur.split(", ")) <= {"0s"}
    page.close()
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(f"{base}/profile?tab=about&track=employment")
    dur = page.locator("[data-phone-note]").evaluate("e => getComputedStyle(e).transitionDuration")
    assert "0.25s" in dur
    page.close()


from tests.test_onboarding_browser import _open  # noqa: E402
from tests.test_onboarding_browser import live as ob_live  # noqa: E402,F401


@pytest.mark.parametrize("theme,width", [("light", 1440), ("dark", 1440), ("light", 390),
                                         ("dark", 390)])
def test_onboarding_phone_formats_on_leaving_and_fits(ob_live, theme, width):  # noqa: F811
    ctx, page, errors = _open(ob_live, theme, width, "about")
    try:
        page.click('[data-ob-fold="ob-more-about"]')
        page.wait_for_selector("#ob-phone")
        wait_for_layout(page)
        page.fill("#ob-phone", "5555550123")
        page.focus("#ob-head")
        page.wait_for_function("document.querySelector('#ob-phone').value === '555-555-0123'")
        page.fill("#ob-phone", "call the front desk")
        page.focus("#ob-head")
        page.wait_for_function(
            "document.querySelector('#ob-phone').closest('.ob-f').querySelector('.ph-note')"
            ".classList.contains('ph-show')")
        assert page.input_value("#ob-phone") == "call the front desk"
        page.fill("#ob-li", "linkedin.com/in/pat-example?trk=x")
        page.focus("#ob-head")
        page.wait_for_function(f"document.querySelector('#ob-li').value === '{LI_GOOD}'")
        wait_for_layout(page)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        _shot(page, f"onboarding-phone-{width}-{theme}", "#ob-phone")
        _shot(page, f"onboarding-linkedin-{width}-{theme}", "#ob-li")
        assert not errors, errors
    finally:
        ctx.close()


LI_NOTE = "Kestrel could not read this as a LinkedIn profile address; it is kept as you typed it."
LI_GOOD = "https://www.linkedin.com/in/pat-example"


def test_profile_linkedin_normalizes_on_leaving_and_keeps_unreadable_text(live):
    base, browser = live
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(f"{base}/profile?tab=about&track=employment")
    wait_for_layout(page)
    page.fill("#pa-li", "linkedin.com/in/pat-example?utm_source=share")
    page.locator("#pa-loc").focus()
    page.wait_for_function(f"document.querySelector('#pa-li').value === '{LI_GOOD}'")
    page.fill("#pa-li", "on request")
    page.locator("#pa-loc").focus()
    page.wait_for_function(
        "document.querySelector('[data-linkedin-note]').classList.contains('ph-show')")
    assert page.input_value("#pa-li") == "on request"
    assert page.locator("[data-linkedin-note]").inner_text() == LI_NOTE
    page.close()


@pytest.mark.parametrize("scheme,width", [("light", 1440), ("dark", 1440), ("light", 390),
                                          ("dark", 390)])
def test_linkedin_fields_fit_and_are_captured(live, scheme, width):
    base, browser = live
    page = browser.new_page(viewport={"width": width, "height": 900}, color_scheme=scheme)
    page.goto(f"{base}/profile?tab=about&track=employment")
    wait_for_layout(page)
    page.fill("#pa-li", "linkedin.com/in/pat-example")
    page.locator("#pa-loc").focus()
    page.wait_for_function(f"document.querySelector('#pa-li').value === '{LI_GOOD}'")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    box = page.locator("#pa-li").bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= width
    _shot(page, f"profile-linkedin-{width}-{scheme}", "#pa-li")
    page.close()


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_resume_contact_line_and_look_checkbox(live, scheme):
    from kestrel.docs.catalog import RenderDoc
    from kestrel.docs.render import render_html

    base, browser = live
    header = {"name": "Pat Example", "email": "pat@example.com", "phone": "555-555-0123",
              "location": "Columbus, OH", "linkedin": "linkedin.com/in/pat-example",
              "linkedin_href": LI_GOOD}
    page = browser.new_page(viewport={"width": 900, "height": 300}, color_scheme=scheme)
    page.set_content(render_html(RenderDoc(header=header, blocks=[])))
    link = page.locator(".contact a")
    assert link.get_attribute("rel") == "noopener noreferrer"
    assert link.get_attribute("href") == LI_GOOD
    out = os.environ.get("KESTREL_SHOT_DIR")
    if out:
        page.screenshot(path=str(Path(out) / f"resume-contact-line-{scheme}.png"))
    page.close()
    page = browser.new_page(viewport={"width": 1280, "height": 900}, color_scheme=scheme)
    page.goto(f"{base}/documents")
    wait_for_layout(page)
    page.locator("#pf-look > summary").click()
    box = page.locator('[data-testid="doc-linkedin"]')
    box.wait_for(state="visible")
    assert page.locator("#show-linkedin").is_checked()
    page.locator("#show-linkedin").uncheck()
    page.wait_for_selector("#look-status:has-text('Look saved')")
    if out:
        box.scroll_into_view_if_needed()
        page.screenshot(path=str(Path(out) / f"documents-look-linkedin-{scheme}.png"))
    page.close()


@pytest.mark.parametrize("scheme,width", [("light", 1440), ("dark", 1440), ("light", 390),
                                          ("dark", 390)])
def test_a_foreign_number_keeps_its_plus_and_never_moves_the_home_country(live, scheme, width):
    base, browser = live
    page = browser.new_page(viewport={"width": width, "height": 900}, color_scheme=scheme)
    page.goto(f"{base}/profile?tab=about&track=employment")
    wait_for_layout(page)
    home = page.locator("[data-phone-country]").input_value()
    page.fill("#pa-phone", "+44 20 7946 0000")
    page.locator("#pa-loc").focus()
    page.wait_for_function("document.querySelector('[data-phone-e164]').value === '+442079460000'")
    assert page.input_value("#pa-phone") == "+44 20 7946 0000"
    assert page.locator("[data-phone-country]").input_value() == home  # typing never moves it
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    _shot(page, f"profile-foreign-phone-{width}-{scheme}")
    page.close()


def test_changing_the_home_country_reformats_the_stored_number(live):
    base, browser = live
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(f"{base}/profile?tab=about&track=employment")
    wait_for_layout(page)
    page.locator("[data-phone-country]").select_option("US")
    page.fill("#pa-phone", "+44 20 7946 0000")
    page.locator("#pa-loc").focus()
    page.wait_for_function("document.querySelector('[data-phone-e164]').value === '+442079460000'")
    page.locator("[data-phone-country]").select_option("GB")
    page.wait_for_function("document.querySelector('#pa-phone').value === '020 7946 0000'")
    page.locator("[data-phone-country]").select_option("US")
    page.wait_for_function("document.querySelector('#pa-phone').value === '+44 20 7946 0000'")
    assert page.input_value("#pa-phone") == "+44 20 7946 0000"
    assert page.locator("[data-phone-country]").input_value() == "US"
    page.close()


def test_a_slow_phone_answer_is_not_dropped_because_another_field_asked_meanwhile(live):
    """Round 2: the stale-reply counter is per field. The phone answer is held back while the
    LinkedIn field makes and finishes its own request; the phone field must still update."""
    base, browser = live
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    held = []
    page.route("**/api/phone/format", lambda route: held.append(route))
    page.goto(f"{base}/profile?tab=about&track=employment")
    wait_for_layout(page)
    page.locator("[data-phone-country]").select_option("US")
    page.fill("#pa-phone", "5555550123")
    page.locator("#pa-li").focus()  # the phone request is now held
    page.wait_for_function("true")
    while not held:
        page.wait_for_timeout(50)
    page.fill("#pa-li", "linkedin.com/in/pat-example")
    page.locator("#pa-loc").focus()
    page.wait_for_function(f"document.querySelector('#pa-li').value === '{LI_GOOD}'")
    for route in held:  # only now do the phone answers arrive; the newest is the one that counts
        route.continue_()
    page.wait_for_function("document.querySelector('#pa-phone').value === '555-555-0123'")
    assert page.input_value("#pa-phone") == "555-555-0123"
    assert page.input_value("#pa-li") == LI_GOOD
    page.close()


def test_a_country_change_never_brings_back_the_old_number_over_newly_typed_text(live):
    """Round 3 (3): type a new number, Tab to the selector, press an arrow key. The country
    request used to send the old E.164 before the blur reply landed, and its answer won."""
    base, browser = live
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(f"{base}/profile?tab=about&track=employment")
    wait_for_layout(page)
    page.locator("[data-phone-country]").select_option("US")
    page.fill("#pa-phone", "5555550123")
    page.locator("#pa-loc").focus()
    page.wait_for_function("document.querySelector('[data-phone-e164]').value === '+15555550123'")
    held = []
    page.route("**/api/phone/format", lambda route: held.append(route))  # replies arrive late
    page.fill("#pa-phone", "+44 20 7946 0123")
    page.keyboard.press("Tab")  # to the country selector, which blurs the field
    page.keyboard.press("ArrowDown")  # and changes the country before any reply lands
    while len(held) < 2:
        page.wait_for_timeout(50)
    for route in held:
        route.continue_()
    page.wait_for_function("document.querySelector('[data-phone-e164]').value === '+442079460123'")
    assert page.input_value("#pa-phone") == "+44 20 7946 0123"
    assert page.locator("[data-phone-country]").input_value() != "US"
    page.close()


def test_changing_the_country_twice_in_a_row_keeps_the_same_number(live):
    base, browser = live
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(f"{base}/profile?tab=about&track=employment")
    wait_for_layout(page)
    page.locator("[data-phone-country]").select_option("US")
    page.fill("#pa-phone", "5555550123")
    page.locator("#pa-loc").focus()
    page.wait_for_function("document.querySelector('[data-phone-e164]').value === '+15555550123'")
    page.locator("[data-phone-country]").select_option("GB")
    page.locator("[data-phone-country]").select_option("DE")
    page.wait_for_function("document.querySelector('#pa-phone').value === '+1 555-555-0123'")
    assert page.locator("[data-phone-e164]").input_value() == "+15555550123"
    page.close()
