"""FAILING-FIRST tests for the review round.

Every test here is written against a defect the review round reports in this branch's own commits,
and each is expected to FAIL at `9fd7d4f`. They are written BEFORE the fixes and must be observed
red, for the right reason, before any production code is touched -- the rule this branch adopted
after `90e21dc` shipped four High bugs behind tests that passed either way.

Nothing here is fixed in this file. If a test passes before its fix, it is testing nothing. If it
fails for a reason other than the named defect, it is wrong and must be rewritten.

  O1  ratings land on the wrong offer when the two offer tables share an id   (High)  -- RED
  O2  pursuit status set directly, so no status_event history is written       (High)  -- RED
  O3  `track` taken from the form, so the benchmark reads a table the offer is not in (High) -- RED
  O4  a consulting offer contributes nothing quantitative to the comparison matrix -- PARKED
  W1  `requested_at` never reaches Tier 2 for a plain Score again (replay guard inert)   -- RED
  W2  a crashed worker is swallowed, so the run is written `done`              (Med)   -- RED
  T1  a failed transmission permanently blocks that payload, with no way back  (High)  -- RED
  T2  a post-send failure leaves the reservation `pending` forever            (Med)   -- RED
  R1  restore races a connection that reopens after reset_engine              (High)  -- not yet red
  #1  confidential mode must not PERSIST the placeholder                      -- PASSES

Two entries are deliberately NOT red, and that is the finding:

  **O4 is parked** on decision D-RR1-1 (annualize a consulting engagement and label it as such).
  The test as first written asserted only that the consulting row has a `first_year`, which pushes
  the fix toward filing a total engagement value under a first-year heading -- the exact unit
  mix-up D-RR1-1 exists to prevent. It is kept commented out, not deleted, so the wrong version
  cannot be resurrected.

  **The #1 test PASSES.** It was the open question in the handoff: does confidential mode actually
  write the placeholder to the database, or is that latent? Driving the detail page with redaction
  live leaves `Org.name` and `Opportunity.org_name` intact. The peer review said latent; this test
  confirms it empirically rather than by reading. It passes now and must keep passing, which is why
  it is in this file: a regression would turn it red.

Cloud-safe: database and web layer only, no model, no network.
"""

from __future__ import annotations

import tempfile
from itertools import count
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import make_async_engine
from kestrel.db.models import (
    EngagementOffer,
    Offer,
    Opportunity,
    Org,
    Pursuit,
    Setting,
    Source,
    StatusEvent,
)

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

_ORG_SEQ = count(1)


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _fresh_sm() -> async_sessionmaker:
    """A sessionmaker on the database the WEB app uses, so the rows seeded here are the rows the
    route actually reads. The `sm` fixture builds its own file, which the app never opens."""
    migrate.run_migrations()
    engine = make_async_engine(paths.db_path())
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed_pursuit(*, track: str = "employment", status: str = "shortlisted") -> int:
    sm = _fresh_sm()
    async with sm() as s:
        src = Source(track=track, kind="greenhouse", name=f"{track}-src", enabled=True)
        org_name = f"Acme {next(_ORG_SEQ)}"
        org = Org(name=org_name, normalized_name=org_name.lower())
        s.add_all([src, org])
        await s.flush()
        opp = Opportunity(
            track=track,
            opp_type="job_posting" if track == "employment" else "contract_posting",
            source_id=src.id,
            org_id=org.id,
            title="VP of IT",
            stage="t3_pass",
            comp_min=180000,
            comp_max=220000,
        )
        s.add(opp)
        await s.flush()
        p = Pursuit(opportunity_id=opp.id, track=track, status=status)
        s.add(p)
        await s.commit()
        return p.id


async def _rows(model=Offer) -> list:
    sm = _fresh_sm()
    async with sm() as s:
        return list((await s.execute(select(model))).scalars().all())


async def _record(*, track: str = "employment", **extra) -> int:
    pid = await _seed_pursuit(track=track)
    with _client() as c:
        r = c.post("/offers", data={"pursuit_id": pid, "track": track, **extra})
    assert r.status_code == 200
    return pid


# --------------------------------------------------------------------------------------- O1
async def test_o1_ratings_never_cross_the_two_offer_tables():
    """`Offer.id` and `EngagementOffer.id` are independent sequences, so both tables hand out id 1
    to their first row. The route resolves a bare `offer_id` against `Offer` first, so rating the
    consulting offer silently writes to the EMPLOYMENT offer.

    Posts to the route that exists today and asserts on the OUTCOME, not on a URL shape: the rated
    offer must change and the other must not. Any fix that resolves the ambiguity (track in the
    path, track in the form, a lookup across both tables) satisfies this; none is prescribed.
    """
    await _record(track="employment", base="185000")
    await _record(track="consulting", day_rate="1500", days_committed="120")

    emp = (await _rows(Offer))[0]
    con = (await _rows(EngagementOffer))[0]
    assert emp.id == con.id, "test needs the id collision to be real"

    with _client() as c:
        r = c.post(
            f"/offers/{con.id}/qual",
            data={"qual_level_scope": "4", "track": "consulting"},
        )
    assert r.status_code == 200, f"rating a valid offer was refused: {r.status_code} {r.text[:200]}"

    emp_after = (await _rows(Offer))[0]
    con_after = (await _rows(EngagementOffer))[0]
    rated = [o for o in (emp_after, con_after) if o.qual_json]
    assert len(rated) == 1, f"exactly one offer may carry the rating, got {rated}"
    assert con_after.qual_json == {"level_scope": 4.0}, (
        "the offer the operator rated did not receive the rating; it landed on its id twin"
    )
    assert emp_after.qual_json is None, "a different offer sharing this id was overwritten"


