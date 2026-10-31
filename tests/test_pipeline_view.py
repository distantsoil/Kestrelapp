"""The pipeline as a triage list (docs/UI_PROPOSAL.html Screen 2): striped rows with the model's
one-line summary, chips that map to the list query, the drawer with the evaluation and its four
actions, confidential-mode redaction in rows and drawer, the summary built without an N+1, the
actions still working from the drawer, and Hide with a reason. Cloud-safe: a migrated SQLite
file and the test client; no model, no browser, no network."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.base import iso
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import (
    DocMaster,
    DocSection,
    Evaluation,
    Opportunity,
    Org,
    Pursuit,
    Setting,
    Source,
    VerdictOverride,
)
from kestrel.web.queries import (
    OppFilters,
    first_sentence,
    list_opportunities,
    local_day_start_iso,
    t0_rules,
)

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

_EMPLOYER = "Northwind Freight"
_PLACEHOLDER = "[Confidential]"
# Read the clock when a test runs, never at import (a suite crossing midnight).
def _now() -> datetime:
    return datetime.now(UTC)


def _seen_today() -> datetime:
    """One second past local midnight (D-UI-9), computed when the row is built. Seeding "an hour
    ago" made the new_today chip test fail every day between midnight and 1am."""
    return datetime.fromisoformat(local_day_start_iso()) + timedelta(seconds=1)


def _fresh_engine():
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    return make_async_engine(paths.db_path())


def _opp(org, src, i, **kw) -> Opportunity:
    base = dict(
        track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
        title=f"VP of IT {i}", org_name=org.name, url=f"https://x/pipe-{i}", stage="t2_done",
        description_md="Own IT strategy, security and vendors.",
        first_seen_at=iso(_seen_today()), seen_count=3, location_raw="Austin, TX",
        remote_type="hybrid", comp_min=230000, comp_max=260000, comp_currency="USD",
        comp_interval="year",
    )
    base.update(kw)
    return Opportunity(**base)


def _ev(opp, **kw) -> Evaluation:
    base = dict(
        opportunity_id=opp.id, tier=2, track=opp.track, verdict="apply", verdict_model="apply",
        score=4.6, weighted_score=4.5, needs_review=False, model="kestrel-tier2",
        dimensions={"role_fit": {"score": 4.7, "why": "owns the function"},
                    "domain_match": {"score": 4.5, "why": "good overlap"}},
        blocks={"a_role_summary": {"what_this_actually_is": (
            f"Owns IT for {_EMPLOYER}, an acquired carrier mid-integration. Reports to the CFO."
        )}},
        blockers=[], legitimacy={"status": "clean", "signals": []},
    )
    base.update(kw)
    return Evaluation(**base)


async def _seed(*, confidential: bool = False) -> dict[str, int]:
    """a: apply, shortlisted (pursuit), seen today. b: needs review. c: consulting, pursue, with
    the situation block. d: no evaluation, first seen two days ago. e: quarantined. h: hidden
    (applied elsewhere). k: kill, hidden pill never shown."""
    await reset_engine()
    engine = _fresh_engine()
    sm = async_sessionmaker(engine, expire_on_commit=False)
    ids: dict[str, int] = {}
    async with sm() as s:
        org = Org(name=_EMPLOYER, normalized_name=_EMPLOYER.lower())
        other = Org(name="Ridgeline Utilities", normalized_name="ridgeline utilities")
        src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
        s.add_all([org, other, src])
        await s.flush()
        a = _opp(org, src, "a")
        b = _opp(org, src, "b")
        c = _opp(other, src, "c", track="consulting", opp_type="rfp",
                 title="Fractional CIO, post-acquisition integration", comp_min=2400,
                 comp_max=2400, comp_interval="day", location_raw=None, remote_type="remote")
        d = _opp(org, src, "d", first_seen_at=iso(_now() - timedelta(days=2)), stage="t0_pass")
        e = _opp(org, src, "e", stage="quarantined")
        h = _opp(org, src, "h", archived_at=iso(_now() - timedelta(hours=2)),
                 archive_reason="applied")
        k = _opp(org, src, "k", title="Senior IT Manager")
        s.add_all([a, b, c, d, e, h, k])
        await s.flush()
        ids.update(a=a.id, b=b.id, c=c.id, d=d.id, e=e.id, h=h.id, k=k.id)
        s.add_all(
            [
                _ev(a),
                _ev(b, score=3.8, weighted_score=3.3, verdict="consider", verdict_model="apply",
                    needs_review=True),
                _ev(c, verdict="pursue", verdict_model="pursue", score=4.4, blocks={
                    "a_situation": {"stated": (
                        "A 90-day integration lead for a utility that closed an acquisition. "
                        "The COO is the likely buyer."
                    )},
                }),
                _ev(h, score=4.0),
                _ev(k, verdict="kill", verdict_model="kill", score=2.6, weighted_score=2.5,
                    blocks={"a_role_summary": {"what_this_actually_is": "Title below band."}}),
            ]
        )
        s.add(Pursuit(opportunity_id=a.id, track="employment", status="shortlisted",
                      last_activity_at=iso(_now())))
        master = DocMaster(track="employment", name="Resume", doc_kind="resume")
        s.add(master)
        await s.flush()
        s.add(DocSection(doc_master_id=master.id, section_kind="experience", sort_order=0,
                         org_label=_EMPLOYER, org_label_redacted=_PLACEHOLDER))
        if confidential:
            s.add(Setting(key="confidential_mode", value=True))
        await s.commit()
    await engine.dispose()
    forget_engine()
    return ids


async def _with_session(fn):
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sm() as s:
            return await fn(s)
    finally:
        await engine.dispose()


def _client() -> TestClient:
    from kestrel.web import app as app_module

    return TestClient(app_module.build_app(worker=False))


def _row_ids(html: str) -> list[int]:
    return [int(m) for m in re.findall(r'data-id="(\d+)"', html)]


async def _whole_list(s, **kw):
    """The grouped list's visible groups followed by its Not a fit section (D-UI-62), as one
    OppPage, for checks that span every group."""
    from dataclasses import replace as _replace

    f = OppFilters(grouped=True, sort="first_seen", page_size=200, **kw)
    main = await list_opportunities(s, f)
    nf = await list_opportunities(s, _replace(f, section="not_fit"))
    # D-UI-198: Not a fit sits between Applied and Being scored.
    scoring = [r for r in main.rows if r.fit.group in ("new", "failed")]
    scored = [r for r in main.rows if r.fit.group not in ("new", "failed")]
    main.rows = scored + nf.rows + scoring
    return main


def _get(c, url: str) -> str:
    """The Pipeline page plus its Not a fit section's rows, as the operator sees them after
    pressing Show (D-UI-62): Not a fit is its own section, outside the paged groups."""
    html = c.get(url).text
    if 'data-testid="not-fit-show"' in html:
        query = url.split("?", 1)[1] if "?" in url else ""
        html += c.get("/opportunities/not-fit" + ("?" + query if query else "")).text
    return html


def _row(html: str, opp_id: int) -> str:
    """The markup of one row, from its opening tag to the next row or the end of the list."""
    anchor = html.index(f'id="row-{opp_id}"')
    start = html.rfind('<div class="pl-row', 0, anchor)
    rest = html[start:]
    nxt = re.search(r'<div class="pl-row', rest[1:])
    return rest[: nxt.start() + 1] if nxt else rest.split("</section>")[0]


# ------------------------------------------------------------------------------------- rows
_OLD_WORDS = re.compile(
    r">\s*(keep|kill|pass|apply|consider|pursue|t0 reject|t0_reject|needs review|shortlisted|"
    r"quarantined|evaluated|override verdict|agree)\s*<",
    re.IGNORECASE,
)


def test_list_groups_rows_by_fit_with_one_word_and_one_number_per_row():
    ids = asyncio.run(_seed())
    with _client() as c:
        html = _get(c, "/opportunities")
    assert 'id="pipeline"' in html and 'id="pl-panel"' in html
    # Grouped (D-UI-45, D-UI-51, D-UI-56): Worth a look, Maybe (none here), Being scored,
    # Scoring failed, Not a fit; Best fit first inside each group (D-UI-96). Hidden rows out. The
    # list is one track, the active tab's (D-UI-57): the consulting lead "c" is on the
    # Consulting tab.
    assert _row_ids(html) == [ids["a"], ids["b"], ids["d"], ids["e"], ids["k"]]
    assert "Worth a look · 2" in html and "Being scored · 1" in html
    assert "Scoring failed · 1" in html
    # Not a fit is its own section below the paged groups, closed until Show (D-UI-62).
    main = html.split('data-testid="not-fit-rows"')[0]
    assert 'data-testid="not-fit-section"' in main and "Not a fit · 1<" in main
    assert 'data-testid="not-fit-show"' in main and ">Show</button>" in main
    assert ids["k"] not in _row_ids(main)
    a = _row(html, ids["a"])
    # The row's stripe takes the fit tone and the score takes it too (D-UI-51).
    assert 'class="pl-row is-good"' in a and 'data-fit="strong fit"' in a
    assert "Strong fit</span>" in a
    assert 'data-testid="on-shortlist">On my shortlist</span>' in a
    assert f"{_EMPLOYER} · Austin, TX · Hybrid · $230,000 to $260,000 · Seen 3 times" in a
    # No summary on the row (D-UI-96): it is in the panel, where the model's first sentence leads.
    assert "Owns IT for" not in a and "Reports to the CFO" not in a
    assert 'data-fit-score>4.6 <small>/ 5</small>' in a
    assert "4.5" not in a  # one number per row: the weighted score is the panel's
    # A healthy row carries no warning icon and no badge beyond its fit and shortlist tags.
    assert 'data-testid="row-warning"' not in a
    b = _row(html, ids["b"])
    assert 'class="pl-row is-warn"' in b and 'data-fit="your call"' in b
    assert "model and score disagree" in b
    # The needs-review divergence is the one warning icon, with its tooltip.
    assert 'data-testid="row-warning"' in b and "disagree on this one" in b
    consulting = _get(c, "/opportunities?track=consulting")
    assert _row_ids(consulting) == [ids["c"]]
    c_row = _row(consulting, ids["c"])
    assert 'class="pl-row is-good"' in c_row and ">RFP</div>" in c_row
    assert "Ridgeline Utilities · Remote · $2,400 a day" in c_row
    assert "The COO is the likely buyer" not in c_row
    d = _row(html, ids["d"])
    # Unscored: grey with a dashed stripe and a pill that says where it is, never a dash.
    assert 'class="pl-row is-new"' in d and 'data-fit="new"' in d
    assert 'data-scoring="not_queued">not queued</span>' in d
    assert "data-fit-score" not in d  # no score, and no dash standing in for one
    k = _row(html, ids["k"])
    assert 'data-fit="not a fit"' in k and "data-fit-score" not in k
    assert 'data-scoring="failed">failed</span>' in _row(html, ids["e"])
    # The header counts every open role, Not a fit's included. Score again is the bulk bar's
    # (and Needs another try's): the old toolbar button is gone (D-UI-96).
    assert "5 open · 1 still being scored" in html and "1 needs another try</a>" in html
    assert 'data-testid="pipeline-score-again"' not in html
    assert 'data-bulk="score-again"' in html
    # Every row is a keyboard target; clicking one opens its panel (pipeline.js).
    assert 'tabindex="0"' in a and 'data-testid="pl-row"' in a
    # The stored enums never reach the operator, and no dashes stand in for a missing number.
    assert not _OLD_WORDS.search(html), _OLD_WORDS.search(html)
    assert "—" not in html and "–" not in html


