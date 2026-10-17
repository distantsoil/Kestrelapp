"""GATE (Phase 8). Cloud-safe: the dynamic P3 schema has exactly the right enums; a hallucinated
variant key is rejected (defaults, flagged) not silently substituted; a non-verbatim derived:false
import variant is flagged; and the rendered HTML bullets are doc_variant.text byte-for-byte. Local
(Playwright/Edge + pypdf): `render` produces a PDF per kind with a selectable text layer, within the
page limit, no generator/organization string in the metadata, and every bullet matching a
doc_variant.text row."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.models import DocVariant
from kestrel.docs.catalog import default_config, load_catalog, resolve, schema_inputs
from kestrel.docs.imports import flag_proposal
from kestrel.docs.render import bullets_in_html, render_html
from kestrel.docs.seed import seed_all
from kestrel.docs.select import select_config
from kestrel.docs.service import render_document
from kestrel.llm.client import ChatResult
from kestrel.llm.docselect import build_doc_select_schema, validate_doc_selection


# --------------------------------------------------------------------- cloud-safe unit gates
async def test_dynamic_p3_schema_has_exactly_the_right_enums(sm):
    async with sm() as s:
        ids = await seed_all(s)
        catalog = await load_catalog(s, ids["resume"])
    schema = build_doc_select_schema(**schema_inputs(catalog))
    slots = schema["properties"]["slots"]["properties"]
    assert slots["r1b1"]["enum"] == ["cost", "scale"]  # required -> no "hide"
    assert slots["r1b3"]["enum"] == ["team", "hide"]  # optional -> "hide" present
    assert "hide" not in slots["r1b1"]["enum"]
    assert schema["properties"]["summary"]["enum"] == ["transformation", "operator"]
    assert "r1b1" in schema["properties"]["slots"]["required"]
    # optional slots are still required KEYS, but their enum carries "hide" so they can be dropped
    assert "hide" in slots["r1b3"]["enum"] and "hide" in slots["r2b2"]["enum"]


def test_validation_rejects_a_hallucinated_key():
    slots = [
        {"key": "r1b1", "optional": False, "variants": [{"key": "cost"}, {"key": "scale"}]},
    ]
    errors = validate_doc_selection(
        {"layout": "executive", "summary": "s", "slots": {"r1b1": "MADE_UP"}},
        slots=slots, layouts=[{"key": "executive"}], summary_variants=[{"key": "s"}],
    )
    assert any("MADE_UP" in e for e in errors)  # a bad key is an error, never accepted


class _FakeP3LLM:
    """Returns a pydantically-valid P3 payload that names a HALLUCINATED variant key for r1b1."""

    def __init__(self, settings):
        self.settings = settings

    async def complete(self, *, tier, messages, schema=None, **_):
        payload = {
            "layout": "executive", "summary": "transformation",
            "slots": {"r1b1": "HALLUCINATED", "r1b2": "accuracy", "r2b1": "process"},
            "skill_groups": {"analysis": True, "process": True, "systems": False},
            "rationale": {}, "coherence_check": "ok",
        }
        return ChatResult(content=json.dumps(payload), model=self.settings.llm.tiers[tier].model)


async def test_hallucinated_key_falls_back_to_defaults_not_the_bad_key(sm):
    settings = get_settings()
    async with sm() as s:
        ids = await seed_all(s)
        catalog = await load_catalog(s, ids["resume"])
        sel = await select_config(s, _FakeP3LLM(settings), catalog)
    assert sel.defaults_used is True  # validation failure -> flagged
    assert sel.config["slots"]["r1b1"] == "cost"  # the DEFAULT, never the hallucinated key
    assert "HALLUCINATED" not in sel.config["slots"].values()


def test_import_flags_non_verbatim_derived_false_variant():
    from types import SimpleNamespace as NS

    source = "Cut managed-service spend by a third. Led a HIPAA remediation."
    value = NS(roles=[NS(company_label="Acme", slots=[NS(key="r1b1", variants=[
        NS(key="verbatim", text="Cut managed-service spend by a third.", derived=False),
        NS(key="invented", text="Tripled revenue and headcount overnight.", derived=False),
        NS(key="d1", text="Reduced managed-service spend materially.", derived=True),
    ])])])
    flags = flag_proposal(value, source)
    assert "Acme/r1b1/invented" in flags.non_verbatim  # not in the source -> flagged
    assert "Acme/r1b1/verbatim" not in flags.non_verbatim  # appears verbatim -> clean
    assert not flags.too_many_derived  # one derived is within the limit


def test_import_flags_too_many_derived_per_slot():
    from types import SimpleNamespace as NS

    value = NS(roles=[NS(company_label="Acme", slots=[NS(key="r1b1", variants=[
        NS(key="d1", text="a", derived=True), NS(key="d2", text="b", derived=True),
        NS(key="d3", text="c", derived=True),
    ])])])
    flags = flag_proposal(value, "")
    assert "Acme/r1b1" in flags.too_many_derived  # 3 > 2


def test_import_flags_non_verbatim_summary_variant():
    from types import SimpleNamespace as NS

    source = "Ran IT for a services firm."
    value = NS(
        roles=[],
        summary_variants=[
            NS(key="real", text="Ran IT for a services firm.", derived=False),
            NS(key="invented", text="Saved forty million dollars.", derived=False),
            NS(key="reworded", text="Led the whole technology function.", derived=True),
        ],
    )
    flags = flag_proposal(value, source)
    assert "summary/invented" in flags.non_verbatim  # invented summary prose caught (rule 2)
    assert "summary/real" not in flags.non_verbatim


async def test_accept_import_rejects_a_bad_proposal(sm):
    from types import SimpleNamespace as NS

    from kestrel.docs.imports import ImportRejected, accept_import

    source = "Cut spend by a third."
    bad = NS(
        roles=[NS(company_label="Acme", title="VP", start_date="2020", end_date="2024",
                  context_line="ctx", slots=[NS(key="r1b1", optional=False,
                  default_variant_key="ghost",  # not one of the variants -> reject
                  variants=[NS(key="v1", text="Cut spend by a third.", tags=[], derived=False)])])],
        summary_variants=[], skill_groups=[], proof_points=[],
    )
    async with sm() as s:
        with pytest.raises(ImportRejected):
            await accept_import(s, bad, track="employment", doc_kind="resume", name="x",
                                source_markdown=source)


async def _texts_by_key(s) -> dict[str, set[str]]:
    """Variant text keyed by variant key. A key may recur across slots, so the value is the set of
    texts stored under that key; a rendered bullet must match one of ITS OWN key's texts."""
    out: dict[str, set[str]] = {}
    for v in (await s.execute(select(DocVariant))).scalars().all():
        out.setdefault(v.key, set()).add(v.text)
    return out


