"""The diagnostics bundle the About page downloads (SPEC-INTERFACES 2.1 `/about`, D-UI-6).

A tester hits a problem, clicks Download diagnostics, and hands the zip to whoever is reviewing.
The bundle therefore has to answer "what build, what machine, what settings, what happened" with
nothing in it that the operator would mind forwarding. The rule is the same as the backup's, only
stricter: no key or secret value under any name, no profile.yml, no document text, no posting
text, no contacts, no database file. Every text member goes through one scrubber that masks bearer
and API tokens regardless of settings and, in confidential mode, replaces the current employer
with the operator's own placeholder (web/redact.redact_text, the same map every list view uses).

The one member that can quote a posting is quarantine.json: a quarantined model response may echo
the description it was given. It is cut to RAW_RESPONSE_CHARS characters and redacted, and the
page tells the tester they can delete that file from the zip before sending it.

Everything here is synchronous file and sqlite I/O plus two best-effort probes (hardware
detection, and `kestrel doctor --quick` when no report has been saved); the route runs
`build_bundle` in a thread. The database is read through a plain sqlite3 connection, never the
app's aiosqlite engine, so the thread never touches the event loop's connections."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sqlite3
import tempfile
import zipfile
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from importlib.metadata import distributions
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import structlog
import yaml

import kestrel
from kestrel import paths
from kestrel.config import Settings
from kestrel.web.redact import redact_text

log = structlog.get_logger()

# kestrel.ingest.runner.DROPPED_SAMPLE_KEY, spelled here so bundles never import ingest (D-SRC-17)
_TITLE_SAMPLE_KEY = "dropped_title_sample"

REDACTED = "<redacted>"
# A key whose name carries one of these words holds a secret when its value is a string.
# `max_tokens` and friends are integers and stay; `api_key_env` names a variable, not a value.
SECRET_KEY_WORDS: tuple[str, ...] = ("key", "secret", "token", "password")
KEEP_KEYS: frozenset[str] = frozenset({"api_key_env"})
LOG_TAIL_LINES = 2000
QUARANTINE_ROWS = 20
ROLE_ROWS = 500  # roles.json: the newest roles by ID (owner round 14)
RAW_RESPONSE_CHARS = 500
RUN_ROWS = 5
SCRAPE_RUN_ROWS = 10
_TEMP_PREFIX = "kestrel-diag-"

# Masked in every text member no matter what settings say. OpenRouter keys start with sk-or-,
# Anthropic keys with sk-ant-, OpenAI keys with sk- or sk-proj-, openmodel.ai keys with om-; the
# first two catch a key however it was pasted into a header.
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)x-api-key[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bom-[A-Za-z0-9_-]{8,}"),
)

# What the zip holds, in the order it is written. The About page and README.txt list exactly this.
CONTENTS: tuple[tuple[str, str], ...] = (
    ("README.txt", "this list, and what was left out"),
    (
        "summary.json",
        "version, frozen or source checkout, Python and platform, detected hardware, the active "
        "hardware profile, data folder path, database schema revision, run mode, hosted mode "
        "state with backend names and models (never keys), row counts per table, Tier 0 set-asides "
        "counted by rule name (no titles or orgs), evaluations counted by model id, the last 5 "
        "runs and the last 10 scans",
    ),
    ("doctor.json", "the saved preflight report, or a quick kestrel doctor run if none was saved"),
    (
        "settings.redacted.yml",
        "the effective settings with every key, secret, token and password value replaced by "
        "<redacted>, backend addresses hidden, and the names of the environment variables that "
        "are set (never values)",
    ),
    (
        "logs/",
        f"the last {LOG_TAIL_LINES} lines of each log file, with bearer and API tokens masked "
        "and, in confidential mode, the current employer replaced by your placeholder",
    ),
    (
        "quarantine.json",
        f"the last {QUARANTINE_ROWS} quarantine rows: tier, model, error, time, and the raw model "
        f"response cut to {RAW_RESPONSE_CHARS} characters; raw model output may quote the posting, "
        "so delete this file from the zip if you would rather not share it",
    ),
    (
        "roles.json",
        f"the newest {ROLE_ROWS} roles by role ID (the ID shows under Show details on a role's "
        "Evaluation tab): title, organization, the location as posted and as Kestrel read it, the "
        "remote type, the stage and the rule that set it aside, the posting's site, and each "
        "scoring pass (tier, model, prompt version, verdict, scores, the five dimension scores and "
        "the warning signs). Never the posting text, your profile, your resume or your notes",
    ),
    ("packages.txt", "installed Python packages and their versions"),
)
EXCLUDED: tuple[str, ...] = (
    ".env and every API key, shared secret or token value, under any name",
    "profile.yml and anything in it",
    "document masters, slots and variants (your resume text)",
    "opportunity titles, descriptions, URLs and organization names",
    "contacts, outreach drafts and interactions",
    "the database file itself",
)

Scrubber = Callable[[str], str]


# ---------------------------------------------------------------------------------- scrubbing
def mask_tokens(text: str) -> str:
    """Mask anything shaped like a bearer token or an API key, regardless of settings."""
    out = text
    for pattern in _TOKEN_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out


def looks_like_secret(value: str) -> bool:
    return any(p.search(value) for p in _TOKEN_PATTERNS)


# A saved value shorter than this is not searched for (it would mangle ordinary words); no
# provider issues a key this short.
MIN_SECRET_CHARS = 4


def saved_secret_values() -> list[str]:
    """Every secret value Kestrel holds right now, read at bundle time: the private key store
    (the data-home .env) and the system environment's secret-named variables when the operator
    opted in to them. Longest first, so a key that
    contains another is masked whole (release round review, finding 8)."""
    from kestrel.config import (
        _dotenv_state,
        is_secret_name,
        load_dotenv_into_environ,
        use_system_keys,
    )

    values: set[str] = set()
    try:
        load_dotenv_into_environ()
        values.update(v for v in (_dotenv_state.get("secrets") or {}).values() if v)
    except (OSError, ValueError) as exc:
        log.warning("diagnostics_secret_store_unreadable", error=type(exc).__name__)
    if use_system_keys():
        values.update(v for k, v in os.environ.items() if v and is_secret_name(k))
    # Backend `api_key` fields in settings are not read here: the local Ollama backend's is the
    # placeholder "ollama", not a secret, and a real key is never kept in settings (CLAUDE.md).
    return sorted((v for v in values if len(v) >= MIN_SECRET_CHARS), key=len, reverse=True)


def make_scrubber(
    redaction: Mapping[str, str] | None, secrets: Iterable[str] = (), *,
    addresses: Iterable[str] = (),
) -> Scrubber:
    """Token masking always, plus every saved key value whatever its shape (a key that matches
    no token pattern must still never ride out in a log line); employer redaction when
    confidential mode supplied a map."""
    from kestrel.privacy import EmployerMap, from_pairs

    mapping = (redaction if isinstance(redaction, EmployerMap)
               else from_pairs(dict(redaction or {}).items()))
    raw = {v for v in secrets if v and len(v) >= MIN_SECRET_CHARS}
    addresses = set(addresses)
    short_hosts = {
        value for value in addresses if value and len(value) < MIN_SECRET_CHARS
    }
    raw.update(value for value in addresses if len(value) >= MIN_SECRET_CHARS)
    # App events reach the log as JSON, which escapes `"` and `\`. A key containing either would
    # appear in its escaped form and slip past an exact match on the raw value, so both are masked.
    escaped = {json.dumps(v, ensure_ascii=False)[1:-1] for v in raw}
    values = sorted(raw | escaped, key=len, reverse=True)

    def scrub(text: str) -> str:
        out = text
        for v in values:
            out = out.replace(v, REDACTED)
        for host in short_hosts:
            out = re.sub(r"(?<![\w.-])" + re.escape(host) + r"(?![\w.-])", REDACTED, out)
        return mask_tokens(redact_text(out, mapping, enabled=bool(mapping)) or "")

    return scrub


def _is_secret_key(name: str) -> bool:
    lowered = name.lower()
    return any(word in lowered for word in SECRET_KEY_WORDS)


def redact_tree(value: Any) -> Any:
    """Walk a dumped settings tree: a string under a key named like a secret, or a string that
    looks like one, becomes <redacted>. Numbers, booleans and None are never secrets and stay, so
    `max_tokens: 2048` and an unset `api_key: null` both still read."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key in KEEP_KEYS:
                out[key] = item
            elif _is_secret_key(key) and isinstance(item, str):
                out[key] = REDACTED
            else:
                out[key] = redact_tree(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_tree(v) for v in value]
    if isinstance(value, str):
        return mask_tokens(value)  # a key pasted into a header or a note, masked in place
    return value


