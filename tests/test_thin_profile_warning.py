"""Issue #64: a profile with no proof points and no narrative is thin. Scoring still runs (owner
decision), but the operator is told plainly: the unverified wording says what to do, and Today
and Profile carry a notice with a link to Profile > Documents."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, reset_engine
from kestrel.pipeline.context import profile_is_thin
from kestrel.services import profile_edit as pe
from kestrel.web import pipeline_view as pv
from kestrel.web import role_view as rv

OPP = SimpleNamespace(track="employment")
EV = {
    "blocks": {
        "b_fit": {
            "matches": [{"requirement": "ERP", "evidence": "", "strength": "unverified"}],
            "gaps": [],
        }
    }
}
NOTICE = 'data-testid="thin-profile-notice"'


# ------------------------------------------------------------------------------------ helper
def test_profile_is_thin_for_none_dict_and_namespace():
    assert profile_is_thin(None)
    assert profile_is_thin({})
    assert profile_is_thin({"proof_points": [], "narrative": "  \n"})
    assert profile_is_thin(SimpleNamespace(proof_points=None, narrative=None))
    assert not profile_is_thin({"proof_points": [{"claim": "Cut churn 20%"}], "narrative": ""})
    assert not profile_is_thin({"proof_points": [], "narrative": "I run ops teams."})
    assert not profile_is_thin(SimpleNamespace(proof_points=[], narrative="I run ops teams."))


# ------------------------------------------------------------------------------------ wording
def test_unverified_wording_switches_only_when_thin():
    assert pv.UNVERIFIED_HOW_THIN == (
        "Your profile has no proof points yet. Add them on Profile > Documents, then score again."
    )
    assert pv.all_gap_rows(EV)[0]["how"] == pv.UNVERIFIED_HOW
    assert pv.all_gap_rows(EV, thin=True)[0]["how"] == pv.UNVERIFIED_HOW_THIN
    assert pv.gap_rows(EV, thin=True)[0]["how"] == pv.UNVERIFIED_HOW_THIN
    assert rv.requirements(OPP, EV)[0]["how"] == pv.UNVERIFIED_HOW
    assert rv.requirements(OPP, EV, thin=True)[0]["how"] == pv.UNVERIFIED_HOW_THIN


# ------------------------------------------------------------------------------------ pages
@pytest.fixture
def fresh():
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()
    pe.profile_path().unlink(missing_ok=True)
    yield
    asyncio.run(reset_engine())


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def test_today_and_profile_show_the_notice_when_thin(fresh):
    # profile.yml wins over the packaged example, which carries proof points.
    pe.profile_path().write_text(
        yaml.safe_dump({"employment": {"first_name": "Sam", "proof_points": []}}), "utf-8"
    )
    with _client() as c:
        for url in ("/", "/profile"):
            r = c.get(url)
            html = r.text
            assert r.status_code == 200, (url, html[:200])
            assert NOTICE in html, (url, "td-page" in html, "sf-notice" in html)
            assert 'href="/profile?tab=documents"' in html.split(NOTICE)[1], url


def test_a_fresh_install_without_profile_yml_is_not_ready_though_the_example_has_points(fresh):
    # The fictional profile.example.yml is only a loader fallback: with no profile.yml of the
    # operator's own, Today and Profile still say "Add your resume".
    assert not pe.profile_path().exists()
    with _client() as c:
        for url in ("/", "/profile"):
            html = c.get(url).text
            assert NOTICE in html and 'data-next-step="import_resume"' in html, url


def test_role_and_pipeline_thin_flag_is_true_without_an_operator_profile(fresh):
    import asyncio as aio

    from kestrel.services.profile_readiness import operator_profile_is_thin
    from kestrel.web.pages.pipeline import _profile_thin

    assert operator_profile_is_thin("employment")
    assert aio.run(_profile_thin("employment"))
    pe.profile_path().write_text(
        yaml.safe_dump({"employment": {"proof_points": [{"claim": "Cut churn 20%"}]}}), "utf-8")
    assert not operator_profile_is_thin("employment")
    assert operator_profile_is_thin("consulting")


def test_today_and_profile_hide_the_notice_with_a_proof_point(fresh):
    pe.profile_path().write_text(
        yaml.safe_dump(
            {
                "employment": {
                    "employment_terms": {"target_titles": ["VP of IT"]},
                    "proof_points": [
                        {"claim": "Cut churn 20%", "evidence": "Q3 report", "metric": "20%"}
                    ]
                }
            }
        ),
        "utf-8",
    )
    with _client() as c:
        for url in ("/", "/profile"):
            assert NOTICE not in c.get(url).text, url
