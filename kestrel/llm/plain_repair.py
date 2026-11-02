"""Plain-writing enforcement around a schema-validated reply (issue #93, D-UI-232).

The flow, once per object: the call site has already run its prompt and holds a validated pydantic
value (hard rule 3 is untouched: nothing here reads a raw reply). `enforce` lints the named prose
fields with `kestrel.writing_check`, and when a blocking rule or an ungrounded detail is found it
asks the model ONCE, listing every problem for the whole object, to say it another way using only
what it already wrote. The new reply must pass the same schema (the call site's `run` is the same
guarded call). Only the flagged prose fields are taken from a good reply; a failed re-ask keeps the
original and writes no quarantine row. A text that still has problems is kept and carries `flags`
(shown as a small mark on stories; other surfaces record them). Nothing is ever rewritten in code,
and operator text is never passed in (the call site names only generated fields, and verbatim
operator text quoted in the output is exempt by span).

This repair is separate from the single schema repair inside the guard and does not use it up."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import ValidationError

from kestrel.writing_check import RULE_LABELS, Violation, check_object, describe

log = structlog.get_logger()

MAX_STYLE_FLAGS = 12
# The smallest num_ctx each tier is built with (models.yml). The re-ask carries the whole original
# prompt plus the reply plus the request, so it is skipped when that and the output budget do not
# fit; the original is kept with its flags.
TIER_NUM_CTX = {"tier1": 4096, "tier2": 16384, "docsel": 8192, "tier3": 20480}
# run(messages, reask=False): the call site's guarded call. With reask=True it must write NO
# quarantine row (the original reply already passed its schema; D-UI-236 amendment).
Run = Callable[..., Awaitable[Any]]
ExtraCheck = Callable[[Any], list[dict[str, str]]]
Accept = Callable[[Any, Any], bool]  # (merged value, the re-asked reply)


@dataclass
class PlainResult:
    value: Any
    flags: list[dict[str, str]] = field(default_factory=list)
    reasked: bool = False


def _flags(violations: Iterable[Violation], extra: Iterable[dict[str, str]]) -> list[dict]:
    """Style flags are capped (the dropped count is stored as one entry); grounding flags are
    never truncated."""
    style: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for f in [v.as_flag() for v in violations]:
        key = (f.get("kind", ""), f.get("field", ""), f.get("phrase", ""))
        if key not in seen:
            seen.add(key)
            style.append(f)
    grounding: list[dict[str, str]] = []
    for f in extra:
        key = (f.get("kind", ""), f.get("field", ""), f.get("phrase", ""))
        if key not in seen:
            seen.add(key)
            grounding.append(f)
    dropped = style[MAX_STYLE_FLAGS:]
    out = style[:MAX_STYLE_FLAGS]
    if dropped:
        out.append({
            "kind": "style_dropped", "field": "", "phrase": f"{len(dropped)} more",
            "count": str(len(dropped)),
        })
    return out + grounding


_PATH_TOKEN = re.compile(r"[^.\[\]]+|\[\d+\]")


def _tokens(path: str) -> list[str | int]:
    return [int(t[1:-1]) if t.startswith("[") else t for t in _PATH_TOKEN.findall(path)]


def _get(data: Any, tokens: list[str | int]) -> Any:
    for t in tokens:
        data = data[t]
    return data


def _set(data: Any, tokens: list[str | int], value: Any) -> None:
    for t in tokens[:-1]:
        data = data[t]
    data[tokens[-1]] = value


# Lists whose items carry keys that must not move away from their text: prompt version -> list
# name -> the key fields of an item. Any other list that holds a flagged field (a list of plain
# strings) is merged only when its length is unchanged.
LIST_KEYS: dict[str, dict[str, tuple[str, ...]]] = {
    "org_summary/v2": {"sentences": ("source_indexes",)},
    "negotiate/v2": {"scripts": ("scenario", "justification_basis")},
    "interview_questions/v2": {
        "questions": ("question", "category", "proof_point_key", "story_id"),
    },
}


def _lists_line_up(
    original: dict, fresh: dict, toks: list[str | int], keys_by_list: dict[str, tuple[str, ...]]
) -> bool:
    """True when every list on the way to this field has the same length in both replies and, for
    a keyed list, the same keys item by item. Otherwise new text would be paired with another
    item's keys (a reorder or a dropped item), so nothing from the re-ask is taken."""
    for pos, tok in enumerate(toks):
        if not isinstance(tok, int):
            continue
        try:
            before, after = _get(original, toks[:pos]), _get(fresh, toks[:pos])
        except (KeyError, IndexError, TypeError):
            return False
        if not isinstance(before, list) or not isinstance(after, list) or len(before) != len(after):
            return False
        name = str(toks[pos - 1]) if pos else ""
        for key in keys_by_list.get(name, ()):
            if any(
                isinstance(a, dict) and isinstance(b, dict) and a.get(key) != b.get(key)
                for a, b in zip(before, after, strict=True)
            ):
                return False
    return True


