"""Every ORM model across SPEC.md 4, SPEC-MODULES.md, and the Phase 1 notification table.

Enum-like columns are String (see enums.py). Timestamps are ISO8601 UTC text. JSON blobs use the
JSON column type. FK columns are declared for every relationship; ORM relationship() is added only
where later phases traverse it (core pipeline + the document hierarchy)."""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from kestrel.db.base import Base, utcnow_iso


# ============================================================================ 4.1 Profile
class Profile(Base):
    __tablename__ = "profile"
    id: Mapped[int] = mapped_column(primary_key=True)
    track: Mapped[str] = mapped_column(String, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    full_name: Mapped[str | None] = mapped_column(String, nullable=True)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    headline: Mapped[str | None] = mapped_column(Text, nullable=True)
    narrative: Mapped[str | None] = mapped_column(Text, nullable=True)
    work_auth: Mapped[str | None] = mapped_column(String, nullable=True)
    requires_sponsorship: Mapped[bool] = mapped_column(Boolean, default=False)
    geo_rules: Mapped[dict | None] = mapped_column(JSON, default=dict)
    must_have: Mapped[list | None] = mapped_column(JSON, default=list)
    must_avoid: Mapped[list | None] = mapped_column(JSON, default=list)
    proof_points: Mapped[list | None] = mapped_column(JSON, default=list)
    employment_terms: Mapped[dict | None] = mapped_column(JSON, default=dict)
    consulting_terms: Mapped[dict | None] = mapped_column(JSON, default=dict)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow_iso, onupdate=utcnow_iso)


# ============================================================================ 4.2 Sources
class Source(Base):
    __tablename__ = "source"
    id: Mapped[int] = mapped_column(primary_key=True)
    track: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    config: Mapped[dict | None] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    cadence_hours: Mapped[int] = mapped_column(Integer, default=24)
    last_run_at: Mapped[str | None] = mapped_column(String, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    health: Mapped[dict | None] = mapped_column(JSON, default=dict)
    # shipped (sources.yml) | generated (built from the profile, rebuilt on every profile save)
    # | custom (added or edited by the operator; a rebuild never touches it). D-SRC-2.
    origin: Mapped[str] = mapped_column(String, default="shipped", server_default="shipped")


class ScrapeRun(Base):
    __tablename__ = "scrape_run"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"), index=True)
    started_at: Mapped[str] = mapped_column(String, default=utcnow_iso)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="running")
    raw_path: Mapped[str | None] = mapped_column(String, nullable=True)
    counts: Mapped[dict | None] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


# ============================================================================ 4.3 Org / Opportunity
class Org(Base):
    __tablename__ = "org"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    normalized_name: Mapped[str] = mapped_column(String, unique=True, index=True)
    domain: Mapped[str | None] = mapped_column(String, nullable=True)
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    size_band: Mapped[str | None] = mapped_column(String, nullable=True)
    sector: Mapped[str | None] = mapped_column(String, nullable=True)
    ownership: Mapped[str | None] = mapped_column(String, nullable=True)
    hq_location: Mapped[str | None] = mapped_column(String, nullable=True)
    signals: Mapped[list | None] = mapped_column(JSON, default=list)
    first_seen_at: Mapped[str] = mapped_column(String, default=utcnow_iso)
    last_seen_at: Mapped[str] = mapped_column(String, default=utcnow_iso)

    opportunities: Mapped[list[Opportunity]] = relationship(back_populates="org")


