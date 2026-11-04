"""D-UI-240: every ask-first panel with a consent box keeps its send button at full strength.

Pressing it unticked is handled by the shared script (kestrel-ui.js, data-consent-send): the hint
"Tick the box to send" appears beside the box and nothing is sent. The server refuses an
unconfirmed post as well; those refusals are pinned by the per-endpoint tests named in
CONSENT_ENDPOINT_TESTS below. Templates are rendered directly with an invented route.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

import jinja2
import pytest
from bs4 import BeautifulSoup

from kestrel.web.deps import templates

ROUTE = NS(
    online=True, provider="Example Service", model="vendor/model-x", dest="dest-1",
    sends="the posting text", this_send="one question", reask=False, employer="hidden",
    steps=[], many_providers=False, problem="", hidden=True, stamp="s1", draft_sends="a draft",
    pick_sends="your wordings", approval_json="{}", free=False, native=False,
)
BASE = {"route": ROUTE, "build_route": ROUTE,
        "ask": NS(route=ROUTE, problem="", approval_json="{}"),
        "qview": NS(route=ROUTE, applied=True, pending=False, questions=[], state="idle"),
        "ctx": NS(route=ROUTE, render=NS(id=1)), "rescoring": "confirm", "track": "employment",
        "back": "today", "opp_id": 1, "pid": 1, "sends_line": "one draft", "facts": {},
        "eligible": True, "result": None, "pending": False, "building": False, "stories": [],
        "approval_json": "{}", "routes": [ROUTE], "both_routes": [ROUTE], "sends": ["x"],
        "sends_both": ["x"], "usual": "Local", "employer": "hidden", "approval": {},
        "suggest_label": "Suggest titles", "facts_stamp": "f", "walk": "", "picked": "",
        "situations": [], "notes_count": 1, "base": "/b", "drafts": [], "themes": [],
        "total_stories": 0}

# (template, testid of the send button, testid of the box)
PANELS = [
    ("partials/profile_stories_list.html", "pf-stories-build", "build-confirm"),
    ("partials/pp_bank.html", "build-stories", "build-confirm"),
    ("partials/pp_qstate.html", "questions-regenerate", "questions-confirm"),
    ("partials/opp_questions.html", "questions-regenerate", "questions-confirm"),
    ("partials/profile_next_step.html", "score-again-send", "score-again-confirm"),
    ("partials/profile_title_suggestions.html", "title-suggest-again", "title-suggest-confirm"),
    ("partials/tailor_pick_dialog.html", "tl-send", "tl-confirm"),
    ("partials/follow_up.html", None, "follow-up-confirm"),
    ("partials/runs_choose.html", "rn-start", "rn-ok"),
    ("partials/sources_check_all.html", None, "sources-check-confirm"),
]

CONSENT_ENDPOINT_TESTS = (
    "tests/test_privacy_review.py (stories build, title suggestions, regenerate questions)",
    "tests/test_offers_screen.py (offer draft)", "tests/test_tailor_screen.py (pick)",
    "tests/test_follow_up.py (follow-up)", "tests/test_runs_page.py (runs start)",
    "tests/test_final_consent.py (check all)", "tests/test_score_again_notice.py (score again)",
)


def _render(name: str) -> BeautifulSoup:
    env = templates().env.overlay(undefined=jinja2.ChainableUndefined)
    ctx = dict(BASE)
    if name.endswith("profile_title_suggestions.html"):
        ctx.update(route=ROUTE, suggestions=[], note="")
    if name.endswith("follow_up.html"):
        ctx.update(route=ROUTE, eligible=True)
    return BeautifulSoup(env.get_template(name).render(**ctx), "html.parser")


@pytest.mark.parametrize(("name", "send_id", "box_id"), PANELS)
def test_the_send_button_is_never_disabled_for_want_of_the_tick(name, send_id, box_id):
    soup = _render(name)
    box = soup.select_one(f'[data-testid="{box_id}"]')
    assert box is not None and box.has_attr("data-consent-box")
    assert not box.has_attr("required") and not box.has_attr("disabled")
    hint = soup.select_one(f'[data-testid="{box_id}-hint"]')
    assert hint is not None and hint.has_attr("data-consent-hint")
    assert hint.get_text(strip=True) == "Tick the box to send"
    assert box["aria-describedby"] == hint["id"]
    assert box.find_parent(attrs={"data-consent-row": True}) is hint.find_parent(
        attrs={"data-consent-row": True})
    sends = soup.select("[data-consent-send]")
    assert sends, "no send button carries the hint hook"
    for btn in sends:
        assert not btn.has_attr("disabled")


@pytest.mark.parametrize(
    ("name", "send_id", "box_id"), [p for p in PANELS if p[1]], ids=lambda v: str(v)[:40]
)
def test_the_named_send_button_carries_the_hint_hook(name, send_id, box_id):
    soup = _render(name)
    btn = soup.select_one(f'[data-testid="{send_id}"]')
    assert btn is not None, f"{name}: no button {send_id}"
    assert btn.has_attr("data-consent-send")


def _scope_of(btn):
    """The panel kestrel-ui.js looks in for a send button's box: the nearest form, element marked
    data-consent-scope, or dialog (consentBoxFor)."""
    return btn.find_parent(lambda t: t.name in ("form", "dialog")
                           or t.has_attr("data-consent-scope") or t.get("role") == "dialog")


@pytest.mark.parametrize(("name", "send_id", "box_id"), PANELS)
def test_every_send_button_has_its_own_box_in_its_own_panel(name, send_id, box_id):
    soup = _render(name)
    for btn in soup.select("[data-consent-send]"):
        scope = _scope_of(btn)
        assert scope is not None, f"{name}: a send button outside any panel"
        assert scope.select_one("[data-consent-box]") is not None, (
            f"{name}: the button would borrow a neighbouring panel's box")


@pytest.mark.parametrize("name", ["partials/offer_negotiate.html",
                                  "partials/offer_negotiate_prep.html"])
def test_the_offer_draft_buttons_are_sends_behind_the_forms_own_box(name):
    """The pages need a full offer to render, so the markup is read with the Jinja removed and
    the notice (which holds the box, see the next test) spliced in where it is included."""
    import re

    root = Path(templates().env.loader.searchpath[0])
    src = (root / name).read_text()
    assert 'include "partials/offer_send_notice.html"' in src
    notice = (root / "partials/offer_send_notice.html").read_text()
    src = src.replace('{% include "partials/offer_send_notice.html" %}',
                      '<span data-testid="of-confirm" data-consent-box></span>')
    del notice
    src = re.sub(r"\{#.*?#\}", "", src, flags=re.S)
    src = re.sub(r"\{%.*?%\}", "", src, flags=re.S)
    src = re.sub(r"\{\{.*?\}\}", "x", src, flags=re.S)
    # data-consent-send sits inside {% if route.online %} ... {% endif %} in the source; the
    # attribute is plain text here, so re-add it for the buttons that carry it
    soup = BeautifulSoup(src, "html.parser")
    for btn in soup.select("[data-consent-send]"):
        assert btn.name == "button"
        scope = _scope_of(btn)
        assert scope is not None and scope.name == "form"
        assert scope.select_one("[data-consent-box]") is not None
    assert {b.get("data-testid") for b in soup.select("[data-consent-send]")} >= {"of-write"}
    writes = soup.select('[data-testid="of-write"]')
    assert writes
    for btn in writes:
        assert not btn.has_attr("disabled")  # never disabled for want of the tick


def test_the_offer_notice_has_the_box_and_the_hint():
    soup = _render("partials/offer_send_notice.html")
    box = soup.select_one('[data-testid="of-confirm"]')
    assert box.has_attr("data-consent-box") and not box.has_attr("disabled")
    assert soup.select_one('[data-testid="of-confirm-hint"]').get_text(strip=True) == (
        "Tick the box to send")