def _assert_bullets_byte_for_byte(doc, by_key: dict[str, set[str]]) -> None:
    html_bullets = bullets_in_html(render_html(doc))
    assert html_bullets, "the gate is vacuous unless at least one bullet was rendered"
    # Every rendered bullet is the stored text of the variant key it claims to be, byte for byte.
    for vk, text in html_bullets:
        assert vk in by_key, f"data-vk {vk!r} is not a stored variant key"
        assert text in by_key[vk], f"bullet under {vk!r} is not that variant's text: {text!r}"
    # And the HTML carries exactly the resolved bullets, in order, no more and no fewer.
    expected = [(b.key, b.text) for blk in doc.blocks for r in blk.roles for b in r.bullets]
    assert html_bullets == expected


async def test_render_flags_defaults_used_on_unknown_variant_key(sm):
    async with sm() as s:
        ids = await seed_all(s)
        catalog = await load_catalog(s, ids["resume"])
        config = default_config(catalog)
        config["slots"]["r1b1"] = "DOES_NOT_EXIST"  # an operator-supplied config with a bad key
        # resolve substitutes the default but records it in `unresolved`; render_document then flags
        doc = resolve(catalog, config)
        assert "r1b1" in doc.unresolved
        assert doc.unresolved_keys["r1b1"] == "DOES_NOT_EXIST"  # the miss is NAMED, not silent
        # every bullet is still a real variant text (never the bad key)
        _assert_bullets_byte_for_byte(doc, await _texts_by_key(s))
        assert "DOES_NOT_EXIST" not in render_html(doc)