class Opportunity(Base):
    __tablename__ = "opportunity"
    id: Mapped[int] = mapped_column(primary_key=True)
    track: Mapped[str] = mapped_column(String, index=True)
    opp_type: Mapped[str] = mapped_column(String)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("org.id"), nullable=True, index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("source.id"), nullable=True)
    scrape_run_id: Mapped[int | None] = mapped_column(ForeignKey("scrape_run.id"), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    org_name: Mapped[str | None] = mapped_column(String, nullable=True)
    org_url: Mapped[str | None] = mapped_column(String, nullable=True)
    location_raw: Mapped[str | None] = mapped_column(String, nullable=True)
    country: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str | None] = mapped_column(String, nullable=True)
    city: Mapped[str | None] = mapped_column(String, nullable=True)
    is_remote: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    remote_type: Mapped[str] = mapped_column(String, default="unknown")
    employment_type: Mapped[str | None] = mapped_column(String, nullable=True)
    comp_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    comp_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    comp_currency: Mapped[str | None] = mapped_column(String, nullable=True)
    comp_interval: Mapped[str | None] = mapped_column(String, nullable=True)
    comp_source: Mapped[str] = mapped_column(String, default="none")
    description_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_tokens: Mapped[int] = mapped_column(Integer, default=0)
    date_posted: Mapped[str | None] = mapped_column(String, nullable=True)
    event_date: Mapped[str | None] = mapped_column(String, nullable=True)
    trigger_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    first_seen_at: Mapped[str] = mapped_column(String, default=utcnow_iso)
    last_seen_at: Mapped[str] = mapped_column(String, default=utcnow_iso)
    seen_count: Mapped[int] = mapped_column(Integer, default=1)
    ghost_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    previous_description_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    dedup_group_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    is_canonical: Mapped[bool] = mapped_column(Boolean, default=True)
    liveness: Mapped[str] = mapped_column(String, default="unchecked")
    liveness_checked_at: Mapped[str | None] = mapped_column(String, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    stage: Mapped[str] = mapped_column(String, default="new", index=True)
    stage_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    pre_score: Mapped[float] = mapped_column(Float, default=0.0)
    # Hides an opportunity from the active pipeline without touching `stage` (which tracks tier
    # progression, not visibility, and has no 'archived' value -- SPEC-INTERFACES 2.1 opportunity
    # detail actions). Reversible: set on Archive, cleared on Unarchive. ISO8601 UTC text like every
    # other timestamp column here; None means not archived.
    archived_at: Mapped[str | None] = mapped_column(String, nullable=True)
    # Why the operator hid it: applied | not_interested | later | other (ARCHIVE_REASONS in
    # kestrel.db.enums). None for rows hidden before 0010 or without a choice. Annotates
    # archived_at; it never replaces it.
    archive_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    org: Mapped[Org | None] = relationship(back_populates="opportunities")
    evaluations: Mapped[list[Evaluation]] = relationship(back_populates="opportunity")
    embedding: Mapped[OpportunityEmbedding | None] = relationship(
        back_populates="opportunity", uselist=False
    )
    pursuit: Mapped[Pursuit | None] = relationship(back_populates="opportunity", uselist=False)


Index("ix_opportunity_track_stage", Opportunity.track, Opportunity.stage)
# Re-seen lookup on every scrape is by (source_id, external_id); index it so ingest stays fast.
Index("ix_opportunity_source_external", Opportunity.source_id, Opportunity.external_id)


class OpportunityEmbedding(Base):
    __tablename__ = "opportunity_embedding"
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunity.id"), primary_key=True)
    model: Mapped[str] = mapped_column(String)
    vector: Mapped[bytes] = mapped_column(LargeBinary)  # float32 numpy.tobytes()
    dims: Mapped[int] = mapped_column(Integer)
    # sha256 of the exact text that produced `vector`. The embed handler re-embeds when this no
    # longer matches, so a re-seen posting whose description changed does not keep a stale vector.
    text_hash: Mapped[str | None] = mapped_column(String, nullable=True)

    opportunity: Mapped[Opportunity] = relationship(back_populates="embedding")


# ============================================================================ 4.4 Evaluation
class Evaluation(Base):
    __tablename__ = "evaluation"
    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunity.id"), index=True)
    tier: Mapped[int] = mapped_column(Integer)
    track: Mapped[str] = mapped_column(String)
    # The opportunity content version this evaluation was made against, so a re-eval of unchanged
    # content is idempotent (SPEC.md 6.3 re-scrape path; hard rule 5). 0 for pre-migration rows.
    content_version: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String, nullable=True)
    verdict: Mapped[str | None] = mapped_column(String, nullable=True)
    verdict_model: Mapped[str | None] = mapped_column(String, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    weighted_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    forced_by: Mapped[str | None] = mapped_column(String, nullable=True)
    dimensions: Mapped[dict | None] = mapped_column(JSON, default=dict)
    blocks: Mapped[dict | None] = mapped_column(JSON, default=dict)
    blockers: Mapped[list | None] = mapped_column(JSON, default=list)
    legitimacy: Mapped[dict | None] = mapped_column(JSON, default=dict)
    few_shot_ids: Mapped[list | None] = mapped_column(JSON, default=list)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String, default=utcnow_iso)

    opportunity: Mapped[Opportunity] = relationship(back_populates="evaluations")


