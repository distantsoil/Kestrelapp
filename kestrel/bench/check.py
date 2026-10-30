"""Model check: the benchmark as a queued background task (D-DRIFT-11, D-UI-105, SPEC.md 8.6,
SPEC-INTERFACES 2.1 `/bench`).

The old Bench page's Run button made no model call and sent the operator to `kestrel bench`; a
feature may not need the command line (D-UI-129). Now **Check my models** queues one `bench` task
(a `task` row) that the worker runs on uvicorn's loop, so a real comparison never blocks the web
loop (CLAUDE.md platform traps) and survives a restart:

* Steps: Quick screen (tier1, keep-or-skip against the gold set), Rating (tier2, the P2/P2C
  evaluation, the same harness `kestrel bench` runs) and optionally Deeper read (tier3). Each step
  tests the model in use and the alternatives that fit this computer (installed local models), or
  the online models the operator named when pressing the button.
* Resumable (hard rule 5): every finished (step, model) result is written into the task payload
  before the next one starts, so a re-leased task skips what is done. Progress (`done` of `total`,
  the current step and model) is in the payload too, for the page to poll.
* Cancel: `cancel_check` marks the row cancelled; the run stops before its next model.
* Honest numbers (hard rule 6): fewer than five rated roles is "too few", never rendered as a
  result, and the check is refused up front. Numbers that measure different things are never put
  side by side: each step is compared only with itself, in relative points per 100 roles.
* Local preferred: an online model is recommended only when it beats the best local option by
  `ONLINE_MARGIN_POINTS` or more. The best local option is always named.

The model client is injected (`make_client`), so tests run offline against a scripted client."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kestrel.bench import harness
from kestrel.config import Settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Label, Setting, Task

log = structlog.get_logger()

TASK_TYPE = "bench"
MAX_ATTEMPTS = 2
MIN_RATINGS = 5  # hard rule 6
ONLINE_MARGIN_POINTS = 5  # SPEC-INTERFACES: local preferred unless online is clearly better
MAX_ALTERNATIVES = 3
HISTORY_KEY = "bench.checks"
HISTORY_KEEP = 10
_LIVE = ("pending", "leased")

STEP_TIER = {"quick_screen": "tier1", "rating": "tier2", "deeper_read": "tier3"}
STEP_LABEL = {"quick_screen": "Quick screen", "rating": "Rating", "deeper_read": "Deeper read"}
DEFAULT_STEPS = ("quick_screen", "rating")
TRIAGE_VERSION_TRACKS = ("employment", "consulting")
TRIAGE_VERSION = {
    "employment": "triage/v2", "consulting": "triage_consulting/v2",
}
# A Quick screen reply under this confidence counts as "keep" (hard rule 4), as in production.
KEEP_BELOW_CONFIDENCE = 0.6


# ================================================================================== results
@dataclass
class StepResult:
    """One model on one step. `agreement` is a fraction of the gold set the model matched, for
    that step's own question (Quick screen: keep or skip; Rating: the verdict)."""

    step: str
    model: str
    online: bool
    n: int
    agreement: float | None
    mean_latency_ms: float | None
    first_attempt_valid: float | None
    quarantined: int = 0
    backend: str = ""

    @property
    def points(self) -> float | None:
        return None if self.agreement is None else self.agreement * 100.0


Runner = Callable[[str, str, Settings, str | None], Awaitable[StepResult]]
ClientFactory = Callable[[Settings], Any]


def is_online(settings: Settings, tier: str) -> bool:
    tcfg = settings.llm.tiers.get(tier)
    backend = settings.llm.backends.get(tcfg.backend) if tcfg is not None else None
    return bool(backend is not None and backend.external)


# ===================================================================================== steps
def _settings_for(settings: Settings, step: str, model: str, backend: str | None) -> Settings:
    """A copy whose step tier runs `model` on `backend` (None: the backend the tier already
    uses). Each candidate runs on its own backend, so an online candidate is never sent to the
    local one and is labeled by where it really ran."""
    out = settings.model_copy(deep=True)
    tier = STEP_TIER[step]
    update: dict[str, Any] = {"model": model}
    if backend:
        update["backend"] = backend
    out.llm.tiers[tier] = out.llm.tiers[tier].model_copy(update=update)
    return out


def backend_of(settings: Settings, step: str, backend: str | None) -> str:
    return backend or settings.llm.tiers[STEP_TIER[step]].backend


def _keep_or_skip(gold_verdict: str) -> str:
    return "skip" if gold_verdict == "pass" else "keep"


