"""Contact discovery (P6, SPEC-MODULES.md 2.2). The model synthesizes the supplied `ExcerptSet` and
never browses; it never invents a person, and it never guesses an email from a domain.

Two anti-hallucination rules are enforced here in CODE, not merely stated in the prompt (hard rule
2, hard rule 3):

  - Every returned person must carry a `source_index` that resolves to one of the supplied
    excerpts. One that does not is DROPPED ENTIRELY: the system never invents a person just
    because the model returned one.
  - Any returned email is kept ONLY if it appears verbatim in some supplied source text. An email
    that does not verify is nulled, never pattern-guessed from the org's domain.
  - The same rule applies to `linkedin_url`: kept only when that URL (scheme/www-insensitive) is
    present verbatim in a supplied source, otherwise nulled. A plausible-looking profile slug is
    exactly the kind of thing a model invents.

Outreach is draft-only (hard rule 1: the system never acts externally on the operator's behalf).
Every `OutreachDraft` created here has `status="draft"`; nothing here ever sends anything. LinkedIn
bodies are capped at 300 characters, matching the schema's own truncation."""

from __future__ import annotations

import re

from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import Contact, Opportunity, Org, OutreachDraft, Quarantine
from kestrel.llm.client import LLMClient
from kestrel.llm.guard import QuarantineHook
from kestrel.llm.prompts import load_prompt
from kestrel.pipeline._shared import run_prompt_hosted
from kestrel.research.errors import raise_if_transport_failure
from kestrel.research.excerpts import ExcerptSet
from kestrel.research.validation import email_verbatim, valid_source_index

# The literal for-each-source marker in kestrel/llm/prompts/contacts.v1.md's USER template. The
# backslash-n is TWO literal characters in the template (verified with cat -A), so it must be
# escaped as \\n here; a real newline would match nothing and silently strip the sources block,
# leaving the model with no excerpts to cite (a hard-rule-2 hole). company.py escapes it the same.
_SOURCES_PLACEHOLDER = '{for each source: "[{index}] {url}\\n{excerpt}"}'
_LINKEDIN_BODY_CHARS = 300
# URL tokens in source text: an absolute http(s) URL, or a bare linkedin.com/in/... reference.
_URL_RE = re.compile(
    r"https?://[^\s<>\"'()\[\]]+|(?:www\.)?linkedin\.com/in/[^\s<>\"'()\[\]]+", re.I
)


def _canonical_url(url: str) -> str:
    """Lowercased, scheme- and www-stripped, trailing punctuation dropped: the identity of a URL for
    verbatim matching, so `https://www.linkedin.com/in/x/` and `linkedin.com/in/x` are the same."""
    u = url.strip().lower().rstrip("/.,;:)")
    u = re.sub(r"^https?://", "", u)
    return u.removeprefix("www.")


def linkedin_url_verbatim(url: str | None, source_texts: list[str]) -> str | None:
    """Return the URL ONLY if it appears as a whole URL token in some supplied source (same rule as
    `email_verbatim`); otherwise None. A prefix or near-miss of a real profile URL does not pass."""
    if not url or not isinstance(url, str):
        return None
    candidate = url.strip()
    key = _canonical_url(candidate)
    if not key:
        return None
    for text in source_texts:
        if isinstance(text, str) and key in {_canonical_url(m) for m in _URL_RE.findall(text)}:
            return candidate
    return None


def _render_user(template: str, *, job_title: str, org_name: str, sources_block: str) -> str:
    text = template.replace("{job.title}", job_title)
    text = text.replace("{org.name}", org_name)
    return text.replace(_SOURCES_PLACEHOLDER, sources_block)


def _quarantine_hook(session: AsyncSession) -> QuarantineHook:
    """Preserve a failed P6 call (hard rule 3: quarantine with the raw response preserved), on the
    same session the caller commits so it lands atomically with any contacts that did survive."""

    async def hook(messages: list[dict], raw_response: str, error: str, model: str) -> None:
        prompt_text = "\n\n".join(f"[{m.get('role')}]\n{m.get('content')}" for m in messages)
        session.add(
            Quarantine(
                opportunity_id=None,
                tier=6,
                model=model,
                prompt=prompt_text[:20000],
                raw_response=(raw_response or "")[:20000],
                error=(error or "")[:4000],
                attempts=2,
            )
        )

    return hook


async def discover_contacts(
    session: AsyncSession,
    client: LLMClient,
    org: Org,
    excerpts: ExcerptSet,
    *,
    opportunity: Opportunity | None = None,
    make_drafts: bool = True,
    commit: bool = True,
    raise_on_transport: bool = False,
) -> list[Contact]:
    """Run P6 against `excerpts`, validate every returned person in code, and persist the survivors
    as `Contact` rows (plus a draft `OutreachDraft` each, when `make_drafts`). Returns the created
    `Contact` rows. Commits once, at the end, after all validated rows are staged."""
    if len(excerpts) == 0:
        # Nothing for the model to synthesize from: with no sources every returned person would be
        # a fabrication and get dropped anyway. Skip the call rather than invite one.
        return []

    spec = load_prompt("contacts/v1")
    job_title = (opportunity.title if opportunity else None) or "(unspecified role)"
    user = _render_user(
        spec.user,
        job_title=job_title,
        org_name=org.name or "(unknown)",
        sources_block=excerpts.prompt_block(),
    )
    messages = [{"role": "system", "content": spec.system}, {"role": "user", "content": user}]

    result = await run_prompt_hosted(
        session, client, spec, messages, quarantine_tier=6,
        opportunity_id=opportunity.id if opportunity else None,
        on_quarantine=_quarantine_hook(session),
    )
    if raise_on_transport:
        raise_if_transport_failure(result)
    if not result.ok or result.value is None:
        if commit:
            await session.commit()
        return []

    source_texts = excerpts.texts()
    n_excerpts = len(excerpts)
    created: list[Contact] = []

    for c in result.value.contacts:
        if not valid_source_index(c.source_index, n_excerpts):
            # Never invent a person: a source_index that resolves to nothing means the model
            # fabricated this contact wholesale, so it is dropped, not stored with a null source.
            continue
        resolved = excerpts.resolve(c.source_index)

        contact = Contact(
            org_id=org.id,
            name=c.name,
            title=c.title,
            contact_type=c.contact_type,
            linkedin_url=linkedin_url_verbatim(c.linkedin_url, source_texts),
            email=email_verbatim(c.email, source_texts),
            confidence=c.confidence,
            source="p6_discovery",
            source_url=resolved.url if resolved else None,
            relationship="none",
        )
        session.add(contact)
        await session.flush()  # assign contact.id before it is needed as OutreachDraft's FK

        if make_drafts and c.draft_message:
            session.add(
                OutreachDraft(
                    opportunity_id=opportunity.id if opportunity else None,
                    contact_id=contact.id,
                    channel="linkedin",
                    body=c.draft_message[:_LINKEDIN_BODY_CHARS],
                    status="draft",  # THE SYSTEM NEVER SENDS: a draft is the only status possible.
                )
            )
        created.append(contact)

    if commit:
        await session.commit()
    return created