# --------------------------------------------------------------------------------------- O2
async def test_o2_recording_an_offer_uses_the_sanctioned_status_path():
    """`record_offer` sets `pursuit.status` directly instead of going through
    `pursuit_board.move_to_status`, the one sanctioned way a card changes. Two distinct defects,
    so two assertions:

      (a) the status actually moves for a status that IS in the move list, and
      (b) every move leaves a `status_event`, because a status with no history is not a transition.

    (a) is asserted on `shortlisted` rather than `applied`, because current code only moves four
    statuses and does not touch `applied` -- seeding `applied` would have failed for the wrong
    reason, proving nothing about the missing history row.
    """
    pid = await _seed_pursuit(track="employment", status="shortlisted")
    with _client() as c:
        r = c.post("/offers", data={"pursuit_id": pid, "track": "employment", "base": "185000"})
    assert r.status_code == 200

    sm = _fresh_sm()
    async with sm() as s:
        pursuit = await s.get(Pursuit, pid)
        assert pursuit.status == "offer", (
            f"the pursuit status did not move: still {pursuit.status!r}"
        )
        events = (
            await s.execute(select(StatusEvent).where(StatusEvent.pursuit_id == pid))
        ).scalars().all()
        assert events, "the pursuit changed status with no status_event written"


# --------------------------------------------------------------------------------------- O3
async def test_o3_the_puruit_track_decides_where_the_offer_is_filed():
    """D-RR1-2: the pursuit is the thing being negotiated, so `pursuit.track` is authoritative and
    the posted `track` is ignored.

    `benchmark_offer` and `_latest_offer` both pick the table from `pursuit.track`. If the offer is
    filed under whatever the form select said, a consulting pursuit's offer lands in the employment
    table and the benchmark never reads it -- the offer is invisible.

    The form defaults to employment, which is exactly what the UI posts unless changed.
    """
    pid = await _seed_pursuit(track="consulting", status="scoping")
    with _client() as c:
        r = c.post("/offers", data={"pursuit_id": pid, "track": "employment", "base": "185000"})
    assert r.status_code == 200

    assert not await _rows(Offer), "a consulting pursuit's offer was filed as employment"
    assert await _rows(EngagementOffer), "the offer was not filed on the pursuit's own track"


# --------------------------------------------------------------------------------------- W4
# D-RR1-4: three upload routes read an UploadFile and never close it. `-W default` surfaced 41
# ResourceWarnings that were first written off as harness noise; they are the app. An unclosed
# SpooledTemporaryFile that has rolled past its in-memory threshold holds an OS file handle, which
# on Windows is a sharing violation -- the same class as the restore lock bug (R1).


def _spy_on_upload_close(monkeypatch) -> list[str]:
    """Record every UploadFile the app closes, by filename.

    Spying on `UploadFile.close` is the observable contract. Asserting on ResourceWarning instead
    would depend on GC timing and would be exactly the kind of indirect, flake-prone assertion
    that let this leak be misdiagnosed in the first place.
    """
    from starlette.datastructures import UploadFile

    closed: list[str] = []
    original = UploadFile.close

    async def spy(self):  # noqa: ANN001 - matching the method being patched
        closed.append(self.filename or "")
        await original(self)

    monkeypatch.setattr(UploadFile, "close", spy)
    return closed


def test_w4_import_profile_closes_the_upload(monkeypatch):
    from tests.test_backup_page import _client

    closed = _spy_on_upload_close(monkeypatch)
    with _client() as c:
        r = c.post(
            "/backup/import-profile",
            files={
                "profile_file": (
                    "profile.yml",
                    b"employment:\n  query: x\n",
                    "application/yaml",
                )
            },
        )
    assert r.status_code == 200
    assert closed == ["profile.yml"], (
        f"the route read the upload and dropped the reference without closing it, leaving the "
        f"spooled temp file open: closed={closed}"
    )


def test_w4_restore_from_upload_closes_the_upload(monkeypatch):
    from tests.test_backup_restore import _client

    closed = _spy_on_upload_close(monkeypatch)
    with _client() as c:
        # An invalid archive is enough: the point is the read path, and a refusal is the cheap
        # case that must still close.
        r = c.post(
            "/settings/restore",
            files={"backup_file": ("not-a-zip.zip", b"just some bytes", "application/zip")},
        )
    assert r.status_code == 200
    assert closed == ["not-a-zip.zip"], (
        f"restore_from_upload read the upload without closing it: closed={closed}"
    )