async def _quick_screen(
    make_client: ClientFactory, settings: Settings, model: str, backend: str | None,
    gold_dir: Any,
) -> StepResult:
    from kestrel.llm.guard import run_prompt
    from kestrel.llm.prompts import load_prompt
    from kestrel.pipeline.context import build_tier1_values, render_user
    from kestrel.pipeline.profile import load_profile

    s = _settings_for(settings, "quick_screen", model, backend)
    client = make_client(s)
    total = right = valid = quarantined = 0
    latency: list[float] = []
    for track, version in TRIAGE_VERSION.items():
        try:
            entries = harness.load_goldset(track, gold_dir=gold_dir)
        except harness.GoldSetMissing:
            continue
        spec = load_prompt(version)
        profile = load_profile(track)
        for entry in entries:
            opp = harness._synthetic_opportunity(entry)
            values = build_tier1_values(
                opp=opp, org=None, profile=profile, source_name="model-check"
            )
            user = render_user(spec.user, values, trigger_event=False, posted=True)
            result = await run_prompt(
                client, spec,
                [{"role": "system", "content": spec.system}, {"role": "user", "content": user}],
            )
            if not result.ok and not result.quarantined:
                # A request error says nothing about the model; the item is left out, not scored.
                log.info("model_check_item_unanswered", model=model, error=result.error)
                continue
            total += 1
            quarantined += int(result.quarantined)
            valid += int(result.ok and not result.repaired)
            latency.append(float(result.latency_ms))
            if result.ok and result.value is not None:
                v = result.value
                said = "keep" if v.confidence < KEEP_BELOW_CONFIDENCE else (
                    "keep" if v.verdict == "keep" else "skip")
            else:
                said = "keep"  # hard rule 4: a quarantined screen escalates, it never rejects
            right += int(said == _keep_or_skip(entry.gold_verdict))
    return StepResult(
        step="quick_screen", model=model, online=is_online(s, "tier1"), n=total,
        backend=backend_of(settings, "quick_screen", backend),
        agreement=(right / total) if total else None,
        mean_latency_ms=(sum(latency) / len(latency)) if latency else None,
        first_attempt_valid=(valid / total) if total else None, quarantined=quarantined,
    )


async def _evaluation_step(
    make_client: ClientFactory, settings: Settings, step: str, model: str,
    backend: str | None, gold_dir: Any,
) -> StepResult:
    """Rating and Deeper read: the evaluation harness (`kestrel bench`'s code) with the step's
    tier standing in for tier2 (the harness reads that role)."""
    base = settings.model_copy(deep=True)
    tier = STEP_TIER[step]
    update: dict[str, Any] = {"model": model}
    if backend:
        update["backend"] = backend
    base.llm.tiers["tier2"] = base.llm.tiers[tier].model_copy(update=update)
    report = await harness.run_bench(
        settings=base, models=[model], make_client=make_client, gold_dir=gold_dir
    )
    n = sum(v.n_items for v in report.variants)
    matches = sum(
        round((v.exact_verdict_agreement_rate or 0.0) * v.n_parsed) for v in report.variants
    )
    lat = [v.mean_latency_ms for v in report.variants if v.mean_latency_ms is not None]
    valid = sum(v.first_attempt_validity_rate * v.n_items for v in report.variants)
    return StepResult(
        step=step, model=model, online=is_online(base, "tier2"), n=n,
        backend=backend_of(settings, step, backend),
        agreement=(matches / n) if n else None,  # over every role, as Quick screen is
        mean_latency_ms=(sum(lat) / len(lat)) if lat else None,
        first_attempt_valid=(valid / n) if n else None,
        quarantined=sum(v.n_quarantined for v in report.variants),
    )


def default_runner(make_client: ClientFactory, gold_dir: Any = None) -> Runner:
    async def run(
        step: str, model: str, settings: Settings, backend: str | None = None
    ) -> StepResult:
        if backend and backend not in settings.llm.backends:
            raise ValueError(f"no backend named {backend}")
        mapping = _SEND_MAP.get()
        factory: ClientFactory = (
            (lambda s: _RedactingClient(make_client(s), mapping)) if mapping else make_client
        )
        if step == "quick_screen":
            return await _quick_screen(factory, settings, model, backend, gold_dir)
        return await _evaluation_step(factory, settings, step, model, backend, gold_dir)

    return run


