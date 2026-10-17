"""Which tracks the operator uses (D-UI-26): employment, consulting, or both.

Some operators never want the consulting track. Choosing employment only must hide consulting
everywhere (nav switch, Profile tabs, Sources, Today, onboarding), not leave it empty. The
choice is one row in the setting table. The default for a NEW install is employment only
(release concern 10, D-UI-54): consulting is optional and off until the operator turns it on in
onboarding or Settings. An install that predates the choice and has no row is pinned to both at
startup by `keep_existing_choice`, so nothing an existing operator sees disappears.

Templates read the cached value through `enabled_tracks_cached()` (set at startup and on every
save, the same pattern as the UI scheme cache), so a page render never awaits the database
for it."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.base import utcnow_iso
from kestrel.db.enums import TRACKS
from kestrel.db.models import Setting

KEY = "tracks.enabled"
DEFAULT: tuple[str, ...] = ("employment",)
BOTH: tuple[str, ...] = tuple(TRACKS)  # ("employment", "consulting")

_cache: dict[str, tuple[str, ...] | None] = {"value": None}


def _clean(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return DEFAULT
    picked = tuple(t for t in TRACKS if t in value)
    return picked or DEFAULT  # never zero tracks: an empty choice reads as the default


async def load_enabled(session: AsyncSession) -> tuple[str, ...]:
    row = await session.get(Setting, KEY)
    value = _clean(row.value) if row is not None else DEFAULT
    _cache["value"] = value
    return value


def _profile_has_consulting() -> bool:
    """The operator's own profile.yml (never the shipped example) holds a consulting block."""
    from kestrel.services import profile_edit

    try:
        data = profile_edit._read(profile_edit.profile_path())
    except profile_edit.ProfileEditError:
        return False  # an unreadable file is the Profile page's problem, not evidence
    return isinstance(data.get("consulting"), dict)


async def has_consulting_artifact(session: AsyncSession) -> bool:
    """Any sign the operator already uses the consulting track: a consulting opportunity, an
    enabled consulting source, a consulting pursuit, a capability statement document, or a
    consulting block in their profile.yml (release round review, finding 7)."""
    from sqlalchemy import select

    from kestrel.db.models import DocMaster, Opportunity, Pursuit, Source

    probes = (
        select(Opportunity.id).where(Opportunity.track == "consulting"),
        select(Source.id).where(Source.track == "consulting", Source.enabled.is_(True)),
        select(Pursuit.id).where(Pursuit.track == "consulting"),
        select(DocMaster.id).where(DocMaster.doc_kind == "capability_statement"),
    )
    for stmt in probes:
        if (await session.execute(stmt.limit(1))).first() is not None:
            return True
    return _profile_has_consulting()


async def keep_existing_choice(session: AsyncSession) -> bool:
    """Startup, before the first load: an install that predates the new default (no row, yet it
    finished onboarding or holds any consulting artifact, see has_consulting_artifact) keeps both
    tracks, its old implicit setting. A new install, or one still in the wizard with nothing
    consulting, gets no row and so the employment-only default. Runs on every startup, with or
    without the worker. Returns True when it wrote the row. The caller commits."""
    from kestrel.services import onboarding

    if await session.get(Setting, KEY) is not None:
        return False
    if await has_consulting_artifact(session) or await onboarding.is_complete(session):
        session.add(Setting(key=KEY, value=list(BOTH)))
        _cache["value"] = BOTH
        return True
    return False


async def save_enabled(session: AsyncSession, tracks: list[str] | tuple[str, ...]) -> tuple:
    value = _clean(list(tracks))
    row = await session.get(Setting, KEY)
    if row is None:
        session.add(Setting(key=KEY, value=list(value)))
    else:
        row.value = list(value)
        row.updated_at = utcnow_iso()
    _cache["value"] = value
    return value


def enabled_tracks_cached() -> tuple[str, ...]:
    """The last loaded or saved choice; the default before the first load."""
    return _cache["value"] or DEFAULT


def switched_off_tracks() -> tuple[str, ...]:
    """The tracks the operator switched off, from the loaded or saved choice. Empty before the
    first load in this process (a CLI that never read the row holds nothing back), so a task is
    only ever held on a choice this process actually read (R2, D-UI-68)."""
    value = _cache["value"]
    if value is None:
        return ()
    return tuple(t for t in TRACKS if t not in value)


def consulting_enabled() -> bool:
    return "consulting" in enabled_tracks_cached()


def forget_cache() -> None:
    """Tests, or a data-dir change."""
    _cache["value"] = None
