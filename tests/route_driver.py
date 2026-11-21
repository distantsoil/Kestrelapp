"""Drive every route the app serves, from the route table (D-INST-14).

Shared by the install ID tests: the egress recorder, the row guard sweep and the another-device
sweep each request every route, in every method it declares, with fixture values for its path
parameters and a body built from its declared parameters. All values are invented."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass

from tests.test_contracts import _walk

# A fixture value for every path parameter any route declares. A route whose parameter is not
# here fails test_every_route_has_a_driver. The seeded rows below have id 1.
PATH_VALUES = {
    "opp_id": "1", "opportunity_id": "1", "org_id": "1", "source_id": "1", "render_id": "1",
    "master_id": "1", "pursuit_id": "1", "quarantine_id": "1", "cal_id": "1", "offer_id": "1",
    "story_id": "1", "mock_session_id": "1", "section_id": "1", "slot_id": "1", "cid": "1",
    "label_id": "1", "entry_id": "1", "suggestion_id": "1", "override_id": "1",
    "prep_pack_id": "1", "run_id": "1", "view_id": "1", "note_id": "1", "variant_id": "1",
    "calibration_id": "1", "announcement_id": "1", "task_id": "1",
    "track": "employment", "key": "ui.theme", "action": "keep", "kind": "resume",
    "name": "invented", "token": "invented-token", "tier": "tier1", "group": "invented",
    "decision": "reject", "section": "data", "verb": "keep", "pane": "data", "do": "later",
}

# A stylesheet seed() puts in the frame stylesheet store (#212, D-SEC-59), so /frame-css serves.
FRAME_CSS_TEXT = "/* invented frame sheet */"
FRAME_CSS_NAME = hashlib.sha256(FRAME_CSS_TEXT.encode("utf-8")).hexdigest() + ".css"

# Path values for routes whose parameter is one of a fixed set the handler checks.
ROUTE_VALUES: dict[str, dict[str, str]] = {
    "/opportunities/{opp_id}/tab/{name}": {"name": "posting"},
    "/setup/action/{name}": {"name": "check"},
    "/onboarding/models/action/{name}": {"name": "check"},
    "/settings/computer/action/{name}": {"name": "engine"},
    "/onboarding/edit/{key}": {"key": "tracks"},
    "/title-variants/{action}": {"action": "restore"},
    "/settings/calibration/{calibration_id}/{action}": {"action": "reject"},
    "/profile/{track}/{section}": {"section": "about"},
    "/documents/{master_id}/sections/{kind}": {"kind": "education"},
    "/documents/{master_id}/sections/{kind}/{entry_id}": {"kind": "education"},
    "/documents/{master_id}/sections/{kind}/{entry_id}/delete": {"kind": "education"},
    "/sources/add/{kind}": {"kind": "greenhouse"},
    "/frame-css/{name}": {"name": FRAME_CSS_NAME},
    # Its own failed row and role (seed() makes id 2), so the Needs another try retry, driven
    # first, has not already queued this role's Fit score (#206).
    "/runs/quarantine/{quarantine_id}/retry": {"quarantine_id": "2"},
}

# Words that mark a route that removes or ends what it names: driven last, so the rows the
# other routes need are still there.
_ENDS = ("delete", "remove", "clear", "reset", "undo", "end", "done", "discard", "forget",
         "restart", "dismiss", "hide", "skip")

# Routes that answer 404 or 400 even with the seeded rows, each with its reason (D-INST-18).
# BODY: the handler reads a field the driver does not build and refuses the generic input, so
# its refusal path ran. STATE: the seeded rows are not in the state the route acts on (no PDF,
# draft or photo on disk, an undo already spent, a source Kestrel ships). Every entry must still
# answer that status: one that starts serving fails test_every_unserved_route_is_still_refused,
# so the list cannot hide a route that now works.
BODY = "refuses generic input: its handler reads fields the driver does not build"
STATE = "the seeded rows are not in the state this route acts on"
# SPAWN: the route needs a
# process (the PDF renderer) that the egress recorder refuses.
SPAWN = "needs a process the egress recorder refuses (the PDF renderer)"
UNSERVED: dict[tuple[str, str], tuple[int, str, str]] = {
    ("GET", "/api/extension/roles/{opp_id}/documents/{kind}.txt"):
        (404, STATE, "no tailored document of that kind for the seeded role"),
    ("PUT", "/api/extension/features"): (400, BODY, "a JSON body of feature switches"),
    ("POST", "/api/extension/roles/{opp_id}/description"):
        (400, BODY, "a JSON body with the description text"),
    ("GET", "/auth"): (401, STATE, "the key prompt answers 401 by design"),
    ("GET", "/api/window/events"):
        (404, STATE, "only while the desktop shell has registered its window (#212)"),
    ("POST", "/api/window/call/{name}"):
        (404, STATE, "only while the desktop shell has registered its window (#212)"),
    ("POST", "/bench/check"): (409, STATE, "a check needs models this computer has not got"),
    ("POST", "/profile/corrections/{cid}/restore"): (409, STATE, "nothing removed to restore"),
    ("POST", "/runs/start"): (409, STATE, "a run cannot start in this state"),
    ("POST", "/profile/undo"): (409, STATE, "nothing to undo"),
    ("GET", "/renders/{render_id}/download"):
        (503, SPAWN, "the PDF needs a process the recorder refuses"),
    ("GET", "/analytics/export/summary.pdf"):
        (503, SPAWN, "the PDF needs a process the recorder refuses"),
    ("POST", "/api/learning/calibration/{cal_id}/accept"):
        (404, STATE, "{'accepted':false}"),
    ("POST", "/api/learning/calibration/{cal_id}/reject"):
        (404, STATE, "{'rejected':false}"),
    ("POST", "/api/learning/label"):
        (400, BODY, "{'error':'opportunity_id required'}"),
    ("POST", "/api/opportunities/ingest"):
        (400, BODY, "{'error':'invalid JSON body'}"),
    ("POST", "/api/opportunities/lookup"):
        (400, BODY, "{'error':'invalid JSON body'}"),
    ("POST", "/api/run/now"):
        (400, BODY, "{'queued':false,'reason':'unknown mode 'inve"),
    ("GET", "/backup/export-profile"):
        (404, STATE, "No profile.yml yet. Kestrel is running on th"),
    ("POST", "/calibrate/quick/answer"):
        (400, BODY, "{'error':'opportunity_id and answer are requ"),
    ("POST", "/calibrate/quick/undo/{label_id}"):
        (404, STATE, "{'undone':false}"),
    ("POST", "/calibrate/rate"):
        (400, BODY, "{'error':'opportunity_id and score are requi"),
    ("POST", "/calibrate/skip"):
        (400, BODY, "{'error':'opportunity_id required'}"),
    ("POST", "/calibrate/tune-undo/{cal_id}"):
        (404, STATE, "{'undone':false,'message':'This can no longe"),
    ("POST", "/calibrate/tune/{cal_id}/{decision}"):
        (404, STATE, "{'error':'no such proposal waiting'}"),
    ("POST", "/documents/slot/{slot_id}/delete"):
        (404, STATE, "not found"),
    ("POST", "/mock/session/{mock_session_id}/claim"):
        (400, BODY, "an empty answer"),
    ("POST", "/onboarding/edit/{key}"):
        (404, STATE, "Unknown step."),
    ("POST", "/onboarding/models/mode"):
        (400, BODY, "unknown run mode"),
    ("POST", "/onboarding/restart"):
        (400, BODY, "Choose keep or clear."),
    ("GET", "/opportunities/{opp_id}/role/cover-letter/{render_id}/download"):
        (404, STATE, "No such draft."),
    ("GET", "/opportunities/{opp_id}/role/cover-letter/{render_id}/fit"):
        (404, STATE, "an empty answer"),
    ("POST", "/opportunities/{opp_id}/role/cover-letter/{render_id}/redraft"):
        (404, STATE, "an empty answer"),
    ("POST", "/opportunities/{opp_id}/role/cover-letter/{render_id}/save"):
        (404, STATE, "an empty answer"),
    ("GET", "/opportunities/{opp_id}/role/similar"):
        (400, BODY, "an empty answer"),
    ("GET", "/prep/{prep_pack_id}/download"):
        (404, STATE, "PDF not found."),
    ("POST", "/profile/corrections/{cid}"):
        (400, BODY, "Pick Met, Partly met or Not met."),
    ("POST", "/profile/corrections/{cid}/remove"):
        (404, STATE, "That correction is already gone."),
    ("GET", "/profile/photo"):
        (404, STATE, "No photo."),
    ("GET", "/pursuits/closed"):
        (404, STATE, "not found"),
    ("POST", "/pursuits/{pursuit_id}/done"):
        (404, STATE, "not found"),
    ("GET", "/pursuits/{pursuit_id}/panel"):
        (404, STATE, "That card is no longer on the board."),
    ("POST", "/pursuits/{pursuit_id}/status"):
        (400, BODY, "'' is not a stage for employment cards; noth"),
    ("POST", "/renders/{render_id}/skills"):
        (400, BODY, "unknown slot key: 'skills:'"),
    ("POST", "/renders/{render_id}/swap"):
        (400, BODY, "slot_key and variant_key are required"),
    ("POST", "/settings/appearance"):
        (400, BODY, "Unknown scheme (empty); expected one of indi"),
    ("POST", "/settings/computer/mode"):
        (400, BODY, "Choose one of the three."),
    ("GET", "/settings/data/applied/copy/{token}"):
        (404, STATE, "That copy is no longer kept. Import the file"),
    ("POST", "/settings/roles/cutoffs"):
        (400, BODY, "Unknown cutoff."),
    ("POST", "/settings/roles/number"):
        (404, STATE, "That rule has no number."),
    ("POST", "/settings/roles/rule"):
        (404, STATE, "That rule is not one Kestrel has."),
    ("GET", "/settings/roles/see"):
        (404, STATE, "That rule is not one Kestrel has."),
    ("POST", "/settings/scoring/learn"):
        (404, STATE, "That switch is not one Kestrel has."),
    ("POST", "/settings/tracks"):
        (400, BODY, "the tracks form with its error"),
    ("POST", "/setup/mode"):
        (400, BODY, "unknown run mode"),
    ("POST", "/setup/profile"):
        (400, BODY, "Unknown hardware profile: ."),
    ("POST", "/sources"):
        (400, BODY, "the Sources panel with the field's error"),
    ("GET", "/sources/add/{kind}"):
        (404, STATE, "<div class='k-modal src-sheet' id='src-sheet"),
    ("POST", "/sources/company-board"):
        (400, BODY, "the Sources panel with the field's error"),
    ("POST", "/sources/pe-portfolio"):
        (400, BODY, "the Sources panel with the field's error"),
    ("POST", "/sources/rss"):
        (400, BODY, "the Sources panel with the field's error"),
    ("POST", "/sources/sam-gov/key"):
        (400, BODY, "the Sources panel with the field's error"),
    ("POST", "/sources/score-everything"):
        (400, BODY, "the Sources panel with the field's error"),
    ("POST", "/sources/{source_id}"):
        (400, BODY, "the Sources panel with the field's error"),
    ("POST", "/sources/{source_id}/cadence"):
        (400, BODY, "how often must be 1 to 720 hours"),
    ("POST", "/sources/{source_id}/default-boards"):
        (404, STATE, "source not found"),
    ("POST", "/sources/{source_id}/remove"):
        (400, BODY, "only a source you added can be removed; turn"),
    ("POST", "/today/quick/restore"):
        (400, BODY, "{'undone':false}"),
}

# Decisions a handler asks before it acts, watched like the endpoints (D-INST-32): name ->
# (the function, a reader of its own typed result that gives the refusal's `why`, None when it
# allows). Read from the function's return value, never from the page or the status.
def _row_retry_plan():
    from kestrel.queue import row_retry

    return row_retry.plan


def _row_retry_why(result) -> object:
    return result.why if result.task is None else None


def _enqueue_once():
    from kestrel.pipeline import tasks

    return tasks.enqueue_once


def _requeue_rescore_task():
    from kestrel.queue import rescore

    return rescore.requeue_rescore_task


ALREADY_QUEUED = "returned False: the role is already waiting for that task"


def _queued_why(result) -> object:
    return None if result is True else ALREADY_QUEUED


DECISIONS: dict[str, tuple] = {
    "row_retry.plan": (_row_retry_plan, _row_retry_why),
    "tasks.enqueue_once": (_enqueue_once, _queued_why),
    "rescore.requeue_rescore_task": (_requeue_rescore_task, _queued_why),
}

# Routes whose work is decided by a named decision (#206, D-UI-790: a retry runs exactly what
# row_retry.plan names from the stored row). Such a route is served only when its handler
# returned a served result AND the decision ran and allowed it AND no watched decision refused in
# that request (a queue that already holds the task), whatever the status: the Needs another try
# page answers a refusal in a 200 card, the Runs card in a 409 toast.
DECIDED_BY: dict[tuple[str, str], str] = {
    ("POST", "/quarantine/{quarantine_id}/retry"): "row_retry.plan",
    ("POST", "/runs/quarantine/{quarantine_id}/retry"): "row_retry.plan",
}


# A DECIDED_BY route listed in UNSERVED must also be here: the decision and a getter for the
# `why` it must have recorded in that request (a typed value from the decision's own module,
# compared by equality), so its refusal is the handler's recorded one, never just a status.
BECAUSE: dict[tuple[str, str], tuple[str, object]] = {}

# Routes another route matches first, so a request to them never reaches their own endpoint
# (D-INST-30). Each is driven like any route and is judged by whether its handler ran; the entry
# says why it still runs. test_every_route_is_judged_by_its_handler fails an entry whose own
# endpoint now runs, so the list cannot hide a route.
SHADOWED: dict[tuple[str, str], str] = {
    ("POST", "/settings/scoring/suggestion/{cal_id}/undo"):
        "matched first by /settings/scoring/suggestion/{cal_id}/{do}, which calls this handler",
}

# Routes not driven, each with the reason. Every entry must still be a route (no stale ones).
EXEMPT: dict[tuple[str, str], str] = {}

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def routes(app) -> list[tuple[str, str, object]]:
    out = []
    for path, r in _walk(app.routes):
        if path.startswith("/static"):
            continue
        for method in sorted(getattr(r, "methods", None) or {"<none>"}):
            if method == "HEAD":
                continue
            out.append((method, path, r))
    return out


def _sample(annotation) -> object:
    text_ = str(annotation)
    if annotation is bool or "bool" in text_:
        return "true"
    if annotation is int or "int" in text_:
        return "1"
    if annotation is float or "float" in text_:
        return "1.0"
    return "invented"


def request_parts(route) -> tuple[dict, dict, dict | None]:
    """Query, form and JSON values from what the endpoint declares."""
    from pydantic import BaseModel

    dep = getattr(route, "dependant", None)
    if dep is None:
        return {}, {}, None
    query = {f.alias: _sample(f.field_info.annotation) for f in dep.query_params}
    form: dict = {}
    body: dict | None = None
    for f in dep.body_params:
        ann = f.field_info.annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            body = {name: json.loads(json.dumps(_sample(fi.annotation)))
                    for name, fi in ann.model_fields.items()}
        else:
            form[f.alias] = _sample(ann)
    return query, form, body


def fill(path: str) -> str:
    own = ROUTE_VALUES.get(path, {})
    return re.sub(r"{([^}:]+)(:[^}]*)?}",
                  lambda m: own.get(m.group(1), PATH_VALUES[m.group(1)]), path)


def _order(entry: tuple[str, str, object]) -> tuple[int, str, str]:
    method, path, _r = entry
    if method != "GET" and (method, path) in DECIDED_BY:
        # First after the GETs, on the seeded rows: later writes (score again, try all again)
        # queue the same roles' tasks, and a queue that already holds a task refuses (#206).
        return (1, path, method)
    ends = any(w in part for part in path.split("/") for w in _ENDS)
    return (0 if method == "GET" else 3 if ends else 2, path, method)


# Values for columns whose meaning a handler checks (a track, a stage, a kind). Everything else a
# row needs is filled from its column type.
COLUMN_VALUES = {
    "track": "employment", "stage": "t2_done", "status": "active", "opp_type": "job_posting",
    "kind": "resume", "verdict": "apply", "tier": 2, "currency": "USD", "interval": "year",
    "url": "https://jobs.example/1", "title": "Invented Role", "name": "Invented",
    "org_name": "Invented Org", "normalized_name": "invented org", "size_band": "mid",
    "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
    "first_seen_at": "2026-01-01T00:00:00+00:00", "started_at": "2026-01-01T00:00:00+00:00",
}
# JSON columns that hold a mapping; every other JSON column gets an empty list.
JSON_VALUES = {
    "dimensions": "{}", "blocks": "{}", "config": "{}", "payload": "{}", "meta": "{}",
    "legitimacy": '{"status": "clean", "signals": []}', "content": "{}", "data": "{}",
}
# Tables never seeded: the queue, settings, the install ID and Alembic's own.
NOT_SEEDED = frozenset({"task", "setting", "install_identity", "alembic_version"})


def _sample_column(column) -> object:
    from sqlalchemy import JSON, Boolean, Float, Integer, LargeBinary

    if column.name in COLUMN_VALUES:
        return COLUMN_VALUES[column.name]
    if column.foreign_keys:
        return 1
    kind = column.type
    if isinstance(kind, Boolean):
        return 0
    if isinstance(kind, Integer):
        return 1
    if isinstance(kind, Float):
        return 1.0
    if column.name.endswith("_at"):
        return "2026-01-01T00:00:00+00:00"
    if isinstance(kind, JSON):
        return JSON_VALUES.get(column.name, "[]")
    if isinstance(kind, LargeBinary):
        return b""
    return "invented"


SEED_FAILURES: dict[str, str] = {}


def _seed_core() -> None:
    """The organization, source and opportunity every role page starts from, through the ORM."""
    import asyncio

    from kestrel.db.base import utcnow_iso
    from kestrel.db.engine import get_sessionmaker
    from kestrel.db.models import Opportunity, Org, Source

    async def go():
        async with get_sessionmaker()() as s:
            org = Org(name="Invented Org", normalized_name="invented org", size_band="mid")
            src = Source(track="employment", kind="greenhouse", name="board", enabled=True)
            s.add_all([org, src])
            await s.flush()
            s.add(Opportunity(track="employment", opp_type="job_posting", org_id=org.id,
                              source_id=src.id, title="Invented Role", org_name="Invented Org",
                              url="https://jobs.example/1", stage="t2_done",
                              first_seen_at=utcnow_iso()))
            await s.commit()

    asyncio.run(go())


def _column_value(column) -> object:
    """The column's own Python default when it has one (a raw INSERT does not apply it), else a
    value from COLUMN_VALUES or its type."""
    import json as json_

    from sqlalchemy import JSON

    if column.name not in COLUMN_VALUES and not column.foreign_keys and column.default is not None:
        try:
            value = column.default.arg(None) if column.default.is_callable else column.default.arg
        except TypeError:
            value = None
        if value is not None:
            if isinstance(column.type, JSON) or isinstance(value, (list, dict)):
                return json_.dumps(value)
            return int(value) if isinstance(value, bool) else value
    return _sample_column(column)


def seed() -> None:
    """A row with id 1 (or key 1) in every table the app reads by path, so a route's handler
    finds what its path names: required columns from COLUMN_VALUES or their type, foreign keys
    pointing at the other id-1 rows. Written with plain sqlite3 and foreign keys off, so the
    order does not matter. Also the frame stylesheet /frame-css serves. Invented values only."""
    import sqlite3

    from kestrel import paths
    from kestrel.db.models import Base
    from kestrel.web import csp

    _seed_core()
    assert csp.frame_css_url(FRAME_CSS_TEXT) == "/frame-css/" + FRAME_CSS_NAME
    con = sqlite3.connect(str(paths.db_path()))
    try:
        con.execute("PRAGMA foreign_keys = OFF")
        for table in Base.metadata.sorted_tables:
            if table.name in NOT_SEEDED:
                continue
            values = {}
            for col in table.columns:
                # Every column, nullable ones too: a handler reads a NULL list or score the same
                # way it reads a filled one, and a NULL there crashed it (round 8).
                if col.server_default is None or col.primary_key or col.foreign_keys:
                    values[col.name] = 1 if col.primary_key else _column_value(col)
            names = ", ".join(f'"{n}"' for n in values)
            marks = ", ".join("?" for _ in values)
            if con.execute(f'SELECT 1 FROM "{table.name}" LIMIT 1').fetchone():  # noqa: S608 - test: names from the schema
                continue
            try:
                con.execute(f'INSERT INTO "{table.name}" ({names}) VALUES ({marks})',  # noqa: S608 - test: names from the schema
                            list(values.values()))
            except sqlite3.IntegrityError as exc:
                SEED_FAILURES[table.name] = str(exc)
        # A second role and failed row (id 2), copies of the first, for the Runs card's retry.
        for table, extra in (("opportunity", ""), ("quarantine", ", opportunity_id = 2")):
            con.execute(f'CREATE TEMP TABLE k204_copy AS SELECT * FROM "{table}" WHERE id = 1')  # noqa: S608 - test: fixed names
            con.execute(f"UPDATE k204_copy SET id = 2{extra}")  # noqa: S608 - test: fixed names
            con.execute(f'INSERT OR IGNORE INTO "{table}" SELECT * FROM k204_copy')  # noqa: S608 - test: fixed names
            con.execute("DROP TABLE k204_copy")
        con.commit()
    finally:
        con.close()


# The browser extension's API answers only a request carrying the shared key (header only).
EXTENSION_PREFIX = "/api/extension"
EXTENSION_KEY = "k204-extension-invented"


def _request(client, method: str, path: str, kw: dict):
    """One request. An extension API route gets the shared key in force (or, when the app has
    none, an invented one for this request only, put back after), so its handler runs instead
    of the key check refusing it."""
    if not path.startswith(EXTENSION_PREFIX) or "X-Kestrel-Key" in kw.get("headers", {}):
        return client.request(method, fill(path), **kw)
    from kestrel.web import auth

    before = auth.cached_key()
    if before is None:
        auth.set_cached_key(EXTENSION_KEY)
    try:
        key = auth.cached_key()
        headers = {**kw.get("headers", {}), "X-Kestrel-Key": key}
        return client.request(method, fill(path), **{**kw, "headers": headers})
    finally:
        if before is None:
            auth.reset_cache()


@dataclass(frozen=True)
class Driven:
    """One driven request: the route, the response, and whether the route's own handler did its
    work (D-INST-30), as its recorder saw it."""

    method: str
    path: str
    response: object
    served: bool
    # (decision, why) for each named decision (DECISIONS) that refused during the request.
    refusals: tuple = ()


class _Recorder:
    """Watches each driven route's own endpoint (route.endpoint, the function FastAPI calls) for
    one sweep, through sys.monitoring on its code object: a runtime record of the handler's own
    return value, independent of how FastAPI wires the route. A raise, a refusal before the
    endpoint ran (a dependency, auth or validation) or a request that never reached it leaves
    no record, so the route is refused (AGENT-RULES 8a.2: served is one positive record)."""

    NAME = "kestrel-route-driver"

    def __init__(self) -> None:
        # Keyed by id(code): code objects compare by value, so two endpoints with the same body
        # would otherwise share one entry (and only the first would be watched).
        self.ran: dict[int, object] = {}
        self._tool: int | None = None
        self._codes: dict[int, object] = {}
        # Named decisions (DECISIONS) watched the same way: id(code) -> name, and what each
        # returned during the current request, in order.
        self._decisions: dict[int, str] = {}
        self.decided: list[tuple[str, object]] = []

    def install(self, entries) -> None:
        mon = sys.monitoring
        free = [i for i in (4, 3, 5, 2, 0) if mon.get_tool(i) is None]
        if not free:
            raise AssertionError("no free sys.monitoring tool id for the route driver")
        self._tool = free[0]
        mon.use_tool_id(self._tool, self.NAME)
        mon.register_callback(self._tool, mon.events.PY_RETURN, self._on_return)
        codes = [endpoint_code(route) for _m, _p, route in entries]
        for name, (fn, _why) in DECISIONS.items():
            code = fn().__code__
            self._decisions[id(code)] = name
            codes.append(code)
        for code in codes:
            if id(code) not in self._codes:
                self._codes[id(code)] = code
                mon.set_local_events(self._tool, code, mon.events.PY_RETURN)

    def _on_return(self, code, _offset, retval) -> None:
        self.ran[id(code)] = retval
        name = self._decisions.get(id(code))
        if name is not None:
            self.decided.append((name, retval))

    def clear(self) -> None:
        self.ran.clear()
        self.decided.clear()

    def refusals(self) -> tuple:
        """(decision, why) for each named decision that refused in this request."""
        out = []
        for name, retval in self.decided:
            why = DECISIONS[name][1](retval)
            if why is not None:
                out.append((name, why))
        return tuple(out)

    def decision_allowed(self, name: str) -> bool:
        """The named decision ran in this request, and no watched decision refused in it."""
        return any(n == name for n, _r in self.decided) and not self.refusals()

    def verdict(self, route) -> bool:
        key = id(endpoint_code(route))
        return key in self.ran and handler_served(
            self.ran[key], getattr(route, "status_code", None))

    def uninstall(self) -> None:
        if self._tool is None:
            return
        mon = sys.monitoring
        for code in self._codes.values():
            mon.set_local_events(self._tool, code, 0)
        mon.register_callback(self._tool, mon.events.PY_RETURN, None)
        mon.free_tool_id(self._tool)
        self._tool, self._codes = None, {}
        self.ran.clear()
        self.decided.clear()
        self._decisions.clear()


def endpoint_code(route) -> object:
    """The code object of the function FastAPI calls for this route."""
    code = getattr(getattr(route, "endpoint", None), "__code__", None)
    if code is None:
        raise AssertionError(f"no recorder for an endpoint without code: {route!r}")
    return code


def handler_served(result: object, declared: int | None = None) -> bool:
    """The handler's own result means it did its work (D-INST-30): a Response with a served
    status of its own, or any other value under the route's declared status (2xx when none)."""
    from starlette.responses import Response

    if isinstance(result, Response):
        return served(result.status_code)
    return declared is None or served(declared)