# --------------------------------------------------------------------------------------- T3
# D-RR1-3: migration 0017 DELETED duplicate frontier_transmission rows so the unique index could
# be created. Those duplicates are the audit record of the double-egress incident the migration
# exists to close -- each carries its own cost_usd and evaluation_id. This test fails against the
# delete and passes only once the duplicates are PRESERVED.


async def test_t3_migration_preserves_duplicate_transmission_rows(tmp_path):
    """0017 must rewrite duplicates, not delete them.

    The original reasoning was "a duplicate can only exist from the race this migration closes".
    That is true, and it does not address that the race DID happen in the field. Deleting the rows
    destroys the only evidence that it did.
    """
    from alembic import command
    from sqlalchemy import create_engine, text

    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.db.models import Opportunity

    db = tmp_path / "kestrel.db"
    with paths.resource_path(paths.packaged_migrations()) as mig_dir:
        cfg = migrate._config(str(mig_dir), db)
        command.upgrade(cfg, "0016")  # before the unique index existed

    engine = create_engine(f"sqlite:///{db.as_posix()}")
    try:
        with engine.begin() as conn:
            # Via the table metadata, so the column defaults the model declares are applied
            # rather than this test having to know which NOT NULL columns exist at 0016.
            conn.execute(
                Opportunity.__table__.insert().values(
                    id=1, track="employment", opp_type="full_time", title="Staff Engineer"
                )
            )
            conn.execute(
                Opportunity.__table__.insert().values(
                    id=2, track="employment", opp_type="full_time", title="Platform Engineer"
                )
            )
            # A second duplicate group on a DIFFERENT opportunity, to prove the ranks are
            # computed per (opportunity_id, payload_sha256) and do not run continuously across
            # the table.
            for i in (1, 2):
                conn.execute(
                    text(
                        "INSERT INTO frontier_transmission "
                        "(opportunity_id, created_at, provider, base_url, model, confidential, "
                        " payload, payload_sha256, prompt_tokens_estimate, status, cost_usd) "
                        "VALUES (2, :ts, 'frontier', 'https://x/v1', 'm', 0, '{}', 'zzz999', "
                        "10, 'sent', 9.99)"
                    ),
                    {"ts": f"2026-10-18T00:00:0{i}+00:00"},
                )
            # Three rows sharing one payload hash: the double-egress race. Distinct costs,
            # because each of these was a real send the provider billed for.
            for i, cost in enumerate([0.11, 0.12, 0.13], start=1):
                conn.execute(
                    text(
                        "INSERT INTO frontier_transmission "
                        "(opportunity_id, created_at, provider, base_url, model, confidential, "
                        " payload, payload_sha256, prompt_tokens_estimate, status, cost_usd) "
                        "VALUES (1, :ts, 'frontier', 'https://x/v1', 'm', 0, '{}', :sha, 10, "
                        " 'sent', :cost)"
                    ),
                    {"ts": f"2026-10-17T00:00:0{i}+00:00", "sha": "abc123", "cost": cost},
                )
            before = conn.execute(
                text("SELECT COUNT(*) FROM frontier_transmission")
            ).scalar()
        assert before == 5, f"fixture failed to seed five rows in two duplicate groups: {before}"

        with paths.resource_path(paths.packaged_migrations()) as mig_dir:
            command.upgrade(migrate._config(str(mig_dir), db), "0017")

        with engine.connect() as conn:
            after = conn.execute(text("SELECT COUNT(*) FROM frontier_transmission")).scalar()
            rows = conn.execute(
                text(
                    "SELECT opportunity_id, payload_sha256, cost_usd FROM frontier_transmission "
                    "ORDER BY opportunity_id, id"
                )
            ).all()
    finally:
        engine.dispose()

    assert after == 5, (
        f"0017 DELETED {before - after} of the duplicate transmission rows. They are the audit "
        f"record of the double-egress incident -- each has its own cost and evaluation_id -- and "
        f"this rewrite is irreversible once a user's database has run it."
    )
    group1 = [r[1] for r in rows if r[0] == 1]
    group2 = [r[1] for r in rows if r[0] == 2]

    # Every row survives, and the unique index is satisfiable, so the hashes must now differ.
    all_hashes = [r[1] for r in rows]
    assert len(set(all_hashes)) == 5, f"duplicates still collide after 0017: {all_hashes}"

    # The EARLIEST row keeps the clean hash: it is the one transmit_escalation's replay guard
    # looks up by exact payload_sha256. Suffixing it instead would break the guard it exists for.
    assert group1[0] == "abc123", (
        f"the earliest row must keep the unsuffixed hash, since that is the one the replay guard "
        f"matches on: got {group1[0]!r}"
    )
    # Exact suffixes, so the ranking is proven rather than merely "starts with -dup".
    assert group1 == ["abc123", "abc123-dup2", "abc123-dup3"], (
        f"duplicates must be suffixed in id order starting at -dup2: {group1}"
    )
    # Ranks restart per group; they must not run continuously across the table.
    assert group2 == ["zzz999", "zzz999-dup2"], (
        f"the second group must rank from its own earliest row, not continue the first: {group2}"
    )
    assert sorted(r[2] for r in rows if r[0] == 1) == [0.11, 0.12, 0.13], (
        "per-send costs must survive: they are the evidence the operator was billed twice"
    )


