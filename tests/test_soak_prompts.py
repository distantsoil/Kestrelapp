"""The rubric, scoring and skip logic of scripts/soak_prompts.py. No model, no GPU, no network:
a fake generator stands in for the real run (which is manual: it needs the person's Ollama)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("soak_prompts", ROOT / "scripts" / "soak_prompts.py")
sp = importlib.util.module_from_spec(_spec)
sys.modules["soak_prompts"] = sp
_spec.loader.exec_module(sp)

FX = sp.load_fixture()
POSTING = FX.postings[0]
GOOD_BODY = (
    "Dear Hiring Team,\n\n"
    + "I shortened the month-end close from 9 days to 5 days and I trained 14 shift leads. " * 2
    + " ".join(["I like clear reports and plain handoffs."] * 40)
)


def _sample(body: str, claims=("pp_close",), run: int = 1, **extra):
    value = SimpleNamespace(body_md=body, claims_used=list(claims), exclusions=[], **extra)
    return sp.Sample(POSTING, run, value)


def _check(rubric: str, check_id: str):
    return next(c for c in sp.RUBRICS[rubric].checks if c.id == check_id)


def test_fixtures_load_and_are_invented():
    assert len(FX.postings) == 3
    assert FX.valid_keys == {"pp_close", "pp_train", "pp_waste", "rv_report", "rv_handoff"}
    floors, overrides = sp.load_floors()
    assert floors == {"safety": 100, "style": 80} and overrides == {}


def test_every_rubric_has_a_prompt_and_both_kinds_of_check():
    for rubric in sp.RUBRICS.values():
        assert rubric.prompt and rubric.checks
        assert {c.severity for c in rubric.checks} == {"safety", "style"}
        assert all(c.rule and c.action for c in rubric.checks)


def test_good_letter_passes_the_voice_and_greeting_checks():
    s = _sample(GOOD_BODY)
    assert _check("cover", "first_person").fn(FX, s)
    assert _check("cover", "greeting").fn(FX, s)
    assert _check("cover", "claims_resolve").fn(FX, s)


def test_third_person_and_wrong_greeting_fail():
    bad = _sample("Hello team,\n\nThe candidate shortened the close.")
    assert not _check("cover", "first_person").fn(FX, bad)
    assert not _check("cover", "greeting").fn(FX, bad)


def test_unresolved_or_empty_claims_fail():
    assert not _check("cover", "claims_resolve").fn(FX, _sample(GOOD_BODY, claims=("pp_zzz",)))
    assert not _check("cover", "claims_resolve").fn(FX, _sample(GOOD_BODY, claims=()))


def test_word_count_range():
    check = _check("cover", "word_count")
    assert check.fn(FX, _sample("word " * 300))
    assert not check.fn(FX, _sample("word " * 100))
    assert not check.fn(FX, _sample("word " * 400))


def test_invented_number_is_flagged_but_a_role_number_is_not():
    check = _check("cover", "no_unverified_details")
    assert not check.fn(FX, _sample("I cut costs by 47 percent at my last job."))
    assert check.fn(FX, _sample("You run 6 distribution centers, and I cut the close to 5 days."))


def test_banned_phrase_fails():
    assert not _check("cover", "banned_phrases").fn(FX, _sample("I am a team player."))


def test_outline_needs_three_exclusions():
    check = _check("outline", "exclusions")
    s = _sample("I will do this.")
    s.value.exclusions = ["a", "b"]
    assert not check.fn(FX, s)
    s.value.exclusions = ["a", "b", "c"]
    assert check.fn(FX, s)


def _rubric(*checks):
    return sp.Rubric("t", "t/v1", None, list(checks))


def _always(ok: bool, severity: str, cid: str):
    return sp.Check(cid, severity, f"rule {cid}", f"fix {cid}", lambda fx, s, ok=ok: ok)


def test_scoring_safety_item_below_100_is_a_finding():
    samples = [_sample("x", run=i) for i in range(4)]
    flaky = sp.Check("f", "safety", "rule f", "fix f", lambda fx, s: s.run != 1)
    rows, findings = sp.score(_rubric(flaky), FX, samples, {"safety": 100, "style": 80}, {})
    assert rows[0]["pass_rate"] == 75.0
    assert len(findings) == 1
    assert "rule f" in findings[0].rule and findings[0].action == "fix f"


def test_scoring_style_item_at_80_percent_passes_and_below_fails():
    samples = [_sample("x", run=i) for i in range(5)]
    floors = {"safety": 100, "style": 80}
    one_bad = sp.Check("s", "style", "r", "a", lambda fx, s: s.run != 1)
    assert sp.score(_rubric(one_bad), FX, samples, floors, {})[1] == []
    two_bad = sp.Check("s", "style", "r", "a", lambda fx, s: s.run > 2)
    assert len(sp.score(_rubric(two_bad), FX, samples, floors, {})[1]) == 1


def test_override_beats_the_default_floor():
    samples = [_sample("x", run=i) for i in range(4)]
    flaky = sp.Check("f", "safety", "r", "a", lambda fx, s: s.run != 1)
    _, findings = sp.score(_rubric(flaky), FX, samples, {"safety": 100, "style": 80}, {"t.f": 70})
    assert findings == []


def test_a_sample_without_a_value_fails_every_check_and_keeps_its_error():
    failed = sp.Sample(POSTING, 1, None, "third person after one repair")
    rubric = _rubric(_always(True, "safety", "a"), _always(True, "style", "b"))
    rows, findings = sp.score(rubric, FX, [failed], {"safety": 100, "style": 80}, {})
    assert [r["pass_rate"] for r in rows] == [0.0, 0.0]
    assert len(findings) == 2 and "third person" in findings[0].detail


def test_a_check_that_raises_counts_as_failed_not_a_crash():
    def boom(fx, s):
        raise ValueError("x")

    rows, findings = sp.score(
        _rubric(sp.Check("b", "style", "r", "a", boom)),
        FX,
        [_sample("x")],
        {"safety": 100, "style": 80},
        {},
    )
    assert rows[0]["failed"] == 1 and findings


def test_runner_runs_every_posting_n_times_and_survives_a_generator_error():
    calls = []

    async def gen(client, fx, posting, run):
        calls.append((posting["id"], run))
        if run == 2:
            raise RuntimeError("model fell over")
        return _sample("x", run=run)

    rubric = sp.Rubric("t", "t/v1", gen, [])
    samples = asyncio.run(sp.run_rubric(rubric, None, FX, 3))
    assert len(calls) == 9 and len(samples) == 9
    assert sum(1 for s in samples if s.value is None) == 3
    assert "fell over" in next(s.error for s in samples if s.value is None)


def test_skips_cleanly_when_ollama_is_not_reachable(monkeypatch, capsys):
    from kestrel.services import setup_state

    monkeypatch.setattr(setup_state, "probe_ollama", lambda settings, fresh=False: (None, []))
    code = sp.main(["--runs", "1"])
    out = capsys.readouterr().out
    assert code == 0
    assert out.strip().splitlines()[-1].startswith("SOAK SKIP: prompts: Ollama not reachable")


def test_online_model_is_refused_without_both_flags(monkeypatch):
    from kestrel import config

    settings = config.load_settings(mode="local")
    backend = settings.llm.backends[settings.llm.tiers["tier3"].backend]
    monkeypatch.setattr(type(backend), "external", property(lambda self: True), raising=False)
    monkeypatch.setattr(config, "load_settings", lambda mode=None: settings)
    args = sp.parse(["--hosted"])
    got, reason = sp.local_client_or_skip(args)
    assert got is None and "--confirm-send-online" in reason


def test_findings_files_are_written(tmp_path):
    from soak_common import Finding, write_findings

    write_findings(tmp_path, "prompts", [Finding("cover/v3", "rule", "do it")], ["Model: m"])
    data = json.loads((tmp_path / "findings.json").read_text(encoding="utf-8"))
    assert data["count"] == 1 and data["findings"][0]["where"] == "cover/v3"
    assert "do it" in (tmp_path / "findings.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "passed,line", [(True, "SOAK PASS: x"), (False, "SOAK FAIL: x: 1 findings")]
)
def test_verdict_line(capsys, passed, line):
    from soak_common import Finding, verdict

    code = verdict("x", [] if passed else [Finding("a", "b", "c")])
    assert capsys.readouterr().out.strip() == line and code == (0 if passed else 1)
