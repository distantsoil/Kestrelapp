"""Issue #250, the owner's rule for the title filter (D-SCORE-914): a shared word that names a
field keeps a posting by itself; a shared GENERIC word (operations, systems, business and the
like, the `title_filter.generic_words` list in filters.yml) keeps it only with a second shared
word, unless that word is all a target title names. The level floor, a blank title, a title
with no field words and a profile with nothing to compare against are as they were.

Named cases: a company board and a search source (both go through `runner.run_source`); manual
add and the extension capture share `manual_ingest`, which never asks the title filter. A role
already stored is not touched by the rule. All text is invented; nothing touches the network."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from sqlalchemy import select

from kestrel import paths
from kestrel.db.models import Opportunity, Source
from kestrel.ingest import runner
from kestrel.ingest import smart_filter as sf
from kestrel.ingest.base import RawPosting
from kestrel.ingest.http import HttpClient
from kestrel.ingest.manual import manual_ingest

# A leadership search in one field, with general words riding along in the target titles.
TARGETS = ["Director of IT", "Director of IT Operations", "Director of Enterprise Systems",
           "Director of Business Systems", "VP of Infrastructure Engineering"]

DROPPED = [
    "Frontend Engineer, Design Systems",  # one general word: systems
    "Enterprise Sales Representative",  # one general word: enterprise
    "Operations Analyst",  # one general word: operations
    "People Business Partner",  # one general word: business
    "Mechanical Engineering Intern",  # one general word: engineering
]
KEPT = [
    "Director of IT",  # a field word, alone
    "Director of Information Technology",
    "Head of Infrastructure",
    "Senior Analyst, Business Systems",  # two general words together
    "Enterprise Systems Architect",
    "IT Operations Manager",  # a field word and a general word
]


def _profile(targets=TARGETS, floor: str = "") -> SimpleNamespace:
    return SimpleNamespace(employment_terms=SimpleNamespace(
        target_titles=list(targets), seniority_floor=floor))


def _write_filters(data: dict) -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_dir().joinpath("filters.yml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _shipped() -> dict:
    return yaml.safe_load((paths.packaged_config() / "filters.yml").read_text(encoding="utf-8"))


async def _ctx(sm, targets=TARGETS, floor: str = "") -> sf.FilterContext:
    async with sm() as s:
        return await sf.build_context(s, "employment", _profile(targets, floor))


# ------------------------------------------------------------------ the list
def test_the_shipped_list_names_general_words_and_no_field():
    words = _shipped()["title_filter"]["generic_words"]
    assert words == sorted(set(words))  # one entry each, in order, so a diff reads cleanly
    # The five words the owner's choice was measured on (D-SCORE-915 as amended). A longer
    # list is the owner's to choose, with numbers.
    assert words == ["business", "engineering", "enterprise", "operations", "systems"]
    generic = sf.generic_words()
    assert generic == {"operation", "enterprise", "system", "business", "engineering"}
    # Words that name a field are never on it.
    assert not generic & sf.profile_tokens(
        ["IT Director", "Head of Infrastructure", "Marketing Manager", "Finance Director",
         "Security Lead", "Nursing Supervisor", "Sales Manager", "Data Analyst"])


def test_a_rules_file_from_before_the_list_gets_the_shipped_list():
    older = {k: v for k, v in _shipped().items() if k != "title_filter"}
    _write_filters(older)
    assert sf.generic_words() == sf.shipped_generic_words() != frozenset()


def test_the_persons_own_list_wins_and_an_empty_one_switches_the_rule_off():
    _write_filters({**_shipped(), "title_filter": {"generic_words": ["Logistics"]}})
    assert sf.generic_words() == frozenset({"logistic"})
    _write_filters({**_shipped(), "title_filter": {"generic_words": []}})
    assert sf.generic_words() == frozenset()


def test_an_unreadable_list_falls_back_to_the_shipped_one():
    _write_filters({**_shipped(), "title_filter": {"generic_words": "operations"}})  # not a list
    assert sf.generic_words() == sf.shipped_generic_words()


def _write_raw(data: bytes) -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_dir().joinpath("filters.yml").write_bytes(data)


_UNREADABLE = {
    "utf16": yaml.safe_dump({"title_filter": {"generic_words": ["logistics"]}}).encode("utf-16"),
    "garbage": bytes(range(128, 256)) * 4,
    "broken_yaml": b"title_filter: {generic_words: [operations\n  - : :\n",
    "not_a_mapping": b"- just\n- a list\n",
    "nul_bytes": b"title_filter:\x00\x00\n  generic_words: [a]\n",
}


@pytest.mark.parametrize("name", sorted(_UNREADABLE))
async def test_a_rules_file_that_cannot_be_read_gets_the_shipped_list_and_a_warning(sm, name):
    """The list's own promise: a file Kestrel cannot read is never a crash, never "keep
    everything" and never "keep nothing". The source run goes on with the shipped list and the
    log says so."""
    from structlog.testing import capture_logs

    _write_raw(_UNREADABLE[name])
    with capture_logs() as logs:
        assert sf.generic_words() == sf.shipped_generic_words() != frozenset()
        ctx = await _ctx(sm)  # what every source run builds first
    warned = [e for e in logs if e["event"] == "title_filter_list_unreadable"]
    assert warned and all(e["log_level"] == "warning" for e in warned)
    assert not sf.keep("Operations Analyst", ctx) and sf.keep("Director of IT", ctx)


async def test_a_folder_named_like_the_rules_file_is_reported_and_survived(sm):
    from structlog.testing import capture_logs

    (paths.config_dir() / "filters.yml").mkdir(parents=True)
    with capture_logs() as logs:
        assert sf.generic_words() == sf.shipped_generic_words() != frozenset()
        ctx = await _ctx(sm)
    assert [e["log_level"] for e in logs if e["event"] == "title_filter_list_unreadable"]
    assert not sf.keep("Operations Analyst", ctx) and sf.keep("Director of IT", ctx)


async def test_a_rules_file_that_may_not_be_opened_is_reported_and_survived(sm, monkeypatch):
    from pathlib import Path

    from structlog.testing import capture_logs

    _write_filters({**_shipped(), "title_filter": {"generic_words": ["logistics"]}})
    target = paths.config_dir() / "filters.yml"
    real = Path.read_text

    def denied(self, *args, **kwargs):
        if self == target:
            raise PermissionError(13, "Permission denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    with capture_logs() as logs:
        assert sf.generic_words() == sf.shipped_generic_words() != frozenset()
        ctx = await _ctx(sm)
    warned = [e for e in logs if e["event"] == "title_filter_list_unreadable"]
    assert warned and warned[0]["error_type"] == "PermissionError"
    assert not sf.keep("Operations Analyst", ctx)


def test_an_empty_rules_file_is_reported_and_gets_the_shipped_list():
    """An empty file is not a file from before the list existed (that one has the tracks): it is
    a damaged file, so it is reported like the others."""
    from structlog.testing import capture_logs

    _write_raw(b"")
    with capture_logs() as logs:
        assert sf.generic_words() == sf.shipped_generic_words() != frozenset()
    assert [e for e in logs if e["event"] == "title_filter_list_unreadable"]


def test_a_utf8_file_with_a_byte_order_mark_is_read_as_written():
    """Intended: "UTF-8 with BOM" is what some Windows editors save, the YAML reader accepts it,
    and the person's list is used with no warning."""
    from structlog.testing import capture_logs

    text = yaml.safe_dump({**_shipped(), "title_filter": {"generic_words": ["Logistics"]}})
    _write_raw(b"\xef\xbb\xbf" + text.encode("utf-8"))
    with capture_logs() as logs:
        assert sf.generic_words() == frozenset({"logistic"})
    assert [e for e in logs if e["event"] == "title_filter_list_unreadable"] == []


