"""Cover letters (P4) and engagement outlines (P4C) — SPEC-MODULES 1.6. Generated prose is permitted
here, under constraint: every factual claim must trace to a real proof point or doc-variant key
(`claims_used`); a banned-phrase check runs on the body; and an engagement outline needs at least
three explicit exclusions (it is an outline for a conversation, never a priced proposal). The system
never sends either — it drafts and hands over (hard rule 1). The verification helpers are pure."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from kestrel.llm.guard import run_prompt
from kestrel.llm.plain_repair import enforce
from kestrel.llm.prompts import load_prompt

COVER = "cover_letter"
OUTLINE = "engagement_outline"
_MIN_EXCLUSIONS = 3
_BANNED = [
    "hit the ground running",
    "think outside the box",
    "team player",
    "results-oriented",
    "results-driven",
    "go-getter",
    "synergy",
    "synergies",
    "proven track record",
    "detail-oriented",
    "self-starter",
    "value-add",
    "move the needle",
    "best of breed",
    "win-win",
    "circle back",
    "low-hanging fruit",
    "boil the ocean",
]
# Word-bounded so "value-add" does not fire inside "value-added" etc. (fewer false positives).
_BANNED_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(p) for p in _BANNED) + r")\b", re.IGNORECASE
)


@dataclass
class LetterDraft:
    value: object | None
    unverified_claims: list[str] = field(default_factory=list)
    banned_hits: list[str] = field(default_factory=list)
    too_few_exclusions: bool = False
    # Plain-writing marks left after the one re-ask (issue #93); shown, never rewritten.
    style_flags: list[dict] = field(default_factory=list)
    error: str | None = None
    # Why there is no value: the reply failed its schema twice (kept on the Quarantine page), or
    # the online key is out of credit. The page shows a fixed sentence for each, never `error`.
    quarantined: bool = False
    out_of_credit: bool = False
    # Kestrel refused the online send before anything left the computer; `error` says why.
    refused: bool = False
    # The reply still spoke of the person in the third person after the one repair (D-UI-241).
    third_person: bool = False

    @property
    def clean(self) -> bool:
        return (
            self.value is not None
            and not self.unverified_claims
            and not self.banned_hits
            and not self.too_few_exclusions
        )


def verify_claims(claims_used, valid_keys: set[str]) -> list[str]:
    """Every claims_used entry must resolve to a real proof-point or variant key; return the ones
    that do not (the draft is then flagged 'unverified claims present')."""
    return [c for c in (claims_used or []) if c not in valid_keys]


def same_claims(first, fresh, valid_keys: set[str]) -> bool:
    """A re-asked letter body is taken only when it cites the same claims as the first reply and
    resolves them the same way: new wording must not bring new claims."""
    return sorted(first.claims_used or []) == sorted(fresh.claims_used or []) and sorted(
        verify_claims(first.claims_used, valid_keys)
    ) == sorted(verify_claims(fresh.claims_used, valid_keys))


def banned_hits(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in _BANNED_RE.finditer(text or "")})


async def _run(
    client, spec, messages, *, session, on_quarantine, reask=False, opportunity_id=None
):
    """The tier3 prompts go through the Hosted mode plan (redaction or refusal) when the caller
    has a session; without one the call is local-only as before."""
    if session is None:
        # A failed re-ask leaves no quarantine row.
        return await run_prompt(
            client, spec, messages, on_quarantine=None if reask else on_quarantine
        )
    from kestrel.pipeline._shared import run_prompt_hosted

    return await run_prompt_hosted(
        session, client, spec, messages, quarantine_tier=4, on_quarantine=on_quarantine,
        reask=reask, opportunity_id=opportunity_id,
    )


def _failed(result) -> LetterDraft:
    return LetterDraft(
        value=None, error=result.error, quarantined=bool(getattr(result, "quarantined", False)),
        out_of_credit=bool(getattr(result, "out_of_credit", False)),
        refused=bool(getattr(result, "refused", False)),
    )


# ------------------------------------------------------------------ the voice check (D-UI-241)
_PRONOUNS = r"this\s+person|the\s+writer|he|she|him|her|his|hers"
_THIRD_COVER = re.compile(
    r"\b(?:the\s+(?:candidate|applicant)|(?:candidate|applicant)['\u2019]s|" + _PRONOUNS + r")\b",
    re.IGNORECASE,
)
_THIRD_OUTLINE = re.compile(
    r"\b(?:the\s+(?:candidate|applicant|practitioner|consultant)|"
    r"(?:candidate|applicant|practitioner|consultant)['\u2019]s|" + _PRONOUNS + r")\b",
    re.IGNORECASE,
)
_QUOTED = re.compile(r'"[^"\n]*"|\u201c[^\u201d\n]*\u201d')


def third_person_hits(body_md: str, kind: str = COVER) -> list[str]:
    """Phrases in an already validated field that speak of the person in the third person
    ("the candidate", "this person", "the writer", he/she/him/her/his/hers). Text inside quotation
    marks is a quotation (of the posting, say) and is not checked. "they" is too ambiguous to
    count. A check on a schema-validated field, never a parse of the raw reply."""
    rx = _THIRD_OUTLINE if kind == OUTLINE else _THIRD_COVER
    text = _QUOTED.sub(" ", body_md or "")
    return sorted({" ".join(m.group(0).lower().split()) for m in rx.finditer(text)})


def reply_texts(value, kind: str = COVER) -> list[str]:
    """Every text field of a validated reply that reaches the person's screen or file."""
    out = [getattr(value, "body_md", "") or ""]
    if kind == OUTLINE:
        out += list(getattr(value, "assumptions", None) or [])
        out += list(getattr(value, "exclusions", None) or [])
        shape = getattr(value, "commercial_shape", None)
        out.append(getattr(shape, "caveat", "") or "")
    else:
        out += list(getattr(value, "opening_alternatives", None) or [])
    return out


