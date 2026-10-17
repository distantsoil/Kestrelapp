"""Settings > Learning (D-UI-5): the operator can see, export and reset what their Agree / Adjust /
Override and Calibrate decisions taught. Both tables (`label`, `verdict_override`) appear as one
ledger; agreement is stated with its n and labeled insufficient under five (hard rule 6); the table
is redacted under confidential mode while the export is not; reset removes labels, the reranker's
rows and only the files Kestrel wrote, and the accepted calibrations with their weight overrides,
and leaves every opportunity, evaluation and stranger file in place."""

from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest
import sklearn
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.config import EmploymentWeights, get_settings
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import (
    Calibration,
    DocMaster,
    DocSection,
    Evaluation,
    Label,
    MlModel,
    Opportunity,
    OpportunityEmbedding,
    Org,
    Setting,
    Source,
    VerdictOverride,
)
from kestrel.learning import ledger
from kestrel.services.setting_overrides import load_settings_with_db

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

_EMPLOYER = "Northwind Freight"
_PLACEHOLDER = "[Confidential]"
_T = "2026-09-0{i}T10:00:00.000000+00:00"


async def _seed(s, *, confidential: bool = False, n_overrides: int = 2) -> dict:
    """Three employment opportunities (two with embeddings) and one consulting; three Calibrate
    labels, `n_overrides` Agree/Override rows; an accepted calibration with its weight rows; one
    trained reranker with its two pickles plus a stranger file in the ML folder; the operator's
    current employer with its placeholder for redaction."""
    org = Org(name=_EMPLOYER, normalized_name="northwind freight")
    src = Source(track="employment", kind="greenhouse", name="b", enabled=True)
    s.add_all([org, src])
    await s.flush()
    ids: list[int] = []
    for i in range(3):
        opp = Opportunity(
            track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
            title=f"VP of IT at {_EMPLOYER} {i}", description_md="Own IT strategy.",
            stage="t2_done", first_seen_at=_T.format(i=1),
        )
        s.add(opp)
        await s.flush()
        ids.append(opp.id)
        if i < 2:
            vec = np.random.default_rng(i).normal(0, 1, size=8).astype(np.float32)
            # The current embed model's vectors: only those count as usable (D-EMBED-2 review).
            s.add(OpportunityEmbedding(opportunity_id=opp.id, model="nomic-embed-text",
                                       vector=vec.tobytes(),
                                       dims=8))
        s.add(Evaluation(opportunity_id=opp.id, tier=2, track="employment", verdict="consider",
                         score=3.5, weighted_score=3.4))
    cons = Opportunity(track="consulting", opp_type="trigger", org_id=org.id, title="PE carve-out",
                       stage="t2_done", first_seen_at=_T.format(i=2))
    s.add(cons)
    await s.flush()
    s.add(Evaluation(opportunity_id=cons.id, tier=2, track="consulting", verdict="pursue",
                     score=4.2, weighted_score=4.0))
    # Calibrate-queue labels: two agree with the model, one disagrees, one on consulting.
    s.add_all([
        Label(opportunity_id=ids[0], track="employment", score=3.5, verdict="consider",
              rationale=f"Good fit, {_EMPLOYER} is my current employer though",
              source="active_learning", created_at=_T.format(i=3)),
        Label(opportunity_id=ids[1], track="employment", score=2.0, verdict="pass",
              rationale="Too junior", source="active_learning", created_at=_T.format(i=4)),
        Label(opportunity_id=cons.id, track="consulting", score=4.0, verdict="pursue",
              source="active_learning", created_at=_T.format(i=5)),
    ])
    # Agree / Override on the opportunity page.
    for k in range(n_overrides):
        s.add(VerdictOverride(
            opportunity_id=ids[2], system_verdict="consider", system_score=3.5,
            operator_verdict="consider" if k % 2 == 0 else "apply", operator_score=3.5 + k,
            reason=None if k % 2 == 0 else "Platform work", created_at=_T.format(i=6 + k % 3),
        ))
    # An accepted calibration and the weight rows accept_calibration wrote for it.
    s.add(Calibration(track="employment", n_samples=40, status="accepted",
                      previous_weights=EmploymentWeights().model_dump(),
                      proposed_weights={"role_fit": 0.5, "domain_match": 0.2,
                                        "company_context": 0.1, "comp_logistics": 0.1,
                                        "trajectory": 0.1},
                      decided_at=_T.format(i=7)))
    s.add(Setting(key="scoring.weights.employment.role_fit", value=0.5))
    s.add(Setting(key="scoring.weights.employment.company_context", value=0.1))
    # A promoted reranker and its artifacts, plus a file Kestrel did not write.
    ml = paths.ml_dir()
    ml.mkdir(parents=True, exist_ok=True)
    (ml / "reranker_employment_v1.pkl").write_bytes(b"x" * 10)
    (ml / "features_employment_v1.pkl").write_bytes(b"y" * 20)
    (ml / "operator-notes.txt").write_text("mine")
    s.add(MlModel(
        kind="reranker", track="employment", version="v1", trained_at=_T.format(i=2),
        n_samples=200, metrics={}, artifact_path=str(ml / "reranker_employment_v1.pkl"),
        pca_artifact_path=str(ml / "features_employment_v1.pkl"),
        sklearn_version=sklearn.__version__, is_active=True, promoted_at=_T.format(i=2),
    ))
    master = DocMaster(track="employment", name="Resume", doc_kind="resume")
    s.add(master)
    await s.flush()
    s.add(DocSection(doc_master_id=master.id, section_kind="experience", sort_order=0,
                     org_label=_EMPLOYER, org_label_redacted=_PLACEHOLDER))
    if confidential:
        s.add(Setting(key="confidential_mode", value=True))
    await s.commit()
    return {"opps": ids, "consulting": cons.id, "org": org.id}


