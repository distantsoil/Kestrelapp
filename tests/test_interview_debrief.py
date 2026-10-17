"""Debrief capture: writes an interaction per person in the room, adds newly discovered contacts,
updates the pursuit, and reports the predicted-vs-asked question hit rate — labeling it insufficient
under five predictions (constraint 6). Fully offline."""

from __future__ import annotations

from sqlalchemy import func, select

from kestrel.db.models import Contact, Interaction, Opportunity, Org, Pursuit, Source
from kestrel.interview.debrief import capture_debrief


async def _seed(sm) -> int:
    async with sm() as s:
        org = Org(name="Acme", normalized_name="acme")
        src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
        s.add_all([org, src])
        await s.flush()
        opp = Opportunity(
            track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
            title="VP IT", org_name="Acme", url="https://x/1",
        )
        s.add(opp)
        await s.flush()
        s.add(Pursuit(opportunity_id=opp.id, track="employment", status="interviewing"))
        await s.commit()
        return opp.id


async def test_debrief_writes_interactions_contacts_and_hit_rate(sm):
    opp_id = await _seed(sm)
    async with sm() as s:
        result = await capture_debrief(
            s, opp_id,
            questions_asked=["Tell me about a turnaround you led", "How do you handle conflict"],
            predicted_questions=[
                "Describe a turnaround", "Walk me through a conflict",
                "What is your leadership style", "Why this role", "Biggest weakness",
            ],
            panel=[{"name": "Dana Lee", "title": "CEO", "contact_type": "exec"}],
            next_steps="Panel with the CFO next week.",
            went_well="Strong on the turnaround story.",
            role_read="Real mandate, engaged CEO.",
        )

    assert result.interactions_written == 1
    assert result.contacts_added == 1
    assert result.predicted == 5 and result.asked == 2
    # Both paraphrased questions were actually asked; the three others were not.
    assert "Describe a turnaround" in result.matched
    assert "Walk me through a conflict" in result.matched
    assert result.hit_rate == 0.4  # 2 of 5
    assert result.sufficient is True

    async with sm() as s:
        contacts = (await s.execute(select(Contact))).scalars().all()
        interactions = (await s.execute(select(Interaction))).scalars().all()
        pursuit = (
            await s.execute(select(Pursuit).where(Pursuit.opportunity_id == opp_id))
        ).scalar_one()
    assert [c.name for c in contacts] == ["Dana Lee"]
    assert len(interactions) == 1
    assert interactions[0].kind == "interview_debrief"
    assert interactions[0].contact_id == contacts[0].id
    assert pursuit.next_action == "Panel with the CFO next week."
    assert "Debrief:" in (pursuit.notes_md or "")


async def test_debrief_labels_small_sample_as_insufficient(sm):
    opp_id = await _seed(sm)
    async with sm() as s:
        result = await capture_debrief(
            s, opp_id,
            questions_asked=["Tell me about a turnaround you led"],
            predicted_questions=["Describe a turnaround", "Why this role"],
        )
    assert result.predicted == 2
    assert result.hit_rate == 0.5
    assert result.sufficient is False
    assert "not yet a reliable signal" in result.note


async def test_debrief_reuses_an_existing_contact(sm):
    opp_id = await _seed(sm)
    async with sm() as s:
        opp = await s.get(Opportunity, opp_id)
        s.add(Contact(org_id=opp.org_id, name="Dana Lee", title="CEO", contact_type="exec"))
        await s.commit()

    async with sm() as s:
        result = await capture_debrief(
            s, opp_id, questions_asked=[], predicted_questions=[],
            panel=[{"name": "Dana Lee", "contact_type": "exec"}],
        )
    assert result.contacts_added == 0  # matched the existing contact, no duplicate
    async with sm() as s:
        n = (await s.execute(select(func.count()).select_from(Contact))).scalar_one()
    assert n == 1