def test_the_sql_grouping_agrees_with_fit_for_row_for_row():
    """The page orders and counts by a SQL CASE; the words come from fit.fit_for. They must
    name the same group for every kind of row, a Tier 0 rule reject and a Tier 1 keep included."""
    from kestrel.web import fit

    ids = asyncio.run(_seed())

    async def more(s):
        org = (await s.execute(select(Org))).scalars().first()
        src = (await s.execute(select(Source))).scalars().first()
        r = _opp(org, src, "r", stage="t0_reject", stage_reason="blocker:anti_title: help desk")
        p = _opp(org, src, "p", stage="t0_reject", stage_reason="blocker:comp_floor: below")
        t1 = _opp(org, src, "t1", stage="t1_pass")
        m = _opp(org, src, "m")
        ap = _opp(org, src, "ap")  # an open pursuit at `applied`: its own group (D-UI-51)
        s.add_all([r, p, t1, m, ap])
        await s.flush()
        s.add(_ev(t1, tier=1, verdict="keep", verdict_model="keep", score=None))
        s.add(_ev(m, verdict="consider", verdict_model="consider", score=2.4))
        s.add(_ev(ap))
        s.add(Pursuit(opportunity_id=ap.id, track="employment", status="applied"))
        await s.commit()
        return r.id, p.id, t1.id, m.id

    r_id, p_id, t1_id, m_id = asyncio.run(_with_session(more))

    pg = asyncio.run(_with_session(_whole_list))
    by_id = {row.opp.id: row for row in pg.rows}
    for row in pg.rows:
        model = fit.fit_for(row.ev, row.opp.stage, t0_rules=t0_rules(row.opp))
        # a has a pursuit: Worth a look whatever the model said (D-UI-51).
        want = fit.GROUP_WORTH if row.pursuit_id else model.group
        if row.pursuit_status == "applied":
            want = fit.GROUP_APPLIED
        assert row.fit.word == model.word and row.fit.group == want
    groups = [row.fit.group for row in pg.rows]
    order = [fit.GROUP_WORTH, fit.GROUP_MAYBE, fit.GROUP_APPLIED, fit.GROUP_NOT_FIT,
             fit.GROUP_NEW, fit.GROUP_FAILED]
    assert groups == sorted(groups, key=order.index)  # rows arrive in group order
    assert by_id[m_id].fit.word == fit.MAYBE and by_id[t1_id].fit.word == fit.NEW
    assert by_id[r_id].fit.why == "title rule" and by_id[p_id].fit.why == "pay rule"
    assert pg.group_counts == {fit.GROUP_WORTH: 3, fit.GROUP_MAYBE: 1, fit.GROUP_APPLIED: 1,
                               fit.GROUP_NOT_FIT: 3, fit.GROUP_NEW: 2, fit.GROUP_FAILED: 1}
    # The paged total leaves Not a fit out (D-UI-62); its note covers the whole filter set.
    assert pg.rule_rejects == 2 and pg.total == 8
    assert sorted(pg.not_fit_rules) == [("pay", 1), ("title", 1)]
    with _client() as c:
        html = _get(c, "/opportunities")
        rules_only = c.get("/opportunities?stage=t0_reject").text
    assert "Maybe · 1" in html
    note = html.split('data-testid="not-fit-note">')[1].split("</div>")[0]
    assert note.startswith("2 set aside by your rules before any model ran (pay 1, title 1); 1 "
                           "scored low by the model. Not counted in the groups.")
    # Only rule rejects: the section opens with its first page, no Show button needed.
    assert "Not a fit · 2<" in rules_only and 'data-testid="not-fit-show"' not in rules_only
    assert 'data-testid="not-fit-rows"' in rules_only
    assert ids["a"] not in _row_ids(rules_only) and len(_row_ids(rules_only)) == 2


def test_unscored_rows_come_before_not_a_fit_and_not_a_fit_pages_twelve_at_a_time():
    """Hard rule 4: a quarantined row or a Tier 1 keep must not hide deep behind Tier 0
    rejects. Being scored sorts ahead of Not a fit; Not a fit never takes a slot in the groups
    above it (D-UI-62) and pages on its own, twelve at a time with Load more (D-UI-96)."""
    ids = asyncio.run(_seed())

    async def rejects(s):
        org = (await s.execute(select(Org))).scalars().first()
        src = (await s.execute(select(Source))).scalars().first()
        s.add_all([_opp(org, src, f"r{i}", stage="t0_reject",
                        stage_reason="blocker:anti_title: help desk") for i in range(14)])
        await s.commit()

    asyncio.run(_with_session(rejects))
    with _client() as c:
        first = _get(c, "/opportunities")
        nf1 = c.get("/opportunities/not-fit").text
        nf2 = c.get("/opportunities/not-fit?nf_page=2").text
    main = first.split('data-testid="not-fit-rows"')[0]
    assert _row_ids(main) == [ids["a"], ids["b"], ids["d"], ids["e"]]
    assert "Being scored · 1" in main and "Scoring failed · 1" in main
    assert "Not a fit · 15<" in main and ">Show</button>" in main
    assert "15 set aside by your rules before any model ran" not in main  # k is the model's
    assert "14 set aside by your rules before any model ran (title 14); 1 scored low" in main
    assert 'data-testid="show-more-' not in main  # every group fits in its first 25
    # Not a fit pages on its own: twelve a page, Load more asks for the next.
    assert len(_row_ids(nf1)) == 12 and len(_row_ids(nf2)) == 3
    assert "Showing 12 of 15" in nf1 and "nf_page=2" in nf1 and 'data-testid="not-fit-more"' in nf1
    assert "Showing 15 of 15" in nf2 and 'data-testid="not-fit-more"' not in nf2
    assert 'hx-swap-oob="true"' in nf2  # the footer is replaced, the rows are appended
    assert not set(_row_ids(nf1)) & set(_row_ids(nf2))


def test_a_group_shows_25_rows_and_show_25_more_asks_for_25_more():
    """D-UI-96: each fit group pages together, 25 rows at a time, the state kept in the URL."""
    asyncio.run(_seed())

    async def lots(s):
        org = (await s.execute(select(Org))).scalars().first()
        src = (await s.execute(select(Source))).scalars().first()
        rows = [_opp(org, src, f"w{i}") for i in range(30)]
        s.add_all(rows)
        await s.flush()
        s.add_all([_ev(r, score=4.0 + (i % 10) / 100) for i, r in enumerate(rows)])
        await s.commit()

    asyncio.run(_with_session(lots))
    with _client() as c:
        first = c.get("/opportunities").text
        more = c.get("/opportunities?lim_worth=50").text
    worth = first.split('<section class="pl-group" data-group="worth"')[1].split("</section>")[0]
    assert worth.count('data-testid="pl-row"') == 25 and "Worth a look · 32" in first
    assert 'data-testid="show-more-worth"' in worth and ">Show 7 more</button>" in worth
    assert "lim_worth=50" in worth  # the button asks for 25 more than are shown, in the URL
    worth2 = more.split('<section class="pl-group" data-group="worth"')[1].split("</section>")[0]
    assert worth2.count('data-testid="pl-row"') == 32
    assert 'data-testid="show-more-worth"' not in worth2


def test_best_fit_sorts_by_the_fit_number_inside_each_group():
    ids = asyncio.run(_seed())
    with _client() as c:
        html = _get(c, "/opportunities?sort=score")
        default = _get(c, "/opportunities")
        newest = _get(c, "/opportunities?sort=first_seen")
        company = c.get("/opportunities?sort=company")
        oldest = c.get("/opportunities?sort=oldest")
    assert _row_ids(html) == [ids["a"], ids["b"], ids["d"], ids["e"], ids["k"]]
    assert _row_ids(default) == _row_ids(html)  # Best fit is the default (the locked mockup)
    assert _row_ids(newest) == [ids["b"], ids["a"], ids["d"], ids["e"], ids["k"]]
    assert oldest.status_code == 200 and company.status_code == 200
    assert re.search(r'class="is-on" data-sort="score"', default)
    assert re.search(r'class="is-on" data-sort="first_seen"', newest)
    assert 'name="sort" value="score"' in default and 'name="sort" value="first_seen"' in newest
    assert ">Newest</button>" in default and ">Best fit</button>" in default
    assert "best fit first" in default and "newest first" in newest
    # The menu beside the toggle: the rest of the sorts.
    for label in ("Date posted", "First seen", "Oldest first", "Company A to Z",
                  "Quick-screen score", "Ghost posting risk"):
        assert f">{label}</button>" in default, label


def test_first_sentence_keeps_abbreviations_and_clips_long_prose():
    assert first_sentence("Reports to the CFO. Owns security.") == "Reports to the CFO."
    assert first_sentence("Acme Inc. is a carrier; it runs 40 sites. Second.") == (
        "Acme Inc. is a carrier; it runs 40 sites."
    )
    assert first_sentence("  spaced   out\nprose ") == "spaced out prose"
    assert first_sentence(None) is None and first_sentence("") is None
    long = "word " * 80
    out = first_sentence(long, limit=50)
    assert len(out) <= 50 and out.endswith("…")