# ----------------------------------------------------------------------------------- members
def _json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=False, default=str)


def readme_text() -> str:
    lines = [
        f"Kestrel diagnostics bundle, version {kestrel.__version__}",
        "",
        "Contents",
        *(f"  {name:<24} {what}" for name, what in CONTENTS),
        "",
        "Never included",
        *(f"  - {item}" for item in EXCLUDED),
        "",
        "Every text file was passed through the same scrubber: bearer and API tokens are",
        "masked and, in confidential mode, the current employer is replaced by the operator's",
        "placeholder.",
    ]
    return "\n".join(lines) + "\n"


def _hardware() -> dict[str, Any]:
    try:
        from kestrel.services.setup_state import detected_hardware

        return dict(detected_hardware().model_dump())
    except Exception as exc:  # detection shells out; a failure is a line in the bundle, not a 500
        log.warning("diagnostics_hardware_failed", error=str(exc))
        return {"error": f"{type(exc).__name__}: {exc}"}


def _active_profile() -> dict[str, Any] | None:
    try:
        from kestrel.llm.profiles import load_active_profile

        active = load_active_profile()
    except Exception as exc:  # a corrupt record is reported, not fatal
        log.warning("diagnostics_profile_failed", error=str(exc))
        return {"error": f"{type(exc).__name__}: {exc}"}
    if active is None:
        return None
    return {"profile": active.profile, "reason": active.reason, "chosen_at": active.chosen_at}