def reply_third_person_hits(value, kind: str = COVER) -> list[str]:
    return sorted({h for t in reply_texts(value, kind) for h in third_person_hits(t, kind)})


def _voice_request(hits: list[str], kind: str) -> str:
    who = "the consultant" if kind == OUTLINE else "the person"
    quoted = ", ".join(f'"{h}"' for h in hits)
    return (
        f"Your reply speaks of {who} in the third person ({quoted}). "
        "Write it again in the first person, as that person, to the reader. Keep the facts and "
        "the structure. Return the same JSON object, complete, with the same fields."
    )


async def _first_person(
    spec, result, messages, kind, *, run
):
    """Return (value, ok). If the validated reply is in the third person, ask once; the new reply
    must pass the same schema (the guarded `run`) and be free of it, else ok is False."""
    from kestrel.llm.plain_repair import reask_fits, reask_messages

    hits = reply_third_person_hits(result.value, kind)
    if not hits:
        return result.value, True
    ask = reask_messages(messages, result.value, _voice_request(hits, kind))
    if not reask_fits(spec, ask):
        return None, False
    fresh = await run(ask, reask=True)
    if not getattr(fresh, "ok", False) or getattr(fresh, "value", None) is None:
        return None, False
    if reply_third_person_hits(fresh.value, kind):
        return None, False
    return fresh.value, True


GREETING_NAME = "hiring team"
_FIRST_PERSON = re.compile(r"\bI\b|\b(?i:my|me|we|our)\b")
_ABOUT_ROLE = re.compile(
    r"\b(?:you|your|the\s+role|this\s+role|the\s+position|this\s+position|the\s+team|"
    r"requires?|required|requirements?|minimum|seeking|looking\s+for|posting)\b", re.IGNORECASE
)


def claims_it(sentence: str) -> bool:
    """True when a sentence puts a detail on the person: it has I (also I'm, I've), my, me, we or
    our as a whole word."""
    return bool(_FIRST_PERSON.search(sentence or ""))


def about_the_role(sentence: str) -> bool:
    """True when a sentence is clearly about the employer or the role (you, your, the role, the
    position, the team, requires, minimum, seeking, looking for, posting), as whole words."""
    return bool(_ABOUT_ROLE.search(sentence or ""))


def _has_token(sentence: str, phrase: str) -> bool:
    """The phrase as a whole token: "5" is not found inside "15" or "2.5"."""
    rx = r"(?<![\w$])(?<!\d[.,])" + re.escape(phrase) + r"(?!\w)(?![.,]\d)"
    return re.search(rx, sentence, re.IGNORECASE) is not None


