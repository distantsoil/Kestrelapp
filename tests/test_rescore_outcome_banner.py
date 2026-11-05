"""After a bulk Score again, Pipeline names what happened to the roles (issue #143): a short
banner with a count for each kind of outcome, the roles stay ticked, and each queued role shows
its state on the first render. Nothing is sent anywhere. Cloud-safe: a migrated SQLite file and
the test client. Invented text only."""

from __future__ import annotations

import asyncio
import html as htmllib
import re
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import select

from kestrel.db.models import Opportunity, Org, Source, Task
from kestrel.web.fit import EMPTY_POSTING_STAGE_REASON
from tests import test_pipeline_view as T
from tests.test_not_scored_reasons import _db


async def _roles(s) -> dict[str, int]:
    org = (await s.execute(select(Org))).scalars().first()
    src = (await s.execute(select(Source))).scalars().first()
    made = {
        "fresh": T._opp(org, src, "f1", stage="t0_pass", title="Head of Platform"),
        "waiting": T._opp(org, src, "f2", stage="t1_pass", title="Director of Systems"),
        "rules": T._opp(org, src, "f3", stage="t0_reject", title="Platform Intern",
                        stage_reason="blocker:seniority_floor: Below seniority floor"),
        "thin": T._opp(org, src, "f4", stage="t1_pass", title="Lead of Tooling",
                       description_md="", stage_reason=EMPTY_POSTING_STAGE_REASON),
    }
    s.add_all(made.values())
    await s.flush()
    # An earlier Score again already waits for "waiting".
    s.add(Task(type="t2", priority=1000, payload={
        "opportunity_id": made["waiting"].id, "rescore": True, "requested": True}))
    await s.commit()
    return {k: o.id for k, o in made.items()}


def _banner(page: str) -> str:
    m = re.search(r'class="pl-notice__t">(.*?)</span>', page, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)) if m else ""


def _press(c, ids: list[int], **form):
    return c.post("/rescore", data={"opp_ids": ",".join(map(str, ids)), "model_choice": "same",
                                    "next": "/opportunities", **form}, follow_redirects=False)


def test_the_banner_names_each_outcome_and_the_selected_roles_show_their_state():
    asyncio.run(T._seed())
    ids = _db(_roles)
    pick = [ids["fresh"], ids["waiting"], ids["rules"]]
    with T._client() as c:
        r = _press(c, pick)
        assert r.status_code == 303
        q = parse_qs(urlsplit(r.headers["location"]).query)
        assert q["outcome"] == ["queued"] and q["n"] == ["1"] and q["a"] == ["1"]
        assert q["rules"] == ["1"] and q["sel"] == [",".join(map(str, pick))]
        page = htmllib.unescape(c.get(r.headers["location"]).text)
    assert "1 queued. 1 already waiting. 1 set aside by your rules." in page
    assert 'data-testid="pl-notice"' in page and 'href="/profile"' in page
    assert f'data-picked="{",".join(map(str, pick))}"' in page
    # First render: the queued role is already Waiting, with no poll needed.
    row = T._row(page, ids["fresh"])
    assert 'data-scoring="queued"' in row and ">Waiting" in row
    # The rules-refused role is untouched: still set aside, never rejected by this path.

    async def stage(s):
        return (await s.get(Opportunity, ids["rules"])).stage

    assert _db(stage) == "t0_reject"
    # The list keeps polling while a drawn row has a live task.
    assert 'every 10s' in page


def test_a_role_with_a_running_task_is_named_already_running():
    asyncio.run(T._seed())
    ids = _db(_roles)

    async def lease(s):
        t = (await s.execute(select(Task).where(Task.type == "t2"))).scalars().first()
        t.status, t.worker_id = "leased", "w1"
        await s.commit()

    _db(lease)
    with T._client() as c:
        r = _press(c, [ids["waiting"]])
        page = htmllib.unescape(c.get(r.headers["location"]).text)
    assert _banner(page) == "1 already running."


def test_only_a_rules_refusal_says_so_and_queues_nothing():
    asyncio.run(T._seed())
    ids = _db(_roles)
    with T._client() as c:
        r = _press(c, [ids["rules"]])
        page = htmllib.unescape(c.get(r.headers["location"]).text)
    assert _banner(page) == "1 set aside by your rules. Review your rules"


def test_the_outcome_is_one_time_and_the_url_carries_only_numbers_and_ids():
    asyncio.run(T._seed())
    ids = _db(_roles)
    with T._client() as c:
        r = _press(c, [ids["fresh"]])
        loc = r.headers["location"]
        plain = c.get("/opportunities").text
    q = parse_qs(urlsplit(loc).query)
    assert set(q) <= {"outcome", "n", "a", "run", "rules", "u", "m", "sel"}
    assert all(v[0].replace(",", "").isdigit() or k == "outcome" for k, v in q.items())
    assert 'data-testid="pl-notice"' not in plain  # no parameters, no banner