# --------------------------------------------------------------------------------------- O4
# O4 WAS PARKED and the park was wrong by the time it shipped. The original test asserted a
# consulting row merely HAD a `first_year`, which would have been satisfied by filing a total
# engagement value under a first-year heading -- the exact unit mix-up D-RR1-4 records on
# OfferBenchmark.offer_first_year. It was parked pending D-RR1-1 with the note "no basis field to
# assert on", and then the implementation landed in the SAME commit that left the test skipped.
# A review caught the consequence: an annualization nobody was asserting.
#
# The replacement below asserts both halves, because only the pair is correct.


async def test_o4_a_consulting_offer_is_annualized_and_labelled_as_such():
    """A consulting offer joins the matrix on an ANNUAL scale, and says so.

    The original O4 test asserted only `first_year is not None`. That assertion is satisfied by the
    bug as easily as by the fix: a $50,000 six-week engagement has a `first_year` whether it holds
    the total ($50k) or the annualized figure ($433k), so the test could not tell a correct
    implementation from a unit mix-up. Asserting the NUMBER and the LABEL together is what makes it
    a test.
    """
    from kestrel.db.models import EngagementOffer
    from kestrel.negotiation import compare

    # The worked example from D-RR1-1: $10k/day x 5 days = $50,000 over six weeks, which scales to
    # about $433,000 on a 52-week year. The number is the point: the original test would have
    # passed with 50000 here too.
    engagement = EngagementOffer(pursuit_id=1, day_rate=10000, days_committed=5, duration_weeks=6)
    con = compare.offer_comp_from(
        offer=engagement, offer_id=7, label="Advisory retainer", track="consulting", qual={}
    )

    expected = 50000 * 52 / 6
    assert con.first_year == pytest.approx(expected), (
        f"a consulting offer must be scaled to an annual figure (52 / duration_weeks), not filed "
        f"as its total engagement value: got {con.first_year}, want ~{expected}"
    )
    assert con.basis == "consulting_annualized"
    label = con.basis_label()
    assert "annualized" in label.lower() and "not guaranteed" in label.lower(), (
        f"the label must tell the reader the figure is an annualized engagement and not a promise "
        f"of income: got {label!r}"
    )


async def test_o4_an_engagement_without_duration_is_left_unvalued_rather_than_guessed():
    """No `duration_weeks` means no defensible denominator, so the row stays unvalued.

    A 6-week engagement annualizes to roughly $433k, which reads like a salary. Guessing the
    denominator from `days_committed` would invent a 5-day week and inflate short engagements
    further, so the implementation returns None and the basis says `unknown`. Asserted because a
    plausible "fix" for O4 would be to fall back to another field and quietly reintroduce the
    misleading number.
    """
    from kestrel.db.models import EngagementOffer
    from kestrel.negotiation import compare

    engagement = EngagementOffer(
        pursuit_id=1, day_rate=10000, days_committed=5, duration_weeks=None
    )
    con = compare.offer_comp_from(
        offer=engagement, offer_id=7, label="Advisory retainer", track="consulting", qual={}
    )

    assert con.first_year is None, (
        "without duration_weeks the engagement must not be given a number that reads as a salary"
    )
    assert con.basis == "unknown"
    assert con.basis_label() == "not recorded"


async def test_o4_an_employment_offer_is_labelled_annual_cash_comp():
    """The label distinguishes the two bases, so the column is not read as one thing.

    The matrix ranks employment cash comp and an annualized engagement in the same quantitative
    column. If both rows render identically the reader has no way to know one of them is an
    arithmetic annualization of six weeks of work rather than a year of salary.
    """
    from kestrel.db.models import Offer
    from kestrel.negotiation import compare

    offer = Offer(pursuit_id=1, base=185000, bonus_target_pct=10)
    con = compare.offer_comp_from(
        offer=offer, offer_id=3, label="Staff Engineer", track="employment", qual={}
    )

    assert con.basis == "employment"
    assert con.basis_label() == "annual cash comp"
    assert con.first_year == pytest.approx(203500)  # 185000 + 10% target bonus


