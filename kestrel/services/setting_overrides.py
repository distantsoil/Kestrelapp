"""The setting-table layer of the config precedence chain (SPEC.md 4.6, highest priority). Reads
the `setting` rows into a nested override dict and hands it to config.load_settings, so a UI change
takes effect on the next request/task lease with no restart.

Bootstrap ordering: path and logging keys must resolve BEFORE the database exists, so those keys
never live in the setting table (config.load_settings' db_overrides is for non-bootstrap keys)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel import config as config_mod
from kestrel.db.models import Setting
from kestrel.services.updater import STATE_KEY as UPDATE_STATE_KEY

# Keys that must never be overridden from the DB (chicken-and-egg with paths/logging).
BOOTSTRAP_KEYS = frozenset({"log_level"})

# A one-shot request from "Run now" (POST /api/run/now, the Runs page form): the serve loop reads
# it at the start of its next cycle, deletes it, and runs that cycle in the named mode. It is a
# row, not an in-memory flag, so it survives a restart between the click and the cycle (hard
# rule 5) and so the CLI and the app see the same queue. Never part of the config layer.
PENDING_RUN_MODE_KEY = "pending_run_mode"
# The embed model the corpus was last swept for (pipeline/embed.py enqueue_stale_embeddings): the
# sweep runs once per model change, not once per worker cycle. State, not configuration.
EMBED_SWEPT_MODEL_KEY = "embed_swept_model"
# Which hosted model each tier got from Connect, and which the operator overrode on the Models
# page (services/hosted_models.py). State about the llm.hosted_tiers rows, not configuration.
HOSTED_MODEL_DEFAULTS_KEY = "hosted_model_defaults"
HOSTED_MODEL_OVERRIDES_KEY = "hosted_model_overrides"
# The update checker's state (etag, last answer, skipped version; services/updater.py STATE_KEY).
# State, not configuration: it must never be read back as a Settings override.
TRANSIENT_KEYS = frozenset({
    PENDING_RUN_MODE_KEY, EMBED_SWEPT_MODEL_KEY, HOSTED_MODEL_DEFAULTS_KEY,
    HOSTED_MODEL_OVERRIDES_KEY, UPDATE_STATE_KEY,
})


# Setting rows that hold content or UI state, not configuration: interview prep notes (up to 20k
# characters each, one row per role), the last story build, dismissed guide strips and asks.
# Kept out of the config layer so a polled page (the status rail, Today's progress panel) does
# not read every note on every poll. Excluded in SQL, so the values are never even fetched.
# `tier0_override.` is Score this role anyway (kestrel.services.tier0_override, D-UI-74),
# `run_model.pending.` a run's one-run model waiting for Tier 0 (kestrel.services.run_model,
# D-UI-72), and `sources.title_variants_removed` the search variants taken out (D-TTL-2).
NON_CONFIG_PREFIXES: tuple[str, ...] = (
    "prep_notes.", "tier0_override.", "run_model.pending.", "tier0_recheck.",
)
STATE_KEYS = frozenset({
    "stories.last_build", "ui.guides_dismissed", "ui.asks_dismissed",
    # What the user last chose on the page itself (the announcement they closed, the board's
    # date range and column cap), never configuration.
    "ui.announcements_dismissed", "ui.board_range", "ui.board_cap", "ui.show_balance",
    "titles.last_run",
    # Stamps and choices the app keeps for itself, never configuration (review finding 15):
    # run_schedule.LAST_AT_KEY, run_summary.RESUMED_KEY, generated.SEC_TITLES_KEY.
    "scheduler.last_scheduled_at", "worker.resumed_at", "sources.sec_use_titles",
    "sources.title_variants_removed",
})
# Sections where configuration and state rows share a prefix: only the section's own fields are
# configuration (`ui.scheme`, `stories.merge_threshold`); anything else under them is state.
_MIXED_SECTIONS = ("ui", "stories")


def config_rows_select():
    """SELECT key, value over the setting rows that can be configuration. Shared by
    db_override_dict and the doctor's sync copy (kestrel.doctor._db_overrides_sync), so both
    read exactly the same rows."""
    stmt = select(Setting.key, Setting.value).where(
        Setting.key.not_in(sorted(STATE_KEYS | TRANSIENT_KEYS))
    )
    for prefix in NON_CONFIG_PREFIXES:
        stmt = stmt.where(Setting.key.not_like(prefix + "%"))
    return stmt


def is_config_key(key: str) -> bool:
    """Whether a setting row is part of the config layer: not a bootstrap key, not transient or
    state, and naming a Settings field (a row for anything else would be ignored by Settings
    anyway, so dropping it here changes nothing but the read)."""
    if key in TRANSIENT_KEYS or key in STATE_KEYS:
        return False
    if any(key.startswith(p) for p in NON_CONFIG_PREFIXES):
        return False
    parts = key.split(".")
    top = parts[0]
    if top in BOOTSTRAP_KEYS:
        return False
    fields = config_mod.Settings.model_fields
    if top not in fields:
        return False
    if top in _MIXED_SECTIONS and len(parts) > 1:
        section = fields[top].annotation
        sub = getattr(section, "model_fields", None)
        if sub is not None and parts[1] not in sub:
            return False
    return True


def overrides_from_rows(rows) -> dict[str, Any]:
    """The nested override dict from (key, value) rows, config keys only."""
    return nest_overrides({k: v for k, v in rows if is_config_key(k)})


def nest_overrides(flat: dict[str, Any]) -> dict[str, Any]:
    """Turn {"thresholds.apply": 4.5, "mcp.port": 9000} into a nested dict for Settings(**...)."""
    nested: dict[str, Any] = {}
    for key, value in flat.items():
        parts = key.split(".")
        cursor = nested
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    return nested


async def db_override_dict(session: AsyncSession) -> dict[str, Any]:
    rows = (await session.execute(config_rows_select())).all()
    return overrides_from_rows(rows)


async def load_settings_with_db(
    session: AsyncSession, *, mode: str | None = None
) -> config_mod.Settings:
    """Full precedence: setting-table row > env > YAML > default. `mode` ("local" or "hosted")
    overrides the stored `llm.mode` for the returned object; an explicit "hosted" that cannot run
    raises config.HostedModeError with the reason."""
    return config_mod.load_settings(db_overrides=await db_override_dict(session), mode=mode)


async def queue_run_mode(session: AsyncSession, mode: str) -> None:
    """Record that the next worker cycle must run in `mode`. Validates the name and, for
    "hosted", that hosted mode can actually run (HostedModeError otherwise); nothing is queued
    that the loop would have to refuse. The caller commits.

    Written as one upsert, not load-then-set: the serve loop's take_run_mode can delete the row
    between a load here and the flush, and an UPDATE of a vanished row is a StaleDataError at
    commit (a 500 from the Run now routes). ON CONFLICT lands the new value either way."""
    if mode not in config_mod.MODES:
        known = ", ".join(config_mod.MODES)
        raise ValueError(f"unknown run mode {mode!r}; expected one of {known}")
    if mode == "hosted":
        problem = config_mod.hosted_mode_problem(await load_settings_with_db(session))
        if problem is not None:
            raise config_mod.HostedModeError(problem)
    stmt = insert(Setting).values(key=PENDING_RUN_MODE_KEY, value=mode)
    stmt = stmt.on_conflict_do_update(index_elements=[Setting.key], set_={"value": mode})
    await session.execute(stmt)
    # A copy already loaded in this session (a prior peek) would otherwise keep the old value,
    # or claim a row that the upsert just recreated under it.
    loaded = await session.get(Setting, PENDING_RUN_MODE_KEY)
    if loaded is not None:
        session.expire(loaded)


async def peek_run_mode(session: AsyncSession) -> str | None:
    """The queued one-shot mode, if any, without consuming it (for the status strip)."""
    row = await session.get(Setting, PENDING_RUN_MODE_KEY)
    return row.value if row is not None and isinstance(row.value, str) else None


async def take_run_mode(session: AsyncSession) -> str | None:
    """Consume the queued one-shot mode: return it and delete the row (committed here), so a
    request is honored by exactly one cycle. None when nothing is queued."""
    row = await session.get(Setting, PENDING_RUN_MODE_KEY)
    if row is None:
        return None
    mode = row.value if isinstance(row.value, str) else None
    await session.delete(row)
    await session.commit()
    return mode