def unverified_details(
    body_md: str, sources: list[str], name_sources: list[str] | None = None, posting: str = ""
) -> list[dict[str, str]]:
    """Numbers and names in the body that the person's own text does not support (hard rule 2).
    One found in neither the person's text nor the posting is always flagged. One found only in
    the posting is flagged unless every sentence holding it is clearly about the role (it
    addresses or describes the employer or role) and does not claim it for the person; a
    pronoun-less resume-style claim is flagged (hard rule 4: uncertainty escalates). The posting
    never grounds a claim."""
    from kestrel.writing_check import ungrounded_details

    fields = {"body_md": body_md}
    # The greeting the prompt asks for is Kestrel's own wording, not a name the model made up.
    mine = [f for f in ungrounded_details(fields, sources, name_sources=name_sources)
            if f["phrase"].casefold() != GREETING_NAME]
    if not mine or not (posting or "").strip():
        return mine
    neither = {f["phrase"] for f in ungrounded_details(
        fields, [*sources, posting], name_sources=name_sources)}
    sentences = [s for para in re.split(r"\n+", body_md or "") for s in _SENTENCE.split(para)]
    out: list[dict[str, str]] = []
    for f in mine:
        phrase = f["phrase"]
        holding = [s for s in sentences if _has_token(s, phrase)]
        if phrase in neither or not holding or any(
            claims_it(s) or not about_the_role(s) for s in holding
        ):
            out.append(f)
    return out


def _grounding(
    sources: list[str] | None, name_sources: list[str] | None, posting: str = ""
):
    """The plain-writing check's number and name grounding against the text the model was given
    (the person's proof points and bullets) and, for names, the person's own resume text. The
    posting only excuses a detail its sentence does not claim for the person
    (`unverified_details`)."""
    if sources is None:
        return None
    return lambda val: unverified_details(val.body_md, sources, name_sources, posting)


async def generate_cover(
    client, *, user_prompt: str, valid_keys: set[str], on_quarantine=None, session=None,
    exempt: tuple[str, ...] = (), grounding: list[str] | None = None,
    name_sources: list[str] | None = None, opportunity_id: int | None = None,
    posting: str = "",
):
    """P4 cover letter. `user_prompt` is the assembled context (profile, selected verbatim bullets,
    evaluation angle, job); the caller supplies only real proof points and variants."""
    spec = load_prompt("cover/v3")
    messages = [
        {"role": "system", "content": spec.system},
        {"role": "user", "content": user_prompt},
    ]
    result = await _run(
        client, spec, messages, session=session, on_quarantine=on_quarantine,
        opportunity_id=opportunity_id,
    )
    if not result.ok or result.value is None:
        return _failed(result)
    first, ok = await _first_person(
        spec, result, messages, COVER, run=lambda m, reask=False: _run(
            client, spec, m, session=session, on_quarantine=on_quarantine, reask=reask,
            opportunity_id=opportunity_id,
        ),
    )
    if not ok:
        return LetterDraft(value=None, third_person=True, error="third person")
    result.value = first
    plain = await enforce(
        spec, result.value, messages=messages, exempt=exempt,
        extra_check=_grounding(grounding, name_sources, posting),
        run=lambda m, reask=False: _run(
            client, spec, m, session=session, on_quarantine=on_quarantine, reask=reask,
            opportunity_id=opportunity_id,
        ),
        accept=lambda _merged, fresh, first=result.value: same_claims(first, fresh, valid_keys),
    )
    v = plain.value
    return LetterDraft(
        style_flags=plain.flags,
        value=v,
        unverified_claims=verify_claims(v.claims_used, valid_keys),
        banned_hits=banned_hits(v.body_md),
    )


