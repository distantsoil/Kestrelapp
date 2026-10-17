"""P6 contact discovery: a person without a resolvable source_index is never created, an email
absent verbatim from every source is nulled rather than guessed, and a valid person with a verbatim
email is preserved exactly. Outreach is draft-only (the system never sends). Fully offline: the LLM
is a hand-rolled fake, never a live model."""

from __future__ import annotations

import json

from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import Contact, Org, OutreachDraft, Quarantine
from kestrel.llm.client import ChatResult
from kestrel.research.contacts import discover_contacts, linkedin_url_verbatim
from kestrel.research.excerpts import Excerpt, ExcerptSet
from kestrel.research.validation import email_verbatim

_EXCERPTS = ExcerptSet(
    excerpts=[
        Excerpt(
            index=1,
            url="https://acme.example/team",
            kind="company_page",
            text="Jane Roe, CIO. Reach her at jane.roe@acme.com.",
        )
    ]
)

# Three contacts in one payload:
#   1. fabricated person -- source_index=99 is out of range -> must be dropped entirely
#   2. fabricated email -- valid source_index but an email that is NOT in any excerpt -> nulled
#   3. valid person -- source_index=1 and an email that IS in the excerpt verbatim -> preserved
_PAYLOAD = {
    "contacts": [
        {
            "name": "Fabricated Ghost",
            "title": "VP Engineering",
            "contact_type": "peer",
            "linkedin_url": None,
            "email": None,
            "confidence": 0.5,
            "source_index": 99,
            "why_this_person": "fabricated wholesale",
            "draft_message": "Hi, connecting about the role.",
        },
        {
            "name": "Jane Roe",
            "title": "CIO",
            "contact_type": "hiring_manager",
            "linkedin_url": "https://linkedin.com/in/janeroe",
            "email": "ceo@acme.com",
            "confidence": 0.6,
            "source_index": 1,
            "why_this_person": "guessed email from the domain, not verbatim",
            "draft_message": "Hi Jane, I'd love to connect about the CIO opening at Acme.",
        },
        {
            "name": "Jane Roe",
            "title": "CIO",
            "contact_type": "hiring_manager",
            "linkedin_url": "https://linkedin.com/in/janeroe",
            "email": "jane.roe@acme.com",
            "confidence": 0.9,
            "source_index": 1,
            "why_this_person": "named CIO with a verbatim contact email on the team page",
            "draft_message": "Hi Jane, I'd love to connect about the CIO opening at Acme.",
        },
    ]
}


class _FakeClient:
    """Drop-in for LLMClient: same `.settings` / async `.complete()` shape, no network."""

    def __init__(self, content: str) -> None:
        self.settings = get_settings()
        self._content = content

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
        return ChatResult(
            content=self._content,
            prompt_tokens=10,
            completion_tokens=20,
            latency_ms=1,
            model=self.settings.llm.tiers[tier].model,
        )


async def _make_org(sm) -> int:
    async with sm() as s:
        org = Org(name="Acme", normalized_name="acme")
        s.add(org)
        await s.commit()
        return org.id


async def test_fabricated_person_dropped_email_guessed_nulled_valid_person_preserved(sm):
    org_id = await _make_org(sm)
    client = _FakeClient(json.dumps(_PAYLOAD))

    async with sm() as s:
        org = await s.get(Org, org_id)
        created = await discover_contacts(s, client, org, _EXCERPTS)

    # exactly two survive: the out-of-range source_index contact is dropped entirely
    assert len(created) == 2
    names = {c.name for c in created}
    assert names == {"Jane Roe"}  # "Fabricated Ghost" (source_index=99) never made it in
    assert all(c.name != "Fabricated Ghost" for c in created)

    async with sm() as s:
        rows = (await s.execute(select(Contact).where(Contact.org_id == org_id))).scalars().all()
    assert len(rows) == 2

    # the fabricated-domain-guess email never survives, even attached to a validly sourced contact
    guessed_row = next(r for r in rows if r.confidence == 0.6)
    assert guessed_row.email is None

    # the verbatim email on the validly sourced contact is preserved exactly
    verified_row = next(r for r in rows if r.confidence == 0.9)
    assert verified_row.email == "jane.roe@acme.com"

    async with sm() as s:
        drafts = (await s.execute(select(OutreachDraft))).scalars().all()
    assert len(drafts) == 2  # one per surviving contact (both had a non-empty draft_message)
    for d in drafts:
        assert d.channel == "linkedin"
        assert d.status == "draft"  # THE SYSTEM NEVER SENDS
        assert len(d.body) <= 300


async def test_no_drafts_when_make_drafts_is_false(sm):
    org_id = await _make_org(sm)
    client = _FakeClient(json.dumps(_PAYLOAD))

    async with sm() as s:
        org = await s.get(Org, org_id)
        created = await discover_contacts(s, client, org, _EXCERPTS, make_drafts=False)

    assert len(created) == 2
    async with sm() as s:
        drafts = (await s.execute(select(OutreachDraft))).scalars().all()
    assert drafts == []


