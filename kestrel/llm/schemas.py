"""A pydantic model per prompt schema in PROMPTS.md (P1 to P15, with the C variants).

TRUNCATE, never reject: maxLength limits are advisory (the grammar drops them), so overlong
strings are truncated by validators, not rejected. Numeric bounds the grammar drops (confidence,
holistic, dimension/mock scores) are CLAMPED. Overlong arrays (maxItems) are capped. Validation
failure is reserved for STRUCTURAL problems: missing required fields, wrong types, invalid enums.
Union types [\"X\",\"null\"] are Optional here; a returned null and an absent key behave identically
(the client also drops the null branch from json_schema-mode requests).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator


# ------------------------------------------------------------------------------------ validators
def _truncate(n: int) -> AfterValidator:
    def v(x):
        return x[:n] if isinstance(x, str) else x

    return AfterValidator(v)


def _truncate_words(n: int) -> AfterValidator:
    """Cut an overlong string at a word boundary and end it with an ellipsis, so it never reads
    as a clipped sentence. Truncation, never rejection."""

    def v(x):
        if not isinstance(x, str) or len(x) <= n:
            return x
        cut = x[: n - 1].rsplit(" ", 1)[0].rstrip(" ,;:.")
        return (cut or x[: n - 1]) + "\u2026"

    return AfterValidator(v)


def _clamp(lo: float, hi: float) -> AfterValidator:
    def v(x):
        if isinstance(x, (int, float)) and not isinstance(x, bool):
            return min(hi, max(lo, x))
        return x

    return AfterValidator(v)


def _cap(n: int) -> AfterValidator:
    def v(x):
        return x[:n] if isinstance(x, list) else x

    return AfterValidator(v)


def _dedupe_cap(n: int | None = None) -> AfterValidator:
    """An enum list: drop repeats keeping first-seen order, then cap at `n`. A model that names
    one signal twice (or, looping, fifty times, D-LLM-2) has still said something valid; the
    repeats are noise, never a validation failure."""

    def v(x):
        if not isinstance(x, list):
            return x
        out = list(dict.fromkeys(x))
        return out[:n] if n is not None else out

    return AfterValidator(v)


def _max_items(n: int) -> Any:
    """Emit `maxItems` in the JSON schema the model sees (PROMPTS.md). Advisory: the grammar or
    the provider may ignore it, and _dedupe_cap/_cap enforce the bound afterward."""
    return Field(json_schema_extra={"maxItems": n})


Confidence = Annotated[float, _clamp(0.0, 1.0)]
Score = Annotated[float, _clamp(1.0, 5.0)]
IntScore = Annotated[int, _clamp(1, 5)]


class _Base(BaseModel):
    # Extra fields are ignored (lenient), never a validation failure; the enum/type/required
    # checks are what matter (STRUCTURAL only).
    model_config = ConfigDict(extra="ignore")


# ===== P1 — triage
P1Signal = Literal[
    "level_match",
    "level_too_low",
    "level_too_high",
    "function_match",
    "function_mismatch",
    "location_ok",
    "location_blocked",
    "comp_ok",
    "comp_below_floor",
    "comp_unstated",
    "agency_posting",
    "scam_signals",
    "description_too_vague",
    "industry_match",
    "industry_mismatch",
]


class P1Triage(_Base):
    verdict: Literal["keep", "kill"]
    confidence: Confidence
    reason: Annotated[str, _truncate(140)]
    signals: Annotated[list[P1Signal], _dedupe_cap(5), _max_items(5)]


# ===== P1C — triage consulting
P1CSignal = Literal[
    "size_fit",
    "size_too_small",
    "size_too_large",
    "sector_fit",
    "sector_excluded",
    "need_matches_service_line",
    "need_outside_service_lines",
    "window_open",
    "window_closing",
    "window_closed",
    "rate_ok",
    "rate_below_floor",
    "rate_unstated",
    "buyer_identifiable",
    "buyer_unclear",
    "agency_bench_fishing",
    "incumbent_advisor_likely",
    "multi_signal_org",
    "procurement_heavy",
]


class P1CTriage(_Base):
    verdict: Literal["keep", "kill"]
    confidence: Confidence
    reason: str
    implied_engagement: str | None = None
    signals: Annotated[list[P1CSignal], _dedupe_cap()]


# ===== P2 — six-block eval
class Dimension(_Base):
    score: Score
    why: str


class P2ARoleSummary(_Base):
    what_this_actually_is: Annotated[str, _truncate(700)]
    title_body_mismatch: Annotated[str, _truncate(300)] | None = None


class P2Match(_Base):
    requirement: Annotated[str, _truncate(200)]
    evidence: Annotated[str, _truncate(300)]
    # "unverified": the posting asks for it and the profile neither shows nor rules it out. Shown
    # as Partly met under Gaps, never Met (hard rule 4, PROMPTS.md P2 rule 6, O20).
    strength: Literal["strong", "partial", "adjacent", "unverified"]


class P2Gap(_Base):
    requirement: Annotated[str, _truncate(200)]
    severity: Literal["fatal", "addressable", "noise"]
    mitigation: Annotated[str, _truncate(300)] | None = None


class P2BFit(_Base):
    matches: Annotated[list[P2Match], _cap(8)]
    gaps: Annotated[list[P2Gap], _cap(6)]


class P2CLevel(_Base):
    relative_level: Literal["above", "at", "below", "unclear"]
    scope_read: Annotated[str, _truncate(600)]
    reports_to: Annotated[str, _truncate(120)] | None = None
    team_size: Annotated[str, _truncate(80)] | None = None
    budget: Annotated[str, _truncate(80)] | None = None


class P2DComp(_Base):
    stated: Annotated[str, _truncate(120)] | None = None
    estimate_min: int | None = None
    estimate_max: int | None = None
    estimate_confidence: Literal["high", "medium", "low"]
    estimate_basis: Annotated[str, _truncate(300)]
    vs_floor: Literal["above_target", "above_floor", "at_floor", "below_floor", "unknown"]
    geo_adjustment_risk: bool


class P2EAngle(_Base):
    lead_with: Annotated[str, _truncate(600)]
    jd_citations: Annotated[list[Annotated[str, _truncate(200)]], _cap(3)]


class StarR(_Base):
    situation: Annotated[str, _truncate(300)]
    task: Annotated[str, _truncate(300)]
    action: Annotated[str, _truncate(400)]
    result: Annotated[str, _truncate(300)]
    reflection: Annotated[str, _truncate(300)]


class P2FInterview(_Base):
    likely_question: Annotated[str, _truncate(250)]
    why_asked: Annotated[str, _truncate(200)]
    proof_point_key: str | None = None
    star_r: StarR


class P2Blocks(_Base):
    a_role_summary: P2ARoleSummary
    b_fit: P2BFit
    c_level: P2CLevel
    d_comp: P2DComp
    e_angle: P2EAngle
    f_interview: Annotated[list[P2FInterview], _cap(4)]


class P2Dimensions(_Base):
    role_fit: Dimension
    domain_match: Dimension
    company_context: Dimension
    comp_logistics: Dimension
    trajectory: Dimension


class P2Blocker(_Base):
    kind: Literal[
        "work_authorization",
        "clearance",
        "location",
        "comp",
        "employment_type",
        "licensure",
        "other",
    ]
    detail: Annotated[str, _truncate(250)]


P2LegitSignal = Literal[
    "no_company_detail",
    "no_comp_no_location_no_team",
    "generic_boilerplate",
    "staffing_agency_unnamed_client",
    "unlimited_earning_language",
    "personal_email_contact",
    "upfront_cost_or_purchase",
    "mlm_pattern",
    "reposted_repeatedly",
    "unrealistic_requirements_for_comp",
    "commission_only",
    "pipeline_building_not_real_opening",
]


class P2Legitimacy(_Base):
    status: Literal["clean", "suspect", "reject"]
    confidence: Confidence
    signals: Annotated[list[P2LegitSignal], _dedupe_cap(8), _max_items(8)]


class P2Eval(_Base):
    blocks: P2Blocks
    dimensions: P2Dimensions
    holistic: Score
    verdict: Literal["apply", "consider", "pass"]
    headline_reason: Annotated[str, _truncate(200)]
    blockers: Annotated[list[P2Blocker], _cap(5)]
    legitimacy: P2Legitimacy
    work_auth_signal: Literal[
        "explicit_no_sponsorship",
        "sponsorship_available",
        "citizenship_required",
        "not_mentioned",
    ]


# ===== P2C — consulting eval
class P2CSituation(_Base):
    stated: str
    inferred: str
    speculative: str
    timing_read: str


class P2CPhase(_Base):
    name: str
    outcome: str
    days: int


class P2CThesis(_Base):
    engagement: str
    service_lines: list[str]
    # What the engagement needs against the practitioner's proof, graded as P2's matches are
    # ("unverified" is Partly met under Gaps, never Met; PROMPTS.md P2C rule 7, O20).
    matches: list[P2Match]
    phases: list[P2CPhase]
    estimated_days: int
    estimated_weeks: int


class P2CBuyers(_Base):
    economic_buyer: str | None = None
    influencers: list[str]
    blockers: list[str]
    access_difficulty: Literal["easy", "moderate", "hard", "unknown"]
    access_path: str


class P2CEconomics(_Base):
    rate_low: int | None = None
    rate_high: int | None = None
    confidence: Literal["high", "medium", "low"]
    basis: str
    engagement_value: int | None = None
    renewal_likelihood: Literal["high", "medium", "low", "unknown"]
    vs_floor: Literal["above_target", "above_floor", "at_floor", "below_floor", "unknown"]


class P2CApproach(_Base):
    opening_move: str
    to_whom: str
    referencing: str
    channel: Literal["linkedin", "email", "warm_intro", "rfp_response", "unknown"]
    why_now_line: str


class P2CDelivery(_Base):
    solo_feasible: bool
    gaps: list[str]
    partner_needed: str | None = None
    effort_shape: str


P2CRiskSignal = Literal[
    "agency_bench_fishing",
    "unpaid_scoping_expected",
    "procurement_heavy",
    "incumbent_advisor_entrenched",
    "no_identifiable_budget",
    "rate_race_to_bottom",
    "scope_undefined",
    "org_in_distress",
    "speculative_only",
]


class P2CRisk(_Base):
    status: Literal["clean", "suspect", "reject"]
    signals: Annotated[list[P2CRiskSignal], _dedupe_cap()]
    incumbent_advisor: str | None = None
    procurement_barrier: str | None = None


class P2CBlocks(_Base):
    a_situation: P2CSituation
    b_thesis: P2CThesis
    c_buyers: P2CBuyers
    d_economics: P2CEconomics
    e_approach: P2CApproach
    f_delivery: P2CDelivery
    g_risk: P2CRisk


class P2CDimensions(_Base):
    engagement_fit: Dimension
    buyer_access: Dimension
    economics: Dimension
    delivery_feasibility: Dimension
    strategic_value: Dimension


class P2CBlocker(_Base):
    kind: Literal[
        "rate",
        "duration",
        "sector",
        "geography",
        "conflict_of_interest",
        "capacity",
        "other",
    ]
    detail: str


class P2CLegitimacy(_Base):
    status: Literal["clean", "suspect", "reject"]
    confidence: Confidence
    # Free strings in PROMPTS.md (no enum), deduplicated and capped like P2's (D-LLM-2/5): a
    # looping flag list is noise, never a validation failure. No maxItems is emitted, since
    # PROMPTS.md specifies none for P2C.
    signals: Annotated[list[str], _dedupe_cap(8)]


class P2CEval(_Base):
    blocks: P2CBlocks
    dimensions: P2CDimensions
    holistic: Score
    verdict: Literal["pursue", "consider", "pass"]
    headline_reason: str
    blockers: list[P2CBlocker]
    legitimacy: P2CLegitimacy


# ===== P3 — doc slot selection
class P3DocSelect(_Base):
    # Keys are validated against the live doc_master catalog by the caller (docselect.py) and,
    # in json_schema mode, constrained by the dynamically built enum schema. This model stays
    # permissive on keys; a hallucinated variant key is caught by strict catalog validation.
    layout: str
    summary: str
    slots: dict[str, str]
    skill_groups: dict[str, bool]
    rationale: dict[str, Annotated[str, _truncate(200)]]
    coherence_check: Annotated[str, _truncate(300)]


# ===== P4 — cover letter
class P4Cover(_Base):
    body_md: str
    word_count: int
    claims_used: list[str]
    opening_alternatives: Annotated[list[Annotated[str, _truncate(300)]], _cap(2)]


# ===== P4C — engagement outline
class P4CCommercial(_Base):
    model: Literal["day_rate", "fixed_fee", "retainer", "hybrid"]
    indicative_days: int | None = None
    indicative_rate_low: int | None = None
    indicative_rate_high: int | None = None
    caveat: str


class P4CEngagement(_Base):
    body_md: str
    word_count: int
    claims_used: list[str]
    assumptions: list[str]
    exclusions: list[str]
    commercial_shape: P4CCommercial


# ===== P5 — company research
class P5Signal(_Base):
    category: Literal[
        "ownership",
        "funding",
        "growth",
        "layoffs",
        "leadership_change",
        "tech_direction",
        "culture",
        "compliance",
        "m_and_a",
        "risk",
        "other",
    ]
    statement: Annotated[str, _truncate(300)]
    source_index: int
    implication: Annotated[str, _truncate(250)]


class P5Research(_Base):
    summary_md: Annotated[str, _truncate(3000)]
    signals: Annotated[list[P5Signal], _cap(12)]
    unknowns: Annotated[list[Annotated[str, _truncate(200)]], _cap(6)]
    angle_for_candidate: Annotated[str, _truncate(600)]
    sources_used: list[int]


# ===== P6 — contact discovery
class P6Contact(_Base):
    name: str
    title: str
    contact_type: Literal[
        "hiring_manager",
        "recruiter",
        "peer",
        "exec",
        "referral",
        "board",
        "pe_operator",
    ]
    linkedin_url: str | None = None
    email: str | None = None
    confidence: Confidence
    source_index: int
    why_this_person: Annotated[str, _truncate(250)]
    draft_message: Annotated[str, _truncate(300)]


class P6Contacts(_Base):
    contacts: Annotated[list[P6Contact], _cap(6)]


# ===== P7 — resume import
class P7Variant(_Base):
    key: str
    text: str
    tags: list[str]
    derived: bool


class P7Slot(_Base):
    key: str
    optional: bool
    default_variant_key: str
    variants: list[P7Variant]


class P7Role(_Base):
    company_label: str
    title: str
    start_date: str
    end_date: str
    context_line: str
    slots: list[P7Slot]


class P7SummaryVariant(_Base):
    key: str
    text: str
    derived: bool


class P7SkillGroup(_Base):
    key: str
    label: str
    items: list[str]
    default_visible: bool


class P7ProofPoint(_Base):
    key: str
    claim: str
    evidence: str
    metric: str | None = None


class P7ResumeImport(_Base):
    roles: list[P7Role]
    summary_variants: list[P7SummaryVariant]
    skill_groups: list[P7SkillGroup]
    proof_points: list[P7ProofPoint]


# ===== P7 v2 — resume import by line reference (D-IMPORT-3)
# The model never returns resume text. It returns line numbers into the numbered source, and the
# app copies those lines byte for byte. The per-call JSON schema restricts every line field to the
# numbers that exist (kestrel.docs.imports.line_ref_schema), so an invented reference is
# structurally impossible on a constrained backend and rejected by validation on any other.
class P7LineBlock(_Base):
    lines: list[int]


class P7OutlineRole(_Base):
    header_lines: list[int]
    first_line: int
    last_line: int
    company_label: str
    title: str
    start_date: str
    end_date: str


class P7OutlineSkills(_Base):
    label: str
    lines: list[int]


class P7Outline(_Base):
    summary_blocks: list[P7LineBlock]
    roles: list[P7OutlineRole]
    skill_groups: list[P7OutlineSkills]


class P7RoleBullet(_Base):
    key: str
    lines: list[int]
    optional: bool
    tags: list[str]


class P7RoleProof(_Base):
    key: str
    lines: list[int]
    metric: str | None = None


class P7RoleMap(_Base):
    context_lines: list[int]
    bullets: list[P7RoleBullet]
    proof_points: list[P7RoleProof]


# ===== P7c — alternate wordings for one slot, on demand (D-IMPORT-3)
class P7SuggestedVariant(_Base):
    text: str
    tags: list[str]


class P7VariantSuggest(_Base):
    variants: list[P7SuggestedVariant]


# ===== P8 — story consolidation
P8Theme = Literal[
    "conflict",
    "failure",
    "influence_without_authority",
    "turnaround",
    "prioritization",
    "difficult_stakeholder",
    "technical_judgment",
    "team_building",
    "crisis",
    "change_management",
    "cost_reduction",
    "security_compliance",
    "m_and_a",
    "vendor_management",
    "scaling",
]


class P8Story(_Base):
    title: str
    situation: str
    task: str
    action: str
    result: str
    reflection: str
    themes: list[P8Theme]
    uncertain_details: list[str]
    spoken_seconds_estimate: int
    source_sketch_ids: list[int]


# ===== P9 — mock critique
class P9Scores(_Base):
    structure: IntScore
    specificity: IntScore
    relevance: IntScore
    length: IntScore
    ownership: IntScore


class P9Mock(_Base):
    scores: P9Scores
    worked_well: str
    improvements: list[str]
    unsupported_claims: list[str]
    revised_opening: str
    next_question: str
    next_question_rationale: str


# ===== P10 — trigger classify
class P10Trigger(_Base):
    is_relevant: bool
    event_type: Literal[
        "cio_departure",
        "cto_departure",
        "acquisition",
        "divestiture",
        "pe_investment",
        "funding_round",
        "security_incident",
        "control_weakness",
        "compliance_obligation",
        "rapid_it_hiring",
        "expansion",
        "restructuring",
        "other",
    ]
    org_name: str
    event_date: str | None = None
    evidence_quote: str
    confidence: Confidence
    implied_need: str
    urgency_window_days: int
    reasoning: str


# ===== P11 — negotiation
class P11Script(_Base):
    scenario: str
    script: str
    justification_basis: str
    likely_pushback: str
    response_to_pushback: str


class P11Negotiate(_Base):
    situation_read: str
    scripts: list[P11Script]
    leverage_inventory: list[str]
    risks: list[str]


# ===== P12 — analytics summary
class P12Analytics(_Base):
    summary_md: str
    headline_finding: str
    one_thing_to_change: str
    evidence_for_it: str
    sample_size_warnings: list[str]
    insufficient_data: bool


# ===== P13 — resume assessment (D-DOC-2). Advisory and displayed only: nothing here is ever
# stored as or rendered into document text. The registered model takes any slot key string;
# kestrel.docs.assess builds a per-master subclass whose slot keys are an enum of the master's
# real keys, so a hallucinated key is structurally impossible (the P3 pattern).
class P13Strength(_Base):
    point: Annotated[str, _truncate(400)]
    slot_keys: Annotated[list[str], _cap(6)]


class P13Gap(_Base):
    gap: Annotated[str, _truncate(300)]
    why_it_matters: Annotated[str, _truncate(400)]


class P13Strengthen(_Base):
    slot_key: str
    reason: Annotated[str, _truncate(400)]


class P13ResumeAssess(_Base):
    overall_fit: Annotated[str, _truncate(900)]
    strengths: Annotated[list[P13Strength], _cap(6)]
    gaps: Annotated[list[P13Gap], _cap(6)]
    strengthen: Annotated[list[P13Strengthen], _cap(8)]


# ===== P14 — interview questions, generated when a role is marked Applied (D-INTERVIEW-1)
# Questions are advice for the operator, not resume text; they never reach a PDF. The registered
# model takes any story id and proof point key; kestrel.interview.question_task builds a per-call
# subclass whose two reference fields are enums of the ids that really exist (plus null), the
# P3 and P13 pattern, so an invented id is structurally impossible on a constrained backend.
QUESTION_CATEGORIES = (
    "behavioral", "technical", "role_specific", "culture", "motivation", "gap_probe",
)
QuestionCategory = Literal[
    "behavioral", "technical", "role_specific", "culture", "motivation", "gap_probe"
]
MAX_QUESTIONS = 12


class P14QuestionCore(_Base):
    question: Annotated[str, _truncate(250)]
    category: QuestionCategory
    why_asked: Annotated[str, _truncate(200)]


class P14Question(P14QuestionCore):
    proof_point_key: str | None = None
    story_id: int | None = None


class P14InterviewQuestions(_Base):
    questions: Annotated[list[P14Question], _cap(MAX_QUESTIONS), _max_items(MAX_QUESTIONS)]


# ===== P15 — suggested target titles (advisory, never auto-added; D-TTL-3)
# The registered model takes any slot key; kestrel.services.title_suggest builds a per-call
# subclass whose evidence keys are an enum of the document's real slot keys (the P3 and P13
# pattern). A suggestion is stored for the operator to accept or dismiss; nothing here reaches
# employment_terms.target_titles.
class P15TitleCore(_Base):
    title: Annotated[str, _truncate(80)]
    why: Annotated[str, _truncate(200)]


class P15Title(P15TitleCore):
    evidence_slot_keys: Annotated[list[str], _dedupe_cap(5), _max_items(5)]


class P15TitleSuggestions(_Base):
    titles: Annotated[list[P15Title], _cap(10), _max_items(10)]


# ===== P16 — company summary (sourced, advisory, displayed only; D-LLM-9)
# The registered model takes any integer source index; kestrel.research.org_summary builds a
# per-call subclass whose indexes are an enum of the supplied excerpt indexes and which checks every
# figure against the excerpts the sentence cites (the P3 and P13 pattern, plus the figure check).
ORG_TAG_KEYS = ("does", "serves", "direction", "hiring")
MAX_SUMMARY_SENTENCES = 5
MAX_SUMMARY_TAGS = 4


class P16Sentence(_Base):
    text: Annotated[str, _truncate_words(300)]
    source_indexes: Annotated[list[int], _dedupe_cap(4), _max_items(4)]


class P16Tag(_Base):
    key: Literal["does", "serves", "direction", "hiring"]
    text: Annotated[str, _truncate_words(80)]
    source_indexes: Annotated[list[int], _dedupe_cap(3), _max_items(3)]


class P16OrgSummary(_Base):
    sentences: Annotated[
        list[P16Sentence], _cap(MAX_SUMMARY_SENTENCES), _max_items(MAX_SUMMARY_SENTENCES)
    ]
    tags: Annotated[list[P16Tag], _cap(MAX_SUMMARY_TAGS), _max_items(MAX_SUMMARY_TAGS)]
    insufficient: bool


class P17FollowUp(_Base):
    subject: Annotated[str, _truncate(160)]
    body: Annotated[str, _truncate(1200)]
    checks: Annotated[list[Annotated[str, _truncate(240)]], _cap(5), _max_items(5)]

    @field_validator("subject", "body")
    @classmethod
    def usable_text(cls, value: str) -> str:
        if not value.strip() or "\u2014" in value:
            raise ValueError("Use nonempty text without em dashes")
        return value.strip()

    @field_validator("checks")
    @classmethod
    def plain_checks(cls, value: list[str]) -> list[str]:
        if any("\u2014" in item for item in value):
            raise ValueError("Use checks without em dashes")
        return value


# Registry: prompt version string -> pydantic model. Used by the guard/caller to pick the model.
SCHEMA_BY_VERSION: dict[str, type[BaseModel]] = {
    # The version before each current one stays runnable so the model check can compare against
    # it (SPEC-INTERFACES 2.1 /models check: "Current ... or the version before it"). Their
    # schemas are unchanged or a strict subset of today's, so the current models validate them.
    # evaluate_consulting/v1 is not kept: P2C v2 added a required field its output lacks.
    "triage/v1": P1Triage,
    "triage_consulting/v1": P1CTriage,
    "evaluate/v1": P2Eval,
    "triage/v2": P1Triage,
    "triage_consulting/v2": P1CTriage,
    "evaluate/v2": P2Eval,
    "evaluate_consulting/v2": P2CEval,
    "doc_select/v1": P3DocSelect,
    "cover/v1": P4Cover,
    "engagement_outline/v1": P4CEngagement,
    "research/v1": P5Research,
    "contacts/v1": P6Contacts,
    "resume_outline/v1": P7Outline,
    "resume_role/v1": P7RoleMap,
    "variant_suggest/v1": P7VariantSuggest,
    "story_consolidate/v1": P8Story,
    "mock_critique/v1": P9Mock,
    "trigger_classify/v1": P10Trigger,
    "negotiate/v1": P11Negotiate,
    "analytics_summary/v1": P12Analytics,
    "resume_assess/v1": P13ResumeAssess,
    "interview_questions/v1": P14InterviewQuestions,
    "title_suggest/v1": P15TitleSuggestions,
    "org_summary/v1": P16OrgSummary,
    "follow_up/v1": P17FollowUp,
}