# ============================================================================ online sends
# Confidential mode (CLAUDE.md Privacy): what an online candidate is sent (gold-set postings and
# the operator's profile) goes through the same plan the pipeline uses (`plan_hosted_send`): the
# current employer is replaced with a code for each call, and with no employer chosen the send is
# held, not made. The runner reads the map from here so its signature stays what tests script.
_SEND_MAP: contextvars.ContextVar[Any] = contextvars.ContextVar("model_check_send_map",
                                                               default=None)

NO_PROVIDER_MESSAGE = (
    "Connect an online provider first. Online models need a key, and none is connected. Set one "
    "up under Models."
)
NO_APPROVAL_MESSAGE = (
    "Online models need your approval first. Tick I understand what gets sent on the Models "
    "page, then try again."
)


class _RedactingClient:
    """Wraps a model client so every call it makes carries the employer map's codes instead of
    the current employer (`pipeline._shared.encode_send`). Replies are only scored (verdict and
    score), never shown, so there is nothing to restore."""

    def __init__(self, inner: Any, mapping: Any) -> None:
        self._inner = inner
        self._mapping = mapping

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def complete(self, *, messages: list[dict], **kw: Any) -> Any:
        from kestrel.pipeline._shared import encode_send

        sent, _book = encode_send(messages, self._mapping)
        return await self._inner.complete(messages=sent, **kw)


def connected_provider(settings: Settings) -> tuple[str, Any] | None:
    """The provider an online pick goes to: the one the hosted tiers already use, else the first
    with a saved key. Only a provider whose key is saved counts (the same rule as
    `web.pages.models_ui.connected_backend`, which a test holds equal)."""
    from kestrel.llm import providers as prov
    from kestrel.services import hosted_models as hm

    def has_key(backend: Any) -> bool:
        return bool(backend.api_key) or prov.secret_present(backend.api_key_env)

    found = hm.catalog_backend(settings)
    if found is not None and has_key(found[1]):
        return found
    for name, backend in prov.external_backends(settings).items():
        if has_key(backend):
            return name, backend
    return None


def online_candidates(
    settings: Settings, models: dict[str, list[Any]] | None, online: dict[str, list[Any]] | None
) -> list[tuple[str, str, str | None]]:
    """Every (step, model, backend) that would be sent online: everything named in `online`, and
    anything in `models` that names an external backend."""
    out: list[tuple[str, str, str | None]] = []
    for source, always in ((online, True), (models, False)):
        for step, items in (source or {}).items():
            for item in items or []:
                cand = _as_candidate(item)
                if cand is None:
                    continue
                if step not in STEP_TIER:
                    continue
                resolved = backend_of(settings, step, cand[1])
                candidate = _settings_for(settings, step, cand[0], cand[1])
                if always or is_online(candidate, STEP_TIER[step]):
                    out.append((step, cand[0], resolved))
    return out


def online_refusal(
    settings: Settings, models: dict[str, list[Any]] | None, online: dict[str, list[Any]] | None
) -> str | None:
    """A plain sentence when a check may not include its online candidates, else None. Needs a
    connected provider with a saved key, the operator's approval of the current disclosure, and
    every online candidate on that provider (never an unknown or a local backend)."""
    cands = online_candidates(settings, models, online)
    if not cands:
        return None
    found = connected_provider(settings)
    if found is None:
        return NO_PROVIDER_MESSAGE
    if not settings.llm.hosted_mode.acknowledgement_current:
        return NO_APPROVAL_MESSAGE
    for step, model, backend in cands:
        if step not in STEP_TIER or backend != found[0]:
            return (f"{model} is not on your connected provider, so it cannot be included. "
                    "Pick a model from the list under Models.")
    return None


def approval_for_candidates(
    settings: Settings,
    steps: Sequence[str],
    models: dict[str, list[Any]] | None,
    online: dict[str, list[Any]] | None,
) -> dict[str, list[str]]:
    """Return destination approvals for every resolved online candidate in a check."""
    from kestrel.services.online_consent import approval_for, merge_approvals

    result: list[dict[str, list[str]]] = []
    for step in steps:
        picked = [_as_candidate(x) for x in (models or {}).get(step) or []]
        candidates = [candidate for candidate in picked if candidate]
        if not candidates:
            candidates = candidates_for(
                settings, step, (), (online or {}).get(step, ())
            )
        for model, backend in candidates:
            candidate_settings = _settings_for(settings, step, model, backend)
            if is_online(candidate_settings, STEP_TIER[step]):
                result.append(approval_for(candidate_settings, [STEP_TIER[step]]))
    return merge_approvals(*result)


