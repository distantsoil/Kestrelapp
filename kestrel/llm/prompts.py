"""Prompt loader. Every prompt lives in kestrel/llm/prompts/{version}.md (the version string's '/'
flattened to '.', e.g. triage/v1 -> triage.v1.md) and is loaded BY VERSION. Prompt text is never
inlined in Python (PROMPTS.md). The shared GLOBAL_RULES block is stored once and substituted in."""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.resources import files

import yaml
from pydantic import BaseModel

from kestrel.llm.schemas import SCHEMA_BY_VERSION

_PROMPTS = files("kestrel.llm").joinpath("prompts")
_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_USER_RE = re.compile(r"(?m)^##\s+USER\s*$")
_SYSTEM_RE = re.compile(r"(?m)^##\s+SYSTEM\s*$")


@dataclass
class PromptSpec:
    id: str
    version: str
    tier: str
    temperature: float | None
    max_tokens: int | None
    system: str
    user: str
    model_cls: type[BaseModel] | None
    # Front-matter `reasoning:` (none/minimal/low/...): a hint for hosted reasoning models, used
    # only when the operator has not set an effort under Settings > Hosted.
    reasoning: str | None = None
    # Front-matter `repair_max_tokens`: the output budget of the repair after a looping reply,
    # sized to the schema so a second loop fails fast instead of running to the hosted cap
    # (D-LLM-5). None: the repair keeps the attempt's budget.
    repair_max_tokens: int | None = None


def _version_filename(version: str) -> str:
    return version.replace("/", ".") + ".md"


def global_rules() -> str:
    return _PROMPTS.joinpath("_global_rules.md").read_text(encoding="utf-8").strip()


_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def plain_writing() -> str:
    """The shared plain-writing block (issue #93), without its header comment."""
    raw = _PROMPTS.joinpath("_plain_writing.md").read_text(encoding="utf-8")
    return _COMMENT_RE.sub("", raw).strip()


def load_prompt(version: str) -> PromptSpec:
    raw = _PROMPTS.joinpath(_version_filename(version)).read_text(encoding="utf-8")
    m = _FM_RE.match(raw)
    if not m:
        raise ValueError(f"prompt {version}: missing YAML frontmatter")
    meta = yaml.safe_load(m.group(1)) or {}
    body = m.group(2)

    parts = _USER_RE.split(body, maxsplit=1)
    system = _SYSTEM_RE.split(parts[0], maxsplit=1)[-1].strip()
    user = parts[1].strip() if len(parts) > 1 else ""
    system = system.replace("{GLOBAL_RULES}", global_rules())
    system = system.replace("{PLAIN_WRITING}", plain_writing())

    return PromptSpec(
        id=str(meta.get("id", "")),
        version=str(meta.get("version", version)),
        tier=str(meta.get("tier", "")),
        temperature=meta.get("temperature"),
        max_tokens=meta.get("max_tokens"),
        system=system,
        user=user,
        model_cls=SCHEMA_BY_VERSION.get(version),
        reasoning=(str(meta["reasoning"]) if meta.get("reasoning") else None),
        repair_max_tokens=(
            int(meta["repair_max_tokens"]) if meta.get("repair_max_tokens") else None
        ),
    )


def all_versions() -> list[str]:
    return sorted(SCHEMA_BY_VERSION.keys())