class Quarantine(Base):
    __tablename__ = "quarantine"
    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunity.id"), nullable=True)
    tier: Mapped[int] = mapped_column(Integer)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String, default=utcnow_iso)


# Migration 0016 (D-UI-58): the live run summary reads these by time on a 5 s poll.
Index("ix_evaluation_created", Evaluation.created_at)
Index("ix_quarantine_created", Quarantine.created_at)
Index("ix_scrape_run_started", ScrapeRun.started_at)


# ============================================================================ 4.5 Tracking
class Pursuit(Base):
    __tablename__ = "pursuit"
    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunity.id"), unique=True)
    track: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, index=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("doc_render.id"), nullable=True)
    cover_letter_id: Mapped[int | None] = mapped_column(ForeignKey("doc_render.id"), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    applied_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_activity_at: Mapped[str | None] = mapped_column(String, nullable=True)
    next_action_at: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    next_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    outcome_stage: Mapped[str | None] = mapped_column(String, nullable=True)
    outcome_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    # SPEC.md 5.6: once this pursuit is applied (or later), a NEW posting that dedups into the
    # same group is hidden on arrival as a re-post, unless this is on. On for aggregators that
    # re-list one title many times and the operator wants to see each listing.
    allow_reposts: Mapped[bool] = mapped_column(Boolean, default=False)

    opportunity: Mapped[Opportunity] = relationship(back_populates="pursuit")
    status_events: Mapped[list[StatusEvent]] = relationship(back_populates="pursuit")


class StatusEvent(Base):
    __tablename__ = "status_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    pursuit_id: Mapped[int] = mapped_column(ForeignKey("pursuit.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String, nullable=True)
    to_status: Mapped[str] = mapped_column(String)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[str] = mapped_column(String, default=utcnow_iso)

    pursuit: Mapped[Pursuit] = relationship(back_populates="status_events")


# ============================================================================ 4.6 Infrastructure
class Task(Base):
    __tablename__ = "task"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String)
    payload: Mapped[dict | None] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, default="pending")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    lease_until: Mapped[str | None] = mapped_column(String, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow_iso, onupdate=utcnow_iso)


Index("ix_task_status_priority", Task.status, Task.priority)
# Migration 0016 (D-UI-58): the live run summary reads tasks, evaluations, quarantine rows and
# scrape runs by time on a 5 s poll.
Index("ix_task_updated", Task.updated_at)
# Migration 0014 (release round review, finding 4): one live scoring task per (type, opportunity),
# so two worker processes enqueuing at once cannot both insert. enqueue_once uses INSERT OR IGNORE.
Index(
    "ux_task_live_scoring",
    Task.type,
    func.json_extract(Task.payload, "$.opportunity_id"),
    unique=True,
    sqlite_where=text(
        "status IN ('pending', 'leased') AND type IN ('t1', 't2', 't3', 'rescore') "
        "AND json_extract(payload, '$.opportunity_id') IS NOT NULL"
    ),
)


class Run(Base):
    __tablename__ = "run"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    track: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[str] = mapped_column(String, default=utcnow_iso)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="running")
    counts: Mapped[dict | None] = mapped_column(JSON, default=dict)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "local" or "hosted": which tier set this run resolved (SPEC.md 6.4, D-HOSTED-2). NULL for
    # runs recorded before migration 0009; each Evaluation also names the model that answered.
    mode: Mapped[str | None] = mapped_column(String, nullable=True)


class Setting(Base):
    __tablename__ = "setting"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow_iso, onupdate=utcnow_iso)


class SavedView(Base):
    __tablename__ = "saved_view"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    track: Mapped[str | None] = mapped_column(String, nullable=True)
    filters: Mapped[dict | None] = mapped_column(JSON, default=dict)
    sort: Mapped[dict | None] = mapped_column(JSON, default=dict)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)