# ------------------------------------------------------------------------------------ chips
def test_each_chip_maps_to_its_query_parameter():
    ids = asyncio.run(_seed())
    with _client() as c:
        base = _get(c, "/opportunities")
        for name in ("all", "shortlisted", "needs_review", "evaluated", "new_today", "saved",
                     "failed", "hidden"):
            assert f'data-chip="{name}"' in base, name
        # Failed scoring opens Needs another try and carries that page's count.
        assert re.search(r'href="/quarantine"[^>]*data-chip="failed"[^>]*>Needs another try'
                         r'<span class="k-chip__n">1</span>', base)
        assert 'data-chip="clear"' not in base  # nothing active: nothing to clear
        for label in ("My shortlist", "Your call", "Scored", "New today", "Saved",
                      "Needs another try", "Hidden", "More filters"):
            assert label in base, label
        # The counts: all open roles outside Not a fit, my shortlist, your call, scored, new.
        assert re.search(r'data-chip="all">All<span class="k-chip__n">4</span>', base)
        assert re.search(r'data-chip="shortlisted">My shortlist<span class="k-chip__n">1</span>',
                         base)
        assert re.search(r'data-chip="needs_review">Your call<span class="k-chip__n">1</span>',
                         base)

        shortlisted = _get(c, "/opportunities?shortlisted=1")
        assert _row_ids(shortlisted) == [ids["a"]]
        assert re.search(r'class="k-chip is-on"[^>]*data-chip="shortlisted"', shortlisted)
        assert 'data-chip="clear"' in shortlisted

        assert _row_ids(_get(c, "/opportunities?needs_review=1")) == [ids["b"]]
        assert _row_ids(_get(c, "/opportunities?stage=t2_done")) == [
            ids["a"], ids["b"], ids["k"]
        ]
        assert _row_ids(_get(c, "/opportunities?new_today=1")) == [
            ids["a"], ids["b"], ids["e"], ids["k"]
        ]
        assert _row_ids(_get(c, "/opportunities?stage=quarantined")) == [ids["e"]]
        hidden = _get(c, "/opportunities?hidden=1")
        assert _row_ids(hidden) == [ids["h"]]
        assert "hidden: applied elsewhere" in _row(hidden, ids["h"])
        assert 'name="hidden" value="1"' in hidden  # the chip's parameter rides on the form
        # The track convention and the search box still work, and search covers org names.
        assert _row_ids(_get(c, "/opportunities?track=consulting")) == [ids["c"]]
        assert _row_ids(_get(c, "/opportunities?q=ridgeline&track=consulting")) == [ids["c"]]
        assert _row_ids(_get(c, "/opportunities?q=senior")) == [ids["k"]]
        # Chips combine: shortlisted AND needs review is empty, with the empty state.
        both = _get(c, "/opportunities?shortlisted=1&needs_review=1")
        assert _row_ids(both) == [] and "Nothing matches these filters" in both
        assert 'data-testid="clear-filters"' in both and "j</kbd>" in both
        # Sort: newest and ghost are accepted; an HTMX request gets only the list block.
        assert c.get("/opportunities?sort=first_seen").status_code == 200
        part = c.get("/opportunities?sort=ghost", headers={"HX-Request": "true"}).text
        assert part.lstrip().startswith('<div id="pipeline-list"') and "<html" not in part
        assert 'id="pl-counts"' in part and 'hx-swap-oob="true"' in part
        # An older URL's filters ride along (More filters inputs, or hidden fields) so chips do
        # not drop them.
        old = _get(c, "/opportunities?score_min=4&org=north&verdict=apply")
        assert '<option value="4.0" selected>' in old and 'name="org" value="north"' in old
        assert 'name="verdict" value="apply"' in old


def test_empty_state_points_at_sources_and_setup_when_nothing_was_ingested():
    asyncio.run(_seed())

    async def _wipe(s):
        for row in (await s.execute(select(Pursuit))).scalars():
            await s.delete(row)
        for row in (await s.execute(select(Evaluation))).scalars():
            await s.delete(row)
        for row in (await s.execute(select(Opportunity))).scalars():
            await s.delete(row)
        await s.commit()

    asyncio.run(_with_session(_wipe))
    with _client() as c:
        html = _get(c, "/opportunities")
    assert "No opportunities yet" in html
    assert 'href="/sources"' in html and 'href="/setup"' in html


# ----------------------------------------------------------------------------------- drawer
def test_panel_header_has_the_link_icon_and_close_rightmost_and_the_footer_opens_the_role():
    """D-UI-96: each panel has its own URL (the link icon copies it) and Close is always the
    rightmost control; the footer's Open full role goes to the role page. The older /drawer
    path still answers with the same panel."""
    ids = asyncio.run(_seed())
    with _client() as c:
        html = c.get(f"/opportunities/{ids['b']}/panel").text
        old = c.get(f"/opportunities/{ids['b']}/drawer").text
    head = html.split('<div class="pl-dh">', 1)[1].split('<div class="pl-db">')[0]
    assert head.index("data-pl-copy") < head.index("data-pl-close")
    assert head.rstrip().endswith("</div>") and head.rindex("data-pl-close") > head.rindex(
        "data-pl-copy")
    link = f'<a class="k-btn k-btn--primary" href="/opportunities/{ids["b"]}" '
    assert link in html and ">Open full role &rarr;<" in html
    assert html.count('data-testid="panel-open"') == 1
    assert old == html


def test_panel_deep_link_renders_the_panel_open_with_the_list():
    """?open=<id> is the panel's own URL: Back and a pasted link land on the page with it open."""
    ids = asyncio.run(_seed())
    with _client() as c:
        page = c.get(f"/opportunities?open={ids['b']}").text
        none = c.get("/opportunities").text
    assert f'data-open="{ids["b"]}"' in page and 'class="pl-panel is-open"' in page
    assert f'<div class="pl-panel__in" data-kind="role" data-id="{ids["b"]}">' in page
    assert 'class="pl-panel"' in none and "pl-panel__in" not in none


def test_panel_has_the_ring_the_boxes_the_summary_and_three_buttons():
    ids = asyncio.run(_seed())
    with _client() as c:
        html = c.get(f"/opportunities/{ids['b']}/panel").text
        a = c.get(f"/opportunities/{ids['a']}/panel").text
        assert c.get("/opportunities/999999/panel").status_code == 404
    assert "<html" not in html and 'data-kind="role"' in html
    assert 'data-fit="your call"' in html
    # The score ring and the dimension bars (the model's own rationale stays on the role page).
    assert 'aria-label="3.8 out of 5"' in html and "Role fit" in html and "Domain match" in html
    assert html.count('class="pl-dim"') == 2
    # The two opinions are named because they differ.
    assert "The model said good fit, the scoring rules said maybe" in html
    # The model-written summary, labeled, with the disclaimer under it.
    assert "Owns IT for Northwind Freight, an acquired carrier mid-integration." in html
    assert "Written by Kestrel's AI from the posting." in html.replace("&#39;", "'")
    assert "AI overviews are the model" in html.replace("&#39;", "'")
    assert "holistic" not in html and "weighted" not in html
    # The pay box: the posted pay, no estimate tag, and no bar without the model's read.
    assert "$230,000 to $260,000" in html and 'class="pl-est"' not in html
    # The footer: Shortlist, Not for me, Open full role; the reasons are one click away.
    assert f'hx-post="/opportunities/{ids["b"]}/decide/shortlist"' in html and ">Shortlist<" in html
    assert "data-pl-ask" in html and ">Not for me<" in html
    for key, text in (("kind", "Not my kind of role"), ("pay_place", "Pay or location"),
                      ("level", "Wrong level"), ("later", "Just not now")):
        tag = f'hx-post="/opportunities/{ids["b"]}/decide/not-for-me?reason={key}"'
        assert tag in html and f">{text}</button>" in html, key
    assert "Not for me because…" in html and "Rate it on the full role page" in html
    # The detailed rating, the follow-ups and the posting link live on the role page.
    assert "Disagree with the fit?" not in html and 'type="range"' not in html
    assert "Deeper read" not in html and "Research the company" not in html
    assert not _OLD_WORDS.search(html) and not _OLD_WORDS.search(a)
    # A shortlisted role shows where it stands; the locked mockup keeps Not for me beside it.
    assert 'data-testid="panel-on-list"' in a and "On my shortlist" in a
    assert 'data-testid="panel-not-for-me"' in a and 'data-testid="panel-open"' in a

    with _client() as c:
        no_ev = c.get(f"/opportunities/{ids['d']}/panel").text
        hidden = c.get(f"/opportunities/{ids['h']}/panel").text
        detail = c.get(f"/opportunities/{ids['b']}").text
        shortlisted = c.get(f"/opportunities/{ids['a']}").text
    assert 'data-kind="pending"' in no_ev and "Being scored" in no_ev
    assert 'data-testid="panel-put-back"' in hidden
    assert 'data-testid="panel-not-for-me"' not in hidden
    # The role page has Your view (Yes or Adjust) in place of the old rating control, Deeper read
    # and Research in its decision column, and Prepare open once it is on the shortlist.
    assert 'data-testid="your-view"' in detail and 'type="range"' not in detail
    assert "Disagree with the fit?" not in detail
    assert 'data-testid="escalate"' in detail and 'data-testid="research"' in detail
    assert 'data-locked="1"' in detail and 'data-locked="1"' not in shortlisted
    assert f"&amp;opportunity_id={ids['a']}&amp;from=/opportunities/{ids['a']}" in shortlisted
    assert ">Tailor resume<" in shortlisted


def test_watch_names_the_legitimacy_flags_in_plain_words():
    from kestrel.web.routes_pages import watch_lines

    ids = asyncio.run(_seed())

    async def flag(s):
        b = await s.get(Opportunity, ids["b"])
        b.ghost_score = 0.7
        # Nothing known about place or pay, so the "nothing named" sign stands (D-UI-205).
        b.location_raw, b.comp_min, b.comp_max = None, None, None
        s.add(_ev(b, needs_review=True, verdict="consider", verdict_model="apply", score=3.8,
                  legitimacy={"status": "suspect", "signals": [
                      "no_comp_no_location_no_team", "staffing_agency_unnamed_client"]}))
        await s.commit()

    asyncio.run(_with_session(flag))
    with _client() as c:
        html = c.get(f"/opportunities/{ids['b']}/panel").text
        rows = _get(c, "/opportunities")
        detail = c.get(f"/opportunities/{ids['b']}").text
    # The panel's "Worth checking" lists every line, in plain words.
    watch = html.split('data-testid="panel-watch"')[1].split('<div class="pl-dgrid">')[0]
    for line in ("No pay, location or team named.", "An agency posting; the client is not named.",
                 "May be a ghost posting."):
        assert line in watch, line
    assert "no_comp" not in html
    # The row carries ONE warning icon whose tooltip says what is wrong.
    row = _row(rows, ids["b"])
    assert row.count('data-testid="row-warning"') == 1
    assert "Check the posting before you rely on it." in row and "Possible ghost posting" in row
    assert "Worth a second look" in row
    assert watch_lines({"signals": ["brand_new_signal"]}) == ["Brand new signal."]
    assert watch_lines(None) == []
    for line in ("No pay, location or team named.", "An agency posting; the client is not named.",
                 "May be a ghost posting."):
        assert f'data-testid="fit-watch">{line}</li>' in detail, line