# --------------------------------------------------------------------------------------- W1
async def test_w1_plain_score_again_reaches_tier2_with_rescore_and_requested_at(sm):
    """Tier 2's replay guard needs BOTH halves: the worker forwards `requested_at` only
    `if payload.get("rescore")`, and the handler runs the guard only `if requested_at:`.
    `tier1.py` sets `t2_extra["rescore"]` ONLY inside `if model_override:`, so for a plain Score
    again the enqueued task carries `requested_at` without `rescore` -- the worker drops it, the
    guard never runs, and a re-leased task scores the role again. That is the replay `7116559`
    claimed to close."""
    from kestrel.config import get_settings
    from kestrel.db.models import Task
    from kestrel.pipeline.tier1 import handle_t1
    from tests.pipeline_fakes import FakeLLM

    oid = await _seed_opportunity(sm, stage="t1_reject")
    requested_at = "2026-10-17T12:00:00+00:00"

    settings = get_settings()
    out = await handle_t1(
        sm,
        FakeLLM(settings),
        settings,
        opportunity_id=oid,
        profiles=_profiles(),
        rescore=True,
        requested_at=requested_at,
    )
    assert out.action in ("t1_pass", "promoted"), f"tier1 did not score: {out}"

    async with sm() as s:
        # Task has no opportunity_id column; it lives inside the JSON payload.
        t2s = (
            await s.execute(select(Task).where(Task.type == "t2"))
        ).scalars().all()
        mine = [dict(t.payload or {}) for t in t2s]
        mine = [p for p in mine if p.get("opportunity_id") == oid]
        assert mine, "no Tier 2 task was enqueued, so the test proves nothing"
        payload = mine[0]

    assert payload.get("rescore") is True, (
        "the Tier 2 payload has no `rescore`, so worker.py never forwards `requested_at` and "
        f"tier2.py's replay guard cannot run. Payload was {payload}"
    )
    assert payload.get("requested_at") == requested_at


# --------------------------------------------------------------------------------------- T1
async def test_t1_a_provider_that_returned_an_http_error_is_not_retryable(monkeypatch):
    """A real HTTP error is NOT a never-left failure.

    A review found this by reading the code my first T1 fix depended on: it decided
    retryability from the `request error:` prefix, but `run_guarded` writes that same prefix for
    EVERY exception that is not a `ModelDeclined` -- so `ProviderError` (401/429/500) and a
    timeout waiting for the reply both looked retryable, and the payload egressed twice.

    This test drives a REAL `ProviderError` through `run_guarded`, because the guard test that
    missed it hand-seeded `error="HTTP 500"`, a string the real code never writes.
    """
    from kestrel.llm.guard import run_guarded
    from kestrel.pipeline import tier3

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    async with _fresh_sm()():
        res = await run_guarded(
            _RaisesProviderError(),
            tier=tier3._FRONTIER_TIER,
            messages=[{"role": "user", "content": "x"}],
            model_cls=_p3_model(),
            allow_external=True,
        )
    assert res.error and res.error.startswith("request error:"), (
        "this test is only meaningful while an HTTP error DOES get the request error label; if "
        "that changes, the retry rule no longer depends on the string and this can be revisited"
    )
    assert res.egressed is True, (
        "an HTTP 500 means the provider received the whole payload; treating it as never-left "
        "would re-send operator data off-machine"
    )


async def test_t1_a_connection_failure_is_retryable_through_the_real_guard(monkeypatch):
    """A refused connection DID never leave the machine, and says so explicitly."""
    from kestrel.llm.guard import run_guarded
    from kestrel.pipeline import tier3

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    async with _fresh_sm()():
        res = await run_guarded(
            _FailsTransport(),
            tier=tier3._FRONTIER_TIER,
            messages=[{"role": "user", "content": "x"}],
            model_cls=_p3_model(),
            allow_external=True,
        )
    assert res.egressed is False, (
        "a refused connection means nothing reached the provider, so the payload is safe to "
        "re-send and the operator must not be locked out of it"
    )


async def test_t1_an_http_error_records_a_failed_row_that_cannot_be_retried(monkeypatch):
    """END TO END through `transmit_escalation`, not just `run_guarded`.

    The two guard-level tests above prove the `egressed` field is computed correctly, and the
    retry test proves a refused connection is re-sent. Neither proves the DECISION: that a
    provider-received failure is refused a second time. That is the bug a review
    found, and it survived because the only test covering it
    (`test_replay_guard_refuses_after_a_failed_send_too`) hand-seeded `error="HTTP 500"`, a
    string the real code never writes, so it exercised a row the application cannot produce.
    """
    from kestrel.config import get_settings
    from kestrel.db.models import FrontierTransmission
    from kestrel.pipeline import tier3

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    settings = get_settings()
    assert not settings.confidential_mode, "the token must be deterministic for this test"

    pid = await _seed_pursuit(track="employment", status="offer")
    opp_id = await _opportunity_id_for(pid)

    async with _fresh_sm()() as s:
        preview = await tier3.build_escalation_preview(s, settings, opp_id)

    # First attempt: the provider returns a real HTTP error.
    async with _fresh_sm()() as s:
        await tier3.transmit_escalation(
            s, _RaisesProviderError(), settings, opp_id,
            confirmation_token=preview.confirmation_token,
        )

    async with _fresh_sm()() as s:
        row = (await s.execute(select(FrontierTransmission))).scalars().one()
        assert row.status == "failed"
        assert row.egressed is True, (
            "an HTTP error means the provider received the payload, so the row must say so"
        )

    # The operator re-previews and confirms. The token is the same, and this must be REFUSED:
    # the data already egressed, and a retry would send it a second time.
    async with _fresh_sm()() as s:
        preview2 = await tier3.build_escalation_preview(s, settings, opp_id)
    assert preview2.confirmation_token == preview.confirmation_token

    async with _fresh_sm()() as s:
        second = await tier3.transmit_escalation(
            s, _Succeeds(), settings, opp_id, confirmation_token=preview2.confirmation_token
        )
    assert second.sent is False, (
        "a payload the provider already received was sent a SECOND time. This is the double-egress "
        "the whole reservation exists to prevent."
    )