def test_a_missing_list_is_not_reported_as_unreadable():
    from structlog.testing import capture_logs

    older = {k: v for k, v in _shipped().items() if k != "title_filter"}
    _write_filters(older)  # a file from before the list existed: normal, nothing to warn about
    with capture_logs() as logs:
        assert sf.generic_words() == sf.shipped_generic_words()
    assert [e for e in logs if e["event"] == "title_filter_list_unreadable"] == []


# ------------------------------------------------------------------ the decision
@pytest.mark.parametrize("title", DROPPED)
async def test_one_shared_general_word_does_not_keep_a_posting(sm, title):
    assert not sf.keep(title, await _ctx(sm))


@pytest.mark.parametrize("title", KEPT)
async def test_a_field_word_or_two_general_words_keep_a_posting(sm, title):
    assert sf.keep(title, await _ctx(sm))


async def test_a_general_word_that_is_all_a_target_title_names_counts_alone(sm):
    ctx = await _ctx(sm, ["Operations Manager", "Director of IT"])
    assert sf.keep("Senior Operations Manager", ctx)
    assert sf.keep("Operations Analyst", ctx)  # they search operations: the word is theirs
    assert not sf.keep("Design Systems Engineer", ctx)
    ctx = await _ctx(sm, ["IT Operations Manager"])  # here operations only rides along
    assert not sf.keep("Operations Analyst", ctx)