def test_hidden_class_rule_stays_and_the_decision_actions_swap_the_panel():
    """`div.row { display:flex }` outranked `.hidden`; the global rule is !important. The panel's
    three writes swap the panel itself (#decision), never just a note."""
    from pathlib import Path

    from kestrel import paths as paths_mod

    css = (Path(paths_mod.web_static_dir()) / "css" / "kestrel.css").read_text(encoding="utf-8")
    assert ".hidden { display: none !important; }" in css
    assert ".pill.idle" in css and ".stripe.idle" in css
    detail_tmpl = (
        Path(paths_mod.web_templates_dir()) / "opportunity_detail.html"
    ).read_text(encoding="utf-8")
    assert "div.row.hidden" not in detail_tmpl  # the page-local patch is gone
    ids = asyncio.run(_seed())
    with _client() as c:
        drawer = c.get(f"/opportunities/{ids['b']}").text
        hidden = c.get(f"/opportunities/{ids['h']}").text
        expand = c.get(f"/opportunities/{ids['a']}/expand").text
    for action in ("shortlist", "not-for-me"):
        assert f'/opportunities/{ids["b"]}/role/{action}' in drawer, action
    tag = hidden.split(f'/opportunities/{ids["h"]}/role/put-back"')[1].split(">")[0]
    assert 'hx-target="#rl-side" hx-swap="outerHTML"' in tag
    assert 'name="fit" value="good fit"' in expand  # the legacy expansion has the control too


def test_confidential_mode_redacts_org_and_summary_in_rows_and_panel():
    ids = asyncio.run(_seed(confidential=True))
    with _client() as c:
        rows = _get(c, "/opportunities")
        panel = c.get(f"/opportunities/{ids['a']}/panel").text
    assert _EMPLOYER not in rows and _PLACEHOLDER in rows
    assert f"{_PLACEHOLDER} · Austin, TX · Hybrid" in _row(rows, ids["a"])
    assert _EMPLOYER not in panel and _PLACEHOLDER in panel
    assert f"Owns IT for {_PLACEHOLDER}" in panel


