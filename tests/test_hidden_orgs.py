"""Hidden companies (D-UI-123 amended): Hide this company, the Settings list, Unhide. A hidden
company's roles stay out of Pipeline and Today; nothing is deleted."""

from __future__ import annotations

from sqlalchemy import func, select

from kestrel.db.models import Opportunity, Org
from kestrel.services import hidden_orgs
from kestrel.web.queries import OppFilters, list_opportunities


async def _seed(s):
    a = Org(name="Alpha Works", normalized_name="alpha works")
    b = Org(name="Beta Mills", normalized_name="beta mills")
    s.add_all([a, b])
    await s.flush()
    opps = [
        Opportunity(track="employment", opp_type="job_posting", org_id=a.id, title="A one",
                    url="https://x/1", stage="t2_done"),
        Opportunity(track="employment", opp_type="job_posting", org_id=a.id, title="A two",
                    url="https://x/2", stage="t2_done"),
        Opportunity(track="employment", opp_type="job_posting", org_id=b.id, title="B one",
                    url="https://x/3", stage="t2_done"),
        Opportunity(track="employment", opp_type="job_posting", org_id=None, title="No org",
                    url="https://x/4", stage="t2_done"),
    ]
    s.add_all(opps)
    await s.commit()
    return a.id, b.id


async def _titles(s, **kw) -> set[str]:
    page = await list_opportunities(s, OppFilters(page_size=100, **kw))
    return {r.opp.title for r in page.rows}


async def test_hiding_a_company_removes_its_roles_from_pipeline_queries(sm):
    async with sm() as s:
        a, _ = await _seed(s)
        assert await _titles(s) == {"A one", "A two", "B one", "No org"}
        assert await hidden_orgs.hide_org(s, a)
        await s.commit()
        assert await _titles(s) == {"B one", "No org"}  # a role with no company is never lost
        assert await _titles(s, grouped=True) == {"B one", "No org"}
        assert await _titles(s, include_archived=True) == {"B one", "No org"}


async def test_nothing_is_deleted_and_the_roles_can_still_be_asked_for(sm):
    async with sm() as s:
        a, _ = await _seed(s)
        await hidden_orgs.hide_org(s, a)
        await s.commit()
        assert (await s.execute(select(func.count()).select_from(Opportunity))).scalar() == 4
        assert await _titles(s, include_hidden_orgs=True) == {
            "A one", "A two", "B one", "No org"
        }


async def test_unhide_puts_the_roles_back(sm):
    async with sm() as s:
        a, _ = await _seed(s)
        await hidden_orgs.hide_org(s, a)
        await s.commit()
        assert await hidden_orgs.is_hidden(s, a)
        assert await hidden_orgs.unhide_org(s, a)
        await s.commit()
        assert not await hidden_orgs.is_hidden(s, a)
        assert await _titles(s) == {"A one", "A two", "B one", "No org"}


async def test_the_settings_list_names_hidden_companies_newest_first_with_role_counts(sm):
    async with sm() as s:
        a, b = await _seed(s)
        await hidden_orgs.hide_org(s, a)
        await s.flush()
        first = await s.get(Org, a)
        first.hidden_at = "2026-01-01T00:00:00+00:00"
        await hidden_orgs.hide_org(s, b)
        await s.commit()
        listed = await hidden_orgs.list_hidden(s)
        assert [(h.name, h.role_count) for h in listed] == [("Beta Mills", 1), ("Alpha Works", 2)]
        assert listed[1].hidden_at == "2026-01-01T00:00:00+00:00"
        await hidden_orgs.unhide_org(s, b)
        await s.commit()
        assert [h.name for h in await hidden_orgs.list_hidden(s)] == ["Alpha Works"]


async def test_hiding_twice_keeps_the_first_time_and_unknown_ids_are_refused(sm):
    async with sm() as s:
        a, _ = await _seed(s)
        await hidden_orgs.hide_org(s, a)
        stamp = (await s.get(Org, a)).hidden_at
        await hidden_orgs.hide_org(s, a)
        assert (await s.get(Org, a)).hidden_at == stamp
        assert not await hidden_orgs.hide_org(s, 9999)
        assert not await hidden_orgs.unhide_org(s, 9999)
        assert not await hidden_orgs.is_hidden(s, None)


async def test_a_hidden_company_stays_out_of_todays_worth_a_look(sm):
    from kestrel.analytics.today import worth_a_look
    from kestrel.db.models import Evaluation

    async with sm() as s:
        a, b = await _seed(s)
        for opp in (await s.execute(select(Opportunity))).scalars():
            s.add(Evaluation(opportunity_id=opp.id, tier=2, track="employment", model="m",
                             prompt_version="v", verdict="apply", score=4.5, needs_review=False,
                             dimensions={}, blocks={}, blockers=[], legitimacy={}))
        await s.commit()
        before = await worth_a_look(s)
        await hidden_orgs.hide_org(s, a)
        await s.commit()
        after = await worth_a_look(s)
    assert before["n"] == 4 and after["n"] == 2


