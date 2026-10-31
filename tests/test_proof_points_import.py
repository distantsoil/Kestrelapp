"""Issues #67, #68: proof points from an accepted resume import land in profile.yml (the file
scoring reads), the review shows them before Accept, and the profile's next step is one plain
line. Score again queues the roles scored before the points, once. No network."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, get_sessionmaker, reset_engine
from kestrel.db.models import DocMaster, Evaluation, Opportunity, Task
from kestrel.docs.imports import accept_import
from kestrel.llm.schemas import P7ProofPoint, P7ResumeImport
from kestrel.pipeline.context import profile_full
from kestrel.pipeline.profile import load_profile
from kestrel.services import profile_edit as pe
from kestrel.services import profile_readiness as pr
from kestrel.web.deps import templates

SOURCE = "Cut spend by a third."
NOTICE = 'data-testid="thin-profile-notice"'


def _value(*points: P7ProofPoint) -> NS:
    return NS(
        roles=[NS(company_label="Acme", title="VP", start_date="2020", end_date="2024",
                  context_line="ctx", slots=[NS(key="r1b1", optional=False,
                  default_variant_key="v1",
                  variants=[NS(key="v1", text=SOURCE, tags=[], derived=False)])])],
        summary_variants=[], skill_groups=[], proof_points=list(points),
    )


COST = P7ProofPoint(key="r1_cost", claim="Cut spend by a third", evidence="FY23 close",
                    metric="33%")
TEAM = P7ProofPoint(key="r1_cost", claim="Grew the team to 40", evidence="HR roster",
                    metric=None)


@pytest.fixture
def fresh():
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()
    pe.profile_path().unlink(missing_ok=True)
    pe.profile_path().write_text(
        yaml.safe_dump({"employment": {"first_name": "Sam", "proof_points": [],
                        "employment_terms": {"target_titles": ["VP of IT"]}}}), "utf-8")
    yield
    asyncio.run(reset_engine())


def _accept(value) -> None:
    async def go():
        async with get_sessionmaker()() as s:
            await accept_import(s, value, track="employment", doc_kind="resume",
                                name="Imported resume", source_markdown=SOURCE)
    asyncio.run(go())


# ------------------------------------------------------------------------------ #68 the write
def test_accepted_proof_points_reach_profile_yml_and_the_scoring_prompt(fresh):
    _accept(_value(COST))
    prof = load_profile("employment")
    assert [p.claim for p in prof.proof_points] == ["Cut spend by a third"]
    assert "Cut spend by a third | evidence: FY23 close | metric: 33%" in profile_full(prof)


def test_reimport_dedupes_by_claim_and_keeps_keys_unique(fresh):
    _accept(_value(COST))
    _accept(_value(COST, TEAM))  # same claim skipped; same key, new claim, gets a fresh key
    prof = load_profile("employment")
    assert [p.claim for p in prof.proof_points] == ["Cut spend by a third", "Grew the team to 40"]
    assert [p.key for p in prof.proof_points] == ["r1_cost", "r1_cost_2"]


def test_accept_records_when_proof_points_changed_only_if_some_were_added(fresh):
    async def stamp():
        async with get_sessionmaker()() as s:
            return await pr.changed_at(s, "employment")

    _accept(_value())
    assert asyncio.run(stamp()) is None
    _accept(_value(COST))
    first = asyncio.run(stamp())
    assert first
    _accept(_value(COST))  # nothing new: the stamp does not move
    assert asyncio.run(stamp()) == first


# ----------------------------------------------------------------------- #68 the review page
def test_both_review_templates_list_the_proposed_proof_points(fresh):
    proposal = NS(value=P7ResumeImport(roles=[], summary_variants=[], skill_groups=[],
                                       proof_points=[COST]),
                  flags=NS(non_verbatim=[], too_many_derived=[], unverified=[], unplaced=[],
                           clean=True), error=None)
    base = dict(proposal=proposal, markdown=SOURCE, track="employment", doc_kind="resume",
                filename="", warnings=[], source_format="", extracted_shown=True,
                current_offer=None, error=None, retryable=False)
    env = templates().env
    docs = env.get_template("partials/import_proposal.html").render(**base, ob=False)
    wiz = env.get_template("partials/onboarding_proposal.html").render(**base, ob=True)
    for html in (docs, wiz):
        assert 'data-testid="import-proof-points"' in html
        assert "Cut spend by a third" in html and "FY23 close" in html
        assert 'name="proof_keep" value="r1_cost" checked' in html


def test_unticked_proof_points_are_not_added(fresh):
    value = P7ResumeImport.model_validate({
        "roles": [{"company_label": "Acme", "title": "VP", "start_date": "2020",
                   "end_date": "2024", "context_line": "ctx",
                   "slots": [{"key": "r1b1", "optional": False, "default_variant_key": "v1",
                              "variants": [{"key": "v1", "text": SOURCE, "tags": [],
                                            "derived": False}]}]}],
        "summary_variants": [], "skill_groups": [],
        "proof_points": [COST.model_dump(), {**TEAM.model_dump(), "key": "r1_team"}],
    })
    from kestrel.web.app import build_app

    with TestClient(build_app(worker=False)) as c:
        r = c.post("/api/documents/import/accept", data={
            "proposal_json": value.model_dump_json(), "text": SOURCE, "track": "employment",
            "doc_kind": "resume", "proof_shown": "1", "proof_keep": "r1_team"})
        assert r.status_code == 200, r.text[:300]
    assert [p.claim for p in load_profile("employment").proof_points] == ["Grew the team to 40"]


# ------------------------------------------------------------------------------ readiness
def _p(**kw):
    base = dict(proof_points=[], narrative="",
                employment_terms=NS(target_titles=["VP of IT"]))
    return NS(**{**base, **kw})


PP = [NS(claim="x", evidence="y", metric=None)]


def test_next_step_import_resume_when_thin_and_no_resume():
    r = pr.assess(_p(), has_resume=False, scored=[], changed_at=None)
    assert r.next_step == pr.IMPORT_RESUME and r.link_href == "/profile?tab=documents"
    assert not r.has_resume and not r.has_proof_points


def test_next_step_review_import_when_resume_is_in_but_no_points():
    assert pr.assess(_p(), has_resume=True, scored=[], changed_at=None).next_step == \
        pr.REVIEW_IMPORT


def test_next_step_add_titles_when_points_exist_but_no_titles():
    r = pr.assess(_p(proof_points=PP, employment_terms=NS(target_titles=[])),
                  has_resume=True, scored=[], changed_at=None)
    assert r.next_step == pr.ADD_TITLES and not r.has_target_titles


def test_next_step_score_again_counts_roles_scored_before_the_stamp():
    scored = [(1, "2026-01-01T00:00:00Z"), (2, "2026-03-01T00:00:00Z")]
    r = pr.assess(_p(proof_points=PP), has_resume=True, scored=scored,
                  changed_at="2026-02-01T00:00:00Z")
    assert r.next_step == pr.SCORE_AGAIN and r.roles_scored_before_proof_points == 1
    assert r.role_ids == [1] and "1 role was scored before your proof points" in r.line


def test_without_a_stamp_only_roles_scored_while_thin_count():
    scored = [(1, "2026-01-01T00:00:00Z")]
    assert pr.assess(_p(proof_points=PP), has_resume=True, scored=scored,
                     changed_at=None).next_step == pr.READY
    thin_but_titled = pr.assess(_p(narrative="I run IT."), has_resume=True, scored=scored,
                                changed_at=None)
    assert thin_but_titled.roles_scored_before_proof_points == 1  # no proof points: all stale


def test_next_step_ready():
    r = pr.assess(_p(proof_points=PP), has_resume=True, scored=[], changed_at=None)
    assert r.next_step == pr.READY and not r.show_notice and r.line == ""


# --------------------------------------------------------- notices and Score again (web)
def _seed_scored_roles(n: int, *, scored_at: str) -> list[int]:
    async def go():
        ids = []
        async with get_sessionmaker()() as s:
            for i in range(n):
                o = Opportunity(track="employment", opp_type="job_posting", title=f"VP {i}",
                                stage="t2_done", seen_count=1,
                                description_md="Own IT leadership.")
                s.add(o)
                await s.flush()
                s.add(Evaluation(opportunity_id=o.id, tier=2, track="employment",
                                 created_at=scored_at))
                ids.append(o.id)
            await s.commit()
        return ids
    return asyncio.run(go())


def _task_count() -> int:
    async def go():
        async with get_sessionmaker()() as s:
            return (await s.execute(select(func.count()).select_from(Task))).scalar_one()
    return asyncio.run(go())


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def test_notices_name_the_single_next_step(fresh):
    with _client() as c:
        for url in ("/", "/profile"):
            html = c.get(url).text
            assert NOTICE in html and 'data-next-step="import_resume"' in html, url
            assert "Add your resume so Kestrel can score roles" in html, url
    # resume present, still no points
    async def add_master():
        async with get_sessionmaker()() as s:
            s.add(DocMaster(track="employment", name="r", doc_kind="resume",
                            summary_variants=[], skill_groups=[]))
            await s.commit()
    asyncio.run(add_master())
    with _client() as c:
        assert 'data-next-step="review_import"' in c.get("/profile").text


def test_score_again_notice_and_post_queue_once(fresh):
    ids = _seed_scored_roles(2, scored_at="2020-01-01T00:00:00Z")
    _accept(_value(COST))  # stamps proof_points_changed_at after the old scores
    with _client() as c:
        html = c.get("/").text
        assert 'data-next-step="score_again"' in html
        assert "2 roles were scored before your proof points." in html
        assert 'action="/api/profile/score-again"' in html
        before = _task_count()
        r = c.post("/api/profile/score-again", data={"track": "employment", "back": "/"},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/?rescoring=2"
        queued = _task_count() - before
        assert queued == 2
        c.post("/api/profile/score-again", data={"track": "employment", "back": "/"},
               follow_redirects=False)
        assert _task_count() - before == queued  # idempotent: no double queue
        assert "2 roles are waiting to be scored again." in c.get("/?rescoring=2").text
    assert ids


def test_score_again_refuses_an_offsite_return_path(fresh):
    with _client() as c:
        r = c.post("/api/profile/score-again",
                   data={"track": "employment", "back": "//evil.example"},
                   follow_redirects=False)
        assert r.headers["location"] == "/"


# ------------------------------------------------------------ review round: selection and safety
def _profile_with_points(track="employment", **extra):
    block = {"first_name": "Sam", "proof_points": [{"key": "p1", "claim": "Cut churn 20%",
             "evidence": "Q3", "metric": None}],
             "employment_terms": {"target_titles": ["VP of IT"]},
             "consulting_terms": {"service_lines": ["IT advisory"]}}
    pe.profile_path().write_text(yaml.safe_dump({track: {**block, **extra}}), "utf-8")


def _readiness(track="employment"):
    async def go():
        async with get_sessionmaker()() as s:
            return await pr.load_readiness(s, track)
    return asyncio.run(go())


def test_score_again_counts_only_roles_a_new_score_can_reach(fresh):
    from kestrel.db.models import Org

    _profile_with_points()
    old = "2020-01-01T00:00:00Z"

    async def seed():
        async with get_sessionmaker()() as s:
            hidden = Org(name="Hidden Co", normalized_name="hidden co",
                         hidden_at="2021-01-01T00:00:00Z")
            s.add(hidden)
            await s.flush()
            specs = {
                "ok": dict(stage="t2_done"), "ok3": dict(stage="t3_done"),
                "t0": dict(stage="t0_reject"), "arch": dict(stage="t2_done", archived_at=old),
                "quar": dict(stage="quarantined"),
                "stub": dict(stage="t2_done", stage_reason="t2 posting has no description"),
                "org": dict(stage="t2_done", org_id=hidden.id),
                "queued": dict(stage="t2_done"),
            }
            ids = {}
            for name, kw in specs.items():
                o = Opportunity(track="employment", opp_type="job_posting", title=name,
                                seen_count=1, **kw)
                s.add(o)
                await s.flush()
                s.add(Evaluation(opportunity_id=o.id, tier=2, track="employment", created_at=old))
                ids[name] = o.id
            s.add(Task(type="t2", status="pending", payload={"opportunity_id": ids["queued"]}))
            await pr.mark_proof_points_changed(s, "employment")
            await s.commit()
            return ids
    ids = asyncio.run(seed())
    r = _readiness()
    assert r.next_step == "score_again"
    assert sorted(r.role_ids) == sorted([ids["ok"], ids["ok3"]])
    assert r.roles_scored_before_proof_points == 2


def test_documents_accept_without_the_review_field_adds_no_proof_points(fresh):
    value = P7ResumeImport.model_validate({
        "roles": [{"company_label": "Acme", "title": "VP", "start_date": "2020",
                   "end_date": "2024", "context_line": "ctx",
                   "slots": [{"key": "r1b1", "optional": False, "default_variant_key": "v1",
                              "variants": [{"key": "v1", "text": SOURCE, "tags": [],
                                            "derived": False}]}]}],
        "summary_variants": [], "skill_groups": [], "proof_points": [COST.model_dump()],
    })
    with _client() as c:
        r = c.post("/api/documents/import/accept", data={
            "proposal_json": value.model_dump_json(), "text": SOURCE, "track": "employment",
            "doc_kind": "resume"})
        assert "Created." in r.text
    assert load_profile("employment").proof_points == []


def test_profile_writes_wait_for_the_lock_and_leave_no_shared_temp_file(fresh):
    import threading

    done = threading.Event()

    def worker():
        pe.add_proof_points("employment", [{"key": "a", "claim": "Grew sales 3x"}])
        done.set()

    with pe._WRITE_LOCK:
        t = threading.Thread(target=worker)
        t.start()
        assert not done.wait(0.5)  # blocked behind the lock holder
    t.join(5)
    assert done.is_set()
    assert [p.name for p in pe.profile_path().parent.iterdir() if "editing" in p.name] == []
    assert pe._temp_beside(pe.profile_path()) != pe._temp_beside(pe.profile_path())


def test_an_import_does_not_replace_the_profile_tab_undo_backup(fresh):
    pe.save_section("employment", "about", {"first_name": "Samuel"})
    bak = pe.profile_path().with_name(pe.PROFILE_BAK)
    before = bak.read_bytes()
    assert pe.add_proof_points("employment", [{"key": "a", "claim": "Grew sales 3x"}]) == 1
    assert bak.read_bytes() == before
    assert "Grew sales 3x" in pe.profile_path().read_text("utf-8")


def test_a_failed_profile_write_leaves_the_resume_and_reads_as_review_import(fresh, monkeypatch):
    def boom(*_a, **_k):
        raise OSError(13, "/secret/place/profile.yml denied")

    monkeypatch.setattr(pe, "add_proof_points", boom)
    from kestrel.docs.imports import ProofPointsNotSaved

    async def go():
        async with get_sessionmaker()() as s:
            with pytest.raises(ProofPointsNotSaved) as ei:
                await accept_import(s, _value(COST), track="employment", doc_kind="resume",
                                    name="Imported resume", source_markdown=SOURCE)
            assert "/secret" not in str(ei.value)
        async with get_sessionmaker()() as s:
            assert (await s.execute(select(func.count()).select_from(DocMaster))).scalar_one() == 1
            assert await pr.changed_at(s, "employment") is None
    asyncio.run(go())
    assert _readiness().next_step == "review_import"


def test_the_stamp_and_resume_are_committed_before_profile_yml_is_written(fresh, monkeypatch):
    seen = {}
    real = pe.add_proof_points

    def spy(track, points):
        async def peek():
            async with get_sessionmaker()() as s:
                n = (await s.execute(select(func.count()).select_from(DocMaster))).scalar_one()
                return n, await pr.changed_at(s, track)
        seen["db"] = asyncio.run(peek())
        return real(track, points)

    monkeypatch.setattr(pe, "add_proof_points", spy)
    _accept(_value(COST))
    assert seen["db"][0] == 1 and seen["db"][1]


def test_the_page_shows_a_plain_sentence_when_the_profile_write_fails(fresh, monkeypatch):
    def boom(*_a, **_k):
        raise OSError(13, "/secret/place/profile.yml denied")

    monkeypatch.setattr(pe, "add_proof_points", boom)
    value = _value(COST)
    proposal = P7ResumeImport.model_validate({
        "roles": [{"company_label": "Acme", "title": "VP", "start_date": "2020",
                   "end_date": "2024", "context_line": "ctx",
                   "slots": [{"key": "r1b1", "optional": False, "default_variant_key": "v1",
                              "variants": [{"key": "v1", "text": SOURCE, "tags": [],
                                            "derived": False}]}]}],
        "summary_variants": [], "skill_groups": [], "proof_points": [COST.model_dump()],
    })
    assert value
    with _client() as c:
        r = c.post("/api/documents/import/accept", data={
            "proposal_json": proposal.model_dump_json(), "text": SOURCE, "track": "employment",
            "doc_kind": "resume", "proof_shown": "1", "proof_keep": "r1_cost"})
    assert "proof points could not be added" in r.text and "permission was denied" in r.text
    assert "/secret" not in r.text


def test_a_forged_track_is_refused_plainly_and_writes_nothing(fresh):
    with pytest.raises(pe.ProfileEditError):
        pe.add_proof_points("bogus", [{"key": "a", "claim": "x y"}])
    from kestrel.docs.imports import ImportTrackRefused

    async def go():
        async with get_sessionmaker()() as s:
            with pytest.raises(ImportTrackRefused):
                await accept_import(s, _value(COST), track="bogus", doc_kind="resume",
                                    name="Imported resume", source_markdown=SOURCE)
        async with get_sessionmaker()() as s:
            return (await s.execute(select(func.count()).select_from(DocMaster))).scalar_one()
    assert asyncio.run(go()) == 0


def test_score_again_keeps_the_consulting_track(fresh):
    _profile_with_points("consulting")

    async def seed():
        async with get_sessionmaker()() as s:
            o = Opportunity(track="consulting", opp_type="engagement", title="x",
                            stage="t2_done", seen_count=1)
            s.add(o)
            await s.flush()
            s.add(Evaluation(opportunity_id=o.id, tier=2, track="consulting",
                             created_at="2020-01-01T00:00:00Z"))
            await pr.mark_proof_points_changed(s, "consulting")
            await s.commit()
    asyncio.run(seed())
    with _client() as c:
        for url, back in (("/?track=consulting", 'value="/?track=consulting"'),
                          ("/profile?tab=documents&track=consulting",
                           'value="/profile?tab=documents&amp;track=consulting"')):
            html = c.get(url).text
            assert 'data-next-step="score_again"' in html, url
            assert back in html, (url, back)
