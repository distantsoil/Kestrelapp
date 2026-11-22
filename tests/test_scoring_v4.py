"""evaluate/v4 and evaluate_consulting/v4 (issue #208 items 3, 4, 5 and 7; D-SCORE-24, D-SCORE-25).

INVARIANTS:
(b) the model selects, never invents: every resume line reaching Tier 2 is the operator's own
    `doc_variant.text` (or skills group) byte for byte;
(d) context never silently truncates: the profile and the resume digest share the 2500-token
    profile allowance, the digest is capped deterministically, and the whole prompt at every
    block's maximum fits the tier's num_ctx with nothing cut (a needle line survives).
Unchanged prompt content renders byte for byte: v3 and Tier 1 do not move. Invented text only;
the model is faked and nothing reaches a network.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from kestrel.config import get_settings, load_settings
from kestrel.db.models import DocMaster, DocSection, DocSlot, DocVariant, Evaluation, Opportunity
from kestrel.ingest.tokens import count_tokens
from kestrel.llm.prompts import load_prompt
from kestrel.pipeline import tier2
from kestrel.pipeline.context import (
    PROFILE_ALLOWANCE_TOKENS,
    RESUME_DIGEST_HEADER,
    RESUME_DIGEST_MAX_TOKENS,
    build_tier1_values,
    build_tier2_values,
    digest_budget,
    profile_compact,
    profile_full,
    profile_full_v4,
    render_user,
    resume_digest_block,
    resume_digest_lines,
)
from kestrel.pipeline.profile import _build, empty_block
from kestrel.pipeline.resume_digest import resume_digest
from tests.pipeline_fakes import FakeLLM
from tests.test_prompt_budgets import (
    HEADROOM,
    JD_900_WORDS,
    NUM_CTX,
    _max_corrections,
    _render_tier2,
    budget_table,
)


def _profile(track: str = "employment", **geo) -> SimpleNamespace:
    block = empty_block(track)
    if track == "employment":
        block["employment_terms"].update(comp_floor_usd=150000, comp_target_usd=210000,
                                         seniority_floor="Director",
                                         target_titles=["Director of Operations"])
    else:
        block["consulting_terms"].update(day_rate_floor=1200, day_rate_target=1600)
    block["geo_rules"].update(remote_ok=True, remote_country_allowlist=["US"],
                              onsite_states=["OR"], relocation_acceptable=False,
                              onsite_metros=[{"name": "Portland", "aliases": ["Portland"]}])
    block["geo_rules"].update(geo)
    block["narrative"] = "Runs ferry terminal operations across three harbors."
    block["proof_points"] = [{"key": "p1", "claim": "Cut crossing delays by a third",
                              "evidence": "harbor ledger", "metric": "33%"}]
    return _build(block)


# ------------------------------------------------------------------- items 3 and 4: wording
def test_v4_states_floor_and_target_plainly_and_remote_first():
    text = profile_full_v4(_profile())
    assert ("Pay floor: $150,000 base. The least you would accept; a stated band that tops out "
            "below it is a dealbreaker.") in text
    assert "Pay target: $210,000. What you would like; never a dealbreaker." in text
    remote = text.index("Remote: yes, from anywhere in US.")
    onsite = text.index("Onsite or hybrid: only in OR; metros Portland.")
    assert remote < onsite < text.index("Relocation: no.")
    assert "Open to remote work" not in text  # the old, ambiguous line is gone in v4
    assert "Location: onsite" not in text


@pytest.mark.parametrize("geo, line", [
    ({"remote_ok": False}, "Remote: no."),
    ({"remote_ok": None}, "Remote: not stated."),
    ({"remote_country_allowlist": []}, "Remote: yes, from anywhere."),
    ({"onsite_states": [], "onsite_metros": []}, "Onsite or hybrid: no places listed."),
])
def test_v4_location_lines_for_each_setting(geo, line):
    assert line in profile_full_v4(_profile(**geo))


def test_v4_consulting_states_the_day_rate_floor_and_target():
    text = profile_full_v4(_profile("consulting"))
    assert "Day rate floor: $1,200/day. The least you would accept;" in text
    assert "Day rate target: $1,600/day. What you would like; never a dealbreaker." in text


# ------------------------------------------------------------------------ item 5: the rule
@pytest.mark.parametrize("version", ["evaluate/v4", "evaluate_consulting/v4"])
def test_v4_defines_a_blocker_and_sends_doubts_to_worth_checking(version):
    spec = load_prompt(version)
    flat = " ".join(spec.system.split())
    assert "target is never a blocker" in flat
    assert "is not a blocker: write it in worth_checking" in flat
    assert spec.user.startswith("{PROFILE_FULL_V4}{RESUME_DIGEST}{OPERATOR_CORRECTIONS}")
    assert spec.model_cls.model_validate_json(_reply(version, ["Confirm the on-site days."])
                                              ).worth_checking == ["Confirm the on-site days."]
    # A reply that leaves the field out reads as no notes, never a failure.
    body = json.loads(_reply(version, []))
    body.pop("worth_checking")
    assert spec.model_cls.model_validate(body).worth_checking == []


def _reply(version: str, notes: list[str]) -> str:
    from tests.pipeline_fakes import _p2, _p2c

    body = json.loads(_p2c(None) if "consulting" in version else _p2(None))
    body["worth_checking"] = notes
    return json.dumps(body)


# --------------------------------------------------------- unchanged content stays unchanged
@pytest.mark.parametrize("version", ["evaluate/v3", "evaluate_consulting/v3"])
def test_v3_renders_byte_for_byte_whatever_the_new_blocks_hold(version):
    track = "consulting" if "consulting" in version else "employment"
    spec = load_prompt(version)
    assert "{PROFILE_FULL_V4}" not in spec.user and "{RESUME_DIGEST}" not in spec.user
    opp = SimpleNamespace(**{**_OPP, "track": track})
    kw = dict(opp=opp, org=None, profile=_profile(track), source_name="board",
              max_jd_tokens=6000, few_shot="", comp_intel=None)
    with_digest = build_tier2_values(**kw, resume_digest=resume_digest_block(["- Ran a dock."]))
    plain = build_tier2_values(**kw)
    old_keys = {k: v for k, v in plain.items() if k not in ("PROFILE_FULL_V4", "RESUME_DIGEST")}
    assert render_user(spec.user, with_digest) == render_user(spec.user, old_keys)
    assert profile_full(_profile(track)) in render_user(spec.user, with_digest)


def test_tier1_profile_is_unchanged():
    values = build_tier1_values(opp=SimpleNamespace(**_OPP), org=None, profile=_profile(),
                                source_name="board")
    assert values["PROFILE_COMPACT"] == profile_compact(_profile())
    assert "Open to remote work: yes." in values["PROFILE_COMPACT"]


_OPP = dict(
    track="employment", opp_type="job_posting", title="Terminal Lead", org_name="Gullwing Ports",
    location_raw="Remote, US", remote_type="remote", employment_type="fulltime", url="u",
    description_md="Lead the terminal.", comp_min=None, comp_max=None, comp_currency="USD",
    comp_interval="year", comp_source="none", date_posted=None, event_date=None, trigger_data=None,
    seen_count=1, first_seen_at="2026-11-01", last_seen_at="2026-11-01", version=1,
    ghost_score=0.0,
)


# ------------------------------------------------------------------ item 7: the digest itself
async def _resume(s, *, roles: list[list[str]], skills=None, kind="resume", track="employment"):
    m = DocMaster(track=track, name="Mine", doc_kind=kind, is_active=True,
                  skill_groups=skills if skills is not None else [
                      {"key": "ops", "label": "Operations", "items": ["Scheduling", "Ledgers"],
                       "default_visible": True},
                      {"key": "hid", "label": "Hidden", "items": ["Never sent"],
                       "default_visible": False}])
    s.add(m)
    await s.flush()
    for i, bullets in enumerate(roles):
        sec = DocSection(doc_master_id=m.id, section_kind="role", title=f"Role {i}",
                         org_label="Gullwing Ports", sort_order=i)
        s.add(sec)
        await s.flush()
        for j, text in enumerate(bullets):
            slot = DocSlot(doc_section_id=sec.id, key=f"r{i}_{j}", sort_order=j,
                           default_variant_key="b")
            s.add(slot)
            await s.flush()
            s.add_all([DocVariant(doc_slot_id=slot.id, key="a", text="An alternate wording."),
                       DocVariant(doc_slot_id=slot.id, key="b", text=text)])
    cred = DocSection(doc_master_id=m.id, section_kind="credential", title="Cert", sort_order=99)
    s.add(cred)
    await s.flush()
    slot = DocSlot(doc_section_id=cred.id, key="c", sort_order=0)
    s.add(slot)
    await s.flush()
    s.add(DocVariant(doc_slot_id=slot.id, key="a", text="A credential line, not experience."))
    await s.commit()
    return m.id


_ODD = "Cut berth idle time 18%  across 3 ports – “quoted”, {job.title} kept"


async def test_the_digest_is_the_operators_own_lines_byte_for_byte(sm):
    roles = [[_ODD, "Second bullet.", "Third.", "Fourth.", "Fifth, past the per-role cap."],
             ["Older role bullet."]]
    async with sm() as s:
        await _resume(s, roles=roles)
        block = await resume_digest(s, "employment", profile_full_v4(_profile()))
    lines = block.split("\n")
    assert lines[:3] == ["", "", RESUME_DIGEST_HEADER]
    assert lines[3:] == ["Skills:", "- Operations: Scheduling, Ledgers", "Experience:",
                         f"- {_ODD}", "- Second bullet.", "- Third.", "- Fourth.",
                         "- Older role bullet."]
    assert "alternate" not in block and "credential" not in block and "Never sent" not in block


async def test_no_resume_means_no_block(sm):
    async with sm() as s:
        assert await resume_digest(s, "employment", profile_full_v4(_profile())) == ""


def test_the_digest_cap_is_what_the_profile_leaves_and_at_most_900():
    assert digest_budget("short") == RESUME_DIGEST_MAX_TOKENS
    big = "ledger " * 2600  # a profile that already fills the allowance
    assert count_tokens(big) >= PROFILE_ALLOWANCE_TOKENS
    assert digest_budget(big) == 0
    mid = "ledger " * 2000
    assert digest_budget(mid) == PROFILE_ALLOWANCE_TOKENS - count_tokens(mid)


def test_lines_are_dropped_whole_at_the_cap_never_cut():
    bullets = [[f"Bullet {i} " + "harbor crane ledger " * 30 for i in range(4)] for _ in range(5)]
    lines = resume_digest_lines([("Ops", ["a", "b"])], bullets, 300)
    assert count_tokens("\n".join([RESUME_DIGEST_HEADER, *lines])) <= 300
    every = {f"- {b}" for role in bullets for b in role}
    assert all(ln in every or ln in ("Skills:", "Experience:", "- Ops: a, b") for ln in lines)
    assert len(resume_digest_lines([], bullets, 10**6)) == 1 + 20  # header + the 20-bullet cap


# ------------------------------------------------------------- (d) the budget and a needle
@pytest.mark.parametrize("track", ["employment", "consulting"])
@pytest.mark.parametrize("tier", ["tier2", "tier3"])
def test_every_block_at_its_maximum_fits_the_window_and_the_needle_survives(tier, track):
    bullets = [[f"Role {r} bullet {i}: " + "reconcile manifest dispatch audit " * 12
                for i in range(6)] for r in range(8)]
    needle_lines = resume_digest_lines([], bullets, RESUME_DIGEST_MAX_TOKENS)
    needle = needle_lines[-1]  # the last line the cap admits
    block = resume_digest_block(needle_lines)
    assert count_tokens(block) <= RESUME_DIGEST_MAX_TOKENS + 2
    long_jd = "\n\n".join([JD_900_WORDS] * 8)
    r = _render_tier2(track, tier=tier, description=long_jd, version=tier2._VERSION_V4[track],
                      corrections=tuple(_max_corrections()), resume_digest=block)
    assert needle in r.user and r.user.count(needle) == 1
    settings = load_settings()
    out = max(load_prompt(tier2._VERSION_V4[track]).max_tokens, settings.llm.tiers[tier].max_tokens)
    assert r.total + out <= r.num_ctx, budget_table([r])
    assert r.total + int(r.num_ctx * HEADROOM) <= NUM_CTX[tier], budget_table([r])


# ------------------------------------------------------------------- the real Tier 2 handler
class _Notes(FakeLLM):
    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.sent: list[str] = []

    async def complete(self, **kw):
        self.sent.append("\n".join(m["content"] for m in kw["messages"]))
        out = await super().complete(**kw)
        body = json.loads(out.content)
        body["worth_checking"] = ["Confirm whether the role needs harbor-side days."]
        out.content = json.dumps(body)
        return out


async def test_tier2_sends_the_digest_and_stores_the_worth_checking_lines(sm):
    async with sm() as s:
        await _resume(s, roles=[[_ODD, "Second bullet."]])
        o = Opportunity(track="employment", opp_type="job_posting", title="Terminal Lead",
                        org_name="Gullwing Ports", stage="t1_pass", stage_reason="invented",
                        description_md="Run the terminal schedule and the crane rota. " * 12,
                        seen_count=1, ghost_score=0.0)
        s.add(o)
        await s.commit()
        oid = o.id
    settings = _v4_settings()
    llm = _Notes(settings)
    out = await tier2.handle_t2(sm, llm, settings, opportunity_id=oid,
                                profiles={"employment": _profile(), "consulting": None})
    assert out.action == "t2_done"
    assert f"\n- {_ODD}\n" in llm.sent[0]  # byte for byte, braces and all
    assert "Pay target: $210,000. What you would like; never a dealbreaker." in llm.sent[0]
    async with sm() as s:
        ev = (await s.execute(select(Evaluation).where(Evaluation.opportunity_id == oid))
              ).scalar_one()
    assert ev.prompt_version == "evaluate/v4"
    assert ev.blocks["worth_checking"] == ["Confirm whether the role needs harbor-side days."]


async def test_the_deeper_read_lists_the_resume_lines_it_sends(sm):
    from kestrel.pipeline import tier3
    from kestrel.web.pages.role import _digest_line_count

    async with sm() as s:
        await _resume(s, roles=[["Ran the dock.", "Kept the ledger."]])
        o = Opportunity(track="employment", opp_type="job_posting", title="Terminal Lead",
                        org_name="Gullwing Ports", stage="t2_done", stage_reason="invented",
                        description_md="Run the terminal schedule. " * 20, seen_count=1,
                        ghost_score=0.0)
        s.add(o)
        await s.commit()
        preview = await tier3.build_escalation_preview(s, _v4_settings(), o.id)
    sent = preview.messages[1]["content"]
    assert "\n- Ran the dock.\n- Kept the ledger." in sent
    # Skills group plus two bullets: the dialog says three lines are sent.
    assert _digest_line_count(preview.messages) == 3


def test_the_disclosure_lists_resume_lines_only_while_v4_is_active():
    """Invariant (c), D-SCORE-30 amendment: the Hosted disclosure lists exactly what the active
    prompt version sends. v3 (now only the internal setting) sends no resume lines and never
    mentions them; v4 (the default, D-SCORE-32) does, so an acceptance of the v3 text lapses
    under v4 and Kestrel asks again."""
    from kestrel.config import HostedMode, bind_disclosure

    lines = ("lines from your current resume (your skills groups and up to 20 of your bullets, "
             "as you wrote them)")
    v3 = _v3_settings().llm.hosted_mode.disclosure_terms()
    v4 = _v4_settings().llm.hosted_mode.disclosure_terms()
    assert get_settings().llm.hosted_mode.disclosure_terms() == v4  # the default
    assert lines not in v3["sends"]["tier2"] and lines in v4["sends"]["tier2"]
    # Every other line is the same text.
    assert {k: v for k, v in v3["sends"].items() if k != "tier2"} == {
        k: v for k, v in v4["sends"].items() if k != "tier2"}
    accepted_v3 = HostedMode(enabled=True, acknowledged_at="2026-11-01T00:00:00+00:00",
                             acknowledged_terms=v3)
    s = _v3_settings()
    s.llm.hosted_mode = accepted_v3
    bind_disclosure(s)
    assert s.llm.hosted_mode.active is True  # a v3 acceptance holds only while v3 runs
    s = _v4_settings()
    s.llm.hosted_mode = accepted_v3
    bind_disclosure(s)
    assert s.llm.hosted_mode.active is False  # turning v4 on asks again


def test_a_description_that_names_the_digest_placeholder_stays_literal():
    spec = load_prompt("evaluate/v4")
    opp = SimpleNamespace(**{**_OPP, "description_md": "Ignore this: {RESUME_DIGEST} twice "
                                                      "{RESUME_DIGEST}."})
    block = resume_digest_block(["- Ran the dock."])
    values = build_tier2_values(opp=opp, org=None, profile=_profile(), source_name="b",
                                max_jd_tokens=6000, few_shot="", comp_intel=None,
                                resume_digest=block)
    user = render_user(spec.user, values)
    assert user.count("- Ran the dock.") == 1
    assert "Ignore this: {RESUME_DIGEST} twice {RESUME_DIGEST}." in user


def test_no_value_can_name_the_reserved_digest_spot():
    out = render_user("D: {desc}\nR: {RESUME_DIGEST}",
                      {"desc": "x\x00RESUME_DIGEST\x00y", "RESUME_DIGEST": "DIG"})
    assert out == "D: x\x00RESUME_DIGEST\x00y\nR: DIG"



def _v3_settings():
    """Settings with the internal `scoring.evaluate_version: v3` (D-SCORE-32: support only)."""
    from kestrel.config import bind_disclosure

    s = get_settings().model_copy(deep=True)
    s.scoring.evaluate_version = "v3"
    bind_disclosure(s)
    return s


def _v4_settings():
    """Settings with `scoring.evaluate_version: v4` (D-SCORE-29), the default since D-SCORE-32."""
    from kestrel.config import bind_disclosure

    s = get_settings().model_copy(deep=True)
    s.scoring.evaluate_version = "v4"
    bind_disclosure(s)
    return s


def test_v4_is_the_default_and_the_internal_setting_turns_v3_on_everywhere(monkeypatch):
    """Owner decision (D-SCORE-32): v4 is the default in 0.7.7; v3 runs only when the internal
    setting says so (support or the bench), and then everywhere at once."""
    from kestrel.config import HostedMode, Scoring
    from kestrel.learning import score_age
    from kestrel.pipeline import tier3

    assert Scoring().evaluate_version == get_settings().scoring.evaluate_version == "v4"
    assert tier2.evaluate_versions(get_settings()) == tier3._versions(get_settings()) == {
        "employment": "evaluate/v4", "consulting": "evaluate_consulting/v4"}
    assert tier2.evaluate_versions(SimpleNamespace()) == tier2._VERSION_V4  # no scoring block
    assert tier3._versions(_v3_settings()) == tier2.evaluate_versions(_v3_settings()) == {
        "employment": "evaluate/v3", "consulting": "evaluate_consulting/v3"}
    # An unbound HostedMode describes the default install, so it never under-states what is
    # sent (it must agree with the default version).
    assert HostedMode().resume_lines is True
    assert HostedMode().disclosure_terms() == get_settings().llm.hosted_mode.disclosure_terms()
    # The staleness check follows the setting. It reads the effective settings (the
    # setting-table row over YAML), never get_settings() alone (round 5, D-SCORE-31).
    monkeypatch.setattr("kestrel.doctor._db_overrides_sync", lambda: None)
    assert score_age.current_versions()["employment"] == "evaluate/v4"
    monkeypatch.setattr("kestrel.doctor._db_overrides_sync",
                        lambda: {"scoring": {"evaluate_version": "v3"}})
    assert get_settings().scoring.evaluate_version == "v4"
    assert score_age.current_versions()["employment"] == "evaluate/v3"


async def test_the_older_score_check_reads_the_setting_row_the_settings_page_writes(sm,
                                                                                   monkeypatch):
    """Round 5 (D-SCORE-31): `scoring.evaluate_version` may be a setting row (support sets it,
    D-SCORE-32); the older-score check must answer from that row, not the process-wide
    settings."""
    from types import SimpleNamespace

    from kestrel.db.models import Setting
    from kestrel.learning import score_age

    monkeypatch.setattr("kestrel.doctor._db_overrides_sync", lambda: None)  # no other source
    opp = SimpleNamespace(id=1, track="employment")
    v3_read = SimpleNamespace(id=9, tier=2, prompt_version="evaluate/v3", created_at="2026-01-01",
                              correction_ids=[], updated_at=None)
    async with sm() as s:
        assert await score_age.older_for(s, opp, v3_read) is True  # v4, the default, is in effect
        s.add(Setting(key="scoring.evaluate_version", value="v3"))
        await s.commit()
    async with sm() as s:
        assert (await score_age.effective_versions(s))["employment"] == "evaluate/v3"
        assert await score_age.older_for(s, opp, v3_read) is False  # v3 is in effect now
    assert get_settings().scoring.evaluate_version == "v4"  # the file layer never changed



@pytest.mark.parametrize("v4", [False, True], ids=["v3", "v4"])
async def test_what_the_disclosure_lists_is_what_each_version_sends(sm, v4):
    """D-SCORE-30 amendment: for each version, the Tier 2 disclosure names resume lines exactly
    when the payload the deeper read previews (and Tier 2 sends) carries them."""
    from kestrel.pipeline import tier3

    settings = _v4_settings() if v4 else _v3_settings()
    async with sm() as s:
        await _resume(s, roles=[["Ran the dock.", "Kept the ledger."]])
        o = Opportunity(track="employment", opp_type="job_posting", title="Terminal Lead",
                        org_name="Gullwing Ports", stage="t1_pass", stage_reason="invented",
                        description_md="Run the terminal schedule. " * 20, seen_count=1,
                        ghost_score=0.0)
        s.add(o)
        await s.commit()
        oid = o.id
        preview = await tier3.build_escalation_preview(s, settings, oid)
    llm = _Notes(settings)
    await tier2.handle_t2(sm, llm, settings, opportunity_id=oid,
                          profiles={"employment": _profile(), "consulting": None})
    listed = "lines from your current resume" in (
        settings.llm.hosted_mode.disclosure_terms()["sends"]["tier2"])
    previewed = any(RESUME_DIGEST_HEADER in m["content"] for m in preview.messages)
    sent = RESUME_DIGEST_HEADER in llm.sent[0]
    assert listed == previewed == sent == v4


def test_settings_loaded_with_v4_list_the_resume_lines():
    """The binding happens on validation, as when settings.yml says evaluate_version: v4."""
    from kestrel.config import Settings

    data = get_settings().model_dump()
    data["scoring"]["evaluate_version"] = "v4"
    loaded = Settings.model_validate(data)
    assert "lines from your current resume" in (
        loaded.llm.hosted_mode.disclosure_terms()["sends"]["tier2"])


# ------------------------------------------------------------------------------- round 4
async def test_the_worker_keeps_a_v4_acceptance_active_mid_run(sm):
    """Round 4 item 2: the worker re-reads Hosted mode from setting rows each cycle; a v4
    acceptance must stay current there, and a row can never set resume_lines."""
    from kestrel.db.models import Setting
    from kestrel.pipeline.worker import Worker

    settings = _v4_settings()
    terms = settings.llm.hosted_mode.disclosure_terms()
    settings.llm.hosted_mode.enabled = True
    settings.llm.hosted_mode.acknowledged_at = "2026-11-01T00:00:00+00:00"
    settings.llm.hosted_mode.acknowledged_terms = terms
    assert settings.llm.hosted_mode.active is True
    async with sm() as s:
        s.add(Setting(key="llm.hosted_mode.resume_lines", value=False))  # a crafted row
        await s.commit()
    hm = await Worker(sm, FakeLLM(settings), settings)._hosted_now()
    assert hm.resume_lines is True and hm.active is True


def test_a_v4_bench_run_lists_and_sends_the_resume_lines_whatever_the_setting(monkeypatch):
    """Round 4 item 1: the confirmation follows the versions the run sends, not the setting."""
    from typer.testing import CliRunner

    from kestrel import cli
    from kestrel.bench import harness
    from kestrel.pipeline import resume_digest
    from tests.test_bench_online import _online

    async def online_settings():
        return _online()  # the app setting stays v3

    async def digests():
        return {"employment": "\n\nRESUME LINES x\n- Ran the dock.", "consulting": ""}

    shown: list[str] = []
    runs: list[dict] = []

    async def fake_run(**kw):
        runs.append(kw)
        return harness.BenchReport(variants=[], skipped_tracks={})

    monkeypatch.setattr(cli, "_db_settings", online_settings)
    monkeypatch.setattr(resume_digest, "digests_for_tracks", digests)
    monkeypatch.setattr(harness, "run_bench", fake_run)
    monkeypatch.setattr(cli, "_stdin_is_terminal", lambda: True)
    monkeypatch.setattr("typer.confirm", lambda text, default=False: shown.append(text) or True)
    for versions, listed in (("evaluate/v4,evaluate/v3", True), ("evaluate/v3", False)):
        shown.clear()
        runs.clear()
        out = CliRunner().invoke(cli.app, ["bench", "--models", "m", "--prompt-version",
                                           versions])
        assert shown, out.output
        assert ("lines from your current resume" in shown[0]) is listed
        assert bool(runs[0]["resume_digests"]) is listed  # sent exactly when listed


def test_a_list_row_never_reads_the_settings_from_inside_the_row(monkeypatch):
    """Round 6: the current versions are read once per request and passed in; warning_text
    and row_view never read the setting rows per row on the event loop."""
    from types import SimpleNamespace as NS

    from kestrel.config import load_settings
    from kestrel.learning import score_age
    from kestrel.web import pipeline_view as pv

    reads: list[int] = []

    def counted():
        reads.append(1)
        return None

    monkeypatch.setattr("kestrel.doctor._db_overrides_sync", counted)
    now = score_age.versions_for(load_settings())
    opp = NS(ghost_score=0.0, seen_count=1, liveness="live", track="employment")
    stale = NS(legitimacy=None, needs_review=False, tier=2, prompt_version="evaluate/v0")
    for _ in range(25):
        assert "older scoring instructions" in pv.warning_text(opp, stale, versions=now)
        assert pv.warning_text(opp, stale) is None  # no versions given: no claim, no read
    assert reads == []
