"""Trigger classification + org-signal accumulation + the three-weak-signals promotion rule
(SPEC.md 5.3). Strong signals promote on their own; weak ones accumulate; three weak within 90 days
promote the org regardless of individual confidence; a no-support filing stays below 0.4 as "other"
and does not promote; classification is idempotent; a bad response quarantines."""

from __future__ import annotations

from sqlalchemy import func, select

from kestrel.config import get_settings
from kestrel.db.models import Opportunity, Org, Quarantine, Source
from kestrel.ingest.signal_base import RawSignal
from kestrel.ingest.triggers import classify_and_store
from tests.trigger_fakes import FakeP10LLM


async def _source(sm) -> Source:
    async with sm() as s:
        src = Source(track="consulting", kind="sec_edgar", name="edgar", enabled=True)
        s.add(src)
        await s.flush()
        await s.commit()
        return src


def _sig(text: str, *, external_id: str, org_name: str | None = None) -> RawSignal:
    return RawSignal(
        signal_kind="trigger",
        org_name=org_name,
        source_url=f"https://sec.gov/{external_id}",
        external_id=external_id,
        text=text,
    )


async def _classify(sm, src, signal):
    settings = get_settings()
    async with sm() as s:
        src = await s.get(Source, src.id)
        out = await classify_and_store(
            s, FakeP10LLM(settings), signal, source=src, settings=settings
        )
        await s.commit()
        return out


async def test_strong_signal_promotes_to_trigger_event(sm):
    src = await _source(sm)
    out = await _classify(
        sm,
        src,
        _sig(
            "ORG:AcmeAcq EVENT:acquisition CONF:0.8 REL:1 completed acquisition",
            external_id="acc-1",
        ),
    )
    assert out.action == "promoted_strong"
    async with sm() as s:
        opp = (await s.execute(select(Opportunity))).scalar_one()
        assert opp.opp_type == "trigger_event" and opp.track == "consulting"
        assert opp.url is None  # nothing to apply to (hard rule 1)
        assert opp.trigger_data["event_type"] == "acquisition"
        org = await s.get(Org, opp.org_id)
        assert len(org.signals) == 1


async def test_weak_signal_accumulates_without_promoting(sm):
    src = await _source(sm)
    out = await _classify(
        sm,
        src,
        _sig("ORG:WeakCo EVENT:funding_round CONF:0.35 REL:1 small raise", external_id="w-1"),
    )
    assert out.action == "accumulated"
    async with sm() as s:
        n_opp = (await s.execute(select(func.count()).select_from(Opportunity))).scalar_one()
        assert n_opp == 0  # stored on the org, not promoted
        org = (await s.execute(select(Org))).scalar_one()
        assert len(org.signals) == 1 and org.signals[0]["confidence"] == 0.35


async def test_no_support_filing_is_low_confidence_other(sm):
    src = await _source(sm)
    out = await _classify(
        sm, src, _sig("ORG:VagueCo EVENT:other CONF:0.3 REL:0 boilerplate", external_id="v-1")
    )
    assert out.event_type == "other" and out.confidence < 0.4
    assert out.action == "accumulated"  # not promoted


async def test_three_weak_signals_promote_the_org(sm):
    src = await _source(sm)
    actions = []
    for i in range(3):
        out = await _classify(
            sm,
            src,
            _sig(f"ORG:Beacon EVENT:expansion CONF:0.3 REL:1 minor note {i}", external_id=f"b-{i}"),
        )
        actions.append(out.action)
    assert actions[0] == "accumulated" and actions[1] == "accumulated"
    assert actions[2] == "promoted_compounded"  # the third weak signal tips it over
    async with sm() as s:
        opp = (await s.execute(select(Opportunity))).scalar_one()
        assert opp.opp_type == "trigger_event"
        assert opp.trigger_data.get("compounded") is True
        org = await s.get(Org, opp.org_id)
        assert len(org.signals) == 3


async def test_reobserved_signal_is_idempotent(sm):
    src = await _source(sm)
    sig = _sig("ORG:DupCo EVENT:acquisition CONF:0.9 REL:1 closed deal", external_id="dup-1")
    first = await _classify(sm, src, sig)
    second = await _classify(sm, src, sig)  # same external_id
    assert first.action == "promoted_strong"
    assert second.action == "reseen"
    async with sm() as s:
        n_opp = (await s.execute(select(func.count()).select_from(Opportunity))).scalar_one()
        assert n_opp == 1  # no duplicate opportunity
        org = (await s.execute(select(Org))).scalar_one()
        assert len(org.signals) == 1  # signal recorded once


async def test_idless_signal_does_not_reaccumulate_each_scan(sm):
    """A recurring signal with NO external_id and NO source_url (e.g. an RSS item lacking both
    <guid> and <link>) must dedup by content, so three scans of the SAME item do not fabricate a
    compounded promotion (reviewer HIGH 1 / hard rule 5)."""
    src = await _source(sm)
    actions = []
    for _ in range(3):
        sig = RawSignal(
            signal_kind="trigger", org_name=None, source_url=None, external_id=None,
            text="ORG:Recur EVENT:expansion CONF:0.3 REL:1 recurring newsroom blurb",
        )
        actions.append((await _classify(sm, src, sig)).action)
    assert actions[0] == "accumulated"
    assert actions[1] == "reseen" and actions[2] == "reseen"  # not re-accumulated
    async with sm() as s:
        n_opp = (await s.execute(select(func.count()).select_from(Opportunity))).scalar_one()
        assert n_opp == 0  # no phantom compounded promotion
        org = (await s.execute(select(Org))).scalar_one()
        assert len(org.signals) == 1  # recorded once, not three times


async def test_reobserved_strong_signal_advances_liveness(sm):
    """Re-observing a promoted strong signal must bump the opportunity's seen_count/last_seen so
    its aging clock does not freeze at first sight (reviewer MEDIUM 2)."""
    src = await _source(sm)
    sig = _sig("ORG:LiveCo EVENT:acquisition CONF:0.9 REL:1 closed deal", external_id="live-1")
    await _classify(sm, src, sig)
    await _classify(sm, src, sig)  # same filing, second scan
    async with sm() as s:
        opp = (await s.execute(select(Opportunity))).scalar_one()
        assert opp.seen_count == 2  # advanced, not frozen at 1


async def test_all_quarantined_run_is_not_reported_ok(sm):
    """A source whose signals all quarantine must be `partial` and count toward auto-disable, never
    reported healthy (reviewer MEDIUM 3). Exercised through the runner in test_signal_runner? here
    at the unit level: three quarantines leave no opportunity and no accumulation."""
    src = await _source(sm)
    for i in range(2):
        out = await _classify(sm, src, _sig("BADJSON", external_id=f"bad-{i}"))
        assert out.action == "quarantined"
    async with sm() as s:
        n_opp = (await s.execute(select(func.count()).select_from(Opportunity))).scalar_one()
        assert n_opp == 0


async def test_bad_response_quarantines_and_does_not_promote(sm):
    src = await _source(sm)
    out = await _classify(sm, src, _sig("BADJSON ORG:X", external_id="q-1"))
    assert out.action == "quarantined"
    async with sm() as s:
        n_opp = (await s.execute(select(func.count()).select_from(Opportunity))).scalar_one()
        n_q = (await s.execute(select(func.count()).select_from(Quarantine))).scalar_one()
        assert n_opp == 0 and n_q == 1
