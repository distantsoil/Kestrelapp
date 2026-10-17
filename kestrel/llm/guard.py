"""Structured-output-or-failure (SPEC.md 0.4 / hard rule 3). Strip thinking spans unconditionally,
parse, validate against the prompt's pydantic model; on failure make ONE repair call at
temperature 0 quoting the error; on a second failure QUARANTINE with the full prompt and raw
response and return a typed failure. Never raise past the caller, and never regex-parse the model's
response for data (json.loads only; brace-extraction is a defensive locate, not field parsing)."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

from kestrel.llm.client import LLMClient, ModelDeclined, json_schema_for
from kestrel.llm.docselect import build_doc_select_schema, validate_doc_selection
from kestrel.llm.schemas import P3DocSelect

log = structlog.get_logger()

_THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

QuarantineHook = Callable[[list[dict], str, str, str], Awaitable[None]]

# How much of a looping first reply the repair call quotes back (D-LLM-2).
REPAIR_ECHO_CHARS = 600

REPAIR_INSTRUCTION = (
    "Your previous response was not valid and could not be used.\n"
    "Error: {error}\n"
    "Return ONLY a single corrected JSON object. No prose, no markdown fences, no explanation."
)

# Added to the repair after a looping reply (D-LLM-5): name the field and value that looped, and
# give the model a hard stop. P2/P2C replies looped inside legitimacy.signals, so evaluation
# prompts get the legitimacy limit spelled out.
LOOP_REPAIR_RULE = (
    "Your previous reply looped: it repeated \"{value}\" in `{field}` {count} times."
)
LOOP_REPAIR_STOP_EVAL = (
    "List each flag at most once; at most 3 legitimacy flags; stop after the closing brace."
)
LOOP_REPAIR_STOP = "List each value at most once; stop after the closing brace."


@dataclass
class GuardResult:
    ok: bool
    value: BaseModel | None = None
    raw: str = ""
    repaired: bool = False
    quarantined: bool = False
    error: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    # Provider-reported USD cost summed across attempts, None when no attempt reported one.
    cost_usd: float | None = None


def _add_cost(total: float | None, part: float | None) -> float | None:
    if part is None:
        return total
    return (total or 0.0) + part


def strip_think(text: str) -> str:
    return _THINK_RE.sub("", text)


def extract_json(content: str, *, lenient: bool = True) -> str:
    """Strip thinking spans and markdown fences. With `lenient`, if there is stray prose around
    the object, take the outermost braces: a defensive locate, not field parsing, allowed only
    where the request had no response_format to hold the model to JSON (prompt-only mode) and on
    the repair attempt. Strict (json_schema and json_object modes), the reply must be the object
    and nothing else, so "Sure! {...}" is a failed attempt, never an answer (hard rule 3)."""
    text = strip_think(content).strip()
    text = _FENCE_RE.sub("", text).strip()
    if not lenient or (text.startswith("{") and text.endswith("}")):
        return text
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        return text[start : end + 1]
    return text


# A reply in which one JSON string value occurs more than this many times IN ONE ARRAY is a
# generation loop (D-LLM-2: a free model wrote "comp_unstated" dozens of times until max_tokens).
# No shipped schema legitimately repeats one value this often within an array: the longest enum
# lists are capped at 12. Values in different arrays (the same tag on 21 resume variants) or in
# object fields are never pooled.
DEGENERATE_REPEAT_LIMIT = 20
# Prompts whose replies are long and legitimately repetitive (P7 resume import: many variants,
# skill lists) get a higher bar, so a long honest reply is never called a loop.
_LOOP_LIMITS: tuple[tuple[str, int], ...] = (("resume_", 60),)


def loop_limit_for(schema_name: str | None) -> int:
    """The repeat threshold for one prompt's reply (its schema name, e.g. "resume_role.v1")."""
    name = schema_name or ""
    for prefix, limit in _LOOP_LIMITS:
        if name.startswith(prefix):
            return limit
    return DEGENERATE_REPEAT_LIMIT


