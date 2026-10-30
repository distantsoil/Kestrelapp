"""A deterministic fake LLM for the pipeline integration test: no HTTP, canned schema-valid P1/P1C
and P2/P2C responses routed by a marker embedded in the opportunity, plus deliberately invalid JSON
to exercise the quarantine paths. Track is detected from the prompt text (the consulting prompts say
"independent consultant or contractor")."""

from __future__ import annotations

import json

from kestrel.config import Settings
from kestrel.llm.client import ChatResult

MARKERS = ("LOWCONF", "KILL", "QUAR1", "QUAR2", "BLOCK", "REJECT", "SUSPECT", "DIVERGE")


def marker_of(text: str) -> str | None:
    for m in MARKERS:
        if f"[{m}]" in text:
            return m
    return None


# ------------------------------------------------------------------------------- P1 / P1C
def _p1(marker: str | None) -> str:
    if marker == "QUAR1":
        return "this is not valid json {"
    if marker == "KILL":
        return json.dumps(
            {"verdict": "kill", "confidence": 0.92, "reason": "wrong function",
             "signals": ["function_mismatch"]}
        )
    if marker == "LOWCONF":  # low confidence -> system forces keep
        return json.dumps(
            {"verdict": "kill", "confidence": 0.2, "reason": "unsure",
             "signals": ["description_too_vague"]}
        )
    return json.dumps(
        {"verdict": "keep", "confidence": 0.9, "reason": "plausible", "signals": ["level_match"]}
    )


def _p1c(marker: str | None) -> str:
    if marker == "QUAR1":
        return "not json at all"
    if marker == "KILL":
        return json.dumps(
            {"verdict": "kill", "confidence": 0.9, "reason": "outside size range",
             "signals": ["size_too_small"]}
        )
    if marker == "LOWCONF":
        return json.dumps(
            {"verdict": "kill", "confidence": 0.2, "reason": "unsure", "signals": ["buyer_unclear"]}
        )
    return json.dumps(
        {"verdict": "keep", "confidence": 0.9, "reason": "profile fit",
         "signals": ["size_fit", "sector_fit"]}
    )


# ------------------------------------------------------------------------------- P2
def _dim(score: float) -> dict:
    return {"score": score, "why": "reasoned"}


def _p2(marker: str | None) -> str:
    if marker == "QUAR2":
        return "definitely not json"
    dim_score = 1.0 if marker == "DIVERGE" else 4.2
    holistic = 5.0 if marker == "DIVERGE" else (4.8 if marker == "SUSPECT" else 4.2)
    legitimacy = {"status": "clean", "confidence": 0.9, "signals": []}
    if marker == "REJECT":
        legitimacy["status"] = "reject"
    elif marker == "SUSPECT":
        legitimacy["status"] = "suspect"
    blockers = [{"kind": "location", "detail": "onsite only"}] if marker == "BLOCK" else []
    body = {
        "blocks": {
            "a_role_summary": {"what_this_actually_is": "An IT leadership role."},
            "b_fit": {
                "matches": [
                    {"requirement": "IT leadership", "evidence": "profile", "strength": "strong"}
                ],
                "gaps": [{"requirement": "SAP", "severity": "noise"}],
            },
            "c_level": {"relative_level": "at", "scope_read": "runs the function"},
            "d_comp": {
                "estimate_confidence": "medium", "estimate_basis": "corpus",
                "vs_floor": "above_floor", "geo_adjustment_risk": False,
            },
            "e_angle": {"lead_with": "cost takeout record", "jd_citations": ["security mandate"]},
            "f_interview": [
                {
                    "likely_question": "Tell me about a turnaround",
                    "why_asked": "gap probe",
                    "star_r": {
                        "situation": "s", "task": "t", "action": "a", "result": "r",
                        "reflection": "x",
                    },
                }
            ],
        },
        "dimensions": {
            "role_fit": _dim(dim_score), "domain_match": _dim(dim_score),
            "company_context": _dim(dim_score), "comp_logistics": _dim(dim_score),
            "trajectory": _dim(dim_score),
        },
        "holistic": holistic,
        "verdict": "apply",
        "headline_reason": "strong fit",
        "blockers": blockers,
        "legitimacy": legitimacy,
        "work_auth_signal": "not_mentioned",
    }
    return json.dumps(body)