async def test_t1_a_failed_transmission_can_be_retried_by_the_operator(monkeypatch):
    """A `failed` row blocks that payload forever. Without confidential redaction the token is
    deterministic, so a re-preview yields the SAME token and the retry is refused -- while the
    message tells the operator to "open a fresh preview to escalate again", which cannot help.

    Asserted on the audit row's status and on whether the call was REFUSED, not on
    `result.sent`: `sent` means "recorded a transmission attempt", so asserting it is True on a
    transport failure reads backwards. The retry uses a client that SUCCEEDS, so the test proves a
    retry can actually egress once, rather than failing twice and proving nothing.
    """
    from kestrel.config import get_settings
    from kestrel.db.models import FrontierTransmission
    from kestrel.pipeline import tier3

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")  # available, so ONLY the token gates it
    # Confidential mode OFF, stated explicitly: it is what makes the token deterministic, and the
    # whole point of the defect is that a re-preview then yields the same one.
    settings = get_settings()
    assert not settings.confidential_mode, "this test needs confidential mode off"

    pid = await _seed_pursuit(track="employment", status="offer")
    opp_id = await _opportunity_id_for(pid)

    async with _fresh_sm()() as s:
        preview = await tier3.build_escalation_preview(s, settings, opp_id)

    # First attempt fails at the transport: nothing left the machine.
    async with _fresh_sm()() as s:
        await tier3.transmit_escalation(
            s, _FailsTransport(), settings, opp_id,
            confirmation_token=preview.confirmation_token,
        )

    async with _fresh_sm()() as s:
        rows = (await s.execute(select(FrontierTransmission))).scalars().all()
        assert len(rows) == 1, "the failed attempt was not recorded at all"
        assert rows[0].status == "failed", f"expected a failed row, got {rows[0].status!r}"

    # The operator previews again and confirms. Nothing egressed, so this must be allowed.
    async with _fresh_sm()() as s:
        preview2 = await tier3.build_escalation_preview(s, settings, opp_id)
    assert preview2.confirmation_token == preview.confirmation_token, (
        "this test needs the token to be deterministic, which is why the retry is blocked"
    )

    async with _fresh_sm()() as s:
        second = await tier3.transmit_escalation(
            s, _Succeeds(), settings, opp_id, confirmation_token=preview2.confirmation_token
        )
    assert not second.error, (
        f"a transmission that never left the machine cannot be retried: {second.error}"
    )
    async with _fresh_sm()() as s:
        rows = (await s.execute(select(FrontierTransmission))).scalars().all()
        assert rows[-1].status == "sent", "the retry did not actually egress"


# --------------------------------------------------------------------------------------- W2
async def test_w2_a_crashed_worker_marks_the_run_failed_not_done(sm, monkeypatch):
    """`_gather_workers` logs a crashed sibling and returns. Nothing propagates, so
    `finish_run` writes `done` and the run reads as a clean pass even though a worker died
    mid-drain. A run that lost a worker must not be reported as `done`."""
    from kestrel.config import get_settings
    from kestrel.pipeline import worker as worker_mod

    async def _boom(idx):
        raise RuntimeError("worker died mid-drain")

    worker = worker_mod.Worker(sm, _FailsTransport(), get_settings())
    stats = worker_mod.RunStats()
    # Returns rather than raising: a crashed worker degrades the tier, it does not abort the run
    # (review) -- re-raising skipped Tier 2 and embed entirely, which is worse than the
    # silent swallow it replaced.
    crashed = await worker._gather_workers(_boom, 2, "t2", stats)
    assert crashed == 2, f"both crashes must be reported: {crashed}"
    assert stats.tier2.errors == 2, (
        f"both crashed workers must be counted on the drained tier's stats: "
        f"tier2.errors={stats.tier2.errors}. Without this the run is recorded as `done` for a "
        "drain that silently lost workers."
    )