def _scan_arrays(text: str) -> tuple[dict[tuple[int, str], int], dict[int, str]]:
    """(array id, value) -> occurrences, for every COMPLETE JSON string literal that is directly
    an element of an array, plus each array's dotted field path ("legitimacy.signals"). A
    structural scan for the loop check only, never a parse of the reply's data (that comes from
    json.loads and pydantic). Tolerates a reply cut at the cap: an unterminated trailing literal
    is skipped, unclosed containers are simply left open."""
    counts: dict[tuple[int, str], int] = {}
    paths: dict[int, str] = {}
    stack: list[tuple[int, str]] = []  # (array id or -1 for an object, dotted path)
    next_id = 0
    pending_key: str | None = None  # the last object key read, awaiting its value
    i, n = 0, len(text)

    def child_path() -> str:
        parent = stack[-1][1] if stack else ""
        if stack and stack[-1][0] == -1 and pending_key is not None:
            return f"{parent}.{pending_key}" if parent else pending_key
        return parent

    while i < n:
        c = text[i]
        if c == "[":
            path = child_path()
            paths[next_id] = path
            stack.append((next_id, path))
            next_id += 1
            pending_key = None
        elif c == "{":
            stack.append((-1, child_path()))
            pending_key = None
        elif c in "]}":
            if stack:
                stack.pop()
            pending_key = None
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                break
            literal = text[i + 1 : j]
            if stack and stack[-1][0] >= 0:
                if len(literal) <= 200:
                    key = (stack[-1][0], literal)
                    counts[key] = counts.get(key, 0) + 1
            elif stack and stack[-1][0] == -1:
                k = j + 1
                while k < n and text[k] in " \t\r\n":
                    k += 1
                if k < n and text[k] == ":":
                    pending_key = literal
            i = j
        i += 1
    return counts, paths


def _array_string_counts(text: str) -> dict[tuple[int, str], int]:
    return _scan_arrays(text)[0]


@dataclass
class LoopSite:
    """Where a generation loop happened: the array's dotted field path, the value, the count."""

    field: str
    value: str
    count: int


def loop_site(text: str, limit: int = DEGENERATE_REPEAT_LIMIT) -> LoopSite | None:
    """The field and value of a generation loop in `text`, or None (see degenerate_repeat)."""
    if not text or text.count('"') <= 2 * limit:
        return None
    counts, paths = _scan_arrays(strip_think(text))
    if not counts:
        return None
    (array_id, value), n = max(counts.items(), key=lambda kv: kv[1])
    if n <= limit:
        return None
    shown = value if len(value) <= 60 else value[:57] + "..."
    return LoopSite(field=paths.get(array_id) or "an array", value=shown, count=n)


def degenerate_repeat(text: str, limit: int = DEGENERATE_REPEAT_LIMIT) -> str | None:
    """Why `text` is a generation loop, or None: one string value repeated more than `limit`
    times within one array. The guard treats such a reply as a failed attempt (one repair, then
    quarantine with the raw reply kept), never as an answer and never by waiting it out (hard
    rule 3)."""
    site = loop_site(text, limit)
    if site is None:
        return None
    return (
        f'the reply repeated "{site.value}" {site.count} times (a generation loop); '
        "list each value once"
    )


def loop_repair_text(site: LoopSite | None, schema_name: str | None) -> str:
    """The loop-resistant lines of the repair message (D-LLM-5)."""
    stop = (
        LOOP_REPAIR_STOP_EVAL if (schema_name or "").startswith("evaluate") else LOOP_REPAIR_STOP
    )
    if site is None:
        return stop
    rule = LOOP_REPAIR_RULE.format(value=site.value, field=site.field, count=site.count)
    return f"{rule} {stop}"


def parse_and_validate(
    content: str, model_cls: type[BaseModel], *, lenient: bool = True
) -> tuple[BaseModel | None, str | None]:
    try:
        data = json.loads(extract_json(content, lenient=lenient))
    except (json.JSONDecodeError, ValueError) as exc:
        hint = "" if lenient else " (the reply must be the JSON object alone, no prose)"
        return None, f"invalid JSON{hint}: {exc}"
    try:
        return model_cls.model_validate(data), None
    except ValidationError as exc:
        return None, f"schema validation failed: {exc}"