def approval_for_offered(
    settings: Settings, offered: dict[str, list[Any]]
) -> dict[str, list[str]]:
    """Destinations for every online model a check card offers, per step, untruncated. The card
    posts this back with the tick; `queue_check` accepts the models it actually picks only when
    this covers them, and it picks from the whole list, not just its first few entries."""
    from kestrel.services.online_consent import approval_for, merge_approvals

    result: list[dict[str, list[str]]] = []
    for step, items in offered.items():
        if step not in STEP_TIER:
            continue
        for item in items or []:
            cand = _as_candidate(item)
            if cand is None:
                continue
            candidate_settings = _settings_for(settings, step, cand[0], cand[1])
            if is_online(candidate_settings, STEP_TIER[step]):
                result.append(approval_for(candidate_settings, [STEP_TIER[step]]))
    return merge_approvals(*result)


# ================================================================================= candidates
def current_model(settings: Settings, step: str) -> str:
    return settings.llm.tiers[STEP_TIER[step]].model


Candidate = tuple[str, str | None]  # (model, backend); None: the tier's own backend


def _as_candidate(item: Any) -> Candidate | None:
    if isinstance(item, str) and item:
        return item, None
    if isinstance(item, dict) and item.get("model"):
        return str(item["model"]), (str(item["backend"]) if item.get("backend") else None)
    model_id = getattr(item, "id", None)
    if model_id:
        return str(model_id), None
    return None


def candidates_for(
    settings: Settings, step: str, installed: Sequence[str], online: Sequence[Any] = ()
) -> list[Candidate]:
    """The model in use first (on its own backend), then up to MAX_ALTERNATIVES others: the
    installed local models that are not embedding models, then the online candidates the operator
    named, each as {"model", "backend"} so it runs on its own backend."""
    cur = current_model(settings, step)
    out: list[Candidate] = [(cur, None)]
    for item in [*installed, *online]:
        cand = _as_candidate(item)
        if cand is None or "embed" in cand[0].lower() or cand in out:
            continue
        if cand[1] is None and cand[0] == cur:
            continue
        out.append(cand)
    return out[: 1 + MAX_ALTERNATIVES]


# ======================================================================== recommendation
@dataclass
class StepVerdict:
    step: str
    label: str
    current: dict[str, Any] | None
    best: dict[str, Any] | None
    best_local: dict[str, Any] | None
    change: bool
    points: int | None  # relative points per 100 roles, best vs current
    n: int
    enough: bool
    verdict: str


def _row(r: StepResult) -> dict[str, Any]:
    return {**asdict(r), "points": r.points}


def recommend_step(
    step: str, results: list[StepResult], current: str, current_backend: str = ""
) -> StepVerdict:
    """`n` is the number of roles in the answer key the step was scored on (not the count of the
    operator's labels, which the page states separately)."""
    label = STEP_LABEL[step]
    rows = [r for r in results if r.step == step and r.agreement is not None]

    def is_current(r: StepResult) -> bool:
        return r.model == current and (not current_backend or r.backend in ("", current_backend))

    cur = next((r for r in rows if is_current(r)), None)
    n = max((r.n for r in rows), default=0)
    if cur is None or n < MIN_RATINGS:
        return StepVerdict(step, label, _row(cur) if cur else None, None, None, False, None, n,
                           False,
                           f"Too few roles in the answer key to compare {label} ({n}).")
    alts = [r for r in rows if not is_current(r)]
    best_local = max((r for r in alts if not r.online), key=lambda r: r.points or 0,
                     default=None)
    best_online = max((r for r in alts if r.online), key=lambda r: r.points or 0, default=None)
    choice = best_local
    gain_local = ((best_local.points or 0) - (cur.points or 0)) if best_local else 0.0
    if best_online is not None:
        gain_online = (best_online.points or 0) - (cur.points or 0)
        # Local preferred unless online is clearly better than the best local option.
        if gain_online - max(gain_local, 0.0) >= ONLINE_MARGIN_POINTS and gain_online > 0:
            choice = best_online
    gain = ((choice.points or 0) - (cur.points or 0)) if choice else 0.0
    change = choice is not None and round(gain) >= 1
    if change and choice is not None:
        slower = (
            choice.mean_latency_ms is not None and cur.mean_latency_ms is not None
            and choice.mean_latency_ms > cur.mean_latency_ms * 1.1
        )
        verdict = (
            f"Switch {label} to {choice.model}. It matches your calls on {round(gain)} more "
            f"roles in every 100, from {n} roles in the answer key"
            + (", and it is a little slower." if slower else ".")
        )
        if choice.online:
            verdict += " It runs online, so it uses your key and sends the posting."
    else:
        verdict = f"Keep {current} for {label}. Nothing tested was clearly better."
        choice = None
    return StepVerdict(step, label, _row(cur), _row(choice) if choice else None,
                       _row(best_local) if best_local else None, change,
                       round(gain) if change else None, n, True, verdict)


