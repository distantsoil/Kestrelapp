"""Every Pipeline row says where Kestrel found the role (a source pill), from the source that
brought it in, never from the posting's link: a pasted Indeed link reads "Pasted", and a link on
any host never names a board by itself. A role with no description says who sent it that way,
in a sentence that is true and grammatical for every source. Invented text only."""

from __future__ import annotations

import asyncio
import html as htmllib

import pytest

from kestrel.db.models import Opportunity, Source
from kestrel.web.fit import EMPTY_POSTING_STAGE_REASON
from kestrel.web.pipeline_view import empty_reason, source_word
from tests import test_pipeline_view as T

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


@pytest.mark.parametrize(
    ("kind", "site", "word"),
    [
        ("jobspy", "glassdoor", "Glassdoor"),
        ("jobspy", "indeed", "Indeed"),
        ("jobspy", "google", "Google Jobs"),
        ("jobspy", None, "Job board"),
        ("greenhouse", None, "Greenhouse"),
        ("workday", None, "Workday"),
        ("careers_page", None, "Company site"),
        ("rss", None, "Feed"),
        ("manual", "indeed", "Pasted"),  # a capture is pasted, whatever its link says
        ("import", None, "Imported"),
        ("page", None, "Single page"),  # every source kind has a word (review round 3)
        (None, None, None),  # source not known: no pill
    ],
)
def test_the_pill_names_where_kestrel_found_the_role(kind, site, word):
    assert source_word(kind, site) == word


@pytest.mark.parametrize(
    ("kind", "site", "sentence"),
    [
        ("jobspy", "glassdoor", "Glassdoor sent it without one, so there is nothing to score."),
        ("jobspy", None, "The job board sent it without one, so there is nothing to score."),
        ("greenhouse", None, "Greenhouse sent it without one, so there is nothing to score."),
        ("careers_page", None,
         "The company's site sent it without one, so there is nothing to score."),
        ("manual", None, "It was saved without one, so there is nothing to score."),
        ("import", None, "It was saved without one, so there is nothing to score."),
        (None, None, "It was saved without one, so there is nothing to score."),
    ],
)
def test_the_empty_posting_sentence_fits_every_source(kind, site, sentence):
    assert empty_reason(kind, site) == sentence
    assert "Pasted sent" not in sentence and "—" not in sentence


def _seed_sources() -> dict[str, int]:
    """Three roles whose links point somewhere other than where Kestrel found them."""
    asyncio.run(T._seed())

    async def go(s):
        org_id = (await s.execute(T.select(Opportunity.org_id).limit(1))).scalar_one()
        manual = Source(track="employment", kind="manual", name="Capture", enabled=False)
        search = Source(track="employment", kind="jobspy", name="Invented search",
                        config={"site_name": ["indeed", "glassdoor"]}, enabled=True)
        board = Source(track="employment", kind="greenhouse", name="Invented board",
                       enabled=True)
        s.add_all([manual, search, board])
        await s.flush()
        common = dict(track="employment", opp_type="job_posting", org_id=org_id,
                      stage="t1_pass", stage_reason=EMPTY_POSTING_STAGE_REASON,
                      description_md=None, first_seen_at=T.iso(T._seen_today()))
        made = {
            "pasted": Opportunity(source_id=manual.id, title="Invented Pasted Role",
                                  url="https://www.indeed.com/viewjob?jk=invented", **common),
            "glassdoor": Opportunity(source_id=search.id, title="Invented Search Role",
                                     url="https://www.glassdoor.com/job-listing/invented",
                                     raw={"site": "glassdoor"}, **common),
            "board": Opportunity(source_id=board.id, title="Invented Board Role",
                                 url="https://www.google.com/invented", **common),
        }
        s.add_all(made.values())
        await s.commit()
        return {k: o.id for k, o in made.items()}

    return asyncio.run(T._with_session(go))


def test_pipeline_rows_name_the_source_not_the_link():
    ids = _seed_sources()
    with T._client() as c:
        html = T._get(c, "/opportunities")

    def pill(oid):
        row = T._row(html, oid)
        start = row.index('data-testid="row-source"')
        return htmllib.unescape(row[row.index(">", start) + 1:row.index("</span>", start)])

    assert pill(ids["pasted"]) == "Pasted"  # an Indeed link, but it was pasted
    assert pill(ids["glassdoor"]) == "Glassdoor"
    assert pill(ids["board"]) == "Greenhouse"  # a google.com link never reads Google
    pasted = htmllib.unescape(T._row(html, ids["pasted"]))
    assert "It was saved without one, so there is nothing to score." in pasted
    assert "Pasted sent" not in pasted and "Indeed sent" not in pasted
    board = htmllib.unescape(T._row(html, ids["board"]))
    assert "Greenhouse sent it without one" in board


def test_every_source_kind_has_a_word():
    from kestrel.db.enums import SOURCE_KINDS

    missing = [k for k in SOURCE_KINDS if not source_word(k)]
    assert missing == []


def test_the_runs_card_never_prints_none_for_a_source():
    asyncio.run(T._seed())

    async def go(s):
        org_id = (await s.execute(T.select(Opportunity.org_id).limit(1))).scalar_one()
        page = Source(track="employment", kind="page", name="Invented page", enabled=False)
        s.add(page)
        await s.flush()
        s.add(Opportunity(track="employment", opp_type="job_posting", org_id=org_id,
                          source_id=page.id, title="Invented Single Page Role",
                          url="https://example.test/invented", stage="t1_pass",
                          stage_reason=EMPTY_POSTING_STAGE_REASON, description_md=None,
                          first_seen_at=T.iso(T._seen_today())))
        await s.commit()

    asyncio.run(T._with_session(go))
    with T._client() as c:
        html = c.get("/runs").text
    start = html.index('data-testid="runs-unscored"')
    card = htmllib.unescape(html[start:html.index("</section>", start)])
    line = card[card.index("Invented Single Page Role"):]
    line = line[:line.index("</li>")]
    assert "Single page" in line
    assert "None" not in line


def test_a_search_board_row_and_role_page_say_the_site_may_have_blocked_the_reader():
    """Issue 182: only a search board gets the note; a company board and a paste keep their
    wording, and the role stays visible either way."""
    ids = _seed_sources()
    with T._client() as c:
        html = T._get(c, "/opportunities")
        role = {k: c.get(f"/opportunities/{oid}").text for k, oid in ids.items()}
    glass = htmllib.unescape(T._row(html, ids["glassdoor"]))
    assert "may have blocked Kestrel's reader" in glass and "Worth checking" in glass
    for other in ("pasted", "board"):
        assert "may have blocked" not in htmllib.unescape(T._row(html, ids[other]))
    assert 'data-testid="eval-blocked-note"' in role["glassdoor"]
    assert "may have blocked" in htmllib.unescape(role["glassdoor"])
    assert "eval-blocked-note" not in role["board"]
    assert "eval-blocked-note" not in role["pasted"]