async def test_an_empty_list_gives_the_rule_as_it_was(sm):
    _write_filters({**_shipped(), "title_filter": {"generic_words": []}})
    ctx = await _ctx(sm)
    assert all(sf.keep(t, ctx) for t in DROPPED + KEPT)


async def test_floor_blank_title_no_field_words_and_nothing_to_judge_are_unchanged(sm):
    ctx = await _ctx(sm, floor="director")
    assert not sf.keep("IT Support Analyst", ctx)  # below the floor, field word or not
    assert sf.keep("Director of IT", ctx)
    assert sf.keep(None, ctx) and sf.keep("   ", ctx)  # blank
    assert sf.keep("Executive Assistant", ctx)  # level words only: cannot be judged
    empty = await _ctx(sm, [])
    assert sf.keep("Operations Analyst", empty)  # nothing to compare against


async def test_a_general_word_learned_from_pursuits_still_needs_a_partner(sm):
    from kestrel.db.models import Pursuit

    async with sm() as s:
        for i in range(2):
            o = Opportunity(track="employment", opp_type="job_posting",
                            title=f"Platform Operations Lead {i}", org_name="Fictional Org",
                            stage="t2_done", seen_count=1)
            s.add(o)
            await s.flush()
            s.add(Pursuit(opportunity_id=o.id, track="employment", status="applied"))
        await s.commit()
    ctx = await _ctx(sm, ["Director of IT"])
    assert {"operation", "platform"} <= ctx.tokens
    assert not sf.keep("Operations Analyst", ctx)
    assert sf.keep("Platform Engineer", ctx)  # a learned field word keeps by itself
    assert sf.keep("Platform Operations Analyst", ctx)


# ------------------------------------------------------------------ the paths
async def _source_run(sm, monkeypatch, kind: str, titles: list[str]) -> dict:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_dir().joinpath("profile.yml").write_text(yaml.safe_dump({"employment": {
        "employment_terms": {"target_titles": TARGETS},
        "geo_rules": {"remote_country_allowlist": ["US"]},
    }}), encoding="utf-8")
    postings = [RawPosting(external_id=str(i), title=t, org_name="Fictional Org",
                           description_md="An invented posting.", location_raw="Remote",
                           url=f"https://example.test/jobs/{i}")
                for i, t in enumerate(titles)]

    class Fake:
        def validate_config(self, config: dict) -> None:
            pass

        async def fetch(self, config: dict, http: HttpClient) -> list[RawPosting]:
            return postings

    monkeypatch.setattr(runner, "get_adapter", lambda _kind: Fake())
    monkeypatch.setattr(runner.board_discovery, "discover_boards", AsyncMock(return_value=0))
    async with sm() as s:
        src = Source(track="employment", kind=kind, name=f"Fake {kind}", config={}, enabled=True)
        s.add(src)
        await s.commit()
        sid = src.id
    async with HttpClient(user_agent="test", respect_robots=False) as http:
        outcome = await runner.run_source(sm, sid, http=http)
    return outcome.counts


