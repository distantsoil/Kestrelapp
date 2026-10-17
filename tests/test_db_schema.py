"""The migration creates the full schema, and WAL + busy_timeout are set on every connection."""

from __future__ import annotations

from sqlalchemy import text

EXPECTED_TABLES = {
    # SPEC.md 4
    "profile",
    "source",
    "scrape_run",
    "opportunity",
    "opportunity_embedding",
    "org",
    "evaluation",
    "quarantine",
    "pursuit",
    "status_event",
    "task",
    "run",
    "setting",
    "saved_view",
    "notification",
    # SPEC-MODULES documents
    "doc_master",
    "doc_layout",
    "doc_section",
    "doc_slot",
    "doc_variant",
    "doc_render",
    # research
    "org_research",
    "contact",
    "outreach_draft",
    "connection",
    "interaction",
    # interview
    "story",
    "story_usage",
    "prep_pack",
    "mock_session",
    # offers
    "offer",
    "engagement_offer",
    # learning
    "verdict_override",
    "selection_override",
    "label",
    "outcome",
    "calibration",
    "ml_model",
    # intelligence
    "comp_observation",
}


async def test_migration_creates_every_table(sm):
    async with sm() as s:
        rows = (
            (await s.execute(text("SELECT name FROM sqlite_master WHERE type='table'")))
            .scalars()
            .all()
        )
    tables = set(rows)
    missing = EXPECTED_TABLES - tables
    assert not missing, f"missing tables: {sorted(missing)}"
    assert "alembic_version" in tables


async def test_required_indexes_exist(sm):
    async with sm() as s:
        rows = (
            (await s.execute(text("SELECT name FROM sqlite_master WHERE type='index'")))
            .scalars()
            .all()
        )
    idx = set(rows)
    for name in (
        "ix_opportunity_content_hash",
        "ix_opportunity_dedup_group_id",
        "ix_opportunity_stage",
        "ix_opportunity_track",
        "ix_opportunity_org_id",
        "ix_opportunity_ghost_score",
        "ix_opportunity_source_external",
        "ix_task_status_priority",
        "ix_pursuit_status",
        "ix_pursuit_next_action_at",
        "ix_comp_observation_title_geo",
    ):
        assert name in idx, f"missing index {name}"


async def test_wal_and_busy_timeout_on_every_connection(sm):
    async with sm() as s:
        mode = (await s.execute(text("PRAGMA journal_mode"))).scalar()
        timeout = (await s.execute(text("PRAGMA busy_timeout"))).scalar()
        fk = (await s.execute(text("PRAGMA foreign_keys"))).scalar()
    assert str(mode).lower() == "wal"
    assert int(timeout) == 5000
    assert int(fk) == 1
