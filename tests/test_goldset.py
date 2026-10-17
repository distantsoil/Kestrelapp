"""Gold-set gate (SPEC.md 8.6, SPEC-MODULES.md 5.5): the two shipped placeholder files parse and
span the verdict/score range, and `regenerate_goldset` turns real Label rows into the same JSONL
shape without ever touching the shipped placeholder file."""

from __future__ import annotations

import json
from pathlib import Path

import structlog.testing

from kestrel.db.enums import REMOTE_TYPES, VERDICTS
from kestrel.db.models import Label, Opportunity, Org, VerdictOverride
from kestrel.evals.goldset import MIN_LABELS_FOR_SOLID_SAMPLE, regenerate_goldset

EVALS_DIR = Path(__file__).resolve().parent.parent / "evals"

REQUIRED_FIELDS = {
    "track",
    "title",
    "org",
    "description_md",
    "location",
    "comp_min",
    "comp_max",
    "remote_type",
    "gold_verdict",
    "gold_score",
    "placeholder",
}


def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))  # every line must be standalone-valid JSON
    return rows


def _assert_valid_gold_entries(rows: list[dict], track: str) -> None:
    assert len(rows) == 20
    verdicts_seen = set()
    scores = []
    for row in rows:
        assert REQUIRED_FIELDS <= row.keys(), f"missing fields: {REQUIRED_FIELDS - row.keys()}"
        assert row["track"] == track
        assert isinstance(row["title"], str) and row["title"]
        assert isinstance(row["org"], str) and row["org"]
        assert isinstance(row["description_md"], str) and row["description_md"]
        assert isinstance(row["location"], str)
        assert row["comp_min"] is None or isinstance(row["comp_min"], (int, float))
        assert row["comp_max"] is None or isinstance(row["comp_max"], (int, float))
        assert row["remote_type"] in REMOTE_TYPES
        assert row["gold_verdict"] in VERDICTS
        assert isinstance(row["gold_score"], (int, float))
        assert 0 <= row["gold_score"] <= 5
        assert row["placeholder"] is True
        verdicts_seen.add(row["gold_verdict"])
        scores.append(row["gold_score"])
    # spans the range: at least one clear top-tier score and one clear low score, and more than
    # one distinct verdict, so the set actually exercises bench agreement metrics.
    assert len(verdicts_seen) >= 3
    assert max(scores) >= 4.5
    assert min(scores) < 2.0


def test_employment_goldset_is_valid_and_spans_the_range():
    rows = _load_jsonl(EVALS_DIR / "goldset.employment.jsonl")
    _assert_valid_gold_entries(rows, "employment")
    assert "apply" in {r["gold_verdict"] for r in rows}


def test_consulting_goldset_is_valid_and_spans_the_range():
    rows = _load_jsonl(EVALS_DIR / "goldset.consulting.jsonl")
    _assert_valid_gold_entries(rows, "consulting")
    assert "pursue" in {r["gold_verdict"] for r in rows}


async def _seed_opportunity(session, *, track: str, title: str, score: float, verdict: str | None):
    org = Org(name="Test Org", normalized_name=f"test-org-{title}-{score}")
    session.add(org)
    await session.flush()
    opp = Opportunity(
        track=track,
        opp_type="job_posting" if track == "employment" else "rfp",
        org_id=org.id,
        title=title,
        org_name=org.name,
        description_md="A real description of a real opportunity the operator judged.",
        location_raw="Remote, US",
        comp_min=150000,
        comp_max=180000,
        remote_type="remote",
        stage="t2_done",
    )
    session.add(opp)
    await session.flush()
    label = Label(
        opportunity_id=opp.id, track=track, score=score, verdict=verdict, source="manual",
    )
    session.add(label)
    await session.commit()
    return opp.id