def build_summary(verdicts: list[StepVerdict]) -> tuple[str, int]:
    changes = sum(1 for v in verdicts if v.change)
    if not any(v.enough for v in verdicts):
        return "Too few roles in the answer key to check your models yet.", 0
    if changes == 0:
        return "No changes recommended.", 0
    if changes == 1:
        return "Kestrel recommends one change. Everything else stays as it is.", 1
    return f"Kestrel recommends {changes} changes. Everything else stays as it is.", changes


# ======================================================================== queue and status
async def _app_settings(session: AsyncSession) -> Settings:
    from kestrel.services.setting_overrides import load_settings_with_db

    return await load_settings_with_db(session)


async def rated_count(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(Label))).scalar_one()


# ================================================================================== answer key
def _line_count(path: Any) -> int:
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def answer_key_info(gold_dir: Any = None) -> dict[str, Any]:
    """What the check is scored against, in plain words. The key is the operator's own ratings
    for a track once they have built it (`build_answer_key`), otherwise the shipped example roles.
    `mine` and `shipped` are role counts; `kind` is the short name stored with a finished check."""
    mine = shipped = 0
    for track in TRIAGE_VERSION_TRACKS:
        try:
            kind, path = harness.goldset_source(track, gold_dir=gold_dir)
        except harness.GoldSetMissing:
            continue
        n = _line_count(path)
        if kind == "regenerated":
            mine += n
        else:
            shipped += n
    if mine and shipped:
        kind = "your ratings and shipped examples"
        line = f"{mine} of your rated roles plus {shipped} shipped example roles"
    elif mine:
        kind, line = "your ratings", f"{mine} of your own rated roles"
    elif shipped:
        kind = "shipped examples"
        line = f"{shipped} shipped example roles, not your ratings"
    else:
        kind, line = "none", "no answer key yet"
    return {"mine": mine, "shipped": shipped, "kind": kind, "line": line,
            "uses_mine": mine > 0}


async def build_answer_key(session: AsyncSession, gold_dir: Any = None) -> dict[str, Any]:
    """Make the operator's ratings the answer key, per track, the in-app form of
    `kestrel goldset regen`. A track with fewer than MIN_RATINGS ratings keeps the shipped
    examples (a key that thin would not be a fair test). Returns {"built": {track: n},
    "kept_shipped": {track: n}}."""
    from kestrel.db.enums import TRACKS
    from kestrel.evals.goldset import regenerate_goldset
    from kestrel.learning.capture import labeled_examples

    built: dict[str, int] = {}
    kept: dict[str, int] = {}
    for track in TRACKS:
        examples = await labeled_examples(session, track)
        if len(examples) < MIN_RATINGS:
            if examples:
                kept[track] = len(examples)
            continue
        out = None if gold_dir is None else Path(gold_dir) / (
            f"goldset.{track}.regenerated.jsonl")
        path = await regenerate_goldset(session, track, out_path=out)
        built[track] = _line_count(path)
    return {"built": built, "kept_shipped": kept}


async def live_task(session: AsyncSession) -> Task | None:
    return (
        await session.execute(
            select(Task).where(Task.type == TASK_TYPE, Task.status.in_(_LIVE))
            .order_by(Task.id).limit(1)
        )
    ).scalar_one_or_none()


@dataclass
class QueueResult:
    queued: bool
    reason: str = ""  # too_few_ratings | already_running | bad_steps | online_refused
    task_id: int | None = None
    message: str = ""