def _schema_revision() -> str | None:
    from kestrel.db import migrate

    try:
        return migrate.current_revision()
    except Exception as exc:  # a locked or corrupt file is itself a finding
        log.warning("diagnostics_revision_failed", error=str(exc))
        return f"error: {type(exc).__name__}: {exc}"


def _hosted_state(settings: Settings) -> dict[str, Any]:
    from kestrel.llm.providers import secret_present

    llm = settings.llm
    return {
        "enabled": llm.hosted_mode.enabled,
        "acknowledged": bool(llm.hosted_mode.acknowledged_at),
        "active": llm.hosted_mode.active,
        "redact_confidential": llm.hosted_mode.redact_confidential,
        "hosted_tiers": {
            tier: {"backend": cfg.backend, "model": cfg.model}
            for tier, cfg in llm.hosted_tiers.items()
        },
        "effective_tiers": {
            tier: {"backend": cfg.backend, "model": cfg.model} for tier, cfg in llm.tiers.items()
        },
        "backends": {
            name: {
                "provider": b.provider,
                "protocol": b.protocol,
                "external": b.external,
                "base_url": b.base_url,
                "api_key_env": b.api_key_env,
                "key_present": bool(b.api_key) or secret_present(b.api_key_env),
            }
            for name, b in llm.backends.items()
        },
    }