async def _count(s, model) -> int:
    return (await s.execute(select(func.count()).select_from(model))).scalar_one()


# ============================================================================ ledger module
async def test_summary_states_counts_floors_weights_and_files(sm):
    async with sm() as s:
        await _seed(s)
        settings = await load_settings_with_db(s)
        out = await ledger.summary(s, settings)
    emp = next(t for t in out.tracks if t.track == "employment")
    con = next(t for t in out.tracks if t.track == "consulting")
    assert out.n_total == 5 and emp.n_total == 4 and con.n_total == 1
    assert emp.n_labels == 2 and emp.n_overrides == 2
    # Four comparable decisions is under the floor: the n is stated, no rate is rendered.
    assert emp.agreement.n == 4 and emp.agreement.insufficient and emp.agreement.rate is None
    assert emp.agreement.text == "too few to judge (4 of 5 needed)"
    floors = {f.key: f for f in emp.floors}
    assert floors["few_shot"].n == 2  # labels with an embedding, not every label
    assert floors["few_shot"].needed == settings.learning.min_labels_for_few_shot
    assert not floors["few_shot"].active
    assert floors["calibration"].active  # weight overrides exist for this track
    assert floors["calibration"].needed == settings.learning.min_labels_for_calibration
    # Judgments with an embedding: the two labeled opps; the overridden one has none.
    assert floors["reranker"].active and floors["reranker"].n == 2
    assert emp.calibrations_accepted == 1
    weights = {w.key: w for w in emp.weights}
    assert weights["role_fit"].current == 0.5 and weights["role_fit"].default == 0.30
    assert weights["role_fit"].overridden and not weights["domain_match"].overridden
    assert con.calibrations_accepted == 0 and not any(w.overridden for w in con.weights)
    assert emp.reranker.active and emp.reranker.loadable
    assert emp.reranker.sklearn_version == sklearn.__version__
    files = {f.name: f for f in out.files}
    assert files["reranker_employment_v1.pkl"].size == 10
    assert files["reranker_employment_v1.pkl"].trained_at == _T.format(i=2)
    pkl = files["reranker_employment_v1.pkl"]
    assert pkl.active and pkl.kestrel_wrote_it
    assert not files["operator-notes.txt"].kestrel_wrote_it
    assert out.db_path == str(paths.db_path()) and out.ml_dir == str(paths.ml_dir())


async def test_agreement_rate_appears_once_five_are_comparable(sm):
    async with sm() as s:
        await _seed(s, n_overrides=4)  # 2 labels + 4 overrides, all with a model verdict
        settings = await load_settings_with_db(s)
        out = await ledger.summary(s, settings)
    emp = next(t for t in out.tracks if t.track == "employment")
    assert emp.agreement.n == 6 and not emp.agreement.insufficient
    # label consider=consider, label pass!=consider, overrides: consider, apply, consider, apply
    assert emp.agreement.agreed == 3 and emp.agreement.rate == pytest.approx(0.5)
    assert emp.agreement.text == "50% (from 6)"


