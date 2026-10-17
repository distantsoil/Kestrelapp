"""A deterministic fake LLM for the P10 trigger-classify pipeline: no HTTP, a schema-valid
P10Trigger routed by directives embedded in the signal text (ORG:/EVENT:/CONF:/REL:), plus invalid
JSON for the quarantine path."""

from __future__ import annotations

import json
import re

from kestrel.config import Settings
from kestrel.llm.client import ChatResult

_ORG = re.compile(r"ORG:([^\s]+)")
_EVENT = re.compile(r"EVENT:([a-z_]+)")
_CONF = re.compile(r"CONF:([0-9.]+)")
_REL = re.compile(r"REL:([01])")


def p10_payload(text: str) -> str:
    if "BADJSON" in text:
        return "this is not valid json {"
    org = (_ORG.search(text) or [None, "Unknown Co"])[1]
    event = (_EVENT.search(text) or [None, "other"])[1]
    conf = float((_CONF.search(text) or [None, "0.3"])[1])
    rel = (_REL.search(text) or [None, "1"])[1] == "1"
    return json.dumps(
        {
            "is_relevant": rel,
            "event_type": event,
            "org_name": org,
            "event_date": None,
            "evidence_quote": "verbatim evidence sentence" if rel else "no supporting sentence",
            "confidence": conf,
            "implied_need": "interim IT leadership" if rel else "unclear",
            "urgency_window_days": 90,
            "reasoning": "test",
        }
    )


class FakeP10LLM:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def complete(
        self,
        *,
        tier: str,
        messages: list[dict],
        schema=None,
        temperature=None,
        max_tokens=None,
        schema_name: str = "response",
        timeout: float = 180.0,
        allow_external: bool = False,
    ) -> ChatResult:
        text = "\n".join(m.get("content", "") for m in messages)
        return ChatResult(
            content=p10_payload(text),
            prompt_tokens=5,
            completion_tokens=10,
            latency_ms=1,
            model=self.settings.llm.tiers[tier].model,
        )