async def test_rendered_html_bullets_are_doc_variant_text_byte_for_byte(sm):
    async with sm() as s:
        ids = await seed_all(s)
        by_key = await _texts_by_key(s)
        for master_id in ids.values():
            catalog = await load_catalog(s, master_id)
            doc = resolve(catalog, default_config(catalog))
            _assert_bullets_byte_for_byte(doc, by_key)  # NO model text; every bullet is stored


async def test_resolve_tracks_unknown_summary_and_layout_keys(sm):
    """An unknown summary or layout key falls back (no summary block; default section order), and
    that fallback is recorded in `unresolved` with the key that was asked for, like a slot miss."""
    async with sm() as s:
        ids = await seed_all(s)
        catalog = await load_catalog(s, ids["resume"])
    config = default_config(catalog)
    config["summary"] = "NO_SUCH_SUMMARY"
    config["layout"] = "NO_SUCH_LAYOUT"
    doc = resolve(catalog, config)
    assert "summary" in doc.unresolved and "layout" in doc.unresolved
    assert doc.unresolved_keys == {"summary": "NO_SUCH_SUMMARY", "layout": "NO_SUCH_LAYOUT"}
    assert not any(b.type == "summary" for b in doc.blocks)  # never invented summary prose
    # A config that simply names no layout/summary is not a miss.
    clean = resolve(catalog, {"slots": default_config(catalog)["slots"]})
    assert clean.unresolved == []


def test_valid_slot_keys_is_the_swap_rule():
    from types import SimpleNamespace as NS

    from kestrel.docs.catalog import valid_slot_keys

    cat = NS(all_slots=lambda: [
        NS(key="req", optional=False, variants=[NS(key="a"), NS(key="b")]),
        NS(key="opt", optional=True, variants=[NS(key="c")]),
    ])
    assert valid_slot_keys(cat, "req") == {"a", "b"}  # required: no "hide"
    assert valid_slot_keys(cat, "opt") == {"c", "hide"}  # optional: "hide" allowed
    assert valid_slot_keys(cat, "nope") is None  # unknown slot


async def test_accept_import_rejects_a_zero_variant_slot(sm):
    """A slot with no variants would give the P3 schema an empty enum (unsatisfiable grammar) and
    has nothing to render; the write boundary refuses it with a message naming the slot."""
    from types import SimpleNamespace as NS

    from kestrel.docs.imports import ImportRejected, accept_import

    source = "Cut spend by a third."
    bad = NS(
        roles=[NS(company_label="Acme", title="VP", start_date="2020", end_date="2024",
                  context_line="ctx", slots=[
                      NS(key="r1b1", optional=False, default_variant_key="v1",
                         variants=[NS(key="v1", text="Cut spend by a third.", tags=[],
                                      derived=False)]),
                      NS(key="r1b2", optional=True, default_variant_key=None, variants=[]),
                  ])],
        summary_variants=[], skill_groups=[], proof_points=[],
    )
    async with sm() as s:
        with pytest.raises(ImportRejected, match="r1b2"):
            await accept_import(s, bad, track="employment", doc_kind="resume", name="x",
                                source_markdown=source)


# ------------------------------------------------------------------------- filename (1.4)
def test_slug_strips_without_recasing_the_operator_name():
    from kestrel.docs.render import _slug, filename_for

    assert _slug("McRae", default="x") == "McRae"  # NOT "Mcrae"
    assert _slug("O'Brien", default="x") == "OBrien"
    assert _slug("de la Cruz", default="x") == "delaCruz"
    assert _slug("  ", default="Org") == "Org"
    name = filename_for({"name": "Ann McRae"}, "Acme Corp", "VP of IT")
    assert name == "McRae_AcmeCorp_VPofIT.pdf"


class _FakePdf:
    """Stand-in for Playwright/Edge (local-required): a fixed one-page PDF payload."""

    async def __call__(self, html: str):
        from kestrel.docs.render import RenderedPdf

        return RenderedPdf(pdf=b"%PDF-1.4 fake", page_count=1)