# ------------------------------------------------- every other Today count (hidden never counts)
async def _today_rows(s):
    """Per company: a role with an open pursuit (applied, quiet, follow-up due), a shortlisted
    one, and three bare roles: scored, rejected by a rule, and parked in Scoring failed."""
    from datetime import date, timedelta

    from kestrel.db.models import Evaluation, Pursuit, Quarantine

    a = Org(name="Alpha Works", normalized_name="alpha works")
    b = Org(name="Beta Mills", normalized_name="beta mills")
    s.add_all([a, b])
    await s.flush()
    old = (date.today() - timedelta(days=60)).isoformat() + "T00:00:00+00:00"
    due = (date.today() - timedelta(days=1)).isoformat()
    future = "2999-01-01T00:00:00+00:00"

    def role(org, tag, **kw):
        return Opportunity(track="employment", opp_type="job_posting", org_id=org.id,
                           title=f"{org.name} {tag}", url=f"https://x/{tag}{org.id}",
                           first_seen_at=future, **kw)

    for org in (a, b):
        applied = role(org, "applied", stage="t2_done")
        short = role(org, "short", stage="t2_done")
        scored = role(org, "scored", stage="t2_done")
        rejected = role(org, "rejected", stage="t0_reject", stage_reason="blocker:geo: x")
        parked = role(org, "parked", stage="quarantined")
        s.add_all([applied, short, scored, rejected, parked])
        await s.flush()
        s.add(Pursuit(opportunity_id=applied.id, track="employment", status="applied",
                      last_activity_at=old, applied_at=old, next_action_at=due))
        s.add(Pursuit(opportunity_id=short.id, track="employment", status="shortlisted"))
        s.add(Evaluation(opportunity_id=scored.id, tier=2, track="employment", model="m",
                         prompt_version="v", verdict="apply", score=4.0, needs_review=False,
                         dimensions={}, blocks={}, blockers=[], legitimacy={},
                         created_at=future))
        s.add(Quarantine(opportunity_id=parked.id, tier=2, model="m", prompt="p",
                         raw_response="r", error="bad", attempts=1))
    await s.commit()
    return a.id, b.id


async def _snapshot(s) -> dict:
    from kestrel.analytics import today as t
    from kestrel.web.queries import failure_summary, scoring_failed_count

    return {
        "act": await t.act_summary(s),
        "due": (await t.actions_due(s))["n"],
        "ghost": await t.ghosting_count(s, None),
        "night": await t.overnight(s),
        "fail": (await failure_summary(s)).total,
        "parked": await scoring_failed_count(s),
    }


async def test_hidden_companies_leave_the_overnight_and_failure_counts(sm):
    async with sm() as s:
        a, _ = await _today_rows(s)
        before = await _snapshot(s)
        await hidden_orgs.hide_org(s, a)
        await s.commit()
        after = await _snapshot(s)
    assert before["night"]["tier0"]["blocked"] == 2 and after["night"]["tier0"]["blocked"] == 1
    assert before["night"]["tier2"]["evaluated"] == 2 and after["night"]["tier2"]["evaluated"] == 1
    assert after["night"]["quarantine_open"] == before["night"]["quarantine_open"] - 1
    assert after["fail"] == before["fail"] - 1 and after["parked"] == before["parked"] - 1


async def test_a_hidden_company_keeps_roles_with_an_active_pursuit(sm):
    """Regression: hiding a company used to drop an application in progress from the Act counts,
    the follow-ups, the ghosting count and Pipeline."""
    async with sm() as s:
        a, _ = await _today_rows(s)
        before = await _snapshot(s)
        titles_before = await _titles(s)
        await hidden_orgs.hide_org(s, a)
        await s.commit()
        after = await _snapshot(s)
        titles_after = await _titles(s)
    assert after["act"] == before["act"] and after["due"] == before["due"] == 2
    assert after["ghost"] == before["ghost"] == 2
    assert {t for t in titles_before - titles_after} == {
        "Alpha Works scored", "Alpha Works rejected", "Alpha Works parked"
    }
    assert {"Alpha Works applied", "Alpha Works short"} <= titles_after


async def test_a_role_whose_pursuit_has_ended_is_hidden_again(sm):
    from sqlalchemy import update

    from kestrel.db.models import Pursuit

    async with sm() as s:
        a, _ = await _today_rows(s)
        await hidden_orgs.hide_org(s, a)
        await s.execute(update(Pursuit).values(status="rejected"))
        await s.commit()
        assert not [t for t in await _titles(s) if t.startswith("Alpha")]


async def test_the_scoring_progress_panel_does_not_count_a_hidden_company(sm):
    from kestrel.analytics.progress import scoring_progress
    from kestrel.config import get_settings
    from kestrel.db.models import Task

    async with sm() as s:
        a, b = await _seed(s)
        for o in (await s.execute(select(Opportunity).where(Opportunity.org_id.in_([a, b])))
                  ).scalars():
            s.add(Task(type="t2", payload={"opportunity_id": o.id}, status="pending"))
        await s.commit()
        before = await scoring_progress(s, get_settings())
        await hidden_orgs.hide_org(s, a)
        await s.commit()
        after = await scoring_progress(s, get_settings())
    assert before["waiting"] == 3 and after["waiting"] == 1
