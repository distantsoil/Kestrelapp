"""Writing the settings the interface owns (SPEC.md 4.6 "State the interface owns").

Each key is a `setting` row, validated on the way in with the strict normalizers of
`kestrel.ui_settings` (an unknown choice is a `ValueError` the route turns into a plain message),
and read back through `Settings` (which falls back to the default for anything stale). Restoring a
default deletes the row, so the precedence chain (environment, YAML, code default) answers again.
The caller commits."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import delete
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel import ui_settings as u
from kestrel.db.models import Setting

# key -> strict normalizer. Key names are the contract with the interface (SPEC.md 4.6).
OWNED_KEYS: dict[str, Callable[[Any], Any]] = {
    "ui.scene": u.scene,
    "ui.scene_daily": u.flag,
    "ui.scene_rotation": u.scene_rotation,
    "ui.scene_animate": u.flag,
    "ui.touches_enabled": u.flag,
    "ui.touches_off": u.touches_off,
    "ui.touch_groups_off": u.touch_groups_off,
    "ui.avatar": u.avatar,
    "ui.text_size": u.text_size,
    "ui.reduce_motion": u.flag,
    "notify.sound": u.flag,
    "notify.sound_name": u.sound_name,
    "notify.sound_volume": u.sound_volume,
    "notify.quiet_hours": u.quiet_hours,
    "region.date_format": u.date_format,
    "region.time_format": u.time_format,
}


def validate(key: str, value: Any) -> Any:
    """The normalized value for an owned key. `KeyError` for a key the interface does not own;
    `ValueError` (plain message) for a value that is not allowed."""
    if key not in OWNED_KEYS:
        raise KeyError(key)
    return OWNED_KEYS[key](value)


async def set_value(session: AsyncSession, key: str, value: Any) -> Any:
    """Validate and store one owned key as a single upsert; returns the stored value."""
    clean = validate(key, value)
    stmt = insert(Setting).values(key=key, value=clean)
    await session.execute(
        stmt.on_conflict_do_update(index_elements=[Setting.key], set_={"value": clean})
    )
    return clean


async def restore_defaults(session: AsyncSession, keys: list[str] | None = None) -> int:
    """Delete the rows of the given owned keys (all of them when `keys` is None) so each reads
    its default again. Returns how many rows were removed."""
    chosen = list(OWNED_KEYS) if keys is None else keys
    for key in chosen:
        if key not in OWNED_KEYS:
            raise KeyError(key)
    result = await session.execute(delete(Setting).where(Setting.key.in_(chosen)))
    return int(result.rowcount or 0)