async def run_guarded(
    client: LLMClient,
    *,
    tier: str,
    messages: list[dict],
    model_cls: type[BaseModel],
    schema: dict[str, Any] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    schema_name: str = "response",
    on_quarantine: QuarantineHook | None = None,
    allow_external: bool = False,
    reasoning_hint: str | None = None,
    repair_max_tokens: int | None = None,
    reply_filter: Any = None,
) -> GuardResult:
    """One call, strip+parse+validate; one repair at temperature 0; then quarantine. Returns a
    typed GuardResult and never raises past this function. `allow_external` is threaded to the
    client so an explicit Tier 3 escalation may reach an external backend (hard rule 1); the other
    opt-in, Hosted mode, is checked inside the client against settings.llm.hosted_mode, and the
    handlers redact the confidential employer before calling here (pipeline._shared).

    The repair after a degenerate (looping) or reasoning-exhausted attempt always runs with
    reasoning off (D-LLM-5). After a loop it also names the looping field and value, asks for
    each flag once, runs at `repair_max_tokens` (the prompt's budget sized to its schema) so a
    second loop fails fast, and is marked as a repair so the client may add a loop penalty the
    model lists (client._apply_openrouter_options).

    `reply_filter` (kestrel.privacy.CodeBook, D-PRIV-1): the send carried this call's random
    stand-in codes. Each reply has exactly those codes restored before it is parsed; a reply that
    still carries a code, or any five-character piece of one, is a failed attempt like invalid
    JSON (one repair, then quarantine with the raw reply), so coded text is never accepted."""

    def check(content: str, lenient: bool) -> tuple[BaseModel | None, str | None]:
        if reply_filter is None:
            return parse_and_validate(content, model_cls, lenient=lenient)
        restored, ferr = reply_filter.restore_raw(content)
        if ferr is not None:
            return None, ferr
        value, perr = parse_and_validate(restored, model_cls, lenient=lenient)
        if value is None:
            return None, perr
        try:
            return reply_filter.finish(value)
        except ValidationError as exc:
            return None, f"schema validation failed: {exc}"

    if model_cls is P3DocSelect and schema is None:
        # Guardrail (hard rule 2): P3's permissive model has no per-slot enums, so the default
        # json_schema_for(P3DocSelect) would NOT make a hallucinated variant key impossible and
        # would not be caught. Use run_doc_select, which builds the dynamic enum schema AND
        # validates against the catalog. Refuse the unsafe default rather than silently allow it.
        raise ValueError(
            "P3 doc-select must go through run_doc_select (dynamic schema + catalog "
            "validation), not run_guarded with the default schema."
        )
    if schema is None:
        schema = json_schema_for(model_cls)

    prompt_tokens = completion_tokens = latency = 0
    cost: float | None = None
    # JSON inside prose is accepted only where nothing held the model to JSON (prompt-only
    # mode); with a json_schema or json_object request a reply that is not the bare object is a
    # failed attempt. The repair attempt is always lenient.
    mode_of = getattr(client, "json_mode_for", None)
    first_lenient = mode_of is not None and mode_of(tier) == "prompt"
    loop_limit = loop_limit_for(schema_name)
    first_loop = False

    def spend(exc_or_res: Any) -> None:
        nonlocal prompt_tokens, completion_tokens, latency, cost
        prompt_tokens += getattr(exc_or_res, "prompt_tokens", 0) or 0
        completion_tokens += getattr(exc_or_res, "completion_tokens", 0) or 0
        latency += getattr(exc_or_res, "latency_ms", 0) or 0
        cost = _add_cost(cost, getattr(exc_or_res, "cost_usd", None))

    # --- attempt 1, plus one re-ask with reasoning off (D-IMPORT-4) --------------------------
    hint = reasoning_hint
    if hint is None:
        # Hosted Tier 1 on a reasoning model: triage is a short classification, so its first
        # attempt runs with reasoning off (D-LLM-5, hosted.tier1_reasoning_off).
        default_of = getattr(client, "default_reasoning_hint", None)
        hint = default_of(tier) if default_of is not None else None
    exhausted = False
    first_content: str | None = None
    err: str | None = None
    while True:
        try:
            res = await client.complete(
                tier=tier,
                messages=messages,
                schema=schema,
                temperature=temperature,
                max_tokens=max_tokens,
                schema_name=schema_name,
                allow_external=allow_external,
                **({"reasoning_hint": hint} if hint else {}),
            )
        except ModelDeclined as exc:
            spend(exc)  # accepted and billed: it belongs in the transmission record
            if exc.degenerate:
                # A generation loop that ran to the cap (D-LLM-2) is a failed attempt like any
                # unusable reply: it gets the one repair below, then quarantine.
                log.info("degenerate_reply", tier=tier, attempt=1)
                first_content, err = exc.content, exc.reason
                first_loop = True
                break
            # A reasoning model that spent the whole cap thinking wrote no answer at all. That
            # is the one declined reply a different request can fix: once, with reasoning off,
            # when that would change the request (a hosted provider that takes the switch and no
            # operator-set effort). Anything else quarantines at once with the reason and the
            # raw response: a repair would be declined the same way (hard rules 3 and 4).
            applies = getattr(client, "reasoning_hint_applies", None)
            if (exc.reasoning_exhausted and hint != "none" and applies is not None
                    and applies(tier)):
                log.info("reasoning_exhausted_retry", tier=tier, previous_hint=hint)
                hint = "none"
                exhausted = True
                continue
            if on_quarantine is not None:
                model = client.settings.llm.tiers[tier].model
                await on_quarantine(messages, exc.raw, exc.reason, model)
            return GuardResult(
                ok=False, raw=exc.raw, quarantined=True, error=exc.reason,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                latency_ms=latency, cost_usd=cost,
            )
        except Exception as exc:  # request/transport error: not a quarantine, the caller can retry
            return GuardResult(
                ok=False, error=f"request error: {type(exc).__name__}: {exc}",
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                latency_ms=latency, cost_usd=cost,
            )
        break
    if first_content is None:
        spend(res)
        first_content = res.content
        value, err = check(res.content, first_lenient)
        if value is not None:
            return GuardResult(
                True,
                value,
                res.content,
                False,
                False,
                None,
                prompt_tokens,
                completion_tokens,
                latency,
                cost_usd=cost,
            )
        loop = degenerate_repeat(res.content, loop_limit)
        if loop:
            log.info("degenerate_reply", tier=tier, attempt=1)
            err = loop
            first_loop = True

    def combined(repair_raw: str) -> str:
        """The quarantine's raw response. After a looping first reply, the loop itself is the
        evidence, so it is kept first with the repair's reply appended."""
        if not first_loop:
            return repair_raw
        return f"{first_content}\n\n--- repair attempt ---\n{repair_raw}"

    # --- attempt 2: one repair at temperature 0 -----------------------------------------------
    echo = first_content
    instruction = REPAIR_INSTRUCTION.format(error=err)
    repair_kw: dict[str, Any] = {}
    if first_loop:
        # Echoing a whole loop back invites the model to continue it; the head is enough to
        # show what went wrong.
        echo = echo[:REPAIR_ECHO_CHARS]
        instruction += "\n" + loop_repair_text(loop_site(first_content, loop_limit), schema_name)
        repair_kw["repair"] = True
        if repair_max_tokens is not None:
            repair_kw["max_tokens_cap"] = repair_max_tokens
    # A repair after a loop or a reasoning-exhausted attempt runs with reasoning off: thinking
    # is what ran out the cap or fed the loop (D-LLM-5). Otherwise the attempt's own hint.
    repair_hint = "none" if (first_loop or exhausted) else hint
    if repair_hint:
        repair_kw["reasoning_hint"] = repair_hint
    repair_messages = [
        *messages,
        {"role": "assistant", "content": echo},
        {"role": "user", "content": instruction},
    ]
    try:
        res2 = await client.complete(
            tier=tier,
            messages=repair_messages,
            schema=schema,
            temperature=0.0,
            max_tokens=max_tokens,
            schema_name=schema_name,
            allow_external=allow_external,
            **repair_kw,
        )
    except ModelDeclined as exc:
        spend(exc)
        if on_quarantine is not None:
            model = client.settings.llm.tiers[tier].model
            await on_quarantine(messages, combined(exc.raw), exc.reason, model)
        return GuardResult(
            ok=False,
            raw=combined(exc.raw),
            repaired=True,
            quarantined=True,
            error=exc.reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency,
            cost_usd=cost,
        )
    except Exception as exc:
        return GuardResult(
            ok=False,
            raw=first_content,
            error=f"repair request error: {type(exc).__name__}: {exc}",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency,
            cost_usd=cost,
        )
    spend(res2)
    value2, err2 = check(res2.content, True)
    if value2 is not None:
        return GuardResult(
            True,
            value2,
            res2.content,
            True,
            False,
            None,
            prompt_tokens,
            completion_tokens,
            latency,
            cost_usd=cost,
        )

    # --- quarantine ---------------------------------------------------------------------------
    err2 = degenerate_repeat(res2.content, loop_limit) or err2
    if on_quarantine is not None:
        await on_quarantine(
            messages, combined(res2.content), err2 or "unknown",
            client.settings.llm.tiers[tier].model,
        )
    return GuardResult(
        ok=False,
        value=None,
        raw=combined(res2.content),
        repaired=True,
        quarantined=True,
        error=err2,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency,
        cost_usd=cost,
    )