async def queue_check(
    session: AsyncSession,
    *,
    steps: Sequence[str] = DEFAULT_STEPS,
    deeper: bool = False,
    models: dict[str, list[str]] | None = None,
    online: dict[str, list[Any]] | None = None,
    settings: Settings | None = None,
    online_approval: object = None,
) -> QueueResult:
    """Check my models. One run at a time. Needs at least MIN_RATINGS rated roles. `models`
    overrides the candidates for a step (the Advanced section); `online` names online models the
    operator chose to include, which is the consent to send the gold-set roles to them. Commits."""
    chosen = list(dict.fromkeys([*steps, *(["deeper_read"] if deeper else [])]))
    if not chosen or any(s not in STEP_TIER for s in chosen):
        return QueueResult(False, "bad_steps", message="Pick at least one step to check.")
    settings = settings or await _app_settings(session)
    if online or models:
        refusal = online_refusal(settings, models, online)
        if refusal:
            return QueueResult(False, "online_refused", message=refusal)
        from kestrel.services.online_consent import approval_covers

        expected = approval_for_candidates(settings, chosen, models, online)
        if expected and not approval_covers(expected, online_approval):
            return QueueResult(
                False, "online_refused",
                message="Confirm the model and online service destinations, then try again.",
            )
    if not online and not models:
        # The untouched check tests the model in use on its own backend. When that backend is
        # online, it would send the rated roles to the provider with no consent. (An explicit
        # pick from Advanced names its backends and is gated above and by the route's tick.)
        hosted = [STEP_LABEL[x] for x in chosen if is_online(settings, STEP_TIER[x])]
        if hosted:
            return QueueResult(
                False, "online_refused",
                message=(f"Your {' and '.join(hosted)} model "
                         f"{'runs' if len(hosted) == 1 else 'run'} online, so checking "
                         "it sends rated roles to your provider. Tick Include online models to "
                         "allow that."),
            )
    approval = approval_for_candidates(settings, chosen, models, online)
    existing = await live_task(session)
    if existing is not None:
        return QueueResult(False, "already_running", existing.id,
                           "A model check is already running.")
    n = await rated_count(session)
    if n < MIN_RATINGS:
        return QueueResult(
            False, "too_few_ratings",
            message=f"Rate at least {MIN_RATINGS} roles first. Kestrel has {n} so far.",
        )
    task = Task(
        type=TASK_TYPE, max_attempts=MAX_ATTEMPTS,
        payload={
            "batch": uuid.uuid4().hex[:12], "steps": chosen, "models": models or {},
            "online": online or {}, "results": [],
            "online_approval": approval,
            "progress": {"done": 0, "total": 0, "step": "", "model": ""},
            "requested_at": utcnow_iso(),
        },
    )
    session.add(task)
    await session.commit()
    return QueueResult(True, task_id=task.id)


async def cancel_check(session: AsyncSession) -> int:
    """Cancel the live check; the running one stops before its next model. Commits."""
    rows = (
        await session.execute(select(Task).where(Task.type == TASK_TYPE, Task.status.in_(_LIVE)))
    ).scalars().all()
    for t in rows:
        t.status = "cancelled"
        t.last_error = "cancelled by the user"
        t.worker_id = None
        t.lease_until = None
    await session.commit()
    return len(rows)