# ------------------------------------------------------------------------------- P2C
def _p2c(marker: str | None) -> str:
    if marker == "QUAR2":
        return "not json"
    dim_score = 1.0 if marker == "DIVERGE" else 4.2
    holistic = 5.0 if marker == "DIVERGE" else (4.8 if marker == "SUSPECT" else 4.2)
    legitimacy = {"status": "clean", "confidence": 0.9, "signals": []}
    if marker == "REJECT":
        legitimacy["status"] = "reject"
    elif marker == "SUSPECT":
        legitimacy["status"] = "suspect"
    blockers = [{"kind": "rate", "detail": "below floor"}] if marker == "BLOCK" else []
    body = {
        "blocks": {
            "a_situation": {
                "stated": "Org is integrating an acquisition",
                "inferred": "IT consolidation needed",
                "speculative": "possible cost pressure",
                "timing_read": "early in the 90-day window",
            },
            "b_thesis": {
                "engagement": "post-acquisition IT integration",
                "service_lines": ["post-acquisition IT integration"],
                "matches": [],
                "phases": [{"name": "assess", "outcome": "plan", "days": 20}],
                "estimated_days": 40, "estimated_weeks": 8,
            },
            "c_buyers": {
                "economic_buyer": "CFO", "influencers": ["Head of IT"], "blockers": [],
                "access_difficulty": "moderate", "access_path": "warm intro via board",
            },
            "d_economics": {
                "confidence": "medium", "basis": "corpus", "renewal_likelihood": "medium",
                "vs_floor": "above_floor",
            },
            "e_approach": {
                "opening_move": "note the integration", "to_whom": "CFO", "referencing": "the deal",
                "channel": "warm_intro", "why_now_line": "window is open",
            },
            "f_delivery": {"solo_feasible": True, "gaps": [], "effort_shape": "2 days/week"},
            "g_risk": {"status": legitimacy["status"], "signals": []},
        },
        "dimensions": {
            "engagement_fit": _dim(dim_score), "buyer_access": _dim(dim_score),
            "economics": _dim(dim_score), "delivery_feasibility": _dim(dim_score),
            "strategic_value": _dim(dim_score),
        },
        "holistic": holistic,
        "verdict": "pursue",
        "headline_reason": "clear need, reachable buyer",
        "blockers": blockers,
        "legitimacy": legitimacy,
    }
    return json.dumps(body)


class FakeLLM:
    """Drop-in for LLMClient in the pipeline handlers: same `.settings` and `.complete()` shape."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def complete(
        self, *, tier: str, messages: list[dict], schema=None, temperature=None,
        max_tokens=None, schema_name: str = "response", timeout: float = 180.0,
        allow_external: bool = False,
    ) -> ChatResult:
        text = "\n".join(m.get("content", "") for m in messages)
        consulting = "independent consultant or contractor" in text.lower()
        marker = marker_of(text)
        if tier == "tier1":
            content = _p1c(marker) if consulting else _p1(marker)
        else:
            content = _p2c(marker) if consulting else _p2(marker)
        return ChatResult(
            content=content, prompt_tokens=10, completion_tokens=20, latency_ms=1,
            model=self.settings.llm.tiers[tier].model,
        )

    async def embed(
        self, texts: list[str], *, tier: str = "embed", timeout: float = 60.0
    ) -> list[list[float]]:
        """Deterministic stand-in for the real embedding call.

        Derived from a hash of the text so identical inputs give identical vectors and different
        inputs give different ones, which is what any cosine-based assertion needs. 8 dims is
        enough for that and keeps the fixtures small; nothing in the pipeline hardcodes a
        dimension, it reads `dims` off the stored row.
        """
        import hashlib

        out: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            out.append([b / 255.0 for b in digest[:8]])
        return out