async def test_recent_labels_are_redacted_under_confidential_mode(sm):
    async with sm() as s:
        await _seed(s, confidential=True)
        rows, total = await ledger.recent_labels(s, track="employment")
    assert total == 4 and len(rows) == 4
    assert rows[0].created_at >= rows[-1].created_at  # newest first
    assert all(r.org == _PLACEHOLDER for r in rows)
    joined = " ".join((r.title or "") + (r.rationale or "") for r in rows)
    assert _EMPLOYER not in joined and _PLACEHOLDER in joined
    assert {r.kind for r in rows} == {"label", "override"}
    labels = [r for r in rows if r.kind == "label"]
    assert all(r.feeds_few_shot for r in labels)  # both labeled opps carry an embedding
    assert all(not r.feeds_few_shot for r in rows if r.kind == "override")


async def test_recent_labels_show_the_org_when_confidential_mode_is_off(sm):
    async with sm() as s:
        await _seed(s)
        rows, _ = await ledger.recent_labels(s, track=None, limit=2, offset=0)
        _, total = await ledger.recent_labels(s, track=None, limit=2, offset=2)
    assert len(rows) == 2 and total == 5
    assert rows[0].org == _EMPLOYER
    lbl = next(r for r in rows if r.kind == "label") if any(
        r.kind == "label" for r in rows) else None
    if lbl is not None:
        assert lbl.model_verdict and lbl.model_score is not None  # from the latest evaluation


async def test_delete_one_from_either_table(sm):
    async with sm() as s:
        await _seed(s)
        lbl = (await s.execute(select(Label).limit(1))).scalar_one()
        ov = (await s.execute(select(VerdictOverride).limit(1))).scalar_one()
        assert await ledger.delete_label(s, lbl.id, kind="label")
        assert await ledger.delete_label(s, ov.id, kind="override")
        assert not await ledger.delete_label(s, lbl.id, kind="label")  # already gone
        assert not await ledger.delete_label(s, ov.id, kind="bogus")
        assert await _count(s, Label) == 2 and await _count(s, VerdictOverride) == 1
        assert await _count(s, Opportunity) == 4  # the opportunities stay


async def test_export_is_raw_and_carries_the_fields(sm):
    async with sm() as s:
        await _seed(s, confidential=True)
        rows = await ledger.export_labels(s)
    assert len(rows) == 5
    assert rows[0]["created_at"] <= rows[-1]["created_at"]  # oldest first in the file
    assert any(r["org"] == _EMPLOYER for r in rows)  # NOT redacted, even in confidential mode
    assert all(_PLACEHOLDER != r["org"] for r in rows)
    for key in ("kind", "id", "created_at", "track", "opportunity_id", "title", "org",
                "model_verdict", "model_score", "your_verdict", "your_score", "rationale",
                "source", "feeds_few_shot", "agreed"):
        assert all(key in r for r in rows), key
    json.dumps(rows)  # serializable as-is


async def test_reset_removes_learning_and_nothing_else(sm):
    async with sm() as s:
        await _seed(s)
        settings = await load_settings_with_db(s)
        assert settings.scoring.weights.employment.role_fit == 0.5
        result = await ledger.reset_learning(s, settings)
        assert result["labels_deleted"] == 3 and result["overrides_deleted"] == 2
        assert result["ml_models_deleted"] == 1
        assert sorted(result["ml_files_removed"]) == [
            "features_employment_v1.pkl", "reranker_employment_v1.pkl",
        ]
        assert result["calibrations_reset"] == 1
        assert result["weight_overrides_removed"] == [
            "scoring.weights.employment.company_context", "scoring.weights.employment.role_fit",
        ]
        assert await _count(s, Label) == 0 and await _count(s, VerdictOverride) == 0
        assert await _count(s, MlModel) == 0
        cal = (await s.execute(select(Calibration))).scalar_one()
        assert cal.status == "rejected"  # the row stays as history
        after = await load_settings_with_db(s)
        assert after.scoring.weights.employment.model_dump() == EmploymentWeights().model_dump()
        # Untouched: opportunities, evaluations, the stranger file, other settings rows.
        assert await _count(s, Opportunity) == 4 and await _count(s, Evaluation) == 4
        assert await _count(s, DocSection) == 1
        out = await ledger.summary(s, after)
    assert [f.name for f in out.files] == ["operator-notes.txt"]
    assert out.n_total == 0 and not any(f.active for t in out.tracks for f in t.floors)


async def test_reset_honors_the_parts(sm):
    async with sm() as s:
        await _seed(s)
        settings = await load_settings_with_db(s)
        result = await ledger.reset_learning(
            s, settings, labels=False, reranker=True, calibrations=False
        )
        assert result["labels_deleted"] == 0 and result["calibrations_reset"] == 0
        assert result["ml_models_deleted"] == 1
        assert await _count(s, Label) == 3 and await _count(s, MlModel) == 0
        assert (await s.get(Setting, "scoring.weights.employment.role_fit")) is not None
    assert (paths.ml_dir() / "operator-notes.txt").exists()
    assert not (paths.ml_dir() / "reranker_employment_v1.pkl").exists()


