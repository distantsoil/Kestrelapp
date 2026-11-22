"""Issue 182: say when a board likely blocked the description, and show it per source.

Typed evidence only (structural emptiness, the source kind), a sample size on every share, and
general wording. All source data is invented and nothing touches the network."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from kestrel import paths
from kestrel.db.models import ScrapeRun, Source
from kestrel.ingest import empty_bodies as eb
from kestrel.ingest import runner
from kestrel.ingest.base import NoticeKind, RawPosting
from kestrel.ingest.http import HttpClient
from kestrel.web.pipeline_view import blocked_note
from kestrel.web.source_view import body_lines


def _raw(md=None, html=None):
    return SimpleNamespace(description_md=md, description_html=html)


def test_body_emptiness_is_measured_structurally():
    assert eb.is_empty_body(_raw())
    assert eb.is_empty_body(_raw("   \n "))
    assert eb.is_empty_body(_raw(html="<div><p> </p><br></div>"))
    assert eb.is_empty_body(_raw(md="## \n* \n"))
    assert eb.is_empty_body(_raw(html="<script>var a=1</script>"))
    assert not eb.is_empty_body(_raw(md="Lead the team."))
    assert not eb.is_empty_body(_raw(html="<p>Lead the team.</p>"))


def test_a_words_in_the_body_never_decide():
    # A body that talks about a captcha is still a body: no page text is matched (8a.2).
    assert not eb.is_empty_body(_raw(md="Please verify you are human. Captcha."))


def test_role_note_only_for_a_search_board():
    note = blocked_note("jobspy")
    assert note and "may have blocked" in note and "Worth checking" in note
    assert "was blocked" not in note
    for kind in ("greenhouse", "lever", "manual", "page", "rss", None):
        assert blocked_note(kind) is None


def test_share_below_five_roles_is_too_few_to_tell():
    r = eb.read_share(4, 4, [])
    assert r.state == "too_few"
    assert "too few to tell" in r.share_text and "4 new roles" in r.share_text
    assert r.warning("Glassdoor") is None
    assert eb.read_share(1, 1, []).share_text == "too few to tell (1 new role)"


def test_jump_against_usual_and_without_history():
    usual = [{"new": 20, "empty": 2}, {"new": 20, "empty": 4}]  # 15 percent
    r = eb.read_share(20, 14, usual)
    assert r.state == "jump"
    warn = r.warning("Glassdoor")
    assert warn is not None and warn.kind is NoticeKind.STOPPED
    assert "Glassdoor: 14 of 20 new roles had no description" in warn.sentence
    assert "may be blocking" in warn.sentence
    assert eb.read_share(20, 6, usual).state == "ok"  # 30 percent is under 15 + 25
    assert eb.read_share(20, 10, []).state == "jump"  # no usual share: half or more
    assert eb.read_share(20, 9, []).state == "ok"
    assert eb.read_share(20, 0, []).state == "ok"
    # A usual share built from fewer than five roles is not a usual share.
    assert eb.usual_share([{"new": 3, "empty": 3}]) is None
    # D-SRC-649: a usual share at or above half is never kept, so a source that is usually
    # nearly empty warns each run (a block cannot hide behind it).
    assert eb.read_share(20, 18, [{"new": 40, "empty": 36}]).state == "jump"
    assert eb.read_share(20, 15, [{"new": 40, "empty": 24}]).state == "jump"


def test_body_lines_show_count_with_sample_and_the_warning():
    src = SimpleNamespace(name="Searches")
    counts = {eb.COUNTS_KEY: {
        "glassdoor": {"new": 20, "empty": 14, "state": "jump"},
        "indeed": {"new": 3, "empty": 3, "state": "too_few"},
    }}
    lines = {x["label"]: x for x in body_lines(src, counts)}
    assert lines["Glassdoor"]["text"] == "14 of 20 new roles had no description"
    assert "may be blocking" in lines["Glassdoor"]["warn"]
    assert lines["Indeed"]["text"].startswith("too few to tell")
    assert lines["Indeed"]["warn"] is None
    assert body_lines(src, {}) == [] and body_lines(src, None) == []


# ---- the runner stores the reading per board, against that source's own history -------------


def _profile() -> None:
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_dir().joinpath("profile.yml").write_text(
        yaml.safe_dump({"employment": {
            "employment_terms": {"target_titles": ["Director of IT"]},
            "geo_rules": {"remote_country_allowlist": ["US"]}}}), encoding="utf-8")


def _post(i: int, empty: bool, site: str = "glassdoor") -> RawPosting:
    return RawPosting(
        external_id=f"{site}-{i}", title="Director of IT", org_name=f"Fictional Org {site}{i}",
        description_md="" if empty else "A fictional description of the work.",
        location_raw="Remote", url=f"https://example.test/{site}/{i}", raw={"site": site})


async def _run(sm, monkeypatch, batches: list[list[RawPosting]]):
    it = iter(batches)

    class Fake:
        def validate_config(self, config: dict) -> None:
            pass

        async def fetch(self, config: dict, http: HttpClient) -> list[RawPosting]:
            return next(it)

    monkeypatch.setattr(runner, "get_adapter", lambda kind: Fake())
    monkeypatch.setattr(runner.board_discovery, "discover_boards", AsyncMock(return_value=0))
    _profile()
    async with sm() as session:
        src = Source(track="employment", kind="jobspy", name="Fake search", config={},
                     enabled=True)
        session.add(src)
        await session.commit()
        sid = src.id
    stored = []
    for _ in batches:
        async with HttpClient(user_agent="test", respect_robots=False) as http:
            out = await runner.run_source(sm, sid, http=http)
        async with sm() as session:
            stored.append((await session.get(ScrapeRun, out.scrape_run_id)).counts)
    return stored


async def test_a_jump_fires_on_a_fixture_run_and_a_small_batch_does_not(sm, monkeypatch):
    usual = [_post(i, empty=i < 2) for i in range(20)] + [_post(i, False, "indeed")
                                                          for i in range(3)]
    jump = [_post(100 + i, empty=i < 14) for i in range(20)] + [
        _post(100 + i, True, "indeed") for i in range(3)]
    first, second = await _run(sm, monkeypatch, [usual, jump])
    assert first[eb.COUNTS_KEY]["glassdoor"] == {"new": 20, "empty": 2, "state": "ok"}
    assert second[eb.COUNTS_KEY]["glassdoor"] == {"new": 20, "empty": 14, "state": "jump"}
    # Three Indeed roles are too few to tell, even all empty.
    assert second[eb.COUNTS_KEY]["indeed"]["state"] == "too_few"
    # The roles are all kept (hard rule 4): the count of stored new roles is unchanged.
    assert second["new"] == 23


# ---- the Quick round card carries the note for a search board only --------------------------


def _card_with_source_kind(kind: str) -> str:
    import re

    from tests import quick_fixtures as qf

    ids = qf.seed(6)

    async def put(s):
        from sqlalchemy import update

        from kestrel.db.models import Opportunity

        for oid in ids:
            (await s.get(Opportunity, oid)).description_md = "<br/><p>&nbsp;</p>"
        await s.execute(update(Source).values(kind=kind))
        await s.commit()

    qf.run(put)
    with qf.client() as c:
        body = c.get("/calibrate/quick").text
    return re.findall(r'<article class="qr-card".*?</article>', body, re.S)[0]


def test_quick_card_says_the_site_may_have_blocked_the_reader_for_a_search_board():
    card = _card_with_source_kind("jobspy")
    assert 'data-testid="qr-blocked-note"' in card
    assert "may have blocked Kestrel&#39;s reader" in card or "may have blocked Kestrel's" in card
    assert "Worth checking" in card


def test_quick_card_keeps_the_current_wording_for_a_company_board():
    card = _card_with_source_kind("greenhouse")
    assert "qr-blocked-note" not in card
    assert "No description was saved for this role." in card


def test_the_runs_counts_list_never_prints_the_raw_bodies_dict():
    from kestrel.web.deps import templates

    shown = templates().env.filters["run_counts"](
        {"new": 3, eb.COUNTS_KEY: {"glassdoor": {"new": 3, "empty": 0, "state": "too_few"}}})
    assert shown == {"new": 3}


# ---- review round: a board that stays blocked, and counts that cannot be trusted ------------


def test_a_board_that_stays_blocked_keeps_warning_every_run():
    history: list[dict] = []
    states = []
    for _ in range(5):  # 20 of 20 empty, every run
        r = eb.read_share(20, 20, history)
        history.append(r.as_counts())
        states.append(r.state)
    assert states == ["jump"] * 5


def test_a_run_that_jumped_does_not_become_the_usual_share():
    calm = [{"new": 20, "empty": 2, "state": "ok"}] * 2
    after_jump = [*calm, {"new": 20, "empty": 14, "state": "jump"}]
    assert eb.usual_share(after_jump) == eb.usual_share(calm) == 0.1
    assert eb.read_share(20, 14, after_jump).state == "jump"  # still a jump, not new normal


def test_the_threshold_catches_a_block_and_not_ordinary_swings():
    """Realistic runs: a board that is usually 10 percent empty. Swings up to 30 percent stay
    quiet; a block (60 percent or more) always warns, and a half-empty first run warns too."""
    usual = [{"new": 20, "empty": 2, "state": "ok"}, {"new": 25, "empty": 3, "state": "ok"}]
    for empty in range(0, 8):  # up to 35 percent of 20
        assert eb.read_share(20, empty, usual).state == "ok", empty
    for empty in range(12, 21):  # 60 percent and up
        assert eb.read_share(20, empty, usual).state == "jump", empty
    assert eb.read_share(10, 5, []).state == "jump"
    assert eb.read_share(10, 4, []).state == "ok"


@pytest.mark.parametrize("bad", [
    {"new": "x", "empty": 0}, {"new": [1], "empty": 0}, {"new": 5, "empty": None},
    {"new": -3, "empty": 9}, {"new": 3, "empty": 9}, {"new": True, "empty": 1},
    {"new": 2.5, "empty": 1}, {"new": 10**9, "empty": 0}, "text", None, [], {},
])
def test_stored_counts_are_rebuilt_never_trusted(bad):
    assert eb.clean_entry(bad) is None
    assert eb.usual_share([bad, {"new": 20, "empty": 2}]) == 0.1  # ignored, never a crash
    src = SimpleNamespace(name="Searches")
    assert body_lines(src, {eb.COUNTS_KEY: {"glassdoor": bad}}) == []


def test_a_stored_jump_on_a_tiny_sample_is_too_few_and_never_warns():
    src = SimpleNamespace(name="Searches")
    e = eb.clean_entry({"new": 1, "empty": 1, "state": "jump"})
    assert e == {"new": 1, "empty": 1, "state": "too_few"}
    line = body_lines(src, {eb.COUNTS_KEY: {"glassdoor": {"new": 1, "empty": 1,
                                                          "state": "jump"}}})[0]
    assert line["state"] == "too_few" and line["warn"] is None
    assert "too few to tell" in line["text"]
    # an unknown state or a jump with nothing empty is rebuilt to ok
    assert eb.clean_entry({"new": 9, "empty": 0, "state": "jump"})["state"] == "ok"
    assert eb.clean_entry({"new": 9, "empty": 3, "state": "boom"})["state"] == "ok"
    for counts in ([1], "x", None, {eb.COUNTS_KEY: [1]}, {eb.COUNTS_KEY: {"": {}, "a" * 99: {}}}):
        assert body_lines(src, counts) == []


async def test_the_runner_keeps_warning_while_a_board_stays_blocked(sm, monkeypatch):
    batches = [[_post(r * 100 + i, empty=True) for i in range(20)] for r in range(3)]
    runs = await _run(sm, monkeypatch, batches)
    assert [c[eb.COUNTS_KEY]["glassdoor"]["state"] for c in runs] == ["jump"] * 3


def test_small_blocked_runs_cannot_hide_a_later_block():
    """Review: runs of 3 of 3 empty pooled to a usual share of 1.0, so 10 of 10 and 20 of 20
    read ok. Too few runs, jumped runs and already-high runs never make a usual share."""
    history: list[dict] = []
    seen = []
    for new, empty in [(3, 3), (3, 3), (10, 10), (20, 20), (20, 20)]:
        r = eb.read_share(new, empty, history)
        history.append(r.as_counts())
        seen.append(r.state)
    assert seen == ["too_few", "too_few", "jump", "jump", "jump"]
    assert eb.usual_share([{"new": 3, "empty": 3}, {"new": 3, "empty": 3}]) is None
    assert eb.usual_share([{"new": 3, "empty": 0}, {"new": 3, "empty": 0}]) is None  # too few


def test_a_fully_empty_run_of_five_or_more_warns_whatever_the_history_was():
    """The property behind "never goes silent": over many histories, including ones made of
    blocked, tiny and calm runs, a run with every role empty always warns."""
    import itertools

    runs = [(3, 3), (3, 0), (5, 5), (10, 10), (20, 20), (20, 0), (20, 2), (40, 19), (40, 20),
            (6, 3), (100, 99)]
    for size in (1, 2, 3):
        for combo in itertools.product(runs, repeat=size):
            history: list[dict] = []
            for new, empty in combo:
                history.append(eb.read_share(new, empty, history).as_counts())
            for new in (5, 6, 20, 100):
                assert eb.read_share(new, new, history).state == "jump", (combo, new)
    assert eb.read_share(4, 4, []).state == "too_few"  # 4 roles never warns
    assert eb.read_share(5, 4, []).state == "jump"
    # A run at the half mark never becomes the usual share.
    assert eb.usual_share([{"new": 10, "empty": 6, "state": "ok"}]) is None


def test_the_report_script_counts_per_source_and_week_and_never_writes(tmp_path):
    import sqlite3
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "scripts"))
    import empty_body_report as rep

    db = tmp_path / "copy.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE source (id INTEGER PRIMARY KEY, name TEXT, kind TEXT)")
    con.execute("CREATE TABLE opportunity (id INTEGER PRIMARY KEY, source_id INT, "
                "first_seen_at TEXT, raw TEXT, description_md TEXT)")
    con.execute("INSERT INTO source VALUES (1, 'Invented search', 'jobspy')")
    rows = [(1, "2026-11-02T10:00:00+00:00", '{"site": "glassdoor"}', None)] * 3
    rows += [(1, "2026-11-02T10:00:00+00:00", '{"site": "glassdoor"}', "Real text.")] * 2
    rows += [(1, "2026-11-10T10:00:00+00:00", '{"site": "indeed"}', "<br/>")]
    for r in rows:
        con.execute("INSERT INTO opportunity (source_id, first_seen_at, raw, description_md) "
                    "VALUES (?,?,?,?)", r)
    con.commit()
    con.close()
    before = db.read_bytes()
    out = rep.report(db)
    assert ("2026-W45", "Invented search / glassdoor", 5, 3) in out
    assert ("2026-W46", "Invented search / indeed", 1, 1) in out
    assert db.read_bytes() == before
    # a read-only handle refuses a write
    ro = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO source VALUES (2, 'x', 'y')")
    ro.close()
    assert rep.main(["x"]) == 2
