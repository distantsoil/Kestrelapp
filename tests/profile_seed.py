"""A fictional Profile for the Profile screen tests (the locked design mockup (kept privately)
locked.html): Jordan A. Rivera's saved About you, Employment and Consulting terms, and one resume
master with a role and two bullets, so the Documents tab has a page to preview.

Assumes the `_isolate_data_home` fixture (a throwaway data directory). Nothing here touches the
network or a model."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker

from kestrel import paths
from kestrel.db import migrate
from kestrel.db.engine import forget_engine, make_async_engine, reset_engine
from kestrel.db.models import DocMaster, DocSection, DocSlot, DocVariant
from kestrel.services import profile_edit as pe
from kestrel.services.tracks import BOTH, save_enabled
from tests.retry_seed import _wipe_db

BULLETS = (
    "Led process improvement across two distribution centers, raising throughput 22%.",
    "Rebuilt demand forecasting in SQL, cutting excess inventory 18%.",
)


async def seed_async() -> int:
    await reset_engine()
    _wipe_db()
    migrate.run_migrations()
    pe.profile_path().unlink(missing_ok=True)
    engine = make_async_engine(paths.db_path())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        await save_enabled(s, BOTH)
        # The shipped layouts, as first-run seeding writes them: without them the Look card's
        # Layout choice has nothing to offer (the empty select in the first review shots).
        from kestrel.docs.seed import _ensure_layouts

        await _ensure_layouts(s)
        master = DocMaster(track="employment", name="Operations resume", doc_kind="resume",
                           summary_variants=[], skill_groups=[])
        s.add(master)
        await s.flush()
        sec = DocSection(doc_master_id=master.id, section_kind="role",
                         org_label="A regional food distributor", title="Operations Analyst",
                         sort_order=0)
        s.add(sec)
        await s.flush()
        for i, text in enumerate(BULLETS):
            slot = DocSlot(doc_section_id=sec.id, key=f"r1_b{i + 1}", sort_order=i,
                           optional=False, default_variant_key="original")
            s.add(slot)
            await s.flush()
            s.add(DocVariant(doc_slot_id=slot.id, key="original", derived=False,
                             tags=["operations"], text=text))
        await s.commit()
        master_id = master.id
    await engine.dispose()
    forget_engine()
    pe.save_section("employment", "about", {
        "first_name": "Jordan", "last_name": "Rivera", "full_name": "Jordan Rivera",
        "headline": "Operations analyst", "email": "jordan.rivera@example.com",
        "phone": "(512) 555-0199", "location": "Austin, TX", "work_auth": "US citizen",
        "requires_sponsorship": False, "must_have": [], "must_avoid": []})
    pe.save_section("employment", "employment", {"employment_terms": {
        "target_titles": ["Senior Operations Analyst", "Process Improvement Lead"],
        "anti_titles": ["Sales Development Rep"], "excluded_employment_types": [],
        "seniority_floor": "", "comp_floor_usd": 110000, "comp_target_usd": 140000}})
    pe.save_section("consulting", "consulting", {"consulting_terms": {
        "service_lines": ["Order-to-cash cleanup"], "day_rate_floor": 1800,
        "day_rate_target": 2400}})
    return master_id


def seed() -> int:
    return asyncio.run(seed_async())
