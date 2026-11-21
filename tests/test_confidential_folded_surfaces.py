"""Confidential mode on every redacting surface, against names written with characters that render
as nothing or in compatibility letters (#210 review round 3, D-PRIV-1 amended): the Pipeline list
and the JSON API, the role page, the Posting tab, Calibrate and the quick round, a generated
document, and an online payload. Invented names; no network."""

from __future__ import annotations

import json
import re
import unicodedata
from types import SimpleNamespace

import pytest

from kestrel.db.models import Opportunity
from tests import quick_fixtures as qf

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

# "Acme Freight" as a reader sees it, written so a plain matcher would miss it.
_HIDDEN = (
    "Ac​me Freight",  # zero-width space
    "Ac‎me Freight",  # left-to-right mark
    "A⁣cme Freight",  # invisible separator
    "Ac͏me Fre️ight",  # combining grapheme joiner, variation selector
    "Ａｃｍｅ Freight",  # fullwidth letters
    "Acme Freight",  # no-break space
)


def _seen(fragment: str) -> str:
    """What a reader sees: tags removed, entities read, NFKC, invisible characters dropped."""
    import html as _html

    # Independent of kestrel.privacy.fold on purpose: the check must not share the code it tests.
    text = unicodedata.normalize("NFKC", _html.unescape(re.sub(r"<[^>]*>", " ", fragment)))
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf"
                   and ch != "\u034f" and not 0xFE00 <= ord(ch) <= 0xFE0F)


def _confidential(title: str) -> list[int]:
    from kestrel.db.engine import forget_engine
    from kestrel.db.models import DocMaster, DocSection, Setting

    ids = qf.seed(6)

    async def on(s):
        s.add(Setting(key="confidential_mode", value=True))
        m = DocMaster(track="employment", doc_kind="resume", name="Resume")
        s.add(m)
        await s.flush()
        s.add(DocSection(doc_master_id=m.id, section_kind="role", title="Experience",
                         org_label="Acme Freight", org_label_redacted="Current Employer",
                         end_date="present"))
        for oid in ids:
            opp = await s.get(Opportunity, oid)
            opp.title = f"Analyst at {title}"
            opp.description_md = (f"Join {title} at the depot. You will sort parcels each "
                                  f"morning for **{title}** and log every late van.")
        await s.commit()

    qf.run(on)
    forget_engine()
    return ids


@pytest.mark.parametrize("written", _HIDDEN, ids=[f"form{i}" for i in range(len(_HIDDEN))])
def test_web_surfaces_hide_the_name_however_it_is_written(written):
    ids = _confidential(written)
    with qf.client() as c:
        pages = {
            "pipeline list": c.get("/pipeline").text,
            "api list": json.dumps(c.get("/api/opportunities").json(), ensure_ascii=False),
            "role page": c.get(f"/opportunities/{ids[0]}").text,
            "posting tab": c.get(f"/opportunities/{ids[0]}/posting").text,
            "calibrate": c.get("/calibrate?tab=detailed").text,
            "quick round": c.get("/calibrate/quick").text,
        }
    for name, body in pages.items():
        seen = _seen(body)
        assert "Acme Freight" not in seen and "Acme" not in seen, (name, written)
    assert "Current Employer" in _seen(pages["posting tab"])
    assert "Current Employer" in _seen(pages["api list"])


@pytest.mark.parametrize("written", _HIDDEN, ids=[f"form{i}" for i in range(len(_HIDDEN))])
def test_a_generated_document_hides_the_name_however_it_is_written(written):
    from kestrel.docs.catalog import org_redactor
    from kestrel.privacy import from_pairs

    emap = from_pairs([("Acme Freight", "Current Employer")], [])
    redact = org_redactor(SimpleNamespace(redaction=emap))
    out = redact(f"Led the {written} depot rollout")
    assert "Acme" not in _seen(out) and "Current Employer" in out


@pytest.mark.parametrize("written", _HIDDEN, ids=[f"form{i}" for i in range(len(_HIDDEN))])
def test_an_online_payload_codes_the_name_and_restores_it_exactly(written):
    from kestrel.privacy import from_pairs

    emap = from_pairs([("Acme Freight", "Current Employer")], [])
    book = emap.codebook()
    messages = [{"role": "user", "content": f"Posting: {written} needs an analyst."}]
    sent = book.encode_messages(messages)
    assert "Acme" not in _seen(sent[0]["content"])
    assert book.decode(sent[0]["content"]) == messages[0]["content"]