class Notification(Base):
    __tablename__ = "notification"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    severity: Mapped[str] = mapped_column(String, default="info")
    title: Mapped[str] = mapped_column(String)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunity.id"), nullable=True)
    pursuit_id: Mapped[int | None] = mapped_column(ForeignKey("pursuit.id"), nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow_iso, index=True)
    read_at: Mapped[str | None] = mapped_column(String, nullable=True)
    delivered_channels: Mapped[list | None] = mapped_column(JSON, default=list)


# ============================================================================ Modules 1: Documents
class DocMaster(Base):
    __tablename__ = "doc_master"
    id: Mapped[int] = mapped_column(primary_key=True)
    track: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    doc_kind: Mapped[str] = mapped_column(String)
    header: Mapped[dict | None] = mapped_column(JSON, default=dict)
    summary_variants: Mapped[list | None] = mapped_column(JSON, default=list)
    skill_groups: Mapped[list | None] = mapped_column(JSON, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Optional sections (D-DOC-3): the operator's own typed entries, printed only when present,
    # never model-selected. Field names per kind: kestrel.docs.sections.FIELDS.
    education: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    certifications: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    licenses: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    projects: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    # Saved presentation choices for this document (D-DOC-4): {"accent": "#rrggbb"}.
    look: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    # D-PRIV-1 (migration 0015): when the document was last saved; the combined confidential
    # map hides an employer only while it is current in the newest document naming it.
    updated_at: Mapped[str | None] = mapped_column(String, nullable=True, default=utcnow_iso,
                                                   onupdate=utcnow_iso)

    sections: Mapped[list[DocSection]] = relationship(back_populates="master")


class DocLayout(Base):
    __tablename__ = "doc_layout"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String, unique=True, index=True)
    label: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_file: Mapped[str | None] = mapped_column(String, nullable=True)
    section_order: Mapped[list | None] = mapped_column(JSON, default=list)
    max_roles_shown: Mapped[int | None] = mapped_column(Integer, nullable=True)


class DocSection(Base):
    __tablename__ = "doc_section"
    id: Mapped[int] = mapped_column(primary_key=True)
    doc_master_id: Mapped[int] = mapped_column(ForeignKey("doc_master.id"), index=True)
    section_kind: Mapped[str] = mapped_column(String)
    org_label: Mapped[str | None] = mapped_column(String, nullable=True)
    org_label_redacted: Mapped[str | None] = mapped_column(String, nullable=True)
    # D-PRIV-1 (migration 0015): the exact names confidential mode hides for this marked role.
    hide_names: Mapped[list | None] = mapped_column(JSON, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    start_date: Mapped[str | None] = mapped_column(String, nullable=True)
    end_date: Mapped[str | None] = mapped_column(String, nullable=True)
    context_line: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    master: Mapped[DocMaster] = relationship(back_populates="sections")
    slots: Mapped[list[DocSlot]] = relationship(back_populates="section")


class DocSlot(Base):
    __tablename__ = "doc_slot"
    id: Mapped[int] = mapped_column(primary_key=True)
    doc_section_id: Mapped[int] = mapped_column(ForeignKey("doc_section.id"), index=True)
    key: Mapped[str] = mapped_column(String)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    optional: Mapped[bool] = mapped_column(Boolean, default=False)
    default_variant_key: Mapped[str | None] = mapped_column(String, nullable=True)

    section: Mapped[DocSection] = relationship(back_populates="slots")
    variants: Mapped[list[DocVariant]] = relationship(back_populates="slot")


class DocVariant(Base):
    __tablename__ = "doc_variant"
    id: Mapped[int] = mapped_column(primary_key=True)
    doc_slot_id: Mapped[int] = mapped_column(ForeignKey("doc_slot.id"), index=True)
    key: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(Text)  # verbatim, byte-for-byte as it prints (hard rule 2)
    tags: Mapped[list | None] = mapped_column(JSON, default=list)
    derived: Mapped[bool] = mapped_column(Boolean, default=False)

    slot: Mapped[DocSlot] = relationship(back_populates="variants")


class DocRender(Base):
    __tablename__ = "doc_render"
    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunity.id"), nullable=True)
    doc_master_id: Mapped[int] = mapped_column(ForeignKey("doc_master.id"))
    config: Mapped[dict | None] = mapped_column(JSON, default=dict)
    rationale: Mapped[dict | None] = mapped_column(JSON, default=dict)
    template: Mapped[str | None] = mapped_column(String, nullable=True)
    pdf_path: Mapped[str | None] = mapped_column(String, nullable=True)
    filename: Mapped[str | None] = mapped_column(String, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    defaults_used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String, default=utcnow_iso)


# ============================================================================ Modules 2: Research
class OrgResearch(Base):
    __tablename__ = "org_research"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("org.id"), index=True)
    summary_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    signals: Mapped[list | None] = mapped_column(JSON, default=list)
    unknowns: Mapped[list | None] = mapped_column(JSON, default=list)
    angle: Mapped[str | None] = mapped_column(Text, nullable=True)
    sources: Mapped[list | None] = mapped_column(JSON, default=list)
    researched_at: Mapped[str] = mapped_column(String, default=utcnow_iso)
    ttl_days: Mapped[int] = mapped_column(Integer, default=30)
    insufficient_sources: Mapped[bool] = mapped_column(Boolean, default=False)