async def generate_engagement(
    client, *, user_prompt: str, valid_keys: set[str], on_quarantine=None, session=None,
    exempt: tuple[str, ...] = (), grounding: list[str] | None = None,
    name_sources: list[str] | None = None, opportunity_id: int | None = None,
    posting: str = "",
):
    """P4C engagement outline. Needs at least three explicit exclusions."""
    spec = load_prompt("engagement_outline/v3")
    messages = [
        {"role": "system", "content": spec.system},
        {"role": "user", "content": user_prompt},
    ]
    result = await _run(
        client, spec, messages, session=session, on_quarantine=on_quarantine,
        opportunity_id=opportunity_id,
    )
    if not result.ok or result.value is None:
        return _failed(result)
    first, ok = await _first_person(
        spec, result, messages, OUTLINE, run=lambda m, reask=False: _run(
            client, spec, m, session=session, on_quarantine=on_quarantine, reask=reask,
            opportunity_id=opportunity_id,
        ),
    )
    if not ok:
        return LetterDraft(value=None, third_person=True, error="third person")
    result.value = first
    plain = await enforce(
        spec, result.value, messages=messages, exempt=exempt,
        extra_check=_grounding(grounding, name_sources, posting),
        run=lambda m, reask=False: _run(
            client, spec, m, session=session, on_quarantine=on_quarantine, reask=reask,
            opportunity_id=opportunity_id,
        ),
        accept=lambda _merged, fresh, first=result.value: same_claims(first, fresh, valid_keys),
    )
    v = plain.value
    return LetterDraft(
        style_flags=plain.flags,
        value=v,
        unverified_claims=verify_claims(v.claims_used, valid_keys),
        banned_hits=banned_hits(v.body_md),
        too_few_exclusions=len(v.exclusions or []) < _MIN_EXCLUSIONS,
    )


# ================================================================ the Role page's letter feature
# Everything below is pure (no database, no model): what the prompt is allowed to say, how the
# check view marks a draft, and the HTML the preview and the PDF are made from (D-UI-239).
LETTER_TEMPLATES = (COVER, OUTLINE)
WORD_RANGE = {COVER: (250, 350), OUTLINE: (600, 900)}
OUTLINE_NOTE = "An outline for a conversation, not a priced proposal."


def word_count(text: str) -> int:
    return len((text or "").split())


def proof_rows(profile) -> list[dict[str, str]]:
    """The person's proof points as {key, claim, evidence, metric}. Reads a profile namespace or
    dict; a point without a key cannot be cited, so it is left out."""
    rows: list[dict[str, str]] = []
    for p in getattr(profile, "proof_points", None) or []:
        def get(name: str, p=p) -> str:
            v = p.get(name) if isinstance(p, dict) else getattr(p, name, None)
            return str(v).strip() if v else ""

        if get("key"):
            rows.append({"key": get("key"), "claim": get("claim"), "evidence": get("evidence"),
                         "metric": get("metric")})
    return rows


def source_map(proofs: list[dict[str, str]], variants: list[tuple[str, str]]) -> dict[str, dict]:
    """Every key a claim may name, with the person's own words behind it: {key: {label, text}}.
    A proof point's text is its claim, evidence and metric as written; a variant's is its text."""
    out: dict[str, dict] = {}
    for key, text in variants:
        out[key] = {"label": "Your resume", "text": text, "parts": [text]}
    for p in proofs:
        parts = [x for x in (p["claim"], p["evidence"], p["metric"]) if x]
        # `text` stays one string for matching and grounding; `parts` is what the page prints, one
        # line each, so the claim, its evidence and its metric do not run together.
        out[p["key"]] = {"label": "Your proof point", "text": " ".join(parts), "parts": parts}
    return out


def build_letter_prompt(
    kind: str, *, proofs: list[dict[str, str]], variants: list[tuple[str, str]],
    headline: str = "", narrative: str = "", evaluation: dict | None = None,
    research: str = "", title: str = "", company: str = "", description: str = "",
    org_line: str = "",
) -> str:
    """The user message of P4 / P4C. It carries ONLY the person's own proof points and selected
    bullets, each with the key the reply must cite in `claims_used`, plus the posting and what
    Kestrel has already worked out about the role. Nothing else about the person is sent: no name,
    no contact details, no resume file."""
    ev = evaluation or {}
    lines = ["ABOUT ME (you are writing as this person)", f"Headline: {headline or 'n/a'}"]
    if narrative.strip():
        lines += ["", "NARRATIVE", narrative.strip()]
    lines += ["", "PROOF POINTS (cite each by its key in claims_used; do not invent others)"]
    lines += [
        f"- [{p['key']}] {p['claim']} | evidence: {p['evidence']} | metric: {p['metric'] or 'n/a'}"
        for p in proofs
    ] or ["(none provided)"]
    heading = (
        "SELECTED PROOF ENGAGEMENTS AND SERVICE LINES FOR THIS OUTLINE" if kind == OUTLINE
        else "SELECTED RESUME BULLETS FOR THIS APPLICATION"
    )
    lines += ["", f"{heading} (verbatim; cite each by its key in claims_used)"]
    lines += [f"- [{k}] {t}" for k, t in variants] or ["(none selected)"]
    lines += ["", "GUIDANCE FOR THE " + ("OUTLINE" if kind == OUTLINE else "LETTER")
              + " (background for you; do not repeat these labels in your text)"]
    lines += [f"{label}: {value}" for label, value in ev.items() if value] or ["(none yet)"]
    lines += ["", "COMPANY RESEARCH (if available)", research.strip() or "(none)"]
    if kind == OUTLINE and org_line:
        lines += ["", "TARGET ORGANIZATION", org_line]
    lines += ["", "JOB", f"{title or '(untitled)'} at {company or '(unknown)'}",
              (description or "(no description)").strip()]
    lines += ["", "ANGLE PREFERENCE (operator-selected, may be null)", "null"]
    return "\n".join(lines)