async def test_quarantined_response_yields_no_contacts_and_preserves_raw(sm):
    org_id = await _make_org(sm)
    client = _FakeClient("not valid json at all {")

    async with sm() as s:
        org = await s.get(Org, org_id)
        created = await discover_contacts(s, client, org, _EXCERPTS)

    assert created == []
    async with sm() as s:
        rows = (await s.execute(select(Contact).where(Contact.org_id == org_id))).scalars().all()
        quarantined = (await s.execute(select(Quarantine))).scalars().all()
    assert rows == []
    # hard rule 3: a failed structured-output call is quarantined WITH the raw response preserved.
    assert len(quarantined) == 1
    assert quarantined[0].raw_response == "not valid json at all {"


class _CapturingClient(_FakeClient):
    """Records the messages actually sent, so a test can prove the excerpts reached the prompt."""

    def __init__(self, content: str) -> None:
        super().__init__(content)
        self.sent: list[dict] = []

    async def complete(self, *, messages: list[dict], **kw) -> ChatResult:
        self.sent = messages
        return await super().complete(messages=messages, **kw)


async def test_real_template_injects_the_excerpt_text(sm):
    # Regression for the \\n-vs-\n placeholder bug: if the for-each-source marker fails to match,
    # the sources block is silently stripped and the model is asked to cite sources it never saw.
    org_id = await _make_org(sm)
    client = _CapturingClient(json.dumps({"contacts": []}))

    async with sm() as s:
        org = await s.get(Org, org_id)
        await discover_contacts(s, client, org, _EXCERPTS)

    user_msg = next(m["content"] for m in client.sent if m["role"] == "user")
    assert "jane.roe@acme.com" in user_msg  # the excerpt body made it into the outgoing prompt
    assert "[1]" in user_msg  # rendered as a numbered, citable source
    assert "{for each source" not in user_msg  # the marker was actually substituted


def test_linkedin_url_verbatim_both_branches():
    sources = ["Jane Roe, CIO. Profile: https://www.linkedin.com/in/janeroe/ and jane@acme.com."]
    # present verbatim (scheme/www/trailing-slash insensitive) -> kept exactly as returned
    assert linkedin_url_verbatim("https://linkedin.com/in/janeroe", sources) == (
        "https://linkedin.com/in/janeroe"
    )
    assert linkedin_url_verbatim("linkedin.com/in/JaneRoe", sources) == "linkedin.com/in/JaneRoe"
    # absent, a prefix of a real profile, or empty -> nulled, never kept on faith
    assert linkedin_url_verbatim("https://linkedin.com/in/johndoe", sources) is None
    assert linkedin_url_verbatim("https://linkedin.com/in/jane", sources) is None
    assert linkedin_url_verbatim(None, sources) is None
    assert linkedin_url_verbatim("", sources) is None


async def test_linkedin_url_is_kept_only_when_verbatim_in_a_source(sm):
    org_id = await _make_org(sm)
    excerpts = ExcerptSet(
        excerpts=[
            Excerpt(
                index=1,
                url="https://acme.example/team",
                kind="company_page",
                text="Jane Roe, CIO (https://www.linkedin.com/in/janeroe). "
                "John Doe, CTO. Reach Jane at jane.roe@acme.com.",
            )
        ]
    )
    payload = {
        "contacts": [
            {
                "name": "Jane Roe",
                "title": "CIO",
                "contact_type": "hiring_manager",
                "linkedin_url": "https://linkedin.com/in/janeroe",
                "email": "jane.roe@acme.com",
                "confidence": 0.9,
                "source_index": 1,
                "why_this_person": "named CIO with a profile link on the team page",
                "draft_message": "Hi Jane.",
            },
            {
                "name": "John Doe",
                "title": "CTO",
                "contact_type": "exec",
                "linkedin_url": "https://linkedin.com/in/john-doe-cto",
                "email": None,
                "confidence": 0.7,
                "source_index": 1,
                "why_this_person": "profile URL guessed from the name, not in any source",
                "draft_message": "Hi John.",
            },
        ]
    }
    client = _FakeClient(json.dumps(payload))
    async with sm() as s:
        org = await s.get(Org, org_id)
        created = await discover_contacts(s, client, org, excerpts, make_drafts=False)
    assert len(created) == 2
    async with sm() as s:
        rows = (await s.execute(select(Contact).where(Contact.org_id == org_id))).scalars().all()
    by_name = {r.name: r for r in rows}
    assert by_name["Jane Roe"].linkedin_url == "https://linkedin.com/in/janeroe"
    assert by_name["John Doe"].linkedin_url is None  # fabricated slug nulled, person still kept


async def test_email_verbatim_rejects_substring_of_a_real_address():
    # roe@acme.com is a substring of jane.roe@acme.com but is NOT itself a source email token:
    # a substring match would silently reduce the verbatim guarantee to a domain guess.
    sources = ["Jane Roe, CIO. Reach her at jane.roe@acme.com."]
    assert email_verbatim("jane.roe@acme.com", sources) == "jane.roe@acme.com"
    assert email_verbatim("roe@acme.com", sources) is None
    assert email_verbatim("someone@acme.com", sources) is None  # pure domain guess