def _connect() -> sqlite3.Connection | None:
    db = paths.db_path()
    if not db.exists():
        return None
    conn = sqlite3.connect(str(db), timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _grouped(conn: sqlite3.Connection, table: str, column: str) -> dict[str, int] | str:
    try:
        rows = conn.execute(
            f"SELECT {column} AS k, COUNT(*) AS n FROM {table} GROUP BY {column} ORDER BY {column}"
        ).fetchall()
    except sqlite3.Error as exc:
        return f"unavailable: {exc}"
    return {str(r["k"]): int(r["n"]) for r in rows}


def _count(conn: sqlite3.Connection, table: str) -> int | str:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error as exc:
        return f"unavailable: {exc}"


def _loads(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


CUSTOM_RULE = "custom"


def shipped_rule_ids() -> frozenset[str]:
    """The hard-blocker ids in the SHIPPED default filters.yml (packaged, never the operator's
    copy). Only these are named in the zip (review finding 8): an operator-written id such as
    `exclude_<employer>` can carry a name, so every other id is counted as "custom"."""
    import yaml

    from kestrel import paths
    from kestrel.rules import helpers

    try:
        with paths.resource_path(paths.packaged_config() / "filters.yml") as p:
            filters = helpers.load_filters(str(p))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        log.warning("shipped_filters_unreadable", error=f"{type(exc).__name__}: {exc}")
        return frozenset()
    return frozenset(
        str(rule.get("id")) for _track, kind, rule in helpers.iter_rules(filters)
        if kind == "hard_blockers" and isinstance(rule, dict) and rule.get("id")
    )


def tier0_rejects_by_rule(conn: sqlite3.Connection) -> dict[str, int] | str:
    """Roles Tier 0 set aside, counted by rule name only (D-UI-67). Tier 0 stores
    `blocker:<rule id>: <detail>`; the detail can quote the posting's title, so only the rule id
    is read out, and only when it is a rule the shipped default filters name. Any other id (the
    operator's own rule, which may name an employer) is "custom" (finding 8); a reason that is
    not a blocker at all is "unknown". No titles, no orgs, no counts per role."""
    try:
        rows = conn.execute(
            "SELECT stage_reason FROM opportunity WHERE stage = 't0_reject'"
        ).fetchall()
    except sqlite3.Error as exc:
        return f"unavailable: {exc}"
    shipped = shipped_rule_ids()
    out: dict[str, int] = {}
    for (reason,) in rows:
        text = str(reason or "")
        rule = "unknown"
        if text.startswith("blocker:"):
            head = text[len("blocker:"):].split(":", 1)[0].strip()
            rule = head if head in shipped else CUSTOM_RULE
        out[rule] = out.get(rule, 0) + 1
    return dict(sorted(out.items()))


def table_counts(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "opportunities_by_stage": _grouped(conn, "opportunity", "stage"),
        "tier0_rejects_by_rule": tier0_rejects_by_rule(conn),
        "evaluations_by_tier": _grouped(conn, "evaluation", "tier"),
        # Model ids only (never a title or org), so "did Score again use the model I picked?"
        # is answerable from the bundle.
        "evaluations_by_model": _grouped(conn, "evaluation", "model"),
        "pursuits_by_status": _grouped(conn, "pursuit", "status"),
        "labels": _count(conn, "label"),
        "quarantine": _count(conn, "quarantine"),
        "runs": _count(conn, "run"),
        "scrape_runs_by_status": _grouped(conn, "scrape_run", "status"),
        "transmissions_by_status": _grouped(conn, "frontier_transmission", "status"),
    }


def recent_runs(conn: sqlite3.Connection) -> list[dict[str, Any]] | str:
    try:
        rows = conn.execute(
            "SELECT id, name, status, mode, counts, started_at, finished_at FROM run "
            f"ORDER BY id DESC LIMIT {RUN_ROWS}"
        ).fetchall()
    except sqlite3.Error as exc:
        return f"unavailable: {exc}"
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "status": r["status"],
            "mode": r["mode"],
            "counts": _loads(r["counts"]),
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
        }
        for r in rows
    ]


def recent_scrape_runs(conn: sqlite3.Connection, scrub: Scrubber) -> list[dict[str, Any]] | str:
    try:
        rows = conn.execute(
            "SELECT s.id, src.kind, s.status, s.counts, s.started_at, s.finished_at, s.error "
            "FROM scrape_run s LEFT JOIN source src ON src.id = s.source_id "
            f"ORDER BY s.id DESC LIMIT {SCRAPE_RUN_ROWS}"
        ).fetchall()
    except sqlite3.Error as exc:
        return f"unavailable: {exc}"
    out = []
    for r in rows:
        loaded = _loads(r["counts"])
        counts = loaded if isinstance(loaded, dict) else {}
        # D-SRC-17: the dropped-title sample holds opportunity titles; a bundle never does.
        counts = {k: v for k, v in counts.items() if k != _TITLE_SAMPLE_KEY}
        out.append({
            "id": r["id"],
            "source_kind": r["kind"],
            "status": r["status"],
            "counts": counts,
            "dropped_outside_area": counts.get("dropped_outside_area", 0),
            "dropped_unrelated_title": counts.get("dropped_unrelated_title", 0),
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "error": scrub(r["error"]) if r["error"] else None,
        })
    return out


WITHHELD = "withheld: Kestrel could not confirm your current employer would be hidden"


