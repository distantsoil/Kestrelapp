"""Prompt token budgets (docs/PROMPT_BUDGETS.md): render every pipeline prompt with a realistic
fixture and assert each fits its tier's num_ctx with headroom for output.

Run `pytest -q -s tests/test_prompt_budgets.py -k table` to print the measured table. The numbers in
docs/PROMPT_BUDGETS.md come from that output; regenerate them when a prompt or a context block
changes.

No implementation is mirrored here: the fixture is built from the shipped fictional
profile.example.yml, a 900-word job description in the shape job boards actually serve (duties,
requirements, benefits, EEO boilerplate), an org with accumulated signals, and few-shot k=3."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from kestrel.docs.select import _render_user as render_docsel_user
from kestrel.ingest.tokens import count_tokens
from kestrel.llm.fewshot import FewShotExample
from kestrel.llm.prompts import load_prompt
from kestrel.pipeline.context import (
    TIER1_HEAD_TOKENS,
    build_tier1_values,
    build_tier2_values,
    few_shot_block,
    org_signals_lines,
    render_user,
)
from kestrel.pipeline.profile import load_profile

# Per-tier num_ctx (kestrel/resources/config/models.yml) and the output reserve each tier needs.
NUM_CTX = {"tier1": 4096, "tier2": 16384, "docsel": 8192, "tier3": 20480}
MAX_JD_TOKENS = 6000  # settings.pipeline.max_jd_tokens default
FEW_SHOT_K = 3
HEADROOM = 0.25  # at least a quarter of num_ctx must remain free for the model's output

# ------------------------------------------------------------------------ fixture: a 900-word JD
JD_900_WORDS = """
About the role

Meridian Fulfillment Partners is a privately held, private-equity-backed third-party logistics
provider operating eleven distribution centers across Texas, Arizona, and the Southeast. We are
hiring a Vice President of Information Technology to own the technology function end to end as the
company integrates two recent acquisitions and prepares for a lender refinancing in the next
eighteen months. This role reports to the Chief Financial Officer, sits on the operating committee,
and carries a team of 24 across infrastructure, applications, service desk, and security.

What you will do

- Own the IT strategy, roadmap, budget, and vendor portfolio for a 1,400-employee organization,
  including a managed-services contract that today represents roughly 40 percent of run spend.
- Lead the integration of the two acquired businesses onto a single identity, endpoint, network,
  and warehouse-management stack within the first twelve months.
- Stand up a security and compliance program that satisfies customer audits, cyber-insurance
  requirements, and the lender's due-diligence questionnaire; close the open findings from the
  most recent assessment.
- Rationalize the application estate: three WMS instances, two ERPs, and a long tail of
  departmental tools, with a decision on the target platform in the first six months.
- Build a dependable service-desk operation with measured SLAs and a quarterly operating review
  with each distribution center's general manager.
- Manage a capital and operating budget of approximately 14 million dollars, with a mandate to
  reduce run costs by at least 20 percent over two years while improving reliability.
- Partner with the CFO and the operating partners on diligence for future bolt-on acquisitions,
  including integration costing and day-one planning.
- Recruit, develop, and retain a senior team; today two of four director seats are open.

What you bring

- Twelve or more years in IT leadership, with at least five running a function with full budget,
  vendor, and people accountability at a company of 800 or more employees.
- A demonstrated post-acquisition integration, ideally more than one, where you consolidated
  environments and can speak to the cost and timeline outcomes.
- Hands-on ownership of a security remediation or audit-closure program: SOC 2, ISO 27001, HIPAA,
  or an equivalent regulated framework.
- Experience in logistics, distribution, manufacturing, or another operations-heavy, margin-thin
  industry where downtime has a direct cost.
- Comfort operating under private-equity ownership: a 100-day plan, a value-creation thesis, and a
  board that expects measured commitments rather than roadmaps.
- Strong vendor negotiation skills; you have renegotiated or exited a managed-services agreement.
- Ability to communicate plainly with non-technical executives and warehouse leadership alike.
- Bachelor's degree in a relevant field or equivalent experience; an MBA is a plus.