class Contact(Base):
    __tablename__ = "contact"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("org.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    contact_type: Mapped[str | None] = mapped_column(String, nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(String, nullable=True)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    relationship: Mapped[str] = mapped_column(String, default="none")
    # Prior employers (org names), for warm-path tiers 2/4 (SPEC-MODULES 2.3). The LinkedIn CSV
    # gives only the current company; the operator or P6 can add history here.
    former_orgs: Mapped[list | None] = mapped_column(JSON, default=list, nullable=True)
    last_contacted_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow_iso)


class OutreachDraft(Base):
    __tablename__ = "outreach_draft"
    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunity.id"), nullable=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contact.id"), index=True)
    channel: Mapped[str] = mapped_column(String)
    subject: Mapped[str | None] = mapped_column(String, nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, default="draft")
    created_at: Mapped[str] = mapped_column(String, default=utcnow_iso)


class Connection(Base):
    __tablename__ = "connection"
    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contact.id"), index=True)
    how_known: Mapped[str | None] = mapped_column(Text, nullable=True)
    strength: Mapped[str | None] = mapped_column(String, nullable=True)
    last_interaction_at: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Interaction(Base):
    __tablename__ = "interaction"
    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contact.id"), index=True)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunity.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String)
    direction: Mapped[str | None] = mapped_column(String, nullable=True)
    occurred_at: Mapped[str] = mapped_column(String, default=utcnow_iso)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)


# ============================================================================ Modules 3: Interview
class Story(Base):
    __tablename__ = "story"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String)
    situation: Mapped[str | None] = mapped_column(Text, nullable=True)
    task: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    reflection: Mapped[str | None] = mapped_column(Text, nullable=True)
    proof_point_key: Mapped[str | None] = mapped_column(String, nullable=True)
    themes: Mapped[list | None] = mapped_column(JSON, default=list)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    is_canonical: Mapped[bool] = mapped_column(Boolean, default=False)
    operator_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    embedding_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow_iso, onupdate=utcnow_iso)


class StoryUsage(Base):
    __tablename__ = "story_usage"
    id: Mapped[int] = mapped_column(primary_key=True)
    story_id: Mapped[int] = mapped_column(ForeignKey("story.id"), index=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunity.id"))
    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow_iso)


class PrepPack(Base):
    __tablename__ = "prep_pack"
    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunity.id"), index=True)
    content_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    stories: Mapped[dict | None] = mapped_column(JSON, default=dict)
    questions_to_ask: Mapped[list | None] = mapped_column(JSON, default=list)
    panel: Mapped[list | None] = mapped_column(JSON, default=list)
    pdf_path: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow_iso)


class MockSession(Base):
    __tablename__ = "mock_session"
    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunity.id"), index=True)
    mode: Mapped[str] = mapped_column(String)
    started_at: Mapped[str] = mapped_column(String, default=utcnow_iso)
    ended_at: Mapped[str | None] = mapped_column(String, nullable=True)
    question_count: Mapped[int] = mapped_column(Integer, default=0)
    mean_scores: Mapped[dict | None] = mapped_column(JSON, default=dict)
    transcript: Mapped[list | None] = mapped_column(JSON, default=list)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