async def run_prompt(
    client: LLMClient,
    spec: Any,
    messages: list[dict],
    *,
    schema: dict[str, Any] | None = None,
    on_quarantine: QuarantineHook | None = None,
    reply_filter: Any = None,
) -> GuardResult:
    """Run a guarded call from a PromptSpec, threading the prompt-declared temperature/max_tokens
    so they WIN over the tier fallback (SPEC.md 8.1). Not for P3 — use run_doc_select."""
    return await run_guarded(
        client,
        tier=spec.tier,
        messages=messages,
        model_cls=spec.model_cls,
        schema=schema,
        temperature=spec.temperature,
        max_tokens=spec.max_tokens,
        schema_name=spec.version.replace("/", "_"),
        on_quarantine=on_quarantine,
        reasoning_hint=getattr(spec, "reasoning", None),
        repair_max_tokens=getattr(spec, "repair_max_tokens", None),
        reply_filter=reply_filter,
    )


async def run_doc_select(
    client: LLMClient,
    *,
    messages: list[dict],
    layouts: list[Any],
    summary_variants: list[dict],
    slots: list[dict],
    skill_groups: list[dict],
    temperature: float | None = 0.0,
    max_tokens: int | None = 1500,
    tier: str = "docsel",
    on_quarantine: QuarantineHook | None = None,
    codebook: Any = None,
) -> GuardResult:
    """The SAFE P3 path (hard rule 2): build the dynamic per-slot enum schema so a hallucinated
    variant key is structurally impossible, run the guard, then validate the selection against the
    live catalog as a second line of defense. A bad key becomes a validation FAILURE (ok=False),
    never a silent default; the caller renders with slot defaults and flags defaults_used."""
    schema = build_doc_select_schema(
        layouts=layouts, summary_variants=summary_variants, slots=slots, skill_groups=skill_groups
    )
    if codebook is not None:
        # D-PRIV-1: slot and variant keys are identifiers sent to the model; one that carries a
        # listed name goes coded like any other text, and the reply's keys are restored before
        # the catalog validation below.
        schema = codebook.encode_json(schema, keys=True)
    result = await run_guarded(
        client,
        tier=tier,
        messages=messages,
        model_cls=P3DocSelect,
        schema=schema,
        temperature=temperature,
        max_tokens=max_tokens,
        schema_name="doc_select",
        on_quarantine=on_quarantine,
        reply_filter=codebook,
    )
    if not result.ok or result.value is None:
        return result
    errors = validate_doc_selection(
        result.value.model_dump(), slots=slots, layouts=layouts, summary_variants=summary_variants
    )
    if errors:
        result.ok = False
        result.error = "doc-select validation: " + "; ".join(errors)
    return result