Nice to have

- WMS platform migration experience (Manhattan, Blue Yonder, or Korber).
- Prior experience as a CIO or CTO at an acquired portfolio company.
- Familiarity with Microsoft 365, Azure, Intune, and a modern SIEM.

Location and travel

This role is based at our headquarters in Round Rock, Texas, with a hybrid schedule of three days
on site. Travel to distribution centers is expected at roughly 20 percent, concentrated in the
first year of the integration program. Relocation assistance is available for the right candidate.

Compensation and benefits

The base salary range for this position is 215,000 to 260,000 dollars, plus an annual bonus
target of 30 percent and participation in the management equity plan. Actual compensation will
depend on experience, skills, and location. Meridian offers medical, dental, and vision coverage
beginning on the first of the month after hire, a 401(k) with a 4 percent company match, 20 days
of paid time off plus company holidays, paid parental leave, short- and long-term disability, life
insurance, an employee assistance program, tuition reimbursement, and a wellness stipend.

Equal opportunity

Meridian Fulfillment Partners is an equal opportunity employer. We celebrate diversity and are
committed to creating an inclusive environment for all employees. All qualified applicants will
receive consideration for employment without regard to race, color, religion, sex, sexual
orientation, gender identity, national origin, age, disability, protected veteran status, or any
other characteristic protected by applicable federal, state, or local law. We participate in
E-Verify. Reasonable accommodations are available for applicants with disabilities throughout the
hiring process; please contact our recruiting team to request one. This posting is not a contract
of employment and the company reserves the right to modify the responsibilities described here.
Applicants must be authorized to work in the United States; we do not sponsor employment visas
for this position. Recruiting agencies: please do not contact this posting; unsolicited resumes
submitted by agencies will be considered the property of Meridian and no fee will be owed.
""".strip()

_SIGNALS = [
    {
        "category": "acquisition",
        "statement": "Closed the acquisition of Lone Star Cold Chain, a 300-employee regional 3PL",
        "event_date": "2026-08-14",
        "url": "https://example.com/news/lone-star-close",
    },
    {
        "category": "leadership_change",
        "statement": "Chief Information Officer departed; CFO named interim technology lead",
        "event_date": "2026-09-02",
        "url": "https://example.com/news/cio-departs",
    },
    {
        "category": "situation",
        "statement": "Two open director seats in IT; lender refinancing expected within 18 months",
        "observed_at": "2026-09-20",
        "source": "p2c_eval",
    },
]


def _org() -> SimpleNamespace:
    return SimpleNamespace(
        name="Meridian Fulfillment Partners",
        sector="logistics",
        size_band="1000-5000",
        ownership="private_equity",
        signals=list(_SIGNALS),
    )


def _opp(track: str = "employment", description: str = JD_900_WORDS) -> SimpleNamespace:
    posted = track == "employment"
    return SimpleNamespace(
        id=1,
        track=track,
        opp_type="posting" if posted else "trigger",
        title="Vice President, Information Technology" if posted else "Post-acquisition IT lead",
        org_name="Meridian Fulfillment Partners",
        url="https://boards.example.com/meridian/jobs/4821",
        location_raw="Round Rock, TX (Hybrid)",
        remote_type="hybrid",
        employment_type="full_time" if posted else "contract",
        comp_min=215000.0 if posted else 1800.0,
        comp_max=260000.0 if posted else 2400.0,
        comp_currency="USD",
        comp_interval="year" if posted else "day",
        description_md=description,
        date_posted="2026-10-01",
        event_date=None if posted else "2026-09-02",
        trigger_data=None
        if posted
        else {
            "event_type": "executive_departure",
            "evidence_quote": "Our Chief Information Officer has resigned effective September 2.",
            "implied_need": "Interim technology leadership through the integration window.",
            "source_url": "https://example.com/news/cio-departs",
        },
        first_seen_at="2026-10-01T08:00:00Z",
        last_seen_at="2026-10-09T08:00:00Z",
        seen_count=3,
        version=1,
        ghost_score=0.12,
        state="TX",
        country="US",
        city="Round Rock",
    )


def _few_shot(track: str) -> list[FewShotExample]:
    """k=3 examples as tier2._gather_few_shot builds them from Label rows: the operator's own
    rationale as the summary, the verdict and holistic score, and the rationale again as `why`."""
    rows = [
        ("apply", 4.3, "VP IT at an acquired services group, full budget ownership, OH hybrid; "
                       "integration mandate matches the strongest proof point"),
        ("consider", 3.1, "Head of Technology at a healthcare provider; scope is right but comp "
                          "band tops out below floor and the role is five days on site"),
        ("pass", 1.8, "Titled Director of IT but the body describes a hands-on sysadmin with no "
                      "reports and no budget; agency repost"),
    ]
    return [
        FewShotExample(
            id=i + 1,
            embedding=[1.0, 0.0],
            verdict=v,
            holistic=h,
            summary=r[:400],
            rationale=r[:200],
        )
        for i, (v, h, r) in enumerate(rows)
    ]


def _catalog() -> SimpleNamespace:
    """A resume master in the shape kestrel.docs.catalog.MasterCatalog exposes to
    docs.select._render_user: four roles of four slots with three variants each, three summary
    variants, three skill groups, two layouts."""
    sections = []
    for r in range(4):
        slots = []
        for s in range(4):
            key = f"r{r}_b{s}"
            variants = [
                SimpleNamespace(
                    key=f"{key}_v{v}",
                    tags=["integration", "cost", "security"][v : v + 2],
                    text=(
                        f"Led the {['consolidation', 'remediation', 'renegotiation'][v]} program "
                        f"for role {r}, delivering a measured {15 + v * 7} percent improvement "
                        "against the committed baseline within the first year."
                    ),
                )
                for v in range(3)
            ]
            slots.append(
                SimpleNamespace(
                    key=key, optional=s >= 2, default_variant_key=f"{key}_v0", variants=variants
                )
            )
        sections.append(
            SimpleNamespace(
                section=SimpleNamespace(
                    title=f"Role {r} title", org_label=f"Org {r}", section_kind="experience"
                ),
                slots=slots,
            )
        )
    return SimpleNamespace(
        sections=sections,
        summary_variants=[
            {"key": f"sum_{i}", "text": "Technology executive who turns under-invested IT "
                                        "functions into dependable, secure platforms for growth, "
                                        f"with an emphasis on {t}."}
            for i, t in enumerate(["integration", "security", "cost takeout"])
        ],
        skill_groups=[
            {"key": "infra", "label": "Infrastructure", "items": ["Azure", "M365", "Intune"]},
            {"key": "sec", "label": "Security", "items": ["SOC 2", "HIPAA", "SIEM", "IAM"]},
            {"key": "ops", "label": "Operations", "items": ["ITIL", "vendor mgmt", "budgeting"]},
        ],
        layouts=[
            SimpleNamespace(key="exec_two_page", description="Two-page executive resume"),
            SimpleNamespace(key="one_page", description="One-page summary resume"),
        ],
    )


def _docsel_eval() -> SimpleNamespace:
    return SimpleNamespace(
        blocks={
            "b_fit": {
                "matches": [{"requirement": "post-acquisition integration"},
                            {"requirement": "security remediation / audit closure"},
                            {"requirement": "acquired operating experience"}],
                "gaps": [{"requirement": "WMS platform migration"}],
            },
            "e_angle": {"lead_with": "integration outcomes with cost evidence"},
            "c_level": {"scope_read": "true VP scope: 24 reports, $14M budget, board exposure"},
        }
    )


# ------------------------------------------------------------------------------ measurement
@dataclass
class Rendered:
    prompt_id: str
    tier: str
    system: str
    user: str
    jd_tokens: int
    few_shot_tokens: int

    @property
    def system_tokens(self) -> int:
        return count_tokens(self.system)

    @property
    def user_tokens(self) -> int:
        return count_tokens(self.user)

    @property
    def template_tokens(self) -> int:
        """User message minus the JD slice and the few-shot block: profile, headers, metadata."""
        return self.user_tokens - self.jd_tokens - self.few_shot_tokens

    @property
    def total(self) -> int:
        return self.system_tokens + self.user_tokens

    @property
    def num_ctx(self) -> int:
        return NUM_CTX[self.tier]

    @property
    def headroom(self) -> int:
        return self.num_ctx - self.total


def _render_tier1(track: str, description: str = JD_900_WORDS) -> Rendered:
    version = "triage/v1" if track == "employment" else "triage_consulting/v1"
    spec = load_prompt(version)
    opp, org = _opp(track, description), _org()
    values = build_tier1_values(
        opp=opp, org=org, profile=load_profile(track), source_name="greenhouse",
        n_tokens=TIER1_HEAD_TOKENS,
    )
    trigger = bool(opp.trigger_data or opp.event_date)
    posted = bool(opp.description_md)
    user = render_user(spec.user, values, trigger_event=trigger, posted=posted or not trigger)
    return Rendered(spec.id, "tier1", spec.system, user,
                    count_tokens(values["description_truncated"]), 0)


def _render_tier2(
    track: str, *, tier: str = "tier2", k: int = FEW_SHOT_K, description: str = JD_900_WORDS
) -> Rendered:
    version = "evaluate/v1" if track == "employment" else "evaluate_consulting/v1"
    spec = load_prompt(version)
    opp, org = _opp(track, description), _org()
    few = few_shot_block(_few_shot(track)[:k])
    values = build_tier2_values(
        opp=opp, org=org, profile=load_profile(track), source_name="greenhouse",
        max_jd_tokens=MAX_JD_TOKENS, few_shot=few,
        comp_intel="median $232,000 (n=7, TX; range $205,000-$255,000), from the operator's own "
                   "observed corpus",
    )
    trigger = bool(opp.trigger_data or opp.event_date)
    posted = bool(opp.description_md)
    user = render_user(
        spec.user, values, trigger_event=trigger, posted=posted or not trigger,
        org_signals=org_signals_lines(org),
    )
    return Rendered(spec.id, tier, spec.system, user,
                    count_tokens(values["description_truncated"]), count_tokens(few))


def _render_docsel() -> Rendered:
    spec = load_prompt("doc_select/v1")
    user = render_docsel_user(
        spec, opp=_opp("employment"), ev=_docsel_eval(), catalog=_catalog()
    )
    return Rendered(spec.id, "docsel", spec.system, user, 0, 0)


def render_all(description: str = JD_900_WORDS) -> list[Rendered]:
    return [
        _render_tier1("employment", description),
        _render_tier1("consulting", description),
        _render_tier2("employment", description=description),
        _render_tier2("consulting", description=description),
        _render_docsel(),
        # Tier 3 (frontier escalation) sends the P2/P2C prompt into the tier3 window.
        _render_tier2("employment", tier="tier3", description=description),
        _render_tier2("consulting", tier="tier3", description=description),
    ]


def budget_table(rows: list[Rendered]) -> str:
    head = ("| prompt | tier | system | template | JD | few-shot | total | num_ctx | headroom |\n"
            "|---|---|---|---|---|---|---|---|---|")
    body = "\n".join(
        f"| {r.prompt_id} | {r.tier} | {r.system_tokens} | {r.template_tokens} | {r.jd_tokens} "
        f"| {r.few_shot_tokens} | {r.total} | {r.num_ctx} | {r.headroom} "
        f"({r.headroom / r.num_ctx:.0%}) |"
        for r in rows
    )
    return head + "\n" + body


# ------------------------------------------------------------------------------------- tests
def test_print_budget_table(capsys):
    """Prints the table for docs/PROMPT_BUDGETS.md (run with -s). Always passes."""
    long_jd = "\n\n".join([JD_900_WORDS] * 8)  # ~10k tokens: forces every truncation path
    with capsys.disabled():
        print("\nREALISTIC (900-word JD):\n" + budget_table(render_all()))
        print("\nWORST CASE (JD over max_jd_tokens):\n" + budget_table(render_all(long_jd)))


@pytest.mark.parametrize(
    "description",
    [JD_900_WORDS, "\n\n".join([JD_900_WORDS] * 8)],
    ids=["jd_900_words", "jd_8x_over_budget"],  # a text id overflows PYTEST_CURRENT_TEST on Windows
)
def test_every_prompt_fits_num_ctx_with_headroom(description: str):
    for r in render_all(description):
        reserve = int(r.num_ctx * HEADROOM)
        assert r.total + reserve <= r.num_ctx, (
            f"{r.prompt_id} ({r.tier}): {r.total} input tokens + {reserve} reserve exceeds "
            f"num_ctx {r.num_ctx}\n{budget_table([r])}"
        )


def test_tier1_sees_a_smaller_jd_slice_than_tier2():
    long_jd = "\n\n".join([JD_900_WORDS] * 8)
    t1 = _render_tier1("employment", long_jd)
    t2 = _render_tier2("employment", description=long_jd)
    assert 0 < t1.jd_tokens < t2.jd_tokens
    assert t1.jd_tokens <= TIER1_HEAD_TOKENS
    assert t2.jd_tokens <= MAX_JD_TOKENS


def test_tier1_slice_carries_marker_only_when_cut():
    from kestrel.pipeline.context import tier1_description

    short = tier1_description(JD_900_WORDS, 5000)
    assert "truncated for triage" not in short
    cut = tier1_description("\n\n".join([JD_900_WORDS] * 3), TIER1_HEAD_TOKENS)
    assert cut.endswith("it continues beyond this point ...]")
    assert count_tokens(cut) <= TIER1_HEAD_TOKENS
    assert cut.startswith("About the role")


def test_normalize_description_removes_only_whitespace():
    from kestrel.pipeline.context import normalize_description

    messy = "Title  here   \n\n\n\n- req one\t\tfast  \r\n\r\n\r\n\r\nEqual opportunity employer."
    out = normalize_description(messy)
    assert out == "Title here\n\n- req one fast\n\nEqual opportunity employer."
    assert out.split() == messy.split()  # every word survives, in order
    # Boilerplate is kept on purpose: it can carry authorization and comp statements.
    assert "Equal opportunity employer." in out


def test_few_shot_block_is_compact_and_never_repeats_the_rationale():
    exs = _few_shot("employment")
    block = few_shot_block(exs)
    assert "Why:" not in block  # summary and rationale came from the same label text
    assert block.count("Verdict:") == 3 and "---" not in block
    distinct = [FewShotExample(id=9, embedding=[1.0], verdict="apply", holistic=4.0,
                               summary="VP IT, acquired, Austin", rationale="scope and comp fit")]
    assert "  Why: scope and comp fit" in few_shot_block(distinct)
    assert few_shot_block([]) == ""


def test_consulting_profile_compact_carries_target_size_and_sectors():
    from kestrel.pipeline.context import profile_compact

    text = profile_compact(load_profile("consulting"))
    assert "Target orgs: size 100-500, 500-1000; sectors professional_services" in text
    assert "Target: $600/day" in text and "Level:" not in text
    emp = profile_compact(load_profile("employment"))
    # The shipped example sets no seniority or pay floor (release concern 10).
    assert "Level: unspecified and above" in emp and "Comp floor: unspecified base" in emp
    assert count_tokens(text) < 400 and count_tokens(emp) < 400  # PROMPTS.md target


def test_no_unresolved_placeholders_in_rendered_prompts():
    for r in render_all():
        assert "{GLOBAL_RULES}" not in r.system
        for marker in ("{PROFILE", "{FEW_SHOT", "{job.", "{opp.", "{org.", "{description",
                       "{comp_", "{if ", "{for each org"):
            assert marker not in r.user, f"{r.prompt_id}: unresolved {marker}"
