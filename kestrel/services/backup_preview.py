"""What restoring a backup's AI and connection settings would set, read without applying it
(D-SEC-11). The restore confirmation lists these values so the person sees, for example, that a
backup points the local model at an internet address, BEFORE choosing to restore them.

Invariants: nothing is sent anywhere (classification uses no DNS, D-LLM-15), nothing in the live
data is touched (the archive is staged in a temp folder and only its database and settings.yml
are read), and no secret is shown (API keys are never in a backup; a webhook or topic shows its
host only, never a path or token; a login inside an address is dropped)."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from kestrel import netloc
from kestrel.services import backup as bk

_WHERE = {
    netloc.THIS_COMPUTER: "this computer",
    netloc.LOCAL_NETWORK: "your local network",
    netloc.INTERNET: "the internet",
}
_TIERS = (
    ("tier1", "Quick screen"), ("tier2", "Fit rating"), ("tier3", "Deeper read"),
    ("embed", "Matching similar roles"), ("docsel", "Resume picking"), ("trigger", "Triggers"),
)
_TOGGLES = (
    ("mcp.enabled", "AI assistant connection (MCP)"), ("mcp.port", "AI assistant port"),
    ("runs.keep_awake", "Keep the computer awake during runs"),
    ("ui.autostart", "Start with the computer"), ("ui.close_to_tray", "Close to the tray"),
    ("updates.enabled", "Check for updates"),
    ("ingest.respect_robots", "Respect robots.txt"),
)
_DESTINATIONS = (
    ("notify.webhook_url", "Notification webhook"), ("notify.ntfy_topic", "ntfy notifications"),
    ("research.searx_url", "Web search server"), ("updates.feed_url", "Update feed"),
)


@dataclass
class Item:
    label: str
    value: str
    warn: bool = False


@dataclass
class SettingsPreview:
    servers: list[Item] = field(default_factory=list)
    tiers: list[Item] = field(default_factory=list)
    destinations: list[Item] = field(default_factory=list)
    switches: list[Item] = field(default_factory=list)
    other_count: int = 0
    kept_here: list[str] = field(default_factory=lambda: [
        "Hosted mode, its accepted disclosure and whether online sends hide your current "
        "employer stay as they are on this computer.",
        "Your own online service keys are never in a backup and are not restored.",
        "Which key each model server uses is kept from this computer.",
        "A model server that uses a key saved on this computer keeps this computer's address.",
        "Settings for letting other devices reach this computer stay as they are here.",
    ])

    @property
    def any_internet(self) -> bool:
        return any(i.warn for i in self.servers)

    @property
    def empty(self) -> bool:
        return not (self.servers or self.tiers or self.destinations or self.switches
                    or self.other_count)


def _host_only(url: str) -> str:
    """scheme://host[:port] of URL: no login, no path, no query (a webhook path is a secret)."""
    text = str(url or "").strip()
    try:
        parts = urlsplit(text)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return "an address Kestrel cannot read"
    if not host:
        return "a name, not an address"
    shown = f"{parts.scheme}://" if parts.scheme else ""
    shown += f"[{host}]" if ":" in host else host
    return shown + (f":{port}" if port else "")


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) \
            else v
    return out


def _backup_tree(staging: Path) -> dict[str, Any]:
    """The settings the backup would set: its settings.yml with its setting rows on top."""
    import yaml

    tree: dict[str, Any] = {}
    yml = staging / "config" / "settings.yml"
    if yml.is_file():
        try:
            loaded = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError, UnicodeDecodeError):
            loaded = {}
        if isinstance(loaded, dict):
            tree = loaded
            bk._clean_backends(bk._backends_in("", tree))  # never applied (D-SEC-19)
    flat: dict[str, Any] = {}
    con = sqlite3.connect(f"file:{(staging / bk.DB_NAME).as_posix()}?mode=ro", uri=True)
    try:
        has = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='setting'"
        ).fetchone()
        rows = con.execute("SELECT key, value, updated_at FROM setting").fetchall() if has else []
    finally:
        con.close()
    # Flattened to leaf keys first, exactly as the restore does (D-SEC-16): a whole-section row
    # (`llm`, `notify`) is listed leaf by leaf instead of slipping past a key-name check.
    clean: dict[str, tuple[str, str]] = {}
    for k, v, u in rows:
        bf = bk._backend_field(k)
        if bf is not None and not bk._BACKEND_NAME.match(bf[0]):
            continue
        value = bk._decode(v)
        if bk._clean_backends(bk._backends_in(k, value)):
            v = json.dumps(value)
        clean[k] = (v, u)
    leaves = bk._leaf_rows(clean)
    for key, (raw, _updated, _origin) in leaves.items():
        if not bk._local_only(key) or bk._always_kept(key):
            continue
        try:
            flat[key] = json.loads(raw)
        except (TypeError, ValueError):
            continue
    from kestrel.services.setting_overrides import nest_overrides

    return _deep_merge(tree, nest_overrides(flat))