async def test_regenerate_goldset_from_real_labels(sm, tmp_path):
    async with sm() as s:
        await _seed_opportunity(
            s, track="employment", title="VP of IT", score=4.6, verdict="apply"
        )
        await _seed_opportunity(
            s, track="employment", title="IT Director", score=1.4, verdict="pass"
        )
        await _seed_opportunity(
            s, track="employment", title="Director of IT", score=3.3, verdict="consider"
        )
        await _seed_opportunity(
            s, track="employment", title="Head of IT", score=4.1, verdict="apply"
        )
        await _seed_opportunity(
            s, track="employment", title="VP Engineering", score=2.0, verdict="pass"
        )

    out_path = tmp_path / "goldset.employment.regenerated.jsonl"
    async with sm() as s:
        result_path = await regenerate_goldset(s, "employment", out_path=out_path)

    assert result_path == out_path
    assert result_path.exists()
    rows = _load_jsonl(result_path)
    assert len(rows) == 5
    for row in rows:
        assert REQUIRED_FIELDS <= row.keys()
        assert row["track"] == "employment"
        assert row["placeholder"] is False
        assert row["gold_verdict"] in VERDICTS
        assert 0 <= row["gold_score"] <= 5
    assert {r["gold_verdict"] for r in rows} == {"apply", "consider", "pass"}


async def test_regenerate_goldset_unifies_labels_and_verdict_overrides(sm, tmp_path):
    """A verdict_override with an operator_score counts as a label too (kestrel.learning.capture),
    and the gold set must reflect it exactly as it would a plain Label row."""
    async with sm() as s:
        opp_id = await _seed_opportunity(
            s, track="consulting", title="Fractional CIO", score=3.0, verdict="consider"
        )
    async with sm() as s:
        s.add(
            VerdictOverride(
                opportunity_id=opp_id,
                operator_verdict="pursue",
                operator_score=4.4,
                reason="Stronger trigger than the model gave it credit for.",
            )
        )
        await s.commit()

    out_path = tmp_path / "goldset.consulting.regenerated.jsonl"
    async with sm() as s:
        await regenerate_goldset(s, "consulting", out_path=out_path)

    rows = _load_jsonl(out_path)
    assert len(rows) == 1
    assert rows[0]["gold_verdict"] == "pursue"  # the override wins (more recent judgment)
    assert rows[0]["gold_score"] == 4.4


async def test_regenerate_goldset_never_overwrites_shipped_placeholder(sm, tmp_path, monkeypatch):
    """The default out_path must never collide with the shipped evals/goldset.<track>.jsonl file,
    even when the CWD is the repo root during a real `kestrel goldset regen` invocation."""
    monkeypatch.chdir(tmp_path)  # a throwaway "repo root" so we never touch the real evals/ dir
    async with sm() as s:
        await _seed_opportunity(
            s, track="employment", title="VP of IT", score=4.6, verdict="apply"
        )
    async with sm() as s:
        result_path = await regenerate_goldset(s, "employment")

    assert result_path.name == "goldset.employment.regenerated.jsonl"
    assert result_path.name != "goldset.employment.jsonl"
    assert result_path.exists()


async def test_regenerate_goldset_thin_sample_still_writes_and_logs_warning(sm, tmp_path):
    """Constraint 6: fewer than five labels still produces a file, but the thinness is logged, not
    presented as a solid comparison baseline."""
    async with sm() as s:
        await _seed_opportunity(
            s, track="employment", title="VP of IT", score=4.6, verdict="apply"
        )
        await _seed_opportunity(
            s, track="employment", title="IT Director", score=1.4, verdict="pass"
        )

    out_path = tmp_path / "thin.jsonl"
    async with sm() as s:
        with structlog.testing.capture_logs() as logs:
            result_path = await regenerate_goldset(s, "employment", out_path=out_path)

    assert result_path.exists()
    rows = _load_jsonl(result_path)
    assert len(rows) == 2 < MIN_LABELS_FOR_SOLID_SAMPLE
    warnings = [entry for entry in logs if entry.get("log_level") == "warning"]
    assert any("insufficient" in entry.get("event", "") for entry in warnings)


async def test_regenerate_goldset_rejects_unknown_track(sm, tmp_path):
    async with sm() as s:
        try:
            await regenerate_goldset(s, "not_a_track", out_path=tmp_path / "x.jsonl")
            raised = False
        except ValueError:
            raised = True
    assert raised