def drive(client, app, headers: dict | None = None) -> list[Driven]:
    """Request every route that is not exempt, without following redirects; returns one Driven
    for each, its verdict read from the route's own handler. The status can only take served
    away (a crash after the handler returned), never grant it."""
    entries = [e for e in sorted(routes(app), key=_order) if e[:2] not in EXEMPT]
    rec = _Recorder()
    out = []
    try:
        rec.install(entries)
        for method, path, route in entries:
            query, form, body = request_parts(route)
            kw: dict = {"params": query, "headers": headers or {}, "follow_redirects": False}
            if method != "GET":
                kw.update({"json": body} if body is not None else {"data": form})
            tries = FLAKY_TRIES if (method, path) in FLAKY else 1
            for _ in range(tries):
                rec.clear()
                resp = _request(client, method, path, kw)
                decided = DECIDED_BY.get((method, path))
                ok = (rec.verdict(route) and resp.status_code < 500
                      and (decided is None or rec.decision_allowed(decided)))
                if ok:
                    break
            out.append(Driven(method, path, resp, ok, rec.refusals()))
    finally:
        rec.uninstall()
    return out


# Routes whose answer varied between runs (round 7): each is requested up to FLAKY_TRIES times
# until one answer is served, and must be served at least once; one that is refused every time is
# unexplained like any other route (round 8). An entry naming a route that is gone fails.
FLAKY_TRIES = 3
FLAKY: dict[tuple[str, str], str] = {
    ("POST", "/api/profile/score-again"): "an intermittent 500 seen in round 7, outside #204",
    ("POST", "/about/diagnostics/save"): "an intermittent 500 seen in round 7, outside #204",
}