async def test_filename_names_the_target_org_never_the_operators_employer(sm, monkeypatch):
    """SPEC-MODULES 1.4's {Org} is the TARGET organization. An earlier cut took the first roles
    block, i.e. the operator's current employer, and put it on every PDF's download name."""
    from kestrel.db.models import Opportunity, Org
    from kestrel.docs import service

    monkeypatch.setattr(service, "render_pdf", _FakePdf())
    settings = get_settings()
    async with sm() as s:
        ids = await seed_all(s)
        org = Org(name="Northwind Health", normalized_name="northwind health")
        s.add(org)
        await s.flush()
        opp = Opportunity(track="employment", opp_type="job_posting", org_id=org.id,
                          org_name="Northwind Health", title="Chief Information Officer",
                          stage="t2_done")
        s.add(opp)
        await s.commit()
        catalog = await load_catalog(s, ids["resume"])
        config = default_config(catalog)
        config["redact_orgs"] = True  # confidential mode on: the employer must not leak anywhere

        tailored = await render_document(
            s, settings, ids["resume"], config=config, opportunity_id=opp.id
        )
        general = await render_document(s, settings, ids["resume"], config=config)

    assert tailored.filename == "Rivera_NorthwindHealth_ChiefInformationOfficer.pdf"
    assert general.filename == "Rivera_General_Resume.pdf"
    for name in (tailored.filename, general.filename):
        for employer in ("Lakeside", "Brightline", "Regional"):
            assert employer not in name  # neither the employer nor its redacted label


async def test_filename_falls_back_to_the_posting_org_name_without_an_org_row(sm, monkeypatch):
    from kestrel.db.models import Opportunity
    from kestrel.docs import service

    monkeypatch.setattr(service, "render_pdf", _FakePdf())
    settings = get_settings()
    async with sm() as s:
        ids = await seed_all(s)
        opp = Opportunity(track="consulting", opp_type="signal", org_id=None,
                          org_name="Blue Ridge Logistics", title=None, stage="t2_done")
        s.add(opp)
        await s.commit()
        catalog = await load_catalog(s, ids["capability"])
        result = await render_document(
            s, settings, ids["capability"], config=default_config(catalog), opportunity_id=opp.id
        )
    # No title on a signal-derived opportunity: the doc kind stands in for the role slug.
    assert result.filename == "Rivera_BlueRidgeLogistics_Capability.pdf"


# --------------------------------------------------------------------------- local PDF gate
async def test_render_pdf_per_kind_passes_all_pdf_asserts(sm, monkeypatch):
    from playwright.async_api import async_playwright

    from tests.browser import launch_async

    async with async_playwright() as p:
        await (await launch_async(p, monkeypatch)).close()

    from pypdf import PdfReader

    settings = get_settings()
    async with sm() as s:
        ids = await seed_all(s)
        variant_texts = {v.text for v in (await s.execute(select(DocVariant))).scalars().all()}
        for kind, master_id in ids.items():
            catalog = await load_catalog(s, master_id)
            result = await render_document(
                s, settings, master_id, config=default_config(catalog)
            )
            assert result is not None
            html_bullets = [t for _k, t in bullets_in_html(result.html)]
            reader = PdfReader(result.pdf_path)

            # selectable text layer present
            extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
            assert extracted.strip(), f"{kind}: no selectable text"
            # within the page limit
            assert result.page_count <= settings.docs.max_pages
            # no generator or organization string anywhere in the metadata
            meta_blob = " ".join(str(v) for v in (reader.metadata or {}).values()).lower()
            assert "kestrel" not in meta_blob
            for org in ("lakeside", "brightline", "distribution project"):
                assert org not in meta_blob
            # every rendered bullet is a real doc_variant.text, present in the PDF text
            norm = " ".join(extracted.split())
            for bullet in html_bullets:
                assert bullet in variant_texts  # byte-for-byte vs the DB
                assert " ".join(bullet.split()) in norm  # and actually in the PDF