# --------------------------------------------------------------------------------------- T2
async def test_t2_a_failure_after_the_send_does_not_leave_the_row_pending(monkeypatch):
    """The reservation commits as `pending` before the payload goes out. Code after the call
    (`load_profile`, `scoring.post_process`, building the `Evaluation`) can raise, and a client
    disconnect cancels the handler. Either way the row stays `pending` forever, the token and cost
    just recorded are lost, no evaluation is written, and the operator sees a 500.

    A transmission whose send COMPLETED must be recorded as what it was, not left in flight."""
    from kestrel.config import get_settings
    from kestrel.db.models import FrontierTransmission
    from kestrel.pipeline import tier3

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    settings = get_settings()

    pid = await _seed_pursuit(track="employment", status="offer")
    opp_id = await _opportunity_id_for(pid)

    async with _fresh_sm()() as s:
        preview = await tier3.build_escalation_preview(s, settings, opp_id)

    # Make the POST-send work fail, without making the send itself fail.
    import kestrel.pipeline.tier3 as t3

    real_post = t3.scoring.post_process

    def _explode(**kw):
        raise RuntimeError("post-send scoring blew up")

    monkeypatch.setattr(t3.scoring, "post_process", _explode)
    try:
        async with _fresh_sm()() as s:
            with pytest.raises(RuntimeError):
                await tier3.transmit_escalation(
                    s, _Succeeds(), settings, opp_id,
                    confirmation_token=preview.confirmation_token,
                )
    finally:
        monkeypatch.setattr(t3.scoring, "post_process", real_post)

    async with _fresh_sm()() as s:
        rows = (await s.execute(select(FrontierTransmission))).scalars().all()
        assert len(rows) == 1
        assert rows[0].status != "pending", (
            "a row left `pending` after the payload was sent is indistinguishable from a send in "
            "flight, and permanently blocks that payload from ever being re-sent"
        )


# --------------------------------------------------------------------------------------- R1
async def test_r1_restore_refuses_while_a_connection_is_still_holding_the_database():
    """COVERS THE CONTRACT, NOT THE RACE. Read this before treating R1 as handled.

    Restore replaces `kestrel.db` and unlinks `-wal`/`-shm`. Windows refuses both while any
    connection holds the file open (`WinError 32`). `reset_engine()` disposes the app engine, but
    the worker and scheduler captured `sm = get_sessionmaker()` at startup and a disposed
    AsyncEngine silently REBUILDS its pool on next use -- so a connection can reopen while restore
    runs.

    This test asserts the OBSERVABLE CONTRACT: a restore attempted while a connection is held must
    not leave a truncated database, and must either succeed cleanly or refuse.

    It does NOT reproduce the race, and it PASSES today. A single checked-out connection does not
    model the worker and scheduler lanes re-opening the pool mid-restore, so a pass here says
    nothing about whether the race is closed. The real test is BLOCKED: see the R1 entry in
    the build log, which records the local Windows concurrency harness it needs. Do not read this
    green test as R1 being covered.
    """
    from kestrel.services import backup as backup_mod

    archive = _make_backup_zip()
    db = paths.db_path()
    migrate.run_migrations()
    before = db.read_bytes() if db.exists() else None

    # Hold a real connection open across the restore, as the worker/scheduler lanes do.
    engine = make_async_engine(db)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sm() as s:
            await s.execute(select(Opportunity).limit(1))  # a live, checked-out connection
            try:
                result = backup_mod.restore_backup(archive)
            except PermissionError:
                result = None  # refused cleanly: acceptable, and what Windows does

        assert db.exists(), "the database was removed by the restore"
        if result is not None:
            # If it "succeeded" while a connection was open, the bytes may be inconsistent.
            after = db.read_bytes()
            assert after != b"", "restore left a truncated database"
        assert before is None or db.read_bytes() != b"" or result is not None
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------------------- #1
async def test_1_confidential_mode_does_not_write_the_placeholder_to_the_database():
    """Settles the #1 question rather than assuming it. `_detail_context` mutates the live ORM
    rows (`org.name`, `opp.org_name`) to the "Show it as" text, then a later commit on the same
    session would persist it. GET routes close without committing, so it is believed to roll back --
    but that is a property of call ordering, not of the code, and one added `commit()` makes it
    real and irreversible.

    Asserts the data, from a fresh session, after driving the read path.
    """

    real_name = "Northwind Freight"
    sm = _fresh_sm()
    async with sm() as s:
        src = Source(track="employment", kind="greenhouse", name="conf-src", enabled=True)
        org = Org(name=real_name, normalized_name=real_name.lower())
        s.add_all([src, org])
        await s.flush()
        opp = Opportunity(
            track="employment", opp_type="job_posting", source_id=src.id, org_id=org.id,
            org_name=real_name, title="VP of IT", stage="t2_done", url="https://x/conf",
        )
        s.add(opp)
        await s.commit()
        opp_id, org_id = opp.id, org.id

    await _enable_confidential_mode(sm, employer=real_name)

    # Confirm the redaction is actually live before drawing any conclusion from the page: an
    # enabled-but-empty map redacts nothing, and would make this test pass for the wrong reason.
    from kestrel.web.redact import confidential

    async with sm() as s:
        enabled, mapping = await confidential(s)
    assert enabled and mapping, (
        "confidential mode is on but the employer map is empty, so nothing would be redacted"
    )

    # Drive the read path the finding describes.
    with _client() as c:
        r = c.get(f"/opportunities/{opp_id}")
    assert r.status_code == 200, r.text[:300]
    assert real_name not in r.text, (
        "the detail page did not redact the employer, so this test cannot judge persistence"
    )

    # Now read the database from a FRESH session: the placeholder must not have been persisted.
    async with sm() as s:
        org_after = await s.get(Org, org_id)
        opp_after = await s.get(Opportunity, opp_id)
    assert org_after.name == real_name, (
        f"confidential mode PERSISTED the placeholder: Org.name is now {org_after.name!r}. "
        "The redaction mutated a live ORM row and a commit flushed it."
    )
    assert opp_after.org_name == real_name, (
        f"Opportunity.org_name was rewritten to {opp_after.org_name!r}"
    )