def quarantine_rows(
    conn: sqlite3.Connection | None, scrub: Scrubber, *, conservative: bool = False
) -> dict[str, Any]:
    """The newest quarantine rows. `conservative` leaves out every excerpt of model output (the
    raw response and the error text, either of which can quote a posting) and keeps the
    counts, tier, model and time: used when confidential mode cannot be honored."""
    if conn is None:
        return {"note": "database not initialized", "rows": []}
    try:
        rows = conn.execute(
            "SELECT id, opportunity_id, tier, model, error, attempts, created_at, raw_response "
            f"FROM quarantine ORDER BY id DESC LIMIT {QUARANTINE_ROWS}"
        ).fetchall()
    except sqlite3.Error as exc:
        return {"note": f"unavailable: {exc}", "rows": []}
    out = []
    for r in rows:
        raw = r["raw_response"] or ""
        out.append(
            {
                "id": r["id"],
                "opportunity_id": r["opportunity_id"],
                "tier": r["tier"],
                "model": r["model"],
                "error": (WITHHELD if conservative else scrub(r["error"])) if r["error"] else None,
                "attempts": r["attempts"],
                "created_at": r["created_at"],
                "raw_response": WITHHELD if conservative else scrub(raw[:RAW_RESPONSE_CHARS]),
                "raw_response_truncated": len(raw) > RAW_RESPONSE_CHARS,
            }
        )
    note = f"last {QUARANTINE_ROWS} rows; raw_response cut to {RAW_RESPONSE_CHARS} characters"
    if conservative:
        note = f"last {QUARANTINE_ROWS} rows; excerpts {WITHHELD}"
    return {"note": note, "rows": out}


def role_rows(
    conn: sqlite3.Connection | None, scrub: Scrubber, *, conservative: bool = False
) -> dict[str, Any]:
    """The newest roles with what the filters and each scoring pass decided, so a role ID the
    operator reads off its Evaluation tab can be looked up (owner round 14). Posting text, the
    model's prose and anything about the operator stay out. `conservative` (confidential mode
    that cannot be honored) withholds titles and organizations."""
    if conn is None:
        return {"note": "database not initialized", "rows": []}
    from urllib.parse import urlparse

    try:
        opps = conn.execute(
            "SELECT id, title, org_name, location_raw, country, state, city, remote_type, stage, "
            "stage_reason, url, first_seen_at, date_posted FROM opportunity "
            f"ORDER BY id DESC LIMIT {ROLE_ROWS}"
        ).fetchall()
        ids = [r["id"] for r in opps]
        evs: dict[int, list[dict[str, Any]]] = {}
        if ids:
            marks = ",".join("?" * len(ids))
            for e in conn.execute(
                "SELECT opportunity_id, tier, model, prompt_version, verdict, score, "
                "weighted_score, needs_review, dimensions, legitimacy, created_at "
                f"FROM evaluation WHERE opportunity_id IN ({marks}) ORDER BY id", ids
            ).fetchall():
                legit = _loads(e["legitimacy"]) or {}
                signals = legit.get("signals") if isinstance(legit, dict) else None
                dims = _loads(e["dimensions"])
                evs.setdefault(e["opportunity_id"], []).append({
                    "tier": e["tier"], "model": e["model"], "prompt": e["prompt_version"],
                    "verdict": e["verdict"], "score": e["score"],
                    "weighted_score": e["weighted_score"], "needs_review": bool(e["needs_review"]),
                    "dimensions": {k: v.get("score") if isinstance(v, dict) else v
                                   for k, v in dims.items()} if isinstance(dims, dict) else None,
                    "signals": [s.get("kind") if isinstance(s, dict) else s
                                for s in signals] if isinstance(signals, list) else None,
                    "created_at": e["created_at"],
                })
    except sqlite3.Error as exc:
        return {"note": f"unavailable: {exc}", "rows": []}
    out = []
    for r in opps:
        out.append({
            "id": r["id"],
            "title": WITHHELD if conservative else scrub(r["title"] or ""),
            "org": WITHHELD if conservative else scrub(r["org_name"] or ""),
            "location_raw": r["location_raw"], "country": r["country"], "state": r["state"],
            "city": r["city"], "remote_type": r["remote_type"], "stage": r["stage"],
            # A site name ("acme.wd5.myworkdayjobs.com") or a set-aside reason can name the
            # employer: scrubbed, and withheld with the title when confidential mode cannot be
            # honored (round 14 review).
            "stage_reason": (WITHHELD if conservative else scrub(r["stage_reason"]))
            if r["stage_reason"] else None,
            "site": (WITHHELD if conservative else scrub(urlparse(r["url"]).hostname or ""))
            if r["url"] else None,
            "first_seen_at": r["first_seen_at"], "date_posted": r["date_posted"],
            "evaluations": evs.get(r["id"], []),
        })
    return {"note": f"newest {ROLE_ROWS} roles by ID; no posting text", "rows": out}