def unexplained(driven: list[Driven]) -> list[tuple[str, str, int]]:
    """Driven routes whose handler did not report served (see _Recorder) and that are not in
    UNSERVED with the status they answered, or, for a route in BECAUSE, whose named decision
    did not record the refusal the entry names."""
    return [(d.method, d.path, d.response.status_code) for d in driven if not d.served
            and (UNSERVED.get((d.method, d.path), (None,))[0] != d.response.status_code
                 or not _because_seen(d))]


def _because_seen(d: Driven) -> bool:
    expected = BECAUSE.get((d.method, d.path))
    if expected is None:
        return True
    name, why = expected
    return (name, why()) in d.refusals


# The redirects a handler sends after doing its work (a form saved, then "see the page"). Any
# other code outside 2xx, named or not, is refused (round 6 merge review: an allowlist of served
# codes, never a list of bad ones).
SERVED_REDIRECTS = frozenset({303})


def served(code: int) -> bool:
    """A status a handler returned itself that means it did its work: any 2xx, or a redirect in
    SERVED_REDIRECTS. Only handler_served reads it; the client's status never grants served."""
    return 200 <= code < 300 or code in SERVED_REDIRECTS


def stale_unserved(driven: list[Driven]) -> list:
    """UNSERVED entries the run did not see refused with their status: a route whose handler now
    serves, or one that no longer exists (it was not driven at all)."""
    seen = {(d.method, d.path): (None if d.served else d.response.status_code) for d in driven}
    gone = sorted(k for k in FLAKY if k not in seen)
    return gone + sorted(k if k not in seen else (*k, seen[k]) for k, (status, _w, _d) in
                         UNSERVED.items() if seen.get(k) != status)