def test_summary_lines_come_from_the_list_query_not_one_statement_per_row():
    asyncio.run(_seed())

    async def _count(s):
        engine = s.get_bind()
        statements: list[str] = []

        def _on_execute(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(engine, "before_cursor_execute", _on_execute)
        try:
            page = await list_opportunities(s, OppFilters())
        finally:
            event.remove(engine, "before_cursor_execute", _on_execute)
        return page, statements

    page, statements = asyncio.run(_with_session(_count))
    assert len(page.rows) == 6
    assert sum(1 for r in page.rows if r.summary) == 4
    # The rows and the total: two statements for six rows, four of them with a summary.
    assert len(statements) == 2, f"expected 2 SELECTs, got {len(statements)}"


# -------------------------------------------------------------------------- actions, hide
async def _labels(s):
    return list(
        (await s.execute(select(VerdictOverride).order_by(VerdictOverride.id))).scalars().all()
    )


def test_shortlist_route_still_works_and_records_the_add_label():
    ids = asyncio.run(_seed())
    with _client() as c:
        r = c.post(f"/opportunities/{ids['b']}/shortlist")
        assert r.status_code == 200 and "Added to your shortlist" in r.text
        assert 'id="opp-actions"' in r.text and 'class="panel"' in r.text
        again = c.post(f"/opportunities/{ids['b']}/shortlist")
        assert "Already on your shortlist" in again.text
        rows = _get(c, "/opportunities?shortlisted=1")
    assert set(_row_ids(rows)) == {ids["a"], ids["b"]}
    labels = asyncio.run(_with_session(_labels))
    # b is a "your call" whose model said apply at 3.8: Add sides with the model, once, at the
    # good fit training score (3.8 is below the apply bar).
    assert [(lb.opportunity_id, lb.operator_verdict, lb.operator_score) for lb in labels] == [
        (ids["b"], "apply", 4.0)
    ]
    assert labels[0].system_verdict == "consider" and labels[0].reason


def test_add_to_my_list_writes_the_pursuit_and_the_agree_label_once():
    ids = asyncio.run(_seed())
    with _client() as c:
        r = c.post(f"/opportunities/{ids['c']}/add", headers={"HX-Request": "true"})
        assert r.status_code == 200
        # The panel re-renders in place, the list refreshes, the badges move out of band.
        assert r.text.lstrip().startswith('<div id="decision"')
        assert r.headers["HX-Trigger"] == "pipelineRefresh"
        assert 'id="nav-count-shortlist" class="n" hx-swap-oob="true"' in r.text
        # Seed "c" is a consulting lead, so the panel speaks consulting (D-UI-54).
        assert "Added to your leads" in r.text and "In my leads · " in r.text
        assert "Once it is in your leads" in r.text and ">Add to my leads<" not in r.text
        r2 = c.post(f"/opportunities/{ids['c']}/add", headers={"HX-Request": "true"})
        assert r2.status_code == 200 and "Already in your leads" in r2.text
        row = _row(_get(c, "/opportunities?track=consulting"), ids["c"])
        assert 'data-testid="on-shortlist">In my leads</span>' in row
        assert "you said" not in row  # an agree is the same word as the fit
        assert c.post("/opportunities/999999/add").status_code == 200

    async def state(s):
        pursuits = (
            await s.execute(select(Pursuit).where(Pursuit.opportunity_id == ids["c"]))
        ).scalars().all()
        return pursuits, await _labels(s)

    pursuits, labels = asyncio.run(_with_session(state))
    assert len(pursuits) == 1 and pursuits[0].track == "consulting"
    assert len(labels) == 1
    lb = labels[0]
    assert lb.opportunity_id == ids["c"]
    assert (lb.operator_verdict, lb.operator_score) == (lb.system_verdict, lb.system_score)
    assert (lb.operator_verdict, lb.operator_score) == ("pursue", 4.4)


def test_add_to_my_list_on_a_role_the_system_did_not_rate_records_good_fit():
    ids = asyncio.run(_seed())
    with _client() as c:
        assert c.post(f"/opportunities/{ids['k']}/add").status_code == 200
        assert c.post(f"/opportunities/{ids['d']}/add").status_code == 200
    labels = {lb.opportunity_id: lb for lb in asyncio.run(_with_session(_labels))}
    assert (labels[ids["k"]].operator_verdict, labels[ids["k"]].operator_score) == ("apply", 4.0)
    assert labels[ids["k"]].system_verdict == "kill"
    assert labels[ids["d"]].evaluation_id is None and labels[ids["d"]].operator_score == 4.0


def test_not_for_me_hides_with_not_interested_and_records_a_pass_once():
    ids = asyncio.run(_seed())
    with _client() as c:
        r = c.post(f"/opportunities/{ids['b']}/not-for-me", headers={"HX-Request": "true"})
        assert r.status_code == 200 and r.headers["HX-Trigger"] == "pipelineRefresh"
        assert r.text.lstrip().startswith('<div id="decision"')
        assert "Hidden (not interested)." in r.text and 'data-testid="restore"' in r.text
        assert 'hx-swap-oob="true"' in r.text
        again = c.post(f"/opportunities/{ids['b']}/not-for-me")
        assert "Already hidden" in again.text
        assert ids["b"] not in _row_ids(_get(c, "/opportunities"))
        hidden = _get(c, "/opportunities?hidden=1")
        assert "hidden: not interested" in _row(hidden, ids["b"])
        back = c.post(f"/opportunities/{ids['b']}/restore")
        assert back.status_code == 200 and "Back in the active pipeline" in back.text
        assert ids["b"] in _row_ids(_get(c, "/opportunities"))

    labels = asyncio.run(_with_session(_labels))
    assert len(labels) == 1
    assert (labels[0].operator_verdict, labels[0].operator_score) == ("pass", 1.5)
    assert labels[0].opportunity_id == ids["b"] and labels[0].reason == "Not for me."


def test_add_after_not_for_me_and_unhide_records_the_new_judgment():
    """Not for me, Unhide, Add: the latest label must be the yes, not the stale pass. A second
    Add with a yes already latest writes nothing more."""
    ids = asyncio.run(_seed())
    with _client() as c:
        c.post(f"/opportunities/{ids['k']}/not-for-me")
        c.post(f"/opportunities/{ids['k']}/restore")
        c.post(f"/opportunities/{ids['k']}/add")
        c.post(f"/opportunities/{ids['k']}/add")
        c.post(f"/opportunities/{ids['k']}/not-for-me")  # and back to no: a new pass
    labels = sorted(asyncio.run(_with_session(_labels)), key=lambda lb: lb.id)
    assert [lb.operator_verdict for lb in labels] == ["pass", "apply", "pass"]


def test_siding_with_the_model_never_stores_apply_with_a_consider_range_score():
    """A your-call row whose model said apply at 3.8 (a consider-range score): Add records the
    model's verdict at the good fit training score, not apply at 3.8."""
    ids = asyncio.run(_seed())
    with _client() as c:
        c.post(f"/opportunities/{ids['b']}/add")
    (lb,) = asyncio.run(_with_session(_labels))
    assert (lb.operator_verdict, lb.operator_score) == ("apply", 4.0)
    assert lb.reason == "Added to my shortlist, siding with the model."


def test_add_is_one_transaction_and_says_where_the_role_sits(monkeypatch):
    """A label failure must not leave a pursuit behind a message saying it was not added; and
    the message names the board column, not a pursuit id and a stored status."""
    from kestrel.web.pages import opportunity_actions as oa

    ids = asyncio.run(_seed())

    async def boom(*a, **kw):
        raise RuntimeError("disk full")

    real = oa.record_override
    monkeypatch.setattr(oa, "record_override", boom)
    with _client() as c:
        r = c.post(f"/opportunities/{ids['d']}/add")
    # The failure is named by type only; its text stays in the log (D-SEC-1).
    assert "Could not add it to your shortlist: an unexpected problem (RuntimeError)" in r.text
    assert "disk full" not in r.text

    async def pursuits(s):
        return (await s.execute(select(Pursuit).where(Pursuit.opportunity_id == ids["d"]))).all()

    assert asyncio.run(_with_session(pursuits)) == []
    monkeypatch.setattr(oa, "record_override", real)
    with _client() as c:
        added = c.post(f"/opportunities/{ids['d']}/add").text
        again = c.post(f"/opportunities/{ids['a']}/shortlist").text
        detail = c.get(f"/opportunities/{ids['a']}").text
    assert "Added to your shortlist (To apply)." in added and "pursuit #" not in added
    assert "Already on your shortlist (To apply)." in again
    assert "On my shortlist" in detail and "pursuit #" not in detail
    assert f'opportunity_id={ids["a"]}&amp;from=/opportunities/{ids["a"]}' in detail


# ------------------------------------------------------------- remove from the list (D-UI-75)
async def _pursuit_rows(s, opp_id: int):
    return (await s.execute(select(Pursuit).where(Pursuit.opportunity_id == opp_id))).all()


def test_remove_undoes_an_accidental_add_and_its_label():
    """Add by mistake, then Remove: no pursuit, no label from the Add, and the panel offers Add
    again. Removing a role that is not on the list says so and never 500s."""
    ids = asyncio.run(_seed())
    with _client() as c:
        c.post(f"/opportunities/{ids['d']}/add")
        panel = c.get(f"/opportunities/{ids['d']}").text
        assert f'hx-post="/opportunities/{ids["d"]}/role/unshortlist"' in panel
        r = c.post(f"/opportunities/{ids['d']}/remove", headers={"HX-Request": "true"})
        assert r.status_code == 200 and r.headers["HX-Trigger"] == "pipelineRefresh"
        assert r.text.lstrip().startswith('<div id="decision"')
        assert "Removed from your shortlist." in r.text
        assert 'data-testid="add-to-list"' in r.text and 'data-testid="not-for-me"' in r.text
        assert 'hx-swap-oob="true"' in r.text
        assert ids["d"] not in _row_ids(_get(c, "/opportunities?shortlisted=1"))
        again = c.post(f"/opportunities/{ids['d']}/remove")
        assert again.status_code == 200 and "Not on your shortlist." in again.text
        assert c.post("/opportunities/999999/remove").status_code == 200
    assert asyncio.run(_with_session(lambda s: _pursuit_rows(s, ids["d"]))) == []
    assert asyncio.run(_with_session(_labels)) == []


def test_remove_keeps_the_judgments_that_came_before_the_add():
    """Not for me, Unhide, Add, Remove: the pass stays and only the Add's yes goes, so the
    learning loop reads the operator's word from before the mistake. An agree label from an Add
    goes too (the add on seed c agrees with the model's pursue)."""
    ids = asyncio.run(_seed())
    with _client() as c:
        c.post(f"/opportunities/{ids['k']}/not-for-me")
        c.post(f"/opportunities/{ids['k']}/restore")
        c.post(f"/opportunities/{ids['k']}/add")
        c.post(f"/opportunities/{ids['k']}/remove")
        c.post(f"/opportunities/{ids['c']}/add")
        r = c.post(f"/opportunities/{ids['c']}/remove")
        assert "Removed from your leads." in r.text
    labels = asyncio.run(_with_session(_labels))
    assert [(lb.opportunity_id, lb.operator_verdict) for lb in labels] == [(ids["k"], "pass")]


def test_remove_after_applying_deletes_the_history_and_warns_first():
    """A role added for the wrong application can be removed at any stage. Its status history,
    outcome and offers go with it; a notification about it stays, unlinked from the pursuit.
    The confirmation says what will be deleted once there is history behind the role."""
    from kestrel.db.models import Notification, Offer, Outcome, StatusEvent
    from kestrel.services.pursuits import set_pursuit_status

    ids = asyncio.run(_seed())
    with _client() as c:
        first = c.get(f"/opportunities/{ids['a']}").text
        assert f'/opportunities/{ids["a"]}/role/unshortlist' in first  # fresh: one click, Undo

    async def advance(s):
        stmt = select(Pursuit).where(Pursuit.opportunity_id == ids["a"])
        p = (await s.execute(stmt)).scalar_one()
        await set_pursuit_status(s, p.id, "applied")
        await set_pursuit_status(s, p.id, "rejected")
        s.add_all([
            Outcome(pursuit_id=p.id, opportunity_id=ids["a"], final_status="rejected"),
            Offer(pursuit_id=p.id, base=150000),
            Notification(kind="liveness", title="Closed", opportunity_id=ids["a"], pursuit_id=p.id),
        ])
        await s.commit()
        return p.id

    pid = asyncio.run(_with_session(advance))
    with _client() as c:
        later = c.get(f"/opportunities/{ids['a']}").text
        assert "Its status history, notes, outcome and offers are deleted too." in later
        r = c.post(f"/opportunities/{ids['a']}/remove")
        assert "Removed from your shortlist." in r.text

    async def left(s):
        events = (await s.execute(select(StatusEvent).where(StatusEvent.pursuit_id == pid))).all()
        outcomes = (await s.execute(select(Outcome).where(Outcome.pursuit_id == pid))).all()
        offers = (await s.execute(select(Offer).where(Offer.pursuit_id == pid))).all()
        notes = (await s.execute(select(Notification))).scalars().all()
        return events, outcomes, offers, notes, await _pursuit_rows(s, ids["a"])

    events, outcomes, offers, notes, pursuits = asyncio.run(_with_session(left))
    assert (events, outcomes, offers, pursuits) == ([], [], [], [])
    assert [(n.opportunity_id, n.pursuit_id) for n in notes] == [(ids["a"], None)]


def test_disagree_maps_the_fit_word_to_a_verdict_and_refreshes_the_row():
    ids = asyncio.run(_seed())
    with _client() as c:
        url_c = f"/api/opportunities/{ids['c']}/override"
        r = c.post(url_c, data={"fit": "maybe", "reason": "Too short."},
                   headers={"HX-Request": "true"})
        assert r.status_code == 200 and "You said maybe." in r.text
        assert r.headers["HX-Trigger"] == "pipelineRefresh"
        assert 'id="nav-count-calibrate" class="n" hx-swap-oob="true"' in r.text
        row = _row(_get(c, "/opportunities?track=consulting"), ids["c"])
        assert 'data-mine="maybe"' in row and "you said maybe" in row
        js = c.post(url_c, json={"fit": "strong fit", "reason": "Rethought it."}).json()
        assert js["override"]["operator_verdict"] == "pursue"  # the consulting spelling
        assert js["override"]["operator_score"] == 4.75 and js["override"]["fit_word"] == (
            "strong fit"
        )
        r = c.post(f"/api/opportunities/{ids['a']}/override",
                   data={"fit": "not a fit", "reason": "Wrong vertical."})
        assert r.json()["override"]["operator_verdict"] == "pass"
        # A word outside the four is tampering; a missing reason is refused as before.
        assert c.post(url_c, json={"fit": "keep", "reason": "x"}).status_code == 422
        assert c.post(url_c, json={"fit": "good fit"}).status_code == 400
        drawer = c.get(f"/opportunities/{ids['a']}").text
    assert "You said <b>not a fit</b>" in drawer
    labels = asyncio.run(_with_session(_labels))
    assert [(lb.operator_verdict, lb.operator_score) for lb in labels] == [
        ("consider", 3.0), ("pursue", 4.75), ("pass", 1.5)
    ]


def test_the_latest_evaluation_prefers_the_highest_tier_over_a_newer_tier1_row():
    """A Tier 1 forced keep written after the Tier 2 read must not stand in for it: the row
    lost its score and read "new" (the dashes bug)."""
    from kestrel.web.queries import get_detail

    ids = asyncio.run(_seed())

    async def rescreen(s):
        a = await s.get(Opportunity, ids["a"])
        s.add(_ev(a, tier=1, verdict="keep", verdict_model="keep", score=None,
                  weighted_score=None, forced_by="low_confidence_forced_keep"))
        await s.commit()

    asyncio.run(_with_session(rescreen))

    async def read(s):
        page = await list_opportunities(s, OppFilters(grouped=True))
        flat = await list_opportunities(s, OppFilters())
        detail = await get_detail(s, ids["a"])
        return page, flat, detail

    page, flat, detail = asyncio.run(_with_session(read))
    row = next(r for r in page.rows if r.opp.id == ids["a"])
    assert row.ev.tier == 2 and row.fit.word == "strong fit" and row.fit.score == 4.6
    assert next(r for r in flat.rows if r.opp.id == ids["a"]).ev.tier == 2
    assert detail["ev"].tier == 2
    with _client() as c:
        html = _row(_get(c, "/opportunities"), ids["a"])
        r = c.post(f"/api/opportunities/{ids['a']}/override", json={"mode": "agree"})
    assert "data-fit-score>4.6 <small>" in html
    assert r.json()["override"]["operator_verdict"] == "apply"


@pytest.mark.parametrize("reason", ["applied", "not_interested", "later", "other"])
def test_hide_records_the_reason_and_the_hidden_chip_shows_it(reason: str):
    from kestrel.db.enums import ARCHIVE_REASONS

    ids = asyncio.run(_seed())
    with _client() as c:
        r = c.post(f"/opportunities/{ids['k']}/archive", data={"reason": reason})
        assert r.status_code == 200
        assert "Hidden" in r.text and ARCHIVE_REASONS[reason].lower() in r.text
        assert "Archived" not in r.text
        assert ids["k"] not in _row_ids(_get(c, "/opportunities"))
        hidden = _get(c, "/opportunities?hidden=1")
        assert set(_row_ids(hidden)) == {ids["h"], ids["k"]}
        assert f"hidden: {ARCHIVE_REASONS[reason].lower()}" in _row(hidden, ids["k"])
        assert f'data-reason="{reason}"' in _row(hidden, ids["k"])

    async def _reason(s):
        return (await s.get(Opportunity, ids["k"])).archive_reason

    assert asyncio.run(_with_session(_reason)) == reason


def test_hide_without_a_reason_and_with_a_bad_reason():
    ids = asyncio.run(_seed())
    with _client() as c:
        r = c.post(f"/opportunities/{ids['k']}/archive")
        assert r.status_code == 200 and "Hidden." in r.text
        bad = c.post(f"/opportunities/{ids['d']}/archive", data={"reason": "bored"})
        assert bad.status_code == 200 and "Unknown reason" in bad.text
        assert ids["d"] in _row_ids(_get(c, "/opportunities"))  # nothing written

    async def _state(s):
        k = await s.get(Opportunity, ids["k"])
        d = await s.get(Opportunity, ids["d"])
        return k.archived_at is not None, k.archive_reason, d.archived_at

    assert asyncio.run(_with_session(_state)) == (True, None, None)


def test_unhide_restores_the_row_and_clears_the_reason():
    ids = asyncio.run(_seed())
    with _client() as c:
        r = c.post(f"/opportunities/{ids['h']}/unarchive")
        assert r.status_code == 200 and "Unhidden" in r.text
        assert ids["h"] in _row_ids(_get(c, "/opportunities"))
        assert _row_ids(_get(c, "/opportunities?hidden=1")) == []
        assert ">hidden" not in _row(_get(c, "/opportunities"), ids["h"])

    async def _state(s):
        h = await s.get(Opportunity, ids["h"])
        return h.archived_at, h.archive_reason

    assert asyncio.run(_with_session(_state)) == (None, None)


def test_hidden_rows_never_reach_the_default_list_or_the_api_default():
    ids = asyncio.run(_seed())
    with _client() as c:
        assert ids["h"] not in _row_ids(_get(c, "/opportunities"))
        assert ids["h"] not in _row_ids(_get(c, "/opportunities?stage=t2_done&q=VP"))
        api = c.get("/api/opportunities").json()
    assert ids["h"] not in {row["id"] for row in api["rows"]}


def test_api_archived_includes_hidden_rows_and_hidden_lists_only_them():
    """`archived=1` keeps its REST meaning (include hidden beside the active rows); the Hidden
    chip's `hidden=1` is the hidden-only view (D-UI-4, amended)."""
    ids = asyncio.run(_seed())
    with _client() as c:
        default = {row["id"] for row in c.get("/api/opportunities").json()["rows"]}
        included = {row["id"] for row in c.get("/api/opportunities?archived=1").json()["rows"]}
        only = {row["id"] for row in c.get("/api/opportunities?hidden=1").json()["rows"]}
        page = _get(c, "/opportunities?archived=1")
    assert ids["h"] not in default
    assert included == default | {ids["h"]}
    assert only == {ids["h"]}
    # The page honors the API parameter too (an older bookmark), and keeps it across chips.
    assert ids["h"] in _row_ids(page) and ids["a"] in _row_ids(page)
    assert 'name="archived" value="1"' in page


def test_nav_counts_agree_with_the_chips():
    """The Needs review badge counts only the LATEST evaluation of each visible opportunity: a
    superseded evaluation's flag and a hidden opportunity's flag are out, exactly as the chip.
    The calibrate count leaves hidden rows out too."""
    ids = asyncio.run(_seed())

    async def flag_more(s):
        a = await s.get(Opportunity, ids["a"])
        h = await s.get(Opportunity, ids["h"])
        # a: a needs-review evaluation, then a newer one that agrees: the flag is superseded.
        s.add(_ev(a, needs_review=True, score=3.0))
        await s.flush()
        s.add(_ev(a, needs_review=False))
        # h is hidden: its evaluation flagged for review must not count.
        s.add(_ev(h, needs_review=True, verdict="consider"))
        # z: an evaluated role with no description: Calibrate cannot ask about it (#32).
        z = _opp(SimpleNamespace(id=a.org_id, name=_EMPLOYER), SimpleNamespace(id=a.source_id),
                 "z", description_md=None)
        s.add(z)
        await s.flush()
        s.add(_ev(z, verdict="consider"))
        await s.commit()

    asyncio.run(_with_session(flag_more))
    with _client() as c:
        chip_rows = _row_ids(_get(c, "/opportunities?needs_review=1"))
        counts = c.get("/status/counts").text
        status = c.get("/api/status").json()["counts"]
    assert chip_rows == [ids["b"]]
    assert len(chip_rows) == 1
    # Finding 12: no nav badge shows Your call, so no out-of-band span (it had no target).
    assert "needs_review" not in status
    assert 'id="nav-count-needs_review"' not in counts
    # calibrate: the page's own pool (learning.active.rank_uncertain): unlabeled tier-2 roles
    # on the track that have a description and are not hidden (#32): a, b, k on the Employment
    # tab (h is hidden; z has no description); c is consulting. /api/status counts every track.
    assert status["calibrate"] == 4
    assert '<span id="nav-count-calibrate" class="n" hx-swap-oob="true">3</span>' in counts


@pytest.mark.skipif(not hasattr(__import__("time"), "tzset"), reason="TZ switching is POSIX only")
def test_new_today_starts_at_local_midnight_not_utc():
    """A Texas evening after 7pm CDT is already tomorrow in UTC; the chip must still count
    everything seen since this morning's local midnight (D-UI-9)."""
    import os
    import time

    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/Chicago"
    time.tzset()
    try:
        evening = datetime(2026, 10, 14, 0, 30, tzinfo=UTC)  # 19:30 CDT on Oct 13
        assert local_day_start_iso(evening) == "2026-10-13T05:00:00.000000+00:00"
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_operator_decisions_outrank_the_model_in_sql_and_in_row_fit_alike():
    """D-UI-51: a role still being scored (unscored or quarantined) is Being scored first; then a
    role on the shortlist is Worth a look whatever the model said, and the
    operator's latest label (apply/pursue, consider, pass) outranks the model's verdict. The SQL
    group (order and counts) and row_fit agree row for row; a closed pursuit no longer counts."""
    from kestrel.web import fit
    from kestrel.web.queries import row_fit

    ids = asyncio.run(_seed())

    async def decide(s):
        org = (await s.execute(select(Org))).scalars().first()
        src = (await s.execute(select(Source))).scalars().first()
        lo = _opp(org, src, "lo")  # the model says not a fit; the operator shortlists it
        up = _opp(org, src, "up")  # the model says maybe; the operator says apply
        dn = _opp(org, src, "dn")  # the model says strong fit; the operator says pass
        mb = _opp(org, src, "mb")  # the model says strong fit; latest label is consider
        cl = _opp(org, src, "cl")  # a closed pursuit on a maybe: the model's group again
        un = _opp(org, src, "un", stage="t1_pass")  # unscored, operator says apply
        # Passed by the operator, then scored again into quarantine: Being scored, never hidden
        # in Not a fit behind the old label (hard rule 4).
        rq = _opp(org, src, "rq", stage="quarantined")
        s.add_all([lo, up, dn, mb, cl, un, rq])
        await s.flush()
        s.add_all([
            _ev(lo, verdict="pass", verdict_model="pass", score=1.8),
            _ev(up, verdict="consider", verdict_model="consider", score=3.2),
            _ev(dn), _ev(mb),
            _ev(cl, verdict="consider", verdict_model="consider", score=3.1),
            _ev(rq, verdict="pass", verdict_model="pass", score=1.9),
        ])
        s.add(VerdictOverride(opportunity_id=rq.id, operator_verdict="pass", operator_score=1.5))
        s.add(Pursuit(opportunity_id=lo.id, track="employment", status="shortlisted"))
        s.add(Pursuit(opportunity_id=cl.id, track="employment", status="rejected",
                      closed_at=iso(_now())))
        s.add(VerdictOverride(opportunity_id=up.id, operator_verdict="apply", operator_score=4))
        s.add(VerdictOverride(opportunity_id=dn.id, operator_verdict="pass", operator_score=1.5))
        s.add(VerdictOverride(opportunity_id=mb.id, operator_verdict="pass", operator_score=1.5))
        await s.flush()
        s.add(VerdictOverride(opportunity_id=mb.id, operator_verdict="consider",
                              operator_score=3.0))
        s.add(VerdictOverride(opportunity_id=un.id, operator_verdict="apply", operator_score=4))
        await s.commit()
        return {"lo": lo.id, "up": up.id, "dn": dn.id, "mb": mb.id, "cl": cl.id, "un": un.id,
                "rq": rq.id}

    new = asyncio.run(_with_session(decide))

    pg = asyncio.run(_with_session(_whole_list))
    by_id = {row.opp.id: row for row in pg.rows}
    want = {"lo": fit.GROUP_WORTH, "up": fit.GROUP_WORTH, "dn": fit.GROUP_NOT_FIT,
            "mb": fit.GROUP_MAYBE, "cl": fit.GROUP_MAYBE,
            # Being scored and Scoring failed outrank every label (D-UI-56).
            "un": fit.GROUP_NEW, "rq": fit.GROUP_FAILED}
    for key, group in want.items():
        assert by_id[new[key]].fit.group == group, key
    # The model's word and tone stay on the row; only the group follows the operator.
    assert by_id[new["lo"]].fit.word == fit.NOT_FIT and by_id[new["dn"]].fit.word == fit.STRONG
    assert by_id[new["un"]].fit.word == fit.NEW and by_id[new["un"]].scoring == "not_queued"
    assert by_id[new["rq"]].fit.word == fit.NEW and by_id[new["rq"]].scoring == "failed"
    # SQL and row_fit agree: rows arrive in group order and the counts match the rows.
    order = list(fit.GROUP_LABELS)
    groups = [r.fit.group for r in pg.rows]
    assert groups == sorted(groups, key=[fit.GROUP_WORTH, fit.GROUP_MAYBE, fit.GROUP_NOT_FIT,
                                         fit.GROUP_NEW, fit.GROUP_FAILED].index)
    for g in order:
        assert pg.group_counts[g] == sum(1 for r in pg.rows if r.fit.group == g), g
    for r in pg.rows:
        assert r.fit == row_fit(
            r.opp, r.ev, pursued=r.pursuit_id is not None and r.opp.id != new["cl"],
            operator_verdict={new["up"]: "apply", new["dn"]: "pass", new["mb"]: "consider",
                              new["un"]: "apply"}.get(r.opp.id),
        )
    assert ids["a"] in by_id and by_id[ids["a"]].fit.group == fit.GROUP_WORTH


def test_unscored_rows_say_scoring_waiting_or_failed():
    """Hard rule 4: an unscored row says where it is in the queue, from the task rows."""
    from kestrel.db.models import Task

    ids = asyncio.run(_seed())

    async def lease(s):
        s.add(Task(type="t2", payload={"opportunity_id": ids["d"]}, status="leased"))
        await s.commit()

    asyncio.run(_with_session(lease))
    with _client() as c:
        html = _get(c, "/opportunities")
    d = _row(html, ids["d"])
    assert 'data-scoring="running">scoring…</span>' in d and 'class="pl-row is-new"' in d
    assert 'data-scoring="failed">failed</span>' in _row(html, ids["e"])
    assert "Being scored · 1" in html and "Scoring failed · 1" in html


def test_role_panel_has_the_ai_overview_disclaimer_research_and_score_again():
    """D-UI-53: "AI overview" with the disclaimer under the summary and under the "may not be a
    real opening" flag; Research the company on every role; Score again and the history."""
    ids = asyncio.run(_seed())
    disclaimer = ("AI overviews are the model's reading of the posting and can be wrong, "
                  "including about the company. Open the posting and research the company "
                  "before you act on it.")

    async def doubt(s):
        b = await s.get(Opportunity, ids["b"])
        s.add(_ev(b, needs_review=True, verdict="consider", verdict_model="apply", score=3.8,
                  legitimacy={"status": "suspect",
                              "signals": ["pipeline_building_not_real_opening"]}))
        await s.commit()

    asyncio.run(_with_session(doubt))
    with _client() as c:
        panel = c.get(f"/opportunities/{ids['b']}/panel").text
        b = c.get(f"/opportunities/{ids['b']}").text
        a = c.get(f"/opportunities/{ids['a']}").text
        d = c.get(f"/opportunities/{ids['d']}").text
        ev_tab = c.get(f"/opportunities/{ids['b']}/tab/evaluation").text
    assert disclaimer.replace("'", "&#39;") in b  # under the model-written read
    assert ">may not be a real opening<" in b
    assert disclaimer.replace("'", "&#39;") in ev_tab and ">may not be a real opening<" in ev_tab
    # The panel says it too: the summary is labeled, and the flag is in "Worth checking".
    assert "AI overviews are the model" in panel.replace("&#39;", "'")
    assert "This may not be a real opening." not in panel  # the check is in the row's icon
    assert "May be collecting resumes, not a real opening." in panel.replace(
        "&#39;", "'") or "collecting resumes" in panel
    for html in (a, b, d):  # on the list, not on it, and not scored yet
        assert 'data-testid="research"' in html
        assert "/rescore/choose?opp_ids=" in html
    assert 'data-testid="add-to-list"' in b and "my list" not in b
    assert "On my shortlist" in a
    assert "Being scored" in d


def test_scoring_failed_is_its_own_group_with_the_reason_and_score_again():
    """D-UI-56: a parked role with nothing queued sits under "Scoring failed", never under Being
    scored, with the newest quarantine reason in plain words and a Score again on the row and on
    the group. Queued to score again (a live task), it returns to Being scored. SQL and row_fit
    agree on both."""
    from kestrel.db.models import Quarantine, Task
    from kestrel.web import fit
    from kestrel.web.queries import row_fit

    ids = asyncio.run(_seed())

    async def more(s):
        org = (await s.execute(select(Org))).scalars().first()
        src = (await s.execute(select(Source))).scalars().first()
        er = _opp(org, src, "er", stage="error")
        rq = _opp(org, src, "rq", stage="quarantined")  # queued again: Being scored
        s.add_all([er, rq])
        await s.flush()
        s.add(Quarantine(opportunity_id=ids["e"], tier=2, error="invalid JSON: Expecting value"))
        s.add(Quarantine(opportunity_id=ids["e"], tier=2,
                         error='the reply repeated "x" 40 times (a generation loop)'))
        s.add(Task(type="t2", payload={"opportunity_id": rq.id, "rescore": True},
                   status="pending"))
        # A finished task does not count as live.
        s.add(Task(type="t2", payload={"opportunity_id": er.id}, status="dead"))
        await s.commit()
        return er.id, rq.id

    er_id, rq_id = asyncio.run(_with_session(more))

    async def page(s):
        return await list_opportunities(s, OppFilters(grouped=True, sort="first_seen"))

    pg = asyncio.run(_with_session(page))
    by_id = {r.opp.id: r for r in pg.rows}
    assert by_id[ids["e"]].fit.group == fit.GROUP_FAILED
    assert by_id[er_id].fit.group == fit.GROUP_FAILED
    assert by_id[rq_id].fit.group == fit.GROUP_NEW and by_id[rq_id].scoring == "queued"
    # The newest quarantine speaks; no quarantine at all reads the fallback.
    assert by_id[ids["e"]].failed_reason == "the model repeated itself"
    assert by_id[er_id].failed_reason == "scoring did not finish"
    assert pg.group_counts[fit.GROUP_FAILED] == 2 and pg.group_counts[fit.GROUP_NEW] == 2
    assert sorted(pg.failed_ids) == sorted([ids["e"], er_id])
    for r in pg.rows:  # SQL and Python agree row for row
        assert r.fit == row_fit(r.opp, r.ev, pursued=r.pursuit_id is not None,
                                live=r.opp.id == rq_id)
    assert all(r.fit.group != fit.GROUP_FAILED for r in pg.rows if r.opp.id == rq_id)

    with _client() as c:
        html = _get(c, "/opportunities")
    assert "Being scored · 2" in html and "Scoring failed · 2" in html
    # Not a fit comes before Being scored, then Scoring failed (D-UI-198).
    assert html.index('data-group="not_fit"') < html.index('data-group="new"') < html.index(
        'data-group="failed"'
    )
    assert 'id="scoring-failed"' in html
    explain = html.split('data-testid="failed-explain"')[1].split("</div>")[0]
    assert "The model could not finish these. Score them again, or try another model." in explain
    # The group links to Needs another try, where each role can be tried again (D-UI-96).
    assert 'href="/quarantine"' in explain and "Open Needs another try" in explain
    e = _row(html, ids["e"])
    assert "the model repeated itself" in e and 'data-testid="row-score-again"' in e
    assert f'href="/rescore/choose?opp_ids={ids["e"]}&page=1&next=/opportunities"' in e
    assert ">Score again</a>" in e
    rq_row = _row(html, rq_id)
    assert 'data-scoring="queued"' in rq_row and "row-score-again" not in rq_row
    assert "—" not in html


def test_a_stuck_orphan_says_stuck_with_score_again():
    """D-UI-56: a role the startup sweep already re-queued once that still has nothing queued
    reads "stuck" with its own Score again, never "waiting" forever."""
    from kestrel.pipeline.orphans import ORPHAN_KEY, record_key

    ids = asyncio.run(_seed())

    async def mark(s):
        s.add(Setting(key=ORPHAN_KEY,
                      value={record_key(ids["d"], "t0_pass"): "2026-10-16T00:00:00+00:00"}))
        await s.commit()

    asyncio.run(_with_session(mark))
    with _client() as c:
        html = _get(c, "/opportunities")
    d = _row(html, ids["d"])
    assert 'data-scoring="stuck">stuck</span>' in d and "scoring stopped" in d
    assert 'data-testid="row-score-again"' in d and "Being scored · 1" in html


def test_remove_unhides_a_role_imported_as_applied_and_speaks_consulting():
    """A role the applied-history import hid as applied, then removed: it comes back to triage,
    so it is not left hidden with no Add, and its re-posts are no longer hidden as applied. On
    the consulting track the button and the confirmation speak of leads."""
    ids = asyncio.run(_seed())

    async def imported(s):
        opp = await s.get(Opportunity, ids["d"])
        opp.archived_at = iso(_now())
        opp.archive_reason = "applied"
        opp.stage_reason = "Imported applied history"
        s.add(Pursuit(opportunity_id=opp.id, track="employment", status="applied",
                      last_activity_at=iso(_now())))
        await s.commit()

    asyncio.run(_with_session(imported))
    with _client() as c:
        r = c.post(f"/opportunities/{ids['d']}/remove")
        assert "Removed from your shortlist." in r.text and 'data-testid="add-to-list"' in r.text
        assert ids["d"] in _row_ids(_get(c, "/opportunities"))
        lead = c.post(f"/opportunities/{ids['c']}/add").text
        assert ">Remove from my leads<" in lead
        assert 'hx-confirm="Remove this from your leads?"' in lead

    async def row(s):
        opp = await s.get(Opportunity, ids["d"])
        return opp.archived_at, opp.archive_reason, opp.stage_reason

    assert asyncio.run(_with_session(row)) == (None, None, None)


# ------------------------------------------------------- WP-09: Applied group (#37)
async def _seed_applied(ids: dict[str, int]) -> dict[str, int]:
    """ap: an apply verdict with an open pursuit at `applied`. iv: the same at `interviewing`.
    rj: a rejected (closed) pursuit on a maybe. All three outrank nothing they should not."""
    async def go(s):
        org = (await s.execute(select(Org))).scalars().first()
        src = (await s.execute(select(Source))).scalars().first()
        ap, iv, rj = (_opp(org, src, k) for k in ("ap", "iv", "rj"))
        s.add_all([ap, iv, rj])
        await s.flush()
        s.add_all([_ev(ap), _ev(iv), _ev(rj, verdict="consider", verdict_model="consider",
                                         score=3.2)])
        s.add_all([
            Pursuit(opportunity_id=ap.id, track="employment", status="applied"),
            Pursuit(opportunity_id=iv.id, track="employment", status="interviewing"),
            Pursuit(opportunity_id=rj.id, track="employment", status="rejected",
                    closed_at=iso(_now())),
        ])
        await s.commit()
        return {"ap": ap.id, "iv": iv.id, "rj": rj.id}

    return await _with_session(go)


def test_applied_roles_leave_worth_a_look_for_their_own_group_with_a_status_pill():
    ids = asyncio.run(_seed())
    new = asyncio.run(_seed_applied(ids))
    with _client() as c:
        html = _get(c, "/opportunities")
    worth = html.split('<section class="pl-group" data-group="worth"')[1].split("</section>")[0]
    applied = html.split('<section class="pl-group" data-group="applied"')[1].split("</section>")[0]
    # The shortlisted role stays in Worth a look; the applied and interviewing ones moved.
    assert f'id="row-{ids["a"]}"' in worth
    assert f'id="row-{new["ap"]}"' not in worth and f'id="row-{new["iv"]}"' not in worth
    assert f'id="row-{new["ap"]}"' in applied and f'id="row-{new["iv"]}"' in applied
    assert "Applied · 2" in html and "Worth a look · 2" in html
    # Order (D-UI-198): Worth a look, Maybe, Applied, Not a fit, Being scored, Scoring failed.
    sec = '<section class="pl-group" data-group="'
    assert html.index(sec + 'worth') < html.index(sec + 'applied') < html.index(
        sec + 'new') < html.index(sec + 'failed')
    row = _row(html, new["ap"])
    assert 'data-group="applied"' in row
    assert 'data-testid="pursuit-status">applied</span>' in row
    assert 'data-testid="on-shortlist"' not in row
    assert 'data-testid="pursuit-status">interviewing</span>' in _row(html, new["iv"])
    # A shortlisted role still shows the shortlist pill, not a status.
    assert 'data-testid="on-shortlist">On my shortlist</span>' in _row(html, ids["a"])
    # A closed pursuit is off the shortlist: the model's own group (Maybe), no status pill.
    rj = _row(html, new["rj"])
    assert 'data-group="maybe"' in rj and "pursuit-status" not in rj


def test_sql_and_python_grouping_agree_for_applied_statuses():
    from kestrel.web import fit
    from kestrel.web.queries import row_fit

    ids = asyncio.run(_seed())
    new = asyncio.run(_seed_applied(ids))

    async def status_map(s):
        return {p.opportunity_id: p for p in (await s.execute(select(Pursuit))).scalars()}

    pursuits = asyncio.run(_with_session(status_map))
    pg = asyncio.run(_with_session(_whole_list))
    by_id = {r.opp.id: r for r in pg.rows}
    assert by_id[new["ap"]].fit.group == fit.GROUP_APPLIED
    assert by_id[new["iv"]].fit.group == fit.GROUP_APPLIED
    assert by_id[new["rj"]].fit.group == fit.GROUP_MAYBE and by_id[ids["a"]].fit.group == (
        fit.GROUP_WORTH)
    assert by_id[new["ap"]].pursuit_status == "applied"
    for r in pg.rows:
        p = pursuits.get(r.opp.id)
        assert r.fit == row_fit(
            r.opp, r.ev, pursued=p is not None and p.closed_at is None,
            pursuit_status=p.status if p is not None else None,
        ), r.opp.id
    for g in fit.GROUP_LABELS:
        assert pg.group_counts[g] == sum(1 for r in pg.rows if r.fit.group == g), g
    order = [fit.GROUP_WORTH, fit.GROUP_MAYBE, fit.GROUP_APPLIED, fit.GROUP_NOT_FIT,
             fit.GROUP_NEW, fit.GROUP_FAILED]
    groups = [r.fit.group for r in pg.rows]
    assert groups == sorted(groups, key=order.index)


def test_operator_group_for_every_applied_or_later_status():
    from kestrel.db.enums import APPLIED_OR_LATER
    from kestrel.web import fit

    for status in sorted(APPLIED_OR_LATER):
        assert fit.operator_group(None, pursued=True, pursuit_status=status) == (
            fit.GROUP_APPLIED), status
    assert fit.operator_group(None, pursued=True, pursuit_status="shortlisted") == fit.GROUP_WORTH
    assert fit.operator_group(None, pursued=True) == fit.GROUP_WORTH
    # A closed pursuit (pursued=False) falls through to the operator's label or the model.
    assert fit.operator_group("consider", pursued=False, pursuit_status="rejected") == (
        fit.GROUP_MAYBE)


def test_review_card_and_badges_exclude_applied_roles():
    from kestrel.analytics.today import worth_a_look

    ids = asyncio.run(_seed())
    asyncio.run(_seed_applied(ids))

    async def counts(s):
        return (await worth_a_look(s, "employment"))["n"]

    n = asyncio.run(_with_session(counts))
    with _client() as c:
        html = _get(c, "/opportunities")
        counts = c.get("/status/counts").text
    worth_header = int(re.search(r"Worth a look · (\d+)", html).group(1))
    assert n == worth_header == 2  # a and b; ap and iv are Applied now
    assert re.search(r'id="nav-count-shortlist"[^>]*>2<', counts)  # the badge agrees


# ------------------------------------------------------- WP-09: scoring states (#34)
def _lane_task(opp_id, **extra):
    from kestrel.db.models import Task
    from kestrel.pipeline import lane

    return Task(type="t1", priority=lane.LANE_PRIORITY,
                payload={"opportunity_id": opp_id, **lane.lane_extra(0.0), **extra})


def test_unscored_rows_say_why_background_queued_running_and_not_queued():
    from kestrel.db.models import Task

    ids = asyncio.run(_seed())

    async def more(s):
        org = (await s.execute(select(Org))).scalars().first()
        src = (await s.execute(select(Source))).scalars().first()
        bg = _opp(org, src, "bg", stage="t0_pass")
        qd = _opp(org, src, "qd", stage="t0_pass")
        s.add_all([bg, qd])
        await s.flush()
        s.add(_lane_task(bg.id))
        s.add(Task(type="t1", payload={"opportunity_id": qd.id}, status="pending"))
        s.add(Task(type="t2", payload={"opportunity_id": ids["d"]}, status="leased"))
        await s.commit()
        return bg.id, qd.id

    bg_id, qd_id = asyncio.run(_with_session(more))
    with _client() as c:
        html = _get(c, "/opportunities")
    assert 'data-scoring="background">background</span>' in _row(html, bg_id)
    assert 'data-scoring="queued">queued</span>' in _row(html, qd_id)
    assert 'data-scoring="running">scoring…</span>' in _row(html, ids["d"])
    # No task at all: it says so instead of "waiting".
    async def lone(s):
        org = (await s.execute(select(Org))).scalars().first()
        src = (await s.execute(select(Source))).scalars().first()
        o = _opp(org, src, "nq", stage="t0_pass")
        s.add(o)
        await s.commit()
        return o.id

    nq = asyncio.run(_with_session(lone))
    with _client() as c:
        html = _get(c, "/opportunities")
    assert 'data-scoring="not_queued">not queued</span>' in _row(html, nq)


def test_a_role_on_a_switched_off_track_reads_held():
    from kestrel.db.models import Task
    from kestrel.services import tracks

    ids = asyncio.run(_seed())

    async def hold(s):
        s.add(Task(type="t1", payload={"opportunity_id": ids["d"]}, status="pending"))
        await s.commit()

    asyncio.run(_with_session(hold))
    try:
        with _client() as c:
            orig = tracks.switched_off_tracks
            tracks.switched_off_tracks = lambda: ("employment",)  # type: ignore[assignment]
            try:
                html = _get(c, "/opportunities")
            finally:
                tracks.switched_off_tracks = orig  # type: ignore[assignment]
    finally:
        tracks.forget_cache()
    assert 'data-scoring="held">held</span>' in _row(html, ids["d"])


def test_being_scored_header_explains_the_background_roles(monkeypatch):
    from kestrel.pipeline import lane

    ids = asyncio.run(_seed())

    async def bg(s):
        org = (await s.execute(select(Org))).scalars().first()
        src = (await s.execute(select(Source))).scalars().first()
        for i in range(3):
            o = _opp(org, src, f"bg{i}", stage="t0_pass")
            s.add(o)
            await s.flush()
            s.add(_lane_task(o.id))
        await s.commit()

    asyncio.run(_with_session(bg))
    with _client() as c:
        html = _get(c, "/opportunities")
        note = html.split('data-testid="background-note"')[1].split("</div>")[0]
        assert "3 roles from earlier runs" in note and "when Kestrel is idle" in note
        assert "Score everything now" not in note
        monkeypatch.setattr(lane, "scores_on_hosted", lambda settings: True)
        hosted = c.get("/opportunities").text
    note = hosted.split('data-testid="background-note"')[1].split("</div>")[0]
    assert "local models only" in note and 'href="/sources"' in note
    assert "—" not in note
    assert ids["d"]  # the base fixture still renders


def test_list_polls_only_while_a_run_is_in_flight():
    from kestrel.db.models import Task

    ids = asyncio.run(_seed())
    with _client() as c:
        quiet = c.get("/opportunities").text
    assert 'hx-trigger="pipelineRefresh from:body"' in quiet

    async def go(s):
        s.add(Task(type="t2", payload={"opportunity_id": ids["d"]}, status="pending"))
        await s.commit()

    asyncio.run(_with_session(go))
    with _client() as c:
        busy = c.get("/opportunities").text
    assert 'hx-trigger="pipelineRefresh from:body, every 10s' in busy


def test_background_note_is_hidden_when_the_track_is_switched_off(monkeypatch):
    # A switched-off track's rows say "held"; a note saying they score when idle would mislead.
    from kestrel.services import tracks

    asyncio.run(_seed())

    async def bg(s):
        org = (await s.execute(select(Org))).scalars().first()
        src = (await s.execute(select(Source))).scalars().first()
        o = _opp(org, src, "bg-held", stage="t0_pass")
        s.add(o)
        await s.flush()
        s.add(_lane_task(o.id))
        await s.commit()

    asyncio.run(_with_session(bg))
    monkeypatch.setattr(tracks, "switched_off_tracks", lambda *a, **k: ["employment"])
    with _client() as c:
        html = _get(c, "/opportunities?track=employment")
    assert 'data-testid="background-note"' not in html


def test_the_list_refreshes_while_any_task_is_live_and_pauses_on_open_not_a_fit():
    # Embed, Tier 3 and Score again work also changes rows, so polling follows queue.in_flight,
    # not only a scrape or t1/t2 run; and it never collapses a Not a fit list the user opened.
    from kestrel.db.models import Task

    def trigger(html: str) -> str:
        return html.split('id="pipeline-list"')[1].split('hx-trigger="')[1].split('"')[0]

    asyncio.run(_seed())
    with _client() as c:
        idle = trigger(_get(c, "/opportunities"))
    assert "every 10s" not in idle

    async def live(s):
        s.add(Task(type="embed", payload={}))
        await s.commit()

    asyncio.run(_with_session(live))
    with _client() as c:
        busy = trigger(_get(c, "/opportunities"))
    assert "every 10s" in busy and "not-fit-rows" in busy


def test_pipeline_group_order_puts_not_a_fit_before_being_scored():
    """D-UI-198: one role per group renders Worth a look, Maybe, Applied, Not a fit, Being
    scored, Scoring failed, in that order."""
    ids = asyncio.run(_seed())
    asyncio.run(_seed_applied(ids))
    with _client() as c:
        html = _get(c, "/opportunities")
    marks = [
        '<section class="pl-group" data-group="worth"',
        '<section class="pl-group" data-group="maybe"',
        '<section class="pl-group" data-group="applied"',
        '<section class="pl-nf" data-group="not_fit"',
        '<section class="pl-group" data-group="new"',
        '<section class="pl-group" data-group="failed"',
    ]
    assert all(m in html for m in marks)
    pos = [html.index(m) for m in marks]
    assert pos == sorted(pos)
    from kestrel.web import fit
    from kestrel.web.queries import GROUP_ORDER

    assert GROUP_ORDER == (fit.GROUP_WORTH, fit.GROUP_MAYBE, fit.GROUP_APPLIED,
                           fit.GROUP_NOT_FIT, fit.GROUP_NEW, fit.GROUP_FAILED)