# ============================================================================ routes
def _wipe_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)


async def _seed_app_db(**kw) -> dict:
    await reset_engine()
    _wipe_db()
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        ids = await _seed(s, **kw)
    await engine.dispose()
    forget_engine()
    return ids


async def _app_counts() -> dict:
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        out = {
            "labels": await _count(s, Label), "overrides": await _count(s, VerdictOverride),
            "ml": await _count(s, MlModel), "opps": await _count(s, Opportunity),
        }
    await engine.dispose()
    return out


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def test_settings_page_has_the_learning_tab_and_the_partial_renders():
    asyncio.run(_seed_app_db(confidential=True))
    with _client() as c:
        page = c.get("/settings")
        assert page.status_code == 200
        assert "tab==='learning'" in page.text and 'hx-get="/settings/learning"' in page.text
        r = c.get("/settings/learning")
    assert r.status_code == 200
    body = r.text
    assert "Yes, every Agree, Adjust, Override and Calibrate decision" in body
    assert str(paths.db_path()) in body and str(paths.ml_dir()) in body
    assert "too few to judge (4 of 5 needed)" in body
    assert "reranker_employment_v1.pkl" in body and "operator-notes.txt" in body
    assert 'hx-post="/settings/learning/reset"' in body and 'name="confirm"' in body
    assert 'href="/settings/learning/export"' in body
    assert _PLACEHOLDER in body and _EMPLOYER not in body  # redacted table
    assert "—" not in body  # no em dashes in operator-facing copy


def test_labels_partial_pages_filters_and_deletes():
    asyncio.run(_seed_app_db())
    with _client() as c:
        r = c.get("/settings/learning/labels?track=consulting")
        assert r.status_code == 200 and "PE carve-out" in r.text and "VP of IT" not in r.text
        r = c.get("/settings/learning/labels?track=employment&page=9")
        assert r.status_code == 200 and "VP of IT" in r.text  # page clamped, not empty
        assert get_settings().learning.min_labels_for_few_shot > 0
        lbl_id = json.loads(c.get("/settings/learning/export").text)["labels"][0]["id"]
        kind = json.loads(c.get("/settings/learning/export").text)["labels"][0]["kind"]
        r = c.post(
            f"/settings/learning/labels/{lbl_id}/delete",
            data={"kind": kind, "track": "employment", "page": "1"},
        )
        assert r.status_code == 200 and "Deleted." in r.text
        r = c.post(f"/settings/learning/labels/{lbl_id}/delete", data={"kind": kind})
        assert "Nothing deleted" in r.text
    counts = asyncio.run(_app_counts())
    assert counts["labels"] + counts["overrides"] == 4 and counts["opps"] == 4


def test_export_downloads_raw_json():
    asyncio.run(_seed_app_db(confidential=True))
    with _client() as c:
        r = c.get("/settings/learning/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("attachment;") and 'filename="kestrel-labels-' in disposition
    assert disposition.endswith('.json"')
    data = r.json()
    assert data["redacted"] is False and data["n"] == 5 and len(data["labels"]) == 5
    assert _EMPLOYER in r.text and _PLACEHOLDER not in r.text


def test_reset_refuses_without_the_word_and_runs_with_it():
    asyncio.run(_seed_app_db())
    with _client() as c:
        r = c.post("/settings/learning/reset", data={"confirm": "reset", "labels": "on"})
        assert r.status_code == 200 and "Nothing was reset" in r.text
        assert asyncio.run(_app_counts())["labels"] == 3
        r = c.post("/settings/learning/reset", data={"confirm": "RESET"})
        assert "Tick at least one part" in r.text
        assert asyncio.run(_app_counts())["labels"] == 3
        r = c.post(
            "/settings/learning/reset",
            data={"confirm": "RESET", "labels": "on", "reranker": "on", "calibrations": "on"},
        )
    assert r.status_code == 200
    assert "Reset complete" in r.text
    assert "3 Calibrate-queue label(s) and 2 Agree / Adjust / Override decision(s) deleted" in (
        r.text
    )
    assert "1 reranker record(s) deleted and 2 file(s) removed" in r.text
    assert "1 calibration(s) marked rejected and 2 weight override(s) removed" in r.text
    assert "were not touched" in r.text
    counts = asyncio.run(_app_counts())
    assert counts == {"labels": 0, "overrides": 0, "ml": 0, "opps": 4}
    assert (paths.ml_dir() / "operator-notes.txt").exists()
    assert not (paths.ml_dir() / "features_employment_v1.pkl").exists()