def _get(tree: dict, dotted: str) -> Any:
    cur: Any = tree
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _yes(value: Any) -> str:
    return "on" if value is True else "off" if value is False else str(value)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _is_name(url: str) -> bool:
    """True when the address names a host rather than giving an IP address."""
    import ipaddress

    host = netloc.host_of(url) or ""
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return True
    return False


def build_preview(tree: dict[str, Any]) -> SettingsPreview:
    out = SettingsPreview()
    llm = _dict(tree.get("llm"))
    backends = _dict(llm.get("backends"))
    where_by_backend: dict[str, str] = {}
    for name, b in backends.items():
        if not isinstance(b, dict):
            continue
        url = str(b.get("base_url") or "")
        if b.get("engine") == "inprocess" or not url:
            where_by_backend[str(name)] = "this computer"
            if b.get("engine") == "inprocess":
                out.servers.append(Item(str(name), "the built-in model, on this computer"))
            continue
        loc = netloc.classify_url(url)
        if b.get("external"):
            loc = netloc.INTERNET
        where = _WHERE[loc]
        where_by_backend[str(name)] = where
        note = f"{_host_only(url)}, on {where}" if loc != netloc.INTERNET else (
            f"{_host_only(url)}, on the internet")
        if loc == netloc.INTERNET and not b.get("external") and _is_name(url):
            note += " (a host name counts as the internet)"
        out.servers.append(Item(str(name), note, warn=loc == netloc.INTERNET))
    for section, kind in (("tiers", ""), ("hosted_tiers", "online mode: ")):
        tiers = _dict(llm.get(section))
        for key, label in _TIERS:
            t = tiers.get(key)
            if not isinstance(t, dict):
                continue
            be = str(t.get("backend") or "local")
            model = str(t.get("model") or "")[:80]
            where = where_by_backend.get(be, "a server not listed here")
            out.tiers.append(Item(
                f"{kind}{label}".capitalize() if kind else label,
                f"{model or 'its default model'} through {be} ({where})",
                warn=where == "the internet",
            ))
    mode = llm.get("mode")
    if mode:
        out.switches.append(Item("Where the models run", str(mode)[:40]))
    for key, label in _TOGGLES:
        value = _get(tree, key)
        if value is not None:
            out.switches.append(Item(label, _yes(value)))
    for key, label in _DESTINATIONS:
        value = _get(tree, key)
        if value:
            out.destinations.append(Item(label, _host_only(value) if "://" in str(value) else
                                         "a topic name is set (not shown)"))
    ua = _get(tree, "ingest.user_agent")
    if ua:
        out.destinations.append(Item("How Kestrel names itself to sites", str(ua)[:80]))
    shown = {"llm.backends", "llm.tiers", "llm.hosted_tiers", "llm.mode",
             *(k for k, _ in _TOGGLES), *(k for k, _ in _DESTINATIONS), "ingest.user_agent"}
    flat = {k: v for k, v in bk._flat(tree).items() if bk._within_local(k)}
    out.other_count = sum(
        1 for k in flat
        if not any(k == s or k.startswith(s + ".") for s in shown) and not bk._always_kept(k)
    )
    return out


def preview_backup(path: str | Path) -> SettingsPreview:
    """Read the backup at PATH and list the AI and connection settings it would set. Raises
    `backup.InvalidBackupError` for anything short of a well-formed Kestrel backup. Nothing is
    applied and nothing is sent."""
    src = Path(path)
    if not src.is_file():
        raise bk.InvalidBackupError(bk.BACKUP_NOT_FOUND)
    with tempfile.TemporaryDirectory(prefix="kestrel-preview-") as tmp:
        staging = Path(tmp)
        bk._stage_backup(src, staging, settings_only=True)
        return build_preview(_backup_tree(staging))
