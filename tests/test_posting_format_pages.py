"""Every page that shows a posting's own text uses the shared renderer (issue #210, D-UI-722):
no raw Markdown mark and no markup from the posting reaches any of them, and showing a posting
never changes the stored description (scoring reads it). Invented text; no network."""

from __future__ import annotations

import re

import pytest

from kestrel.db.models import Opportunity
from tests import quick_fixtures as qf

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

_TEXT = (
    "**Zephyr Marker** heads the posting.\n"
    "You will sort parcels at the depot each morning and log every late van. The team is small.\n\n"
    "## Zephyr heading\n"
    "* Zephyr item **strong**\n"
    "- Plain *quiet* item\n\n"
    "<script>alert('zephyr')</script> <img src=x onerror=alert(1)> [map](javascript:alert(1))\n"
    "Apply on [our page](https://depot.example/jobs)."
)


def _seed(text: str, *, stage: str = "t2_done") -> list[int]:
    ids = qf.seed(6)

    async def put(s):
        for oid in ids:
            opp = await s.get(Opportunity, oid)
            opp.description_md = text
            opp.stage = stage
        await s.commit()

    qf.run(put)
    return ids


def _stored(ids: list[int]) -> list[bytes]:
    async def go(s):
        return [((await s.get(Opportunity, oid)).description_md or "").encode() for oid in ids]

    return qf.run(go)


def _no_raw_marks_or_markup(body: str) -> None:
    assert "**Zephyr" not in body and "## Zephyr" not in body and "* Zephyr" not in body
    assert "alert('zephyr')</script>" not in body and "<img src=x" not in body
    assert 'href="javascript:' not in body and 'javascript:alert(1)"' not in body


def test_the_posting_tab_quick_round_and_calibration_render_the_shared_nodes():
    ids = _seed(_TEXT)
    before = _stored(ids)
    with qf.client() as c:
        posting = c.get(f"/opportunities/{ids[0]}/posting").text
        quick = c.get("/calibrate/quick").text
        calibrate = c.get("/calibrate").text
    desc = re.search(r'data-testid="description">(.*?)</div>', posting, re.S).group(1)
    assert desc.startswith('<p class="kp-p"><strong>Zephyr Marker</strong> heads the posting.')
    assert '<h4 class="kp-h kp-h--2">Zephyr heading</h4>' in desc
    assert '<li class="kp-li">Zephyr item <strong>strong</strong></li>' in desc
    assert "our page (https://depot.example/jobs)" in desc and "<a" not in desc

    card = re.findall(r'<article class="qr-card".*?</article>', quick, re.S)[0]
    full = re.search(r'data-testid="qr-full".*?</div>', card, re.S).group(0)
    assert "<strong>Zephyr Marker</strong>" in full and "<a" not in full
    excerpt = re.search(r'data-testid="qr-excerpt">(.*?)</p>', card, re.S).group(1)
    assert "**" not in excerpt

    fly = re.search(r'data-testid="cal-fulltext">(.*?)</div>', calibrate, re.S).group(1)
    assert "<strong>Zephyr Marker</strong>" in fly and "<a" not in fly
    cal_excerpt = re.search(r'<p class="cal-text">(.*?)</p>', calibrate, re.S).group(1)
    assert cal_excerpt.startswith("Zephyr Marker heads the posting.")

    for body in (posting, quick, calibrate):
        _no_raw_marks_or_markup(body)
    assert _stored(ids) == before == [_TEXT.encode()] * len(ids)


def test_the_not_a_fit_panel_excerpt_is_plain_words_without_marks():
    from kestrel.web.pages import pipeline

    out = pipeline._excerpt(_TEXT, {"mapping": None, "confidential": False})
    assert out.startswith("Zephyr Marker heads the posting. You will sort parcels")
    assert "**" not in out and "##" not in out and "<script" not in out


def test_the_not_a_fit_panel_excerpt_redacts_after_the_marks_are_gone(monkeypatch):
    from kestrel.web import redact
    from kestrel.web.pages import pipeline

    def fake(text, mapping, *, enabled):
        return text.replace("Zephyr Marker", "Current Employer") if enabled else text

    monkeypatch.setattr(redact, "redact_text", fake)
    out = pipeline._excerpt(
        "<b>Zephyr</b> Marker and **Zephyr Marker**", {"mapping": {}, "confidential": True}
    )
    assert out == "Current Employer and Current Employer"


def test_an_empty_description_still_says_so_on_the_posting_tab():
    ids = _seed("  \n<p>&nbsp;</p>\n")
    with qf.client() as c:
        posting = c.get(f"/opportunities/{ids[0]}/posting").text
    assert re.search(r'data-testid="description">\(no description\)</div>', posting)


