"""P11 negotiation scripts: the operator's walk-away is echoed back unchanged and reaches the prompt
verbatim (the model never adjusts it), a script whose justification rests on no supplied proof point
is flagged, and the competing-offer scenario is only scripted when a real competing offer exists.
Fully offline: the LLM is a capturing fake."""

from __future__ import annotations

import json

from kestrel.config import get_settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Offer, Opportunity, Org, Pursuit, Source
from kestrel.llm.client import ChatResult
from kestrel.negotiation.scripts import draft_scripts

_PROOF_POINTS = [
    {"claim": "Cut infrastructure costs by 30 percent", "evidence": "FY23 budget", "metric": "30%"}
]

_P11 = {
    "situation_read": "Below target but movable.",
    "scripts": [
        {
            "scenario": "counter_below_target",
            "script": "Given the scope, I was targeting 260.",
            # Grounded: overlaps the supplied proof point (cut/infrastructure/costs).
            "justification_basis": "I cut infrastructure costs by 30 percent in my last role.",
            "likely_pushback": "Budget is fixed.",
            "response_to_pushback": "Then let us talk equity.",
        },
        {
            "scenario": "title_level",
            "script": "I would want the VP title from day one.",
            # Ungrounded: rests on nothing supplied (no proof point, no market data, no competitor).
            "justification_basis": "Everyone at this seniority simply expects it.",
            "likely_pushback": "Title needs sign-off.",
            "response_to_pushback": "Understood, when can you confirm.",
        },
    ],
    "leverage_inventory": ["scarce skill set"],
    "risks": ["pushing title too hard"],
}


class _CapturingClient:
    def __init__(self, payload: dict) -> None:
        self.settings = get_settings()
        self._payload = payload
        self.sent: list[dict] = []

    async def complete(self, *, tier, messages, schema=None, schema_name="response", **_):
        self.sent = messages
        return ChatResult(content=json.dumps(self._payload), prompt_tokens=1, completion_tokens=1,
                          latency_ms=1, model=self.settings.llm.tiers[tier].model)


async def _seed_pursuit(sm) -> int:
    async with sm() as s:
        org = Org(name="Acme", normalized_name="acme", size_band="mid")
        src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
        s.add_all([org, src])
        await s.flush()
        opp = Opportunity(
            track="employment", opp_type="job_posting", org_id=org.id, source_id=src.id,
            title="VP of IT", org_name="Acme", url="https://x/1", state="CA",
        )
        s.add(opp)
        await s.flush()
        p = Pursuit(opportunity_id=opp.id, track="employment", status="offer")
        s.add(p)
        await s.flush()
        pid = p.id
        s.add(Offer(pursuit_id=pid, base=240000, bonus_target_pct=20, level_title="Director",
                    status="written", received_at=utcnow_iso()))
        await s.commit()
        return pid


async def test_walk_away_unchanged_and_unsupported_script_flagged(sm):
    pid = await _seed_pursuit(sm)
    client = _CapturingClient(_P11)
    walk = "I do not go below a 250k base; below that I walk."

    async with sm() as s:
        result = await draft_scripts(s, client, pid, walk_away=walk, proof_points=_PROOF_POINTS)

    assert result is not None
    # The operator's walk-away is echoed back exactly and reached the model verbatim.
    assert result.walk_away == walk
    user_msg = next(m["content"] for m in client.sent if m["role"] == "user")
    assert walk in user_msg

    # The grounded counter is supported; the title script that rests on nothing supplied is flagged.
    by_scenario = {sc["scenario"]: sc for sc in result.scripts}
    assert by_scenario["counter_below_target"]["supported"] is True
    assert by_scenario["title_level"]["supported"] is False
    assert "title_level" in result.unsupported_scenarios
    assert "counter_below_target" not in result.unsupported_scenarios


async def test_competing_offer_scenario_absent_without_a_real_competing_offer(sm):
    pid = await _seed_pursuit(sm)  # no other open offers exist
    client = _CapturingClient({**_P11, "scripts": []})

    async with sm() as s:
        await draft_scripts(s, client, pid, walk_away="floor 250k", proof_points=_PROOF_POINTS)

    # With no real competing offer, the competing-offer scenario is never even sent (no bluffs).
    user_msg = next(m["content"] for m in client.sent if m["role"] == "user")
    assert "Using a competing offer" not in user_msg


async def test_explicit_competing_scenario_is_still_suppressed_without_a_real_offer(sm):
    # The no-bluff guard must hold even when the caller passes scenarios explicitly.
    pid = await _seed_pursuit(sm)  # no other open offers
    returned = {
        "scenario": "competing_offer",
        "script": "I have another offer at 280.",
        "justification_basis": "A competing offer of 280.",
        "likely_pushback": "Can you share it?",
        "response_to_pushback": "It is confidential.",
    }
    client = _CapturingClient({**_P11, "scripts": [returned]})

    async with sm() as s:
        result = await draft_scripts(
            s, client, pid, walk_away="floor 250k",
            scenarios=["competing_offer", "counter_below_target"], proof_points=_PROOF_POINTS,
        )

    user_msg = next(m["content"] for m in client.sent if m["role"] == "user")
    assert "Using a competing offer" not in user_msg  # not sent to the model
    # And even though the fake model volunteered one, it is dropped, never shipped as a bluff.
    assert all(sc["scenario"] != "competing_offer" for sc in result.scripts)


async def test_grounding_rejects_a_market_bluff_sharing_evidence_words(sm):
    pid = await _seed_pursuit(sm)
    bluff = {
        "scenario": "counter_below_target",
        "script": "The number should be higher.",
        # Shares 'infrastructure'/'budget' with the proof point's EVIDENCE, but rests on an
        # ungrounded market claim, not the proof point's actual achievement.
        "justification_basis": "The market budget for infrastructure runs higher here.",
        "likely_pushback": "Says who?",
        "response_to_pushback": "Industry surveys.",
    }
    client = _CapturingClient({**_P11, "scripts": [bluff]})

    async with sm() as s:
        result = await draft_scripts(s, client, pid, walk_away="floor 250k",
                                     proof_points=_PROOF_POINTS)

    assert result.scripts[0]["supported"] is False  # not grounded by the claim
    assert "counter_below_target" in result.unsupported_scenarios
