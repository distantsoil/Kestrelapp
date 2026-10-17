"""The onboarding wizard's state (D-UI-30): which step the operator is on, which steps they have
passed, and whether onboarding is complete.

The state is one row in the setting table (`onboarding.state`), written on every Back, Next and
skip, so killing the app mid-way resumes at the same step (hard rule 5). Whether a step is DONE
is never stored: it is read from the real thing each time the step renders (the run mode row, the
profile file, the documents, the generated sources), so a save anywhere, in the wizard or on the
Profile page, shows on the next render and no indicator can go stale.

First run (D-UI-30): an install with no state row, no profile.yml and no stored `llm.mode` is
fresh and GET / opens the wizard. An install from before the wizard that has a profile and a
stored mode counts as onboarded. Once a state row exists, its `completed` flag decides, so
"Restart onboarding, keep my details" reopens the wizard although the profile is in place."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.models import Setting

KEY = "onboarding.state"
MODE_KEY = "llm.mode"


@dataclass(frozen=True)
class StepInfo:
    key: str
    title: str
    track: str | None = None  # shown only when this track is in use


# Every step, in order. Employment and consulting terms are separate steps so choosing one
# track removes the other's step from the flow and from the count.
STEPS: tuple[StepInfo, ...] = (
    StepInfo("welcome", "Welcome"),
    StepInfo("models", "How Kestrel will think"),
    StepInfo("tracks", "What you are looking for"),
    StepInfo("about", "About you"),
    StepInfo("resume", "Your resume"),
    StepInfo("employment", "What you want in a job", track="employment"),
    StepInfo("consulting", "What you want in consulting work", track="consulting"),
    StepInfo("location", "Where you will work"),
    StepInfo("searches", "Your searches"),
    StepInfo("done", "Done"),
)
STEP_KEYS: tuple[str, ...] = tuple(s.key for s in STEPS)
_BY_KEY = {s.key: s for s in STEPS}


def step_info(key: str) -> StepInfo:
    return _BY_KEY[key]


def steps_for(tracks: tuple[str, ...] | list[str]) -> list[StepInfo]:
    """The steps for the tracks in use, in order."""
    return [s for s in STEPS if s.track is None or s.track in tracks]


@dataclass
class OnboardingState:
    step: str = "welcome"
    visited: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    completed: bool = False
    completed_at: str | None = None
    started_at: str = field(default_factory=utcnow_iso)

    @classmethod
    def from_value(cls, value: Any) -> OnboardingState:
        if not isinstance(value, dict):
            return cls()
        step = value.get("step")
        return cls(
            step=step if step in STEP_KEYS else "welcome",
            visited=[k for k in (value.get("visited") or []) if k in STEP_KEYS],
            skipped=[k for k in (value.get("skipped") or []) if k in STEP_KEYS],
            completed=bool(value.get("completed")),
            completed_at=value.get("completed_at") or None,
            started_at=str(value.get("started_at") or utcnow_iso()),
        )

    def to_value(self) -> dict[str, Any]:
        return asdict(self)


async def load(session: AsyncSession) -> OnboardingState | None:
    """The stored state, or None when the wizard has never been opened."""
    row = await session.get(Setting, KEY)
    return OnboardingState.from_value(row.value) if row is not None else None


async def save(session: AsyncSession, state: OnboardingState) -> None:
    """Write the state row. The caller commits."""
    row = await session.get(Setting, KEY)
    value = state.to_value()
    if row is None:
        session.add(Setting(key=KEY, value=value))
    else:
        row.value = value
        row.updated_at = utcnow_iso()


def _profile_exists() -> bool:
    from kestrel.services.profile_edit import profile_path

    return profile_path().exists()


async def _mode_stored(session: AsyncSession) -> bool:
    return await session.get(Setting, MODE_KEY) is not None


async def is_complete(session: AsyncSession) -> bool:
    state = await load(session)
    if state is not None:
        return state.completed
    # Installed before the wizard existed: a profile and a stored mode mean it was set up.
    return _profile_exists() and await _mode_stored(session)


async def is_fresh_install(session: AsyncSession) -> bool:
    """Nothing set up at all: GET / opens the wizard instead of Today."""
    state = await load(session)
    if state is not None and state.completed:
        return False
    return not _profile_exists() and not await _mode_stored(session)


async def restart(session: AsyncSession) -> OnboardingState:
    """"Restart onboarding, keep my details": back to step 1, nothing else changes. The forms
    open prefilled because they read the profile that is still there. The caller commits."""
    state = OnboardingState()
    await save(session, state)
    return state


def _order(key: str) -> int:
    return STEP_KEYS.index(key) if key in STEP_KEYS else 0


def current_key(current: str, tracks: tuple[str, ...] | list[str]) -> str:
    """The stored step, or the next one in the flow when the stored step no longer applies
    (consulting terms after the operator switched to employment only)."""
    keys = [s.key for s in steps_for(tracks)]
    if current in keys:
        return current
    return next((k for k in keys if _order(k) > _order(current)), keys[-1])


def next_key(current: str, tracks: tuple[str, ...] | list[str]) -> str:
    keys = [s.key for s in steps_for(tracks)]
    return next((k for k in keys if _order(k) > _order(current)), keys[-1])


def prev_key(current: str, tracks: tuple[str, ...] | list[str]) -> str:
    keys = [s.key for s in steps_for(tracks)]
    return next((k for k in reversed(keys) if _order(k) < _order(current)), keys[0])