@pytest.mark.parametrize("kind", ["greenhouse", "jobspy"])  # a company board, a search
async def test_a_source_run_stores_only_what_the_rule_keeps(sm, monkeypatch, kind):
    counts = await _source_run(sm, monkeypatch, kind, DROPPED + KEPT)
    async with sm() as s:
        stored = sorted((await s.execute(select(Opportunity.title))).scalars())
    assert stored == sorted(KEPT)
    assert counts["dropped_unrelated_title"] == len(DROPPED)


async def test_manual_add_and_capture_never_ask_the_title_filter(sm, monkeypatch):
    """Manual add and the extension capture (opportunities_ingest) call manual_ingest: the person
    chose the role, so the title filter is not applied there, before or after this rule."""
    monkeypatch.setattr(sf, "keep", lambda *a, **k: pytest.fail("the title filter was asked"))
    async with sm() as s:
        opp = await manual_ingest(s, url="https://example.test/jobs/m1",
                                  text="An invented posting.", title="Operations Analyst",
                                  org_name="Fictional Org")
    assert opp is not None and opp.title == "Operations Analyst" and opp.stage == "new"


async def test_a_stored_role_seen_again_under_a_title_the_rule_drops_is_left_as_it_is(
        sm, monkeypatch):
    """Stored while one shared general word was enough, then seen again by the same source with
    the rule on: the posting is dropped before storage, so the stored role is not updated, not
    restaged and not queued for anything (SPEC.md "Country screening at ingest": a dropped
    posting does not update an existing row)."""
    from kestrel.db.models import Task

    _write_filters({**_shipped(), "title_filter": {"generic_words": []}})  # the rule as it was
    await _source_run(sm, monkeypatch, "greenhouse", ["Operations Analyst"])
    async with sm() as s:
        opp = (await s.execute(select(Opportunity))).scalar_one()
        opp.stage, opp.stage_reason = "t2_done", "seeded"
        await s.commit()
        before = (opp.id, opp.title, opp.description_md, opp.version, opp.seen_count,
                  opp.last_seen_at)
        sid = opp.source_id
        tasks_before = len((await s.execute(select(Task))).scalars().all())
    paths.config_dir().joinpath("filters.yml").unlink()  # back to the shipped list

    posting = RawPosting(external_id="0", title="Operations Analyst", org_name="Fictional Org",
                         description_md="Entirely different invented words for this posting.",
                         location_raw="Remote", url="https://example.test/jobs/0")

    class Again:
        def validate_config(self, config: dict) -> None:
            pass

        async def fetch(self, config: dict, http: HttpClient) -> list[RawPosting]:
            return [posting]

    monkeypatch.setattr(runner, "get_adapter", lambda _kind: Again())
    async with HttpClient(user_agent="test", respect_robots=False) as http:
        outcome = await runner.run_source(sm, sid, http=http)
    assert outcome.counts["dropped_unrelated_title"] == 1
    assert (outcome.counts["new"], outcome.counts["reseen"], outcome.counts["changed"]) == (0, 0, 0)
    async with sm() as s:
        rows = (await s.execute(select(Opportunity))).scalars().all()
        tasks_after = len((await s.execute(select(Task))).scalars().all())
    assert len(rows) == 1
    opp = rows[0]
    assert (opp.id, opp.title, opp.description_md, opp.version, opp.seen_count,
            opp.last_seen_at) == before
    assert (opp.stage, opp.stage_reason, opp.archived_at) == ("t2_done", "seeded", None)
    assert tasks_after == tasks_before


async def test_a_stored_role_is_not_changed_by_the_rule(sm, monkeypatch):
    async with sm() as s:
        o = Opportunity(track="employment", opp_type="job_posting", title="Operations Analyst",
                        org_name="Fictional Org", stage="t2_done", stage_reason="seeded",
                        seen_count=1)
        s.add(o)
        await s.commit()
        oid = o.id
    await _source_run(sm, monkeypatch, "greenhouse", ["Director of IT"])
    async with sm() as s:
        opp = await s.get(Opportunity, oid)
    assert (opp.stage, opp.stage_reason, opp.archived_at) == ("t2_done", "seeded", None)