def test_the_calibration_card_hides_the_current_employer_in_confidential_mode():
    """#210 review: Calibrate is not exempt from Confidential mode. The card's organization,
    title, place, excerpt and full posting all hide the current employer, including a name the
    posting split with marks."""
    from kestrel.db.engine import forget_engine
    from kestrel.db.models import DocMaster, DocSection, Setting

    ids = _seed("Acme Freight is hiring. Join Ac*me* Freight at the depot.\n\n"
                "## About Acme Freight\n* Work with the **Acme Freight** team")

    async def on(s):
        s.add(Setting(key="confidential_mode", value=True))
        m = DocMaster(track="employment", doc_kind="resume", name="Resume")
        s.add(m)
        await s.flush()
        s.add(DocSection(doc_master_id=m.id, section_kind="role", title="Experience",
                         org_label="Acme Freight", org_label_redacted="Current Employer",
                         end_date="present"))
        for oid in ids:
            (await s.get(Opportunity, oid)).title = "Analyst at Acme Freight"
        await s.commit()

    qf.run(on)
    forget_engine()
    with qf.client() as c:
        body = c.get("/calibrate?tab=detailed").text
    card = re.search(r'<div id="cal-card".*', body, re.S).group(0)
    assert 'data-testid="cal-fulltext"' in card and "Current Employer" in card
    assert "Acme" not in card


_SPLITS = ("Join Ac*me* Freight at the depot. You will sort parcels each morning and log "
           "every late van for the floor leads.\n\n"
           "## About Ac**me** Freight\n"
           "* Work with the **Ac**me Freight team\n"
           "- Ask Ac[me Freight](https://x.example) about it\n"
           "Ac­me Freight and Acme​ Freight pay weekly.")


def _confidential_with_splits() -> list[int]:
    from kestrel.db.engine import forget_engine
    from kestrel.db.models import DocMaster, DocSection, Setting

    ids = _seed(_SPLITS)

    async def on(s):
        s.add(Setting(key="confidential_mode", value=True))
        m = DocMaster(track="employment", doc_kind="resume", name="Resume")
        s.add(m)
        await s.flush()
        s.add(DocSection(doc_master_id=m.id, section_kind="role", title="Experience",
                         org_label="Acme Freight", org_label_redacted="Current Employer",
                         end_date="present"))
        await s.commit()

    qf.run(on)
    forget_engine()
    return ids


def _words(fragment: str) -> str:
    """The text a reader sees: tags removed, entities read, invisible marks kept."""
    import html as _html

    return _html.unescape(re.sub(r"<[^>]*>", "", fragment))


def test_every_surface_hides_a_name_split_by_marks_in_confidential_mode():
    """Owner (#210 review): Confidential mode is redacted on every surface the renderer serves,
    Calibrate included, and a name split by marks, a link or an invisible character never shows."""
    ids = _confidential_with_splits()
    with qf.client() as c:
        posting = c.get(f"/opportunities/{ids[0]}/posting").text
        quick = c.get("/calibrate/quick").text
        calibrate = c.get("/calibrate?tab=detailed").text
    surfaces = {
        "posting tab": re.search(r'data-testid="description">(.*?)</div>', posting, re.S).group(1),
        "quick full": re.search(r'data-testid="qr-full".*?</div>', quick, re.S).group(0),
        "quick excerpt": re.search(r'data-testid="qr-excerpt">(.*?)</p>', quick, re.S).group(1),
        "calibrate flyout": re.search(r'data-testid="cal-fulltext">(.*?)</div>', calibrate,
                                      re.S).group(1),
        "calibrate excerpt": re.search(r'<p class="cal-text">(.*?)</p>', calibrate, re.S).group(1),
    }
    for name, fragment in surfaces.items():
        seen = _words(fragment).replace("­", "").replace("​", "")
        assert "Acme" not in seen, (name, seen)
        assert "Current Employer" in seen, (name, seen)


def test_the_not_a_fit_excerpt_hides_a_split_name_with_a_real_employer_map():
    from kestrel.privacy import Employer, EmployerMap
    from kestrel.web.pages import pipeline

    mapping = EmployerMap([Employer(name="Acme Freight", shown_as="Current Employer")])
    out = pipeline._excerpt(_SPLITS, {"mapping": mapping, "confidential": True})
    assert "Acme" not in out.replace("­", "").replace("​", "")
    assert out.startswith("Join Current Employer at the depot.")