async def status(session: AsyncSession) -> dict[str, Any]:
    """What the page polls: whether a check is running and its progress, the latest finished
    check, and the earlier ones."""
    newest = (
        await session.execute(
            select(Task).where(Task.type == TASK_TYPE).order_by(Task.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    history = await list_checks(session)
    view: dict[str, Any] = {
        "live": False, "state": "idle", "progress": None, "error": "",
        "latest": history[0] if history else None, "history": history,
    }
    if newest is not None:
        p = dict((newest.payload or {}).get("progress") or {})
        total, done = int(p.get("total") or 0), int(p.get("done") or 0)
        view["progress"] = {
            "done": done, "total": total, "step": p.get("step", ""), "model": p.get("model", ""),
            "percent": int(done * 100 / total) if total else 0,
        }
        view["live"] = newest.status in _LIVE
        view["state"] = {"pending": "waiting", "leased": "running", "done": "done",
                         "cancelled": "cancelled"}.get(newest.status, "failed")
        if newest.status in ("dead", "failed"):
            view["error"] = (newest.last_error or "it stopped before it finished")[:300]
    return view


async def list_checks(session: AsyncSession) -> list[dict[str, Any]]:
    row = await session.get(Setting, HISTORY_KEY)
    return list(row.value) if row is not None and isinstance(row.value, list) else []


async def _store_check(session: AsyncSession, check: dict[str, Any]) -> None:
    kept = [check, *(await list_checks(session))][:HISTORY_KEEP]
    row = await session.get(Setting, HISTORY_KEY)
    if row is None:
        session.add(Setting(key=HISTORY_KEY, value=kept))
    else:
        row.value = kept
        row.updated_at = utcnow_iso()


# ==================================================================================== the run
@dataclass
class RunOutcome:
    status: str  # done | cancelled | stopped | error
    check: dict[str, Any] | None = None
    error: str | None = None
    ran: list[tuple[str, str]] = field(default_factory=list)  # (step, model) run this time


async def _task_status(sm: async_sessionmaker[AsyncSession], task_id: int) -> str:
    async with sm() as s:
        t = await s.get(Task, task_id)
        return t.status if t is not None else "gone"


async def _save_progress(
    sm: async_sessionmaker[AsyncSession], task_id: int, *, results: list[dict[str, Any]],
    progress: dict[str, Any], held: list[dict[str, Any]] | None = None,
) -> None:
    async with sm() as s:
        t = await s.get(Task, task_id)
        if t is None:
            return
        extra = {} if held is None else {"held": held}
        t.payload = {**(t.payload or {}), "results": results, "progress": progress, **extra}
        await s.commit()


async def _send_plan(
    sm: async_sessionmaker[AsyncSession], settings: Settings, step: str, model: str,
    backend: str | None,
) -> Any:
    """What the pipeline would do for this candidate's send (`plan_hosted_send`): nothing for a
    local model; for an online one, the employer map (confidential mode) or a refusal."""
    from kestrel.pipeline._shared import plan_hosted_send

    candidate = _settings_for(settings, step, model, backend)
    async with sm() as s:
        return await plan_hosted_send(s, candidate, STEP_TIER[step])


async def run_check(
    sm: async_sessionmaker[AsyncSession],
    task: Task,
    settings: Settings,
    runner: Runner,
    installed: Sequence[str],
    stop: Any = None,
) -> RunOutcome:
    """Run one check task to its end, writing each result as it lands. Skips what an earlier
    lease already finished (resume). A model that fails is recorded as no result and the check
    carries on; an unexpected error propagates for the caller to retry or dead-letter."""
    payload = dict(task.payload or {})
    steps = [s for s in STEP_TIER if s in (payload.get("steps") or [])]
    plan: list[tuple[str, str, str | None]] = []
    for step in steps:  # steps in tier order: one tier drains before the next starts
        picked = [_as_candidate(x) for x in (payload.get("models") or {}).get(step) or []]
        cands = [c for c in picked if c] or candidates_for(
            settings, step, installed, (payload.get("online") or {}).get(step, ()))
        plan += [(step, m, b) for m, b in cands]
    results: list[dict[str, Any]] = list(payload.get("results") or [])
    held: list[dict[str, Any]] = list(payload.get("held") or [])
    done_keys = {(r["step"], r["model"], r.get("backend") or "") for r in results}
    ran: list[tuple[str, str]] = []
    for step, model, backend in plan:
        resolved = backend_of(settings, step, backend)
        if (step, model, resolved) in done_keys:
            continue
        if stop is not None and stop.is_set():
            return RunOutcome("stopped", ran=ran)
        if await _task_status(sm, task.id) in ("cancelled", "gone"):
            return RunOutcome("cancelled", ran=ran)
        progress = {"total": len(plan), "step": STEP_LABEL[step], "model": model}
        await _save_progress(sm, task.id, results=results,
                             progress={**progress, "done": len(results)})
        from kestrel.services.online_consent import approval_covers, approval_for

        candidate_settings = _settings_for(settings, step, model, backend)
        expected = (
            approval_for(candidate_settings, [STEP_TIER[step]])
            if is_online(candidate_settings, STEP_TIER[step]) else {}
        )
        if expected and not approval_covers(expected, payload.get("online_approval")):
            return RunOutcome(
                "held",
                error="Model or online service changed. Confirm the destination again.",
                ran=ran,
            )
        send = await _send_plan(sm, settings, step, model, backend)
        if send.refuse:
            # Held, not sent: confidential mode is on and no current employer is chosen, so the
            # redaction map is missing. The model is left out of the result, with the reason.
            held.append({"step": STEP_LABEL[step], "model": model, "reason": send.refuse})
            res = StepResult(step, model, True, 0, None, None, None, backend=resolved)
            res.backend = res.backend or resolved
            ran.append((step, model))
            results.append(_row(res))
            done_keys.add((step, model, res.backend))
            await _save_progress(sm, task.id, results=results,
                                 progress={**progress, "done": len(results)}, held=held)
            continue
        token = _SEND_MAP.set(send.mapping or None)
        try:
            res = await runner(step, model, settings, backend)
        except harness.GoldSetMissing as exc:
            return RunOutcome("error", error=str(exc), ran=ran)
        except (OSError, RuntimeError, ValueError, httpx.HTTPError) as exc:
            log.warning("model_check_model_failed", step=step, model=model,
                        error_type=type(exc).__name__, error=str(exc))
            res = StepResult(step, model, False, 0, None, None, None, backend=resolved)
        finally:
            _SEND_MAP.reset(token)
        res.backend = res.backend or resolved
        ran.append((step, model))
        results.append(_row(res))
        done_keys.add((step, model, res.backend))
        await _save_progress(sm, task.id, results=results,
                             progress={**progress, "done": len(results)})

    # A cancel that landed during the last model must stick: nothing is stored, nothing completes.
    if await _task_status(sm, task.id) in ("cancelled", "gone"):
        return RunOutcome("cancelled", ran=ran)
    typed = [StepResult(**{k: v for k, v in r.items() if k != "points"}) for r in results]
    verdicts = [
        recommend_step(s, typed, current_model(settings, s), backend_of(settings, s, None))
        for s in steps
    ]
    summary, changes = build_summary(verdicts)
    async with sm() as s:
        labels = await rated_count(s)
    check = {
        "id": payload.get("batch"), "at": utcnow_iso(), "summary": summary, "changes": changes,
        "steps": [asdict(v) for v in verdicts], "results": results, "held": held,
        # two different counts, never mixed: the roles the steps were scored on (the answer key)
        # and the operator's own ratings at the time of the check
        "answer_key_roles": max((v.n for v in verdicts), default=0), "rated_roles": labels,
        "answer_key": answer_key_info()["kind"],
    }
    # Complete only a task that is still running, and store the check in the same transaction:
    # a cancel that lands at any point before this commit wins, and one after it finds the check
    # already finished. Nothing can overwrite a cancel.
    async with sm() as s:
        res = await s.execute(
            update(Task).where(Task.id == task.id, Task.status.in_(_LIVE))
            .values(status="done", updated_at=utcnow_iso())
        )
        if res.rowcount != 1:
            await s.rollback()
            return RunOutcome("cancelled", ran=ran)
        await _store_check(s, check)
        await s.commit()
    return RunOutcome("done", check=check, ran=ran)


def _installed_local(settings: Settings) -> list[str]:
    """Models the local Ollama reports, or none when it cannot be reached. Sync, so callers use
    `asyncio.to_thread`."""
    from kestrel.llm.client import OllamaClient

    try:
        backend = settings.llm.backends["local"]
        client = OllamaClient(
            backend.base_url, settings.llm.ollama_native_url, backend.api_key or "ollama"
        )
        return list(client.list_models())
    except Exception as exc:  # noqa: BLE001 - any reachability failure just means no alternatives
        log.info("model_check_local_models_unreachable", error_type=type(exc).__name__)
        return []


async def drain_checks(
    sm: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    make_client: ClientFactory,
    stop: Any = None,
    runner: Runner | None = None,
    installed: Callable[[Settings], Sequence[str]] | None = None,
    gold_dir: Any = None,
) -> int:
    """Run every queued check, one at a time, on the worker. A failed task goes back to the
    queue while attempts remain, then dead with the reason; never raises into the worker cycle."""
    from kestrel.interview.story_task import LEASE_S, _renew
    from kestrel.queue.queue import TaskQueue

    run = runner or default_runner(make_client, gold_dir)
    queue = TaskQueue(sm)
    wid = "pipeline-model-check"
    done = 0
    while stop is None or not stop.is_set():
        task = await queue.lease(wid, types=[TASK_TYPE], lease_seconds=LEASE_S)
        if task is None:
            break
        beat = asyncio.create_task(_renew(queue, task.id, wid))
        try:
            found = await asyncio.to_thread(installed or _installed_local, settings)
            outcome = await run_check(sm, task, settings, run, found, stop)
        except Exception as exc:  # noqa: BLE001 - a failed task is retried, never fatal
            beat.cancel()
            reason = f"{type(exc).__name__}: {exc}"
            log.warning("model_check_failed", task=task.id, error=reason)
            await queue.fail(task.id, reason, worker_id=wid, retry=True)
            continue
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
        if outcome.status == "cancelled":
            continue  # the row is already cancelled
        if outcome.status == "stopped":
            # The app is quitting: give the task back with its finished results kept (resume).
            await queue.defer(task.id, "stopped; resumes at the next start", worker_id=wid)
            break
        if outcome.status == "held":
            await queue.defer(task.id, outcome.error or "held", worker_id=wid)
            break
        if outcome.status == "error":
            await queue.fail(task.id, outcome.error or "error", worker_id=wid, retry=False)
            continue
        await queue.complete(task.id, worker_id=wid)
        done += 1
    return done