_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]{3,}|\d[\d,.]*%?")
_FILLER = frozenset(
    "that this with from have been were your their there which would could about into than "
    "then them they what when will more also over such only both each other these those while "
    "across around after before between through during under again being does done just like "
    "make made much many most some very well".split()
)


def _tokens(text: str) -> set[str]:
    return {w.lower().rstrip(".,") for w in _WORD.findall(text or "")
            if w.lower() not in _FILLER}


TRACE_MIN_SHARED = 3
TRACE_MIN_SHARE = 0.5


def traced(shared: int, sentence_tokens: int) -> bool:
    """The rule for the dotted underline. A sentence is traced to a source only when it restates
    that source: it shares at least TRACE_MIN_SHARED content words (or numbers) with it AND those
    words are at least TRACE_MIN_SHARE of the sentence's own content words. Three incidental words
    in a long sentence of other things do not trace it. Only keys the draft cites in
    `claims_used` are candidates (see `annotate`), so a source nobody cited never traces."""
    return shared >= TRACE_MIN_SHARED and shared >= TRACE_MIN_SHARE * max(sentence_tokens, 1)


def annotate(
    body_md: str, sources: dict[str, dict], claims: list[str], flags: list[dict],
    banned: list[str],
) -> list[list[dict]]:
    """The check view: the body as paragraphs of segments {text, kind, key}. `kind` is "plain";
    "claim" for a sentence that traces (`traced`) to one of the person's own cited sources (`key`
    names it, the page shows its text on a click); "unverified" for a detail the plain-writing
    check found in none of them (a number or a name); "stock" for stock phrasing. Sentences are
    never reworded, only marked. Deterministic, and it reads the text only."""
    claim_tokens = {k: _tokens(sources[k]["text"]) for k in claims if k in sources}
    flagged = [f.get("phrase", "") for f in flags
               if f.get("kind") == "ungrounded" and f.get("phrase")]
    stock = [f.get("phrase", "") for f in flags
             if f.get("kind") == "style" and f.get("phrase")] + list(banned)
    paragraphs: list[list[dict]] = []
    for para in re.split(r"\n\s*\n", (body_md or "").strip()):
        if not para.strip():
            continue
        segs: list[dict] = []
        for sentence in _SENTENCE.split(para.strip()):
            toks = _tokens(sentence)
            best, best_n = "", 0
            for key, src in claim_tokens.items():
                n = len(toks & src)
                if n > best_n and traced(n, len(toks)):
                    best, best_n = key, n
            kind = "claim" if best else "plain"
            segs += _mark(sentence + " ", kind, best if kind == "claim" else "", flagged, stock)
        paragraphs.append(segs)
    return paragraphs


def _mark(text: str, kind: str, key: str, unverified: list[str], stock: list[str]) -> list[dict]:
    """Split one sentence at its unverified details and stock phrases (which win over the
    sentence's own mark), keeping every character of the text in order."""
    spots: list[tuple[int, int, str]] = []
    low = text.lower()
    for phrase, mark in [(p, "unverified") for p in unverified] + [(p, "stock") for p in stock]:
        if not phrase:
            continue
        needle = phrase.lower()
        at = low.find(needle)
        while at >= 0:  # every occurrence, not only the first
            if not any(a < at + len(phrase) and at < b for a, b, _ in spots):
                spots.append((at, at + len(phrase), mark))
            at = low.find(needle, at + len(needle))
    if not spots:
        return [{"text": text, "kind": kind, "key": key}]
    spots.sort()
    out: list[dict] = []
    pos = 0
    for a, b, mark in spots:
        if a > pos:
            out.append({"text": text[pos:a], "kind": kind, "key": key})
        out.append({"text": text[a:b], "kind": mark, "key": ""})
        pos = b
    if pos < len(text):
        out.append({"text": text[pos:], "kind": kind, "key": key})
    return out