# ============================================================================ Modules 4: Offers
class Offer(Base):
    __tablename__ = "offer"
    id: Mapped[int] = mapped_column(primary_key=True)
    pursuit_id: Mapped[int] = mapped_column(ForeignKey("pursuit.id"), index=True)
    base: Mapped[float | None] = mapped_column(Float, nullable=True)
    bonus_target_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    equity_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    equity_value_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    signing: Mapped[float | None] = mapped_column(Float, nullable=True)
    benefits_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    pto_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_date: Mapped[str | None] = mapped_column(String, nullable=True)
    deadline: Mapped[str | None] = mapped_column(String, nullable=True)
    level_title: Mapped[str | None] = mapped_column(String, nullable=True)
    reports_to: Mapped[str | None] = mapped_column(String, nullable=True)
    location_policy: Mapped[str | None] = mapped_column(String, nullable=True)
    received_at: Mapped[str] = mapped_column(String, default=utcnow_iso)
    status: Mapped[str] = mapped_column(String, default="verbal")
    notes_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The operator's own 0-5 ratings for the comparison matrix's qualitative axes (level/scope,
    # reporting line, location/travel, org risk, trajectory fit). Judgments the tool must not make
    # for them, so until they are set the matrix renormalizes over the axes actually present and
    # this offer is not penalised for them. Migration 0018.
    qual_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class EngagementOffer(Base):
    __tablename__ = "engagement_offer"
    id: Mapped[int] = mapped_column(primary_key=True)
    pursuit_id: Mapped[int] = mapped_column(ForeignKey("pursuit.id"), index=True)
    day_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    days_committed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_weeks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_terms: Mapped[str | None] = mapped_column(Text, nullable=True)
    expenses: Mapped[str | None] = mapped_column(Text, nullable=True)
    renewal_terms: Mapped[str | None] = mapped_column(Text, nullable=True)
    exclusivity: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_terms: Mapped[str | None] = mapped_column(Text, nullable=True)
    notice_period: Mapped[str | None] = mapped_column(String, nullable=True)
    received_at: Mapped[str] = mapped_column(String, default=utcnow_iso)
    status: Mapped[str] = mapped_column(String, default="verbal")
    notes_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Same operator ratings as Offer.qual_json, for the consulting comparison row.
    qual_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


# ============================================================================ Modules 5: Learning
class VerdictOverride(Base):
    __tablename__ = "verdict_override"
    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunity.id"), index=True)
    evaluation_id: Mapped[int | None] = mapped_column(ForeignKey("evaluation.id"), nullable=True)
    system_verdict: Mapped[str | None] = mapped_column(String, nullable=True)
    system_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    operator_verdict: Mapped[str | None] = mapped_column(String, nullable=True)
    operator_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    dimension_corrections: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow_iso)


class SelectionOverride(Base):
    __tablename__ = "selection_override"
    id: Mapped[int] = mapped_column(primary_key=True)
    doc_render_id: Mapped[int] = mapped_column(ForeignKey("doc_render.id"), index=True)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunity.id"), nullable=True)
    slot_key: Mapped[str] = mapped_column(String)
    model_choice: Mapped[str | None] = mapped_column(String, nullable=True)
    operator_choice: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow_iso)


class Label(Base):
    __tablename__ = "label"
    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunity.id"), index=True)
    track: Mapped[str] = mapped_column(String)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    verdict: Mapped[str | None] = mapped_column(String, nullable=True)
    dimensions: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String, default="manual")
    created_at: Mapped[str] = mapped_column(String, default=utcnow_iso)


class Outcome(Base):
    __tablename__ = "outcome"
    id: Mapped[int] = mapped_column(primary_key=True)
    pursuit_id: Mapped[int] = mapped_column(ForeignKey("pursuit.id"), index=True)
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunity.id"))
    final_status: Mapped[str | None] = mapped_column(String, nullable=True)
    furthest_stage: Mapped[str | None] = mapped_column(String, nullable=True)
    days_to_first_response: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator_retrospective: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[str] = mapped_column(String, default=utcnow_iso)


class Calibration(Base):
    __tablename__ = "calibration"
    id: Mapped[int] = mapped_column(primary_key=True)
    track: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=utcnow_iso)
    n_samples: Mapped[int] = mapped_column(Integer, default=0)
    previous_weights: Mapped[dict | None] = mapped_column(JSON, default=dict)
    proposed_weights: Mapped[dict | None] = mapped_column(JSON, default=dict)
    cv_mae_previous: Mapped[float | None] = mapped_column(Float, nullable=True)
    cv_mae_proposed: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String, default="proposed")
    decided_at: Mapped[str | None] = mapped_column(String, nullable=True)


