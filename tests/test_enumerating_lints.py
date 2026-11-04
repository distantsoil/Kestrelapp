"""D-TEST-8: enumerating lints. Each one scans the repo for a class of defect found in the
ompepic review and fails with a plain message naming the rule, the file and the fix. A new
offender fails here the day it is added; an allowlist entry says why it is fine and goes stale
(and fails) when the code it excuses is gone.

Lints: (a) orphan partials, (b) dead controls, (c) placeholder data, (d) registry completeness,
(e) model calls only from allowed layers, (f) browser storage inside try, (g) `|safe`.
The last section proves each lint fails on a deliberately broken input.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
KESTREL = ROOT / "kestrel"
TEMPLATES = KESTREL / "web" / "templates"
JS = KESTREL / "web" / "static" / "js"
VENDORED = {"htmx.min.js", "alpine.min.js"}


def fail(rule: str, where: str, fix: str) -> str:
    return f"RULE: {rule}. WHERE: {where}. FIX: {fix}."


def py_files() -> list[Path]:
    return [p for p in sorted(KESTREL.rglob("*.py")) if "__pycache__" not in p.parts]


def rel(p: Path) -> str:
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return p.name


def stale(allow: dict, found: set, what: str) -> list[str]:
    return [
        fail(f"the {what} allowlist names only live entries", str(k), "delete the entry")
        for k in sorted(set(allow) - found, key=str)
    ]


# ============================================================ (a) orphan partials
ORPHAN_ALLOW: dict[str, str] = {}

ORPHAN_BUGS: dict[str, str] = {}


def orphan_partials(templates: Path, code_files: list[Path], allow: dict) -> list[str]:
    texts = [p.read_text(encoding="utf-8") for p in sorted(templates.rglob("*.html"))]
    texts += [p.read_text(encoding="utf-8") for p in code_files]
    blob = "\n".join(texts)
    # f-strings that build a name: partials/prefix{...}; any partial under the prefix is reached
    prefixes = re.findall(r"partials/([A-Za-z0-9_/]*)(?:\{|%|<|\"?\s*~)", blob)
    out = []
    for p in sorted((templates / "partials").rglob("*.html")):
        name = p.relative_to(templates).as_posix()
        if name in allow:
            continue
        short = name[len("partials/") :]
        own = p.read_text(encoding="utf-8")
        # The full name, or the file's own name (views call render("models_change.html")).
        base = name.rsplit("/", 1)[1]
        stem = base.removesuffix(".html")
        quoted = len(re.findall(rf"[\"']{re.escape(stem)}[\"']", blob)) - len(
            re.findall(rf"[\"']{re.escape(stem)}[\"']", own)
        )
        refs = blob.count(name) + blob.count(base) - own.count(name) - own.count(base) + quoted
        by_prefix = any(short.startswith(x) for x in prefixes if x)
        if refs <= 0 and not by_prefix:
            out.append(
                fail(
                    "every partial is included, imported or rendered by something",
                    name,
                    "delete the file, or render it; if a dynamic name reaches it, add it to "
                    "ORPHAN_ALLOW with the reason",
                )
            )
    return out


def test_no_orphan_partials():
    problems = orphan_partials(TEMPLATES, py_files(), {**ORPHAN_ALLOW, **ORPHAN_BUGS})
    names = {p.relative_to(TEMPLATES).as_posix() for p in (TEMPLATES / "partials").rglob("*.html")}
    problems += stale(ORPHAN_ALLOW, names, "orphan-partial")
    assert not problems, "\n".join(problems)


# ============================================================ (b) dead controls
# Attributes that make a control do something without a data-* name a script selects.
LIVE_ATTR = re.compile(r"^(hx-|x-|@|:?on|popovertarget|command$|commandfor$|formaction$)")
DEAD_ALLOW: dict[str, str] = {}


def script_texts(js_dir: Path, templates: Path | None = None) -> list[str]:
    """Every script a control can be wired by: static/js files and inline template scripts."""
    out = [p.read_text(encoding="utf-8") for p in js_dir.rglob("*.js") if p.name not in VENDORED]
    for p in sorted(templates.rglob("*.html")) if templates else []:
        out += re.findall(r"<script\b[^>]*>(.*?)</script>", p.read_text(encoding="utf-8"), re.S)
    return out


def js_selected_data_names(js_dir: Path, templates: Path | None = None) -> set[str]:
    names: set[str] = set()
    for text in script_texts(js_dir, templates):
        names.update(re.findall(r"data-[a-z0-9]+(?:-[a-z0-9]+)*", text))
        for camel in re.findall(
            r"dataset\.([A-Za-z0-9_]+)|dataset\[['\"]([A-Za-z0-9_]+)['\"]\]", text
        ):
            c = camel[0] or camel[1]
            names.add("data-" + re.sub(r"([A-Z])", lambda m: "-" + m.group(1).lower(), c))
    return names


def has_handler(tag, selected: set[str], ids: set[str] = frozenset()) -> bool:
    """Wired by hx-*, Alpine, a JS-selected data-* name or id, or (delegation) by such an
    attribute on one of its three nearest ancestors, e.g. a segmented control."""
    chain = [tag, *list(tag.parents)[:3]]
    for node in chain:
        for attr, val in getattr(node, "attrs", {}).items():
            a = attr.lower()
            if LIVE_ATTR.match(a):
                return True
            if a.startswith("data-") and a in selected and a != "data-testid":
                return True
            if a == "id" and node is tag and str(val) in ids:
                return True
    return False


def dead_controls(
    templates: Path, selected: set[str], allow: dict, ids: set[str] = frozenset()
) -> list[str]:
    """`<a href="#">` and a `type="button"` button inside a form must be wired. (A button with
    no type inside a form submits it, which is its job.)"""
    out = []
    for p in sorted(templates.rglob("*.html")):
        soup = BeautifulSoup(p.read_text(encoding="utf-8"), "html.parser")
        name = p.relative_to(templates).as_posix()
        for a in soup.find_all("a", href="#"):
            key = f'{name} <a href="#"> {a.get_text(" ", strip=True)[:30]}'
            if not has_handler(a, selected, ids) and key not in allow:
                out.append(key)
        for b in soup.find_all("button"):
            kind = (b.get("type") or "").lower()
            if kind == "submit" or not kind or not b.find_parent("form"):
                continue
            key = f"{name} <button type={kind}> {b.get_text(' ', strip=True)[:30]}"
            if not has_handler(b, selected, ids) and key not in allow:
                out.append(key)
    return [
        fail(
            "a link to # or a non-submit button in a form does something",
            k,
            "add hx-* or a data-* attribute that a script under static/js selects, or remove "
            "the control; if it is wired some other way, add it to DEAD_ALLOW with the reason",
        )
        for k in out
    ]


def test_no_dead_controls():
    blob = "\n".join(script_texts(JS, TEMPLATES))
    ids = {m for m in re.findall(r"[#'\"]([A-Za-z][\w-]{2,})", blob)}
    problems = dead_controls(TEMPLATES, js_selected_data_names(JS, TEMPLATES), DEAD_ALLOW, ids)
    assert not problems, "\n".join(problems)


# ============================================================ (c) placeholder data
DATA_SUFFIX = {".yml", ".yaml", ".json", ".jsonl", ".csv", ".toml", ".txt", ".md", ".webmanifest"}
PLACEHOLDER_WORDS = re.compile(
    r"TODO|FIXME|lorem|placeholder|\(fake\)|example-digest|\bxxx+\b", re.IGNORECASE
)
HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
PLACEHOLDER_ALLOW: dict[str, str] = {
    "kestrel/resources/config/profile.example.yml: word 'placeholder'": "its comment says the "
    "Profile page shows the file's values as grey placeholder hints; that is the input "
    "placeholder feature, not an unfinished value",
    "kestrel/resources/evals/goldset.consulting.jsonl: word 'placeholder'": "the word appears in "
    "an invented posting's own text",
    "kestrel/resources/evals/goldset.employment.jsonl: word 'placeholder'": "the word appears in "
    "an invented posting's own text",
}

PLACEHOLDER_BUGS: dict[str, str] = {}


def repeating(s: str) -> bool:
    s = s.lower()
    if len(set(s)) <= 2:
        return True
    return any(len(s) % k == 0 and s == s[:k] * (len(s) // k) for k in range(1, 9))


def placeholder_data(roots: list[Path], allow: dict) -> list[str]:
    out = []
    for root in roots:
        for p in sorted(root.rglob("*")):
            if (
                not p.is_file()
                or p.suffix.lower() not in DATA_SUFFIX
                or "__pycache__" in p.parts
                or p.name in VENDORED
            ):
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            hits = [
                f"digest {m.group(0)[:16]}..." for m in HEX.finditer(text) if repeating(m.group(0))
            ]
            hits += [f"word {m.group(0)!r}" for m in PLACEHOLDER_WORDS.finditer(text)]
            for h in sorted(set(hits)):
                key = f"{rel(p)}: {h}"
                if key not in allow:
                    out.append(key)
    return [
        fail(
            "shipped data holds no placeholder digest or marker text",
            k,
            "replace it with the real value (for a digest, run the model's pull and record the "
            "real sha256), or add it to PLACEHOLDER_ALLOW with the reason",
        )
        for k in out
    ]


def test_no_placeholder_data_ships():
    roots = [KESTREL / "resources", KESTREL / "web" / "static"]
    problems = placeholder_data(roots, {**PLACEHOLDER_ALLOW, **PLACEHOLDER_BUGS})
    present = {k for k in PLACEHOLDER_ALLOW if _present(k)}
    problems += stale(PLACEHOLDER_ALLOW, present, "placeholder")
    assert not problems, "\n".join(problems)


def _present(key: str) -> bool:
    path = key.partition(": ")[0]
    text = (ROOT / path).read_text(encoding="utf-8", errors="replace")
    return bool(PLACEHOLDER_WORDS.search(text))


# ============================================================ (d) registries
def test_every_adapter_is_in_the_sources_ui_and_the_outbound_doc():
    from kestrel.ingest.adapters import known_kinds

    ui = "\n".join(
        (KESTREL / "web" / f).read_text(encoding="utf-8")
        for f in ("source_view.py", "pages/sources_ui.py", "pages/sources.py")
    )
    doc = (ROOT / "docs" / "OUTBOUND-CALLS.md").read_text(encoding="utf-8").lower()
    problems = []
    for kind in known_kinds():
        if f'"{kind}"' not in ui and f"'{kind}'" not in ui:
            problems.append(
                fail(
                    "every registered adapter has a place in the Sources UI",
                    f"adapter {kind!r} (kestrel/ingest/adapters/__init__.py)",
                    "add it to the catalog in kestrel/web/source_view.py (a feature reachable "
                    "only from the CLI is a bug, D-UI-129)",
                )
            )
        if kind.lower() not in doc and kind.replace("_", " ") not in doc:
            problems.append(
                fail(
                    "every registered adapter is listed in docs/OUTBOUND-CALLS.md",
                    f"adapter {kind!r}",
                    "add a row naming the host it calls and what is sent",
                )
            )
    assert not problems, "\n".join(problems)


def test_every_prompt_version_file_has_an_entry_in_prompts_md():
    import yaml

    doc = (ROOT / "PROMPTS.md").read_text(encoding="utf-8")
    ids_in_table = set(re.findall(r"^\| (P\w+) \|", doc, re.M))
    problems = []
    for f in sorted((KESTREL / "llm" / "prompts").glob("*.v*.md")):
        head = f.read_text(encoding="utf-8").split("---")[1]
        meta = yaml.safe_load(head) or {}
        pid, version = str(meta.get("id", "")), f.stem.replace(".", "/")
        if pid not in ids_in_table and pid.rstrip("abcdefgh") not in ids_in_table:
            problems.append(
                fail(
                    "every prompt has a row in the PROMPTS.md table",
                    f"{f.name} (id {pid!r})",
                    "add the row, with its track and tier",
                )
            )
        if version not in doc and f.stem not in doc:
            # Older versions are summarized by the version history; the current one must be named.
            latest = max(
                (g.stem for g in f.parent.glob(f"{f.stem.split('.')[0]}.v*.md")),
                key=lambda s: int(s.rsplit(".v", 1)[1]),
            )
            if f.stem == latest:
                problems.append(
                    fail(
                        "the current version of every prompt is named in PROMPTS.md",
                        f"{version} ({f.name})",
                        f"write {version} in the prompt's section or the version history",
                    )
                )
    assert not problems, "\n".join(problems)


def _module_consts(tree: ast.Module) -> dict[str, list[str]]:
    """Module-level names -> the strings they hold, following names to earlier constants."""
    out: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            node = ast.Assign(targets=[node.target], value=node.value)
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Name):
                vals: list[str] = []
                for c in ast.walk(node.value):
                    if isinstance(c, ast.Constant) and isinstance(c.value, str):
                        vals.append(c.value)
                    elif isinstance(c, ast.Name) and c.id in out:
                        vals.extend(out[c.id])
                if vals:
                    out[t.id] = vals
    return out


KIND_NAMES = re.compile(r"^(TASK_TYPE|PULL_TYPE|CREATE_TYPE|TYPES)$")
CORE_KINDS_RUN_BY_THE_WORKER = {"scrape", "embed", "t1", "t2", "t3", "rescore", "t0_recheck"}


def task_kinds(files: list[Path]) -> tuple[dict[str, str], set[str]]:
    """(kind -> file that makes it, kinds some lease(types=...) call asks for)."""
    made: dict[str, str] = {}
    leased: set[str] = set()
    for p in files:
        tree = ast.parse(p.read_text(encoding="utf-8"))
        consts = _module_consts(tree)
        leases = ".lease(" in p.read_text(encoding="utf-8")
        for name, vals in consts.items():
            if (
                KIND_NAMES.search(name)
                and (leases or not name.endswith("TYPES"))
                and re.fullmatch(r"[a-z0-9_]+", vals[0] or "")
            ):
                for v in vals:
                    made.setdefault(v, rel(p))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            if fn in ("enqueue", "enqueue_once"):
                for a in node.args[:2]:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        made.setdefault(a.value, rel(p))
            if fn == "Task":
                for k in node.keywords:
                    if k.arg == "type" and isinstance(k.value, ast.Constant):
                        made.setdefault(k.value.value, rel(p))
            if fn == "lease":
                for k in node.keywords:
                    if k.arg == "types":
                        for c in ast.walk(k.value):
                            if isinstance(c, ast.Constant) and isinstance(c.value, str):
                                leased.add(c.value)
                            if isinstance(c, ast.Name) and c.id in consts:
                                leased.update(consts[c.id])
    return made, leased


def unhandled_task_kinds(files: list[Path], worker_text: str) -> list[str]:
    made, leased = task_kinds(files)
    out = []
    for kind, where in sorted(made.items()):
        if kind in leased:
            continue
        if kind in CORE_KINDS_RUN_BY_THE_WORKER and f'"{kind}"' in worker_text:
            continue
        out.append(
            fail(
                "every task kind the queue is given has a drain that leases it",
                f"kind {kind!r} made in {where}",
                "add a drain that calls queue.lease(types=[...]) for it and start it from the "
                "worker; a task nobody leases is work that silently never happens (hard rule 5)",
            )
        )
    return out


def test_every_task_kind_has_a_drain():
    worker = (KESTREL / "pipeline" / "worker.py").read_text(encoding="utf-8")
    problems = unhandled_task_kinds(py_files(), worker)
    made, _ = task_kinds(py_files())
    assert len(made) >= 10, f"the scan found too few task kinds ({sorted(made)}); fix the scanner"
    assert not problems, "\n".join(problems)


# A setting is a switch when it is a bool on a model nested in Settings. It must be read by code
# that is not settings code, or turning it does nothing.
SETTINGS_CODE = re.compile(
    r"(kestrel/config\.py|kestrel/ui_settings\.py|kestrel/services/setting_overrides\.py"
    r"|kestrel/web/settings_\w+\.py|kestrel/web/pages/settings\w*\.py)$"
)
SWITCH_ALLOW: dict[str, str] = {}


def switch_keys() -> dict[str, tuple[str, str]]:
    from pydantic import BaseModel

    from kestrel.config import Settings

    out = {}
    for sect, field in Settings.model_fields.items():
        model = field.annotation
        if isinstance(model, type) and issubclass(model, BaseModel):
            for name, f in model.model_fields.items():
                if f.annotation is bool:
                    out[f"{sect}.{name}"] = (sect, name)
        elif field.annotation is bool:
            out[sect] = ("", sect)
    return out


def unread_switches(files: list[Path], keys: dict, allow: dict) -> list[str]:
    reads: set[str] = set()
    for p in files:
        if SETTINGS_CODE.search(rel(p)):
            continue
        text = p.read_text(encoding="utf-8")
        for key, (_sect, name) in keys.items():
            if key in reads:
                continue
            if (
                re.search(rf"\.{re.escape(name)}\b", text)
                or f'"{key}"' in text
                or f"'{key}'" in text
            ):
                reads.add(key)
    return [
        fail(
            "every Settings switch is read by code outside settings",
            f"setting {k}",
            "make the feature honor it, or delete the switch; a switch nobody reads is a lie "
            "in the UI. If it is read in a way this scan cannot see, add it to SWITCH_ALLOW",
        )
        for k in sorted(keys)
        if k not in reads and k not in allow
    ]


def test_every_settings_switch_is_read():
    problems = unread_switches(py_files(), switch_keys(), SWITCH_ALLOW)
    problems += stale(SWITCH_ALLOW, set(switch_keys()), "settings switch")
    assert not problems, "\n".join(problems)


# ============================================================ (e) model calls only from layers
# Files under kestrel/web that build a model client today. Each runs a call the person asked
# for by pressing a button, behind the ask-first check; nothing else in web may. New work goes
# through a worker task (hard rule 5) instead of a new entry here.
LLM_ALLOW: dict[str, str] = {
    "kestrel/web/app.py": "builds the one client the worker, scheduler and drains share, started "
    "from the FastAPI lifespan on uvicorn's loop (CLAUDE.md platform traps)",
    "kestrel/web/pages/analytics.py": "the honest-summary narrative the person asks for; "
    "online use goes through the ask-first panel",
    "kestrel/web/pages/bench.py": "the model benchmark the person starts; local models only "
    "unless Include online models is confirmed",
    "kestrel/web/pages/documents.py": "resume import, assessment, and variant suggestions, each "
    "started by a button on the Documents page",
    "kestrel/web/pages/escalate.py": "the per-role Tier 3 send, behind its confirmed ask-first "
    "dialog (hard rule: opt-in per opportunity)",
    "kestrel/web/pages/mock.py": "critiques the answer the person just submitted in a mock "
    "interview",
    "kestrel/web/pages/offers.py": "the negotiation scripts the person asks for on an offer",
    "kestrel/web/pages/renders.py": "Pick again (P3 document selection) pressed on a render",
    "kestrel/web/pages/role.py": "the role page's research, story and cover letter actions, "
    "each behind the ask-first check when online",
    "kestrel/web/pages/sources.py": "runs a signal source (SEC trigger classification) when "
    "the person presses Run now on it",
}
CLIENT_NAMES = {"LLMClient", "OllamaClient"}


def llm_callers(files: list[Path]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for p in files:
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name in CLIENT_NAMES:
                    out.setdefault(rel(p), []).append(node.lineno)
    return out


def test_model_clients_are_built_only_in_allowed_web_files():
    web = [p for p in py_files() if "web" in p.relative_to(KESTREL).parts[:1]]
    found = llm_callers(web)
    problems = [
        fail(
            "web code does not call a model directly",
            f"{f}:{lines[0]}",
            "queue a task and let the worker run it (kestrel/pipeline or a drain), or, for a "
            "button the person pressed after the ask-first check, add the file to LLM_ALLOW "
            "with the reason",
        )
        for f, lines in sorted(found.items())
        if f not in LLM_ALLOW
    ]
    problems += stale(LLM_ALLOW, set(found), "model-call")
    assert not problems, "\n".join(problems)


# ============================================================ (f) browser storage
STORAGE = re.compile(r"\b(?:local|session)Storage\b")
STORAGE_ALLOW: dict[str, str] = {}


def _blank(m: re.Match) -> str:
    return re.sub(r"[^\n]", " ", m.group(0))


def code_only(name: str, text: str) -> str:
    """The script code of a file with comments blanked (line numbers kept). For a template only
    the <script> bodies count; prose, Jinja comments and HTML comments are blanked."""
    if name.endswith(".html"):
        keep = [
            (m.start(1), m.end(1))
            for m in re.finditer(r"<script\b[^>]*>(.*?)</script>", text, re.S)
        ]
        masked = list(re.sub(r"[^\n]", " ", text))
        for a, b in keep:
            masked[a:b] = text[a:b]
        text = "".join(masked)
        text = re.sub(r"\{#.*?#\}", _blank, text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", _blank, text, flags=re.S)
    return re.sub(r"(?m)(?<![:\w'\"])//[^\n]*", _blank, text)


def inside_try(text: str, pos: int) -> bool:
    """True when some block that encloses `pos` was opened by `try`. Counts braces backwards."""
    depth = 0
    for i in range(pos - 1, -1, -1):
        ch = text[i]
        if ch == "}":
            depth += 1
        elif ch == "{":
            if depth:
                depth -= 1
            elif re.search(r"\btry\s*$", text[max(0, i - 12) : i]):
                return True
    return False


def unguarded_storage(name: str, text: str, allow: dict) -> list[str]:
    code = code_only(name, text)
    out = []
    for m in STORAGE.finditer(code):
        key = f"{name}:{code.count(chr(10), 0, m.start()) + 1}"
        if not inside_try(code, m.start()) and key not in allow:
            out.append(
                fail(
                    "browser storage is read and written inside try (it throws in a private "
                    "window or when blocked)",
                    key,
                    "wrap the access in try { ... } catch (e) { ... } and render without it, or "
                    "add the line to STORAGE_ALLOW with the reason",
                )
            )
    return out


def test_browser_storage_is_always_inside_try():
    problems = []
    seen = set()
    for p in sorted(JS.rglob("*.js")) + sorted(TEMPLATES.rglob("*.html")):
        if p.name in VENDORED:
            continue
        text = p.read_text(encoding="utf-8")
        problems += unguarded_storage(rel(p), text, STORAGE_ALLOW)
        seen.update(
            f"{rel(p)}:{text.count(chr(10), 0, m.start()) + 1}" for m in STORAGE.finditer(text)
        )
    problems += stale(STORAGE_ALLOW, seen, "browser-storage")
    assert not problems, "\n".join(problems)


# ============================================================ (g) |safe
_ICON = "a fixed SVG fragment from a constant table in the code, chosen by an icon name"
_SWAP = "a constant string of hx-* attributes built by the view, no user text"
SAFE_ALLOW: dict[tuple[str, str], str] = {
    ("about.html", "AB_GLYPHS[name]"): _ICON,
    (
        "about.html",
        "p.body",
    ): "the promises text is written in the code (kestrel/web/pages/about.py)",
    ("analytics_print.html", "css"): "the print stylesheet read from Kestrel's own static file",
    ("help.html", "a.body"): "help article HTML shipped in kestrel/web/help_articles.py",
    ("opportunities.html", "panel_html"): "the role panel, itself rendered by a template "
    "(autoescaped) and passed on as markup",
    ("partials/company_macros.html", "CI[name]"): _ICON,
    ("partials/offer_card.html", "b.first"): "kestrel/web/offers_view.py builds it from two "
    "fixed words and <b> tags; no user text",
    ("partials/opp_rows.html", "swap"): _SWAP,
    ("partials/pl_views.html", "swap"): _SWAP,
    ("partials/pp_icons.html", "EXTRA[name]"): _ICON,
    ("partials/pp_preview.html", "paper_css"): "the document stylesheet built from the person's "
    "look choices (a closed set of values), inside a style tag",
    ("partials/pp_preview.html", "p"): "the prep pack page markup rendered by Kestrel's own "
    "template for the PDF",
    ("partials/profile_help.html", "body"): "help text written in the code",
    ("partials/pursuit_icons.html", "EXTRA[name]"): _ICON,
    ("partials/role_icons.html", "(RIC.get(name) or RIC['info'])"): _ICON,
    ("partials/st_access.html", "lan_qr"): "an SVG QR code generated by the library from the "
    "join address",
    ("partials/st_macros.html", "EXTRA[name]"): _ICON,
    ("partials/st_preferences.html", "pf.data_json"): "JSON in a script tag with '<' escaped to "
    "\\u003c in kestrel/web/pages/settings.py:167",
    ("partials/topbar_avatar.html", "avatar_marks[av]"): _ICON,
    ("settings.html", "body"): "flyout help text written in the code",
    ("settings.html", "how_it_works"): "flyout help text written in the code",
    ("ui/macros.html", "(ICONS.get(name) or ICONS['info'])"): _ICON,
}
SAFE_RE = re.compile(r"((?:[\w.\[\]'\"() ]|\bor\b)+?)\s*\|\s*safe\b")


def safe_uses(templates: Path) -> set[tuple[str, str]]:
    found = set()
    for p in sorted(templates.rglob("*.html")):
        text = re.sub(r"\{#.*?#\}", "", p.read_text(encoding="utf-8"), flags=re.S)
        for m in re.finditer(r"\{\{-?\s*([^{}]*?)\s*\|\s*safe\b", text):
            found.add((p.relative_to(templates).as_posix(), m.group(1).strip()))
        for m in re.finditer(r"\(([^(){}]*?)\)\s*\|\s*safe\b", text):
            found.add((p.relative_to(templates).as_posix(), m.group(1).strip()))
    return found


def test_safe_filter_only_on_allowlisted_values():
    found = safe_uses(TEMPLATES)
    problems = [
        fail(
            "|safe is used only on values the server builds from constants or escapes itself",
            f"{t}: {{{{ {e}|safe }}}}",
            "drop |safe (let Jinja escape it), or build the value with markupsafe.escape and add "
            "(template, expression) to SAFE_ALLOW with the reason",
        )
        for t, e in sorted(found)
        if (t, e) not in SAFE_ALLOW
    ]
    problems += stale(SAFE_ALLOW, found, "|safe")
    assert not problems, "\n".join(problems)


# ============================================================ proof each guard can fail
def test_orphan_lint_flags_an_unreferenced_partial(tmp_path):
    (tmp_path / "partials").mkdir()
    (tmp_path / "partials" / "used.html").write_text("x")
    (tmp_path / "partials" / "lonely.html").write_text("x")
    (tmp_path / "page.html").write_text('{% include "partials/used.html" %}')
    got = orphan_partials(tmp_path, [], {})
    assert len(got) == 1 and "partials/lonely.html" in got[0] and "FIX:" in got[0]
    assert orphan_partials(tmp_path, [], {"partials/lonely.html": "dynamic"}) == []


def test_dead_control_lint_flags_unwired_controls(tmp_path):
    (tmp_path / "a.html").write_text(
        '<a href="#">Dead</a><a href="#" hx-get="/x">Live</a><a href="#" data-pick>Picked</a>'
        '<form><button type="button">Nothing</button><button type="button" data-go>Go</button>'
        "<button>Submit</button></form>"
    )
    got = dead_controls(tmp_path, {"data-go", "data-pick"}, {})
    assert len(got) == 2, got
    assert any("Dead" in g for g in got) and any("Nothing" in g for g in got)


def test_js_selector_reader_finds_attribute_and_dataset_names(tmp_path):
    (tmp_path / "a.js").write_text("q('[data-open-it]'); el.dataset.closeIt; el.dataset['x1']")
    assert js_selected_data_names(tmp_path) >= {"data-open-it", "data-close-it", "data-x1"}


def test_placeholder_lint_flags_repeating_digests_and_markers(tmp_path):
    (tmp_path / "d.json").write_text('{"digest": "' + "ab" * 32 + '", "note": "TODO later"}')
    (tmp_path / "ok.json").write_text('{"digest": "' + "0123456789abcdef" * 4 + '"}')
    got = placeholder_data([tmp_path], {})
    assert len(got) == 2 and all("d.json" in g for g in got), got
    assert repeating("abab" * 8) and repeating("0" * 64) and not repeating("0123456789abcdef" * 2)


def test_task_kind_lint_flags_a_kind_nobody_leases(tmp_path):
    f = tmp_path / "m.py"
    f.write_text(
        'TASK_TYPE = "orphan_kind"\nOTHER_TASK_TYPE = "led"\n'
        "async def drain(queue):\n    await queue.lease('w', types=[OTHER_TASK_TYPE])\n"
    )
    got = unhandled_task_kinds([f], "")
    assert len(got) == 1 and "orphan_kind" in got[0]


def test_switch_lint_flags_a_switch_nobody_reads(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("def f(s):\n    return s.sect.read_me\n")
    keys = {"sect.read_me": ("sect", "read_me"), "sect.dead_switch": ("sect", "dead_switch")}
    got = unread_switches([f], keys, {})
    assert len(got) == 1 and "sect.dead_switch" in got[0]


def test_model_call_lint_flags_a_direct_client(tmp_path):
    f = tmp_path / "page.py"
    f.write_text(
        "from kestrel.llm.client import LLMClient\n\ndef go(s):\n    return LLMClient(s)\n"
    )
    assert llm_callers([f]) and next(iter(llm_callers([f]).values())) == [4]


def test_storage_lint_flags_an_unguarded_access_and_passes_a_guarded_one():
    bad = "const v = localStorage.getItem('k');"
    good = "function r() { try { return localStorage.getItem('k'); } catch (e) { return null; } }"
    assert len(unguarded_storage("x.js", bad, {})) == 1
    assert unguarded_storage("x.js", good, {}) == []
    assert unguarded_storage("x.js", "// localStorage is used below\n" + good, {}) == []
    page = "<p>localStorage prose</p><script>" + bad + "</script>"
    assert len(unguarded_storage("x.html", page, {})) == 1


def test_safe_lint_finds_values_and_ignores_comments(tmp_path):
    (tmp_path / "t.html").write_text(
        "{# never |safe #}<p>{{ user_text|safe }}</p>{{ (A.get(n) or A['i'])|safe }}"
    )
    found = safe_uses(tmp_path)
    assert ("t.html", "user_text") in found
    assert any("A.get" in e for _, e in found)
    assert not any("never" in e for _, e in found)