def take_fields(
    original: Any, reasked: Any, paths: Iterable[str], *, version: str = ""
) -> Any | None:
    """The original object with ONLY the listed prose fields taken from the re-asked reply. Every
    other field (scores, figures, themes, titles) stays as first validated. None when the result
    does not validate, or when a list holding a flagged field does not line up with the original
    (different length, or different keys at any position): the original is then kept whole."""
    merged = original.model_dump(mode="json")
    fresh = reasked.model_dump(mode="json")
    keys_by_list = LIST_KEYS.get(version, {})
    for path in dict.fromkeys(paths):
        toks = _tokens(path)
        if not toks:
            continue
        if not _lists_line_up(merged, fresh, toks, keys_by_list):
            return None
        try:
            _set(merged, toks, _get(fresh, toks))
        except (KeyError, IndexError, TypeError):
            continue
    try:
        return type(original).model_validate(merged)
    except ValidationError:
        return None


def repair_request(violations: list[Violation], extra: list[dict[str, str]]) -> str:
    lines = [
        "Your reply has problems with how it is written. Fix every one in a single new reply.",
        "Say each one another way. Use only facts, numbers and names already in the supplied "
        "material. Do not add anything new, and do not change fields that are not listed.",
        "",
    ]
    for v in violations:
        lines.append(f"- {v.field}: \"{v.phrase}\" ({RULE_LABELS.get(v.rule, v.rule)})")
    for f in extra:
        lines.append(f"- {f.get('field', '')}: {describe(f)}. Remove it or say what is known.")
    lines += ["", "Return the same JSON object, complete, with the same fields."]
    return "\n".join(lines)


def reask_messages(messages: list[dict], value: Any, request: str) -> list[dict]:
    """What the re-ask sends: the original prompt, the validated reply, and the request."""
    return [
        *messages,
        {"role": "assistant", "content": value.model_dump_json()},
        {"role": "user", "content": request},
    ]


def reask_fits(spec: Any, ask: list[dict]) -> bool:
    """True when the re-ask prompt and the prompt's output budget fit the tier's num_ctx."""
    from kestrel.ingest.tokens import count_tokens

    used = sum(count_tokens(str(m.get("content") or "")) for m in ask)
    limit = TIER_NUM_CTX.get(getattr(spec, "tier", ""), 4096)
    return used + int(getattr(spec, "max_tokens", 0) or 0) <= limit


async def enforce(
    spec: Any,
    value: Any,
    *,
    messages: list[dict],
    run: Run,
    exempt: Iterable[str] = (),
    extra_check: ExtraCheck | None = None,
    accept: Accept | None = None,
) -> PlainResult:
    """Check `value` (a validated pydantic object) and re-ask once on a blocking hit.

    The re-ask is best effort: if it fails (schema or quarantine) the original validated value is
    kept and nothing is written for the failure (it is logged). Only the flagged prose fields are
    taken from a good re-asked reply. `accept` can veto the merged result (a story that turned
    into a refusal), which also keeps the original."""
    exempt = tuple(exempt)

    def look(v: Any) -> tuple[list[Violation], list[dict[str, str]]]:
        found = check_object(spec.version, v.model_dump(mode="json"), exempt=exempt)
        return found, (extra_check(v) if extra_check else [])

    violations, extra = look(value)
    blocking = [v for v in violations if v.blocking]
    if not blocking and not extra:
        return PlainResult(value, _flags(violations, extra))

    ask = reask_messages(messages, value, repair_request(blocking, extra))
    if not reask_fits(spec, ask):
        log.info("plain_writing_reask_skipped", prompt=spec.version, reason="context")
        return PlainResult(value, _flags(violations, extra))
    result = await run(ask, reask=True)
    if getattr(result, "ok", False) and getattr(result, "value", None) is not None:
        paths = [v.field for v in blocking] + [f.get("field", "") for f in extra]
        merged = take_fields(
            value, result.value, [p for p in paths if p], version=spec.version
        )
        if merged is not None and (accept is None or accept(merged, result.value)):
            new_violations, new_extra = look(merged)
            log.info(
                "plain_writing_reasked", prompt=spec.version, before=len(blocking) + len(extra),
                after=len([v for v in new_violations if v.blocking]) + len(new_extra),
            )
            return PlainResult(merged, _flags(new_violations, new_extra), reasked=True)
    # The re-ask failed, did not validate, or was vetoed: keep the first reply as it was.
    log.info("plain_writing_reask_failed", prompt=spec.version)
    return PlainResult(value, _flags(violations, extra), reasked=True)