class MlModel(Base):
    __tablename__ = "ml_model"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    track: Mapped[str] = mapped_column(String)
    version: Mapped[str] = mapped_column(String)
    trained_at: Mapped[str] = mapped_column(String, default=utcnow_iso)
    n_samples: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[dict | None] = mapped_column(JSON, default=dict)
    artifact_path: Mapped[str | None] = mapped_column(String, nullable=True)
    pca_artifact_path: Mapped[str | None] = mapped_column(String, nullable=True)
    sklearn_version: Mapped[str | None] = mapped_column(String, nullable=True)
    feature_names: Mapped[list | None] = mapped_column(JSON, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    promoted_at: Mapped[str | None] = mapped_column(String, nullable=True)
    promoted_by: Mapped[str | None] = mapped_column(String, nullable=True)


# ===================================================================== Modules 6: Comp corpus
class CompObservation(Base):
    __tablename__ = "comp_observation"
    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunity.id"), nullable=True)
    normalized_title: Mapped[str] = mapped_column(String)
    seniority_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    geo_bucket: Mapped[str | None] = mapped_column(String, nullable=True)
    org_size_band: Mapped[str | None] = mapped_column(String, nullable=True)
    org_ownership: Mapped[str | None] = mapped_column(String, nullable=True)
    sector: Mapped[str | None] = mapped_column(String, nullable=True)
    min: Mapped[float | None] = mapped_column(Float, nullable=True)
    max: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String, nullable=True)
    interval: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    observed_at: Mapped[str] = mapped_column(String, default=utcnow_iso)


Index("ix_comp_observation_title_geo", CompObservation.normalized_title, CompObservation.geo_bucket)


class FrontierTransmission(Base):
    """Audit trail for Tier 3 escalation (SPEC 6.4, hard rule 1). One row per ACTUAL transmission
    to the hosted frontier: what was sent (payload, byte-for-byte), where (provider/base_url/model),
    when (created_at), whether confidential redaction was applied, and the confirmation the operator
    approved (payload_sha256 — the token that gated the send, binding preview to transmission)."""

    __tablename__ = "frontier_transmission"
    id: Mapped[int] = mapped_column(primary_key=True)
    # index=True matches migration 0005's ix_frontier_transmission_opportunity, so autogenerate
    # does not propose dropping it.
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunity.id"), index=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow_iso)
    provider: Mapped[str] = mapped_column(String)  # the backend name, e.g. "frontier"
    base_url: Mapped[str] = mapped_column(String)  # the destination the data actually went to
    model: Mapped[str] = mapped_column(String)
    # employer redacted before send:
    confidential: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict | None] = mapped_column(JSON, default=dict)  # exact messages transmitted
    payload_sha256: Mapped[str] = mapped_column(String)  # confirmation token the operator approved
    prompt_tokens_estimate: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String)  # "sent" | "failed" | "quarantined"
    evaluation_id: Mapped[int | None] = mapped_column(ForeignKey("evaluation.id"), nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    # What the provider billed for this send (BYOK, SPEC 6.4): actual token counts from the
    # response and the USD cost when the provider reports one. NULL means "not reported", which is
    # rendered as unknown, never as free.
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Did the payload reach the provider? False ONLY for a failure to establish the connection
    # (refused, DNS, connect timeout), where a retry cannot duplicate an egress. True for every
    # HTTP error and for a timeout waiting for the reply. Migration 0019. Defaults True: an unknown
    # outcome is treated as egressed, because a duplicate egress cannot be undone.
    egressed: Mapped[bool] = mapped_column(Boolean, default=True)


# One transmission per confirmed payload (review finding, Tier 3 double egress).
# transmit_escalation inserts and COMMITS this row as a reservation BEFORE the payload leaves the
# concurrent confirmations of the same previewed payload cannot both send: the second reservation
# fails here instead of racing the first. Application-level "have I sent this?" checks cannot close
# that window; a unique constraint can.
Index(
    "ux_frontier_transmission_payload",
    FrontierTransmission.opportunity_id,
    FrontierTransmission.payload_sha256,
    unique=True,
)