def summary(settings: Settings, scrub: Scrubber) -> dict[str, Any]:
    conn = _connect()
    try:
        counts: dict[str, Any] | str = "database not initialized"
        runs: list[dict[str, Any]] | str = []
        scans: list[dict[str, Any]] | str = []
        if conn is not None:
            counts = table_counts(conn)
            runs = recent_runs(conn)
            scans = recent_scrape_runs(conn, scrub)
    finally:
        if conn is not None:
            conn.close()
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "version": kestrel.__version__,
        "frozen": paths.is_frozen(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "os": platform.system(),
        "arch": platform.machine(),
        "data_home": str(paths.data_home()),
        "schema_revision": _schema_revision(),
        "hardware": _hardware(),
        "active_profile": _active_profile(),
        "run_mode": settings.llm.mode,
        "confidential_mode": settings.confidential_mode,
        "hosted_mode": _hosted_state(settings),
        "counts": counts,
        "runs": runs,
        "scrape_runs": scans,
    }


def doctor_rows() -> dict[str, Any]:
    """The persisted report when there is one; otherwise `kestrel doctor --quick`, which probes
    Ollama with a short timeout and skips the needle test, so it finishes in seconds."""
    from kestrel.services.setup_state import load_report

    saved = load_report()
    if saved is not None:
        return {
            "source": "saved report",
            "ran_at": saved.get("ran_at"),
            "blocking": saved.get("blocking"),
            "results": [
                {
                    "name": r.get("name"),
                    "status": r.get("status"),
                    "detail": r.get("detail", ""),
                    "remediation": r.get("remediation", ""),
                }
                for r in saved["results"]
                if isinstance(r, dict)
            ],
        }
    try:
        from kestrel.doctor import run_doctor

        rep = run_doctor(quick=True)
    except Exception as exc:  # doctor must not take the bundle down with it
        log.warning("diagnostics_doctor_failed", error=str(exc))
        return {"source": "quick run", "error": f"{type(exc).__name__}: {exc}", "results": []}
    return {
        "source": "quick run",
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "blocking": rep.has_blocking_failure,
        "results": [
            {
                "name": r.name,
                "status": r.status,
                "detail": r.detail,
                "remediation": r.remediation,
            }
            for r in rep.results
        ],
    }


def environment_names(settings: Settings) -> dict[str, Any]:
    """Which variables are set, by name only: every `api_key_env` a backend names, and every
    KESTREL_* or OLLAMA_* variable in the process environment."""
    referenced = sorted(
        {b.api_key_env for b in settings.llm.backends.values() if b.api_key_env}
    )
    prefixed = sorted(k for k in os.environ if k.startswith(("KESTREL_", "OLLAMA_")))
    from kestrel.config import get_secret

    present = {name: bool(get_secret(name)) for name in referenced}
    return {
        "api_key_env": present,
        "set": sorted(set(prefixed) | {n for n, ok in present.items() if ok}),
    }


def redacted_settings_yaml(
    settings: Settings, *, scrub: Scrubber | None = None, key_scrub: Scrubber | None = None,
) -> str:
    doc = {
        "settings": redact_tree(settings.model_dump(mode="json")),
        "environment": environment_names(settings),
    }
    scrub = scrub or make_scrubber(None, addresses=_backend_addresses(settings))
    return yaml.safe_dump(
        _scrub_values(doc, scrub, key_scrub or make_scrubber(None)),
        sort_keys=False, allow_unicode=True, default_flow_style=False,
    )


def _scrub_values(value: Any, scrub: Scrubber, key_scrub: Scrubber) -> Any:
    if isinstance(value, dict):
        return {
            key_scrub(key) if isinstance(key, str) else key: _scrub_values(item, scrub, key_scrub)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub_values(item, scrub, key_scrub) for item in value]
    return scrub(value) if isinstance(value, str) else value


def _backend_addresses(settings: Settings) -> set[str]:
    """Endpoint addresses and hosts are private regardless of where they were configured."""
    values = set()
    for backend in settings.llm.backends.values():
        address = backend.base_url
        if not address:
            continue
        values.add(address)
        try:
            host = urlsplit(address).hostname
        except ValueError:
            continue
        if host:
            values.add(host)
    return values