# ------------------------------------------------------------------------------- helpers
def _make_backup_zip() -> Path:
    """A real, valid backup archive built from the current database. Synchronous."""
    from kestrel.services.backup import create_backup

    tmp = Path(tempfile.mkdtemp(prefix="r1-"))
    return create_backup(tmp)


async def _enable_confidential_mode(sm, *, employer: str) -> None:
    """Turn confidential mode on and give the map a real employer to redact.

    Both halves are required. `confidential_mode` is a settings override read through
    `load_settings_with_db`, stored in the `setting` table. The redaction MAP is built from
    document sections: `current_employer._marked` treats a section as a current-employer mark only
    when it has BOTH `org_label` and `org_label_redacted`. Without both, confidential mode is on
    but the map is empty -- which `web/redact.py` already calls out as a privacy control that
    "appears enabled and does nothing".
    """
    from kestrel.db.models import DocMaster, DocSection

    async with sm() as s:
        row = await s.get(Setting, "confidential_mode")
        if row is None:
            s.add(Setting(key="confidential_mode", value=True))
        else:
            row.value = True
        master = DocMaster(track="employment", doc_kind="resume", name="Resume")
        s.add(master)
        await s.flush()
        s.add(
            DocSection(
                doc_master_id=master.id,
                # "role" is one of current_employer.EMPLOYER_KINDS: the kinds whose org_label
                # names an employer, so it is what the map is built from.
                section_kind="role",
                title="Experience",
                org_label=employer,
                org_label_redacted="Current Employer",
                end_date="present",  # open-ended, so it reads as the CURRENT role
            )
        )
        await s.commit()


async def _seed_opportunity(sm, *, stage: str = "t1_reject") -> int:
    async with sm() as s:
        src = Source(track="employment", kind="greenhouse", name="t1-src", enabled=True)
        org = Org(name="Acme T1", normalized_name="acme t1")
        s.add_all([src, org])
        await s.flush()
        opp = Opportunity(
            track="employment",
            opp_type="job_posting",
            source_id=src.id,
            org_id=org.id,
            title="VP of IT",
            stage=stage,
            date_posted="2026-10-14",
            seen_count=1,
            description_md="Own IT leadership, budget and vendor accountability, security.",
        )
        s.add(opp)
        await s.commit()
        return opp.id


async def _opportunity_id_for(pursuit_id: int) -> int:
    async with _fresh_sm()() as s:
        p = await s.get(Pursuit, pursuit_id)
        return p.opportunity_id


def _profiles():
    from kestrel.pipeline.profile import load_profile

    return {t: load_profile(t) for t in ("employment", "consulting")}


class _FailsTransport:
    """A hosted call that never connects: nothing reaches the provider.

    A plain transport error, NOT a declined model, because the distinction is the whole point of
    T1: `run_guarded` labels a request-level failure "request error: ...", and only that class
    proves the payload never left the machine. An HTTP 500 or 401 means the provider answered, so
    the data HAS egressed and must not be re-sent."""

    async def complete(self, **kw):
        raise ConnectionRefusedError("provider unreachable")


def _p3_model():
    """The real Tier 3 response model, so `run_guarded` builds a genuine schema. `object` has no
    `model_json_schema`, and a fake would not exercise the actual call path."""
    from kestrel.pipeline import tier3

    return tier3.prompt_spec(tier3._VERSION["employment"]).model_cls


class _RaisesProviderError(_FailsTransport):
    """A real HTTP error from the provider, raised the way `LLMClient` raises it.

    The review's critical finding: this is NOT a `ModelDeclined`, so `run_guarded`'s generic
    handler gives it the same `request error:` label as a refused connection -- while the provider
    has in fact received the entire payload. Testing a hand-written `error="HTTP 500"` string
    instead of the real exception is what let the bug through the first time."""

    async def complete(self, **kw):
        from kestrel.llm.client import ProviderError

        raise ProviderError(500, "internal server error")


class _Succeeds(_FailsTransport):

    async def complete(self, **kw):
        from kestrel.config import get_settings
        from tests.pipeline_fakes import FakeLLM

        return await FakeLLM(get_settings()).complete(**kw)