_BOLD = re.compile(r"\*\*(.+?)\*\*")


def blocks(body_md: str) -> list[dict]:
    """The markdown subset a letter or outline uses, as blocks a template prints with autoescape:
    paragraphs, `#` headings, `-` bullets, and `**bold**` runs. Nothing else is interpreted, so no
    markup from the text itself ever reaches the page."""
    out: list[dict] = []
    for chunk in re.split(r"\n\s*\n", (body_md or "").strip()):
        lines = [ln for ln in chunk.splitlines() if ln.strip()]
        if not lines:
            continue
        if all(ln.lstrip().startswith(("- ", "* ")) for ln in lines):
            out.append({"type": "list", "items": [_runs(ln.lstrip()[2:]) for ln in lines]})
            continue
        for ln in lines:
            if ln.startswith("#"):
                out.append({"type": "heading", "runs": _runs(ln.lstrip("# ").strip())})
            else:
                out.append({"type": "para", "runs": _runs(ln.strip())})
    return out


def _runs(text: str) -> list[dict]:
    runs: list[dict] = []
    pos = 0
    for m in _BOLD.finditer(text):
        if m.start() > pos:
            runs.append({"text": text[pos:m.start()], "bold": False})
        runs.append({"text": m.group(1), "bold": True})
        pos = m.end()
    if pos < len(text):
        runs.append({"text": text[pos:], "bold": False})
    return runs or [{"text": "", "bold": False}]


_SIGNOFF = re.compile(
    r"^(?:sincerely|best regards|kind regards|warm regards|regards|best|yours truly|"
    r"respectfully|thank you)[,.!]?$", re.IGNORECASE
)


_CONTACT = re.compile(
    r"^(?:[\w.+-]+@[\w-]+(?:\.[\w-]+)+|[+()\d][\d\s().+-]{6,}|(?:https?://|www\.)\S+|"
    r"(?:linkedin\.com|github\.com)\S*)$", re.IGNORECASE
)


def _name_or_contact(line: str) -> bool:
    """A short line that is a name (a few words, no sentence punctuation) or a contact detail."""
    t = line.strip()
    if _CONTACT.match(t):
        return True
    words = t.split()
    return 0 < len(words) <= 4 and not re.search(r"[.!?:;]$", t) and len(t) <= 40


def strip_signoff(body_md: str) -> str:
    """The text without a closing the model wrote anyway ("Sincerely," and a name below it):
    Kestrel prints its own at render time, so there is never a second one. Only a closing word on
    its own line followed by nothing but name and contact lines (blank lines allowed) is removed;
    a body sentence such as "Thank you." followed by more body text stays."""
    lines = (body_md or "").rstrip().splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if not _SIGNOFF.match(lines[i].strip()):
            continue
        tail = [t for t in lines[i + 1:] if t.strip()]
        if len(tail) <= 4 and all(_name_or_contact(t) for t in tail):
            return "\n".join(lines[:i]).rstrip()
    return (body_md or "").strip()


def letter_html(header: dict, body_md: str, kind: str, *, preview: bool = False) -> str:
    """The letter as the same HTML the preview shows and the PDF is made from: Letter size, one
    column, selectable text, the person's header, and nothing that names an organization in the
    document's own properties (the print stylesheet and the PDF scrub carry none). A cover letter
    ends with "Sincerely," and the person's name from the local profile, added here and never by
    the model, so the name is never part of an online payload (D-UI-241). `preview` puts the
    page's one-inch margins inside the page, because @page margins do not apply in a frame."""
    from kestrel.docs.render import _env

    return _env().get_template("letter.html").render(
        header=header or {}, blocks=blocks(strip_signoff(body_md) if kind == COVER else body_md),
        outline=(kind == OUTLINE), closing=(kind == COVER), preview=preview,
    )