def log_tails(scrub: Scrubber) -> list[tuple[str, str]]:
    """(zip member name, scrubbed text) for each file under the log folder."""
    folder = paths.logs_dir()
    if not folder.is_dir():
        return []
    out: list[tuple[str, str]] = []
    for f in sorted(folder.iterdir()):
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            out.append((f"logs/{f.name}", f"unreadable: {exc}\n"))
            continue
        lines = text.splitlines()
        tail = lines[-LOG_TAIL_LINES:]
        header = ""
        if len(lines) > len(tail):
            header = f"# {f.name}: last {len(tail)} of {len(lines)} lines\n"
        out.append((f"logs/{f.name}", scrub(header + "\n".join(tail) + "\n")))
    return out


def packages_text() -> str:
    names: set[str] = set()
    for dist in distributions():
        name = dist.metadata["Name"] if dist.metadata else None
        if name:
            names.add(f"{name}=={dist.version}")
    return "\n".join(sorted(names, key=str.lower)) + "\n"


# ------------------------------------------------------------------------------------- build
def build_bundle(
    settings: Settings,
    *,
    out_dir: str | Path,
    redaction: Mapping[str, str] | None = None,
    conservative: bool = False,
) -> Path:
    """Write the zip under `out_dir` and return its path. `redaction` is the confidential-mode
    map (web/redact.redaction_map) when that mode is on; the caller resolves it on the event loop
    and this function runs in a thread. `conservative` drops the quarantine excerpts (see
    quarantine_rows) for a caller that cannot guarantee the employer is hidden."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    archive = _unique_archive(out)
    secrets = saved_secret_values()
    key_scrub = make_scrubber(redaction, secrets)
    scrub = make_scrubber(redaction, secrets, addresses=_backend_addresses(settings))
    conn = _connect()
    try:
        quarantine = quarantine_rows(conn, scrub, conservative=conservative)
        roles = role_rows(conn, scrub, conservative=conservative)
    finally:
        if conn is not None:
            conn.close()
    # Built under a name the save and reveal routes never match, then renamed into place in one
    # step: a failure part way leaves no corrupt zip in the operator's Downloads folder.
    part = archive.with_name(f".{archive.name}.part")
    finished = False
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("README.txt", readme_text())
            z.writestr("summary.json", _json(
                _scrub_values(summary(settings, scrub), scrub, key_scrub),
            ))
            z.writestr("doctor.json", _json(_scrub_values(doctor_rows(), scrub, key_scrub)))
            z.writestr("settings.redacted.yml", redacted_settings_yaml(
                settings, scrub=scrub, key_scrub=key_scrub,
            ))
            for name, text in log_tails(scrub):
                z.writestr(name, text)
            z.writestr("quarantine.json", _json(_scrub_values(quarantine, scrub, key_scrub)))
            z.writestr("roles.json", _json(_scrub_values(roles, scrub, key_scrub)))
            z.writestr("packages.txt", packages_text())
        os.replace(part, archive)
        finished = True
    finally:
        if not finished:
            part.unlink(missing_ok=True)
    return archive


def _unique_archive(out: Path) -> Path:
    """A name to the second, with a counter when two saves land in the same second, so a second
    save never overwrites the first."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    base = f"kestrel-diagnostics-{kestrel.__version__}-{stamp}"
    archive = out / f"{base}.zip"
    n = 2
    while archive.exists():
        archive = out / f"{base}-{n}.zip"
        n += 1
    return archive


def build_bundle_to_temp(
    settings: Settings, *, redaction: Mapping[str, str] | None = None, conservative: bool = False
) -> Path:
    """The same zip in a private temp directory for the web route to stream and then discard
    through `discard_bundle`; a failure during the build removes the directory itself."""
    staging = Path(tempfile.mkdtemp(prefix=_TEMP_PREFIX))
    try:
        return build_bundle(
            settings, out_dir=staging, redaction=redaction, conservative=conservative
        )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def discard_bundle(archive: str | Path) -> None:
    """Remove the archive and, only when its parent is a staging directory this module named,
    that directory; any other parent keeps everything but the file."""
    p = Path(archive)
    parent = p.parent
    if parent.name.startswith(_TEMP_PREFIX) and parent.parent == Path(tempfile.gettempdir()):
        shutil.rmtree(parent, ignore_errors=True)
        return
    p.unlink(missing_ok=True)
