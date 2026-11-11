"""Model servers on the person's own network (D-LLM-15).

A server at a home or office address (an Ollama, oMLX, vLLM or llama.cpp server on another
computer) is added on the Models page: its address, an optional key and its kind. The kind sets the
JSON and thinking presets and says whether Kestrel may download or build models there (Ollama
only). Where it runs is read from the ADDRESS (kestrel.netloc), so an internet address cannot be
saved here: it is an online service and goes through Settings > Hosted with its consent dialog.

Invariants: nothing about the person is sent by Test connection (one GET of the server's model
list, with the key if the person gave one); a key is written to the private key store, never to the
settings table, a backup or a log; every write is LOCAL_ONLY (the routes carry the guard)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
import structlog
from sqlalchemy import delete

from kestrel import netloc
from kestrel.config import Settings
from kestrel.db.models import Setting

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()

# kind -> (label, JSON mode, thinking control, needs a num_ctx Modelfile, can load models here).
# The Ollama row is the shipped `local` backend's; oMLX is the spec's example `mini`. vLLM and
# llama.cpp use the same OpenAI-compatible wire: JSON mode through response_format and thinking
# switched off through chat_template_kwargs (vLLM) or not controllable (llama.cpp). Unverified
# against live servers; tests/test_lan_servers.py pins what the presets send.
KINDS: dict[str, dict[str, Any]] = {
    "ollama": {"label": "Ollama", "json_mode": "json_schema",
               "thinking_control": "reasoning_effort", "num_ctx": True},
    "omlx": {"label": "oMLX", "json_mode": "json_object",
             "thinking_control": "chat_template_kwargs", "num_ctx": False},
    "vllm": {"label": "vLLM", "json_mode": "json_object",
             "thinking_control": "chat_template_kwargs", "num_ctx": False},
    "llamacpp": {"label": "llama.cpp or another server", "json_mode": "json_object",
                 "thinking_control": "none", "num_ctx": False},
}
KINDS["other"] = KINDS["llamacpp"]
KIND_ORDER = ("ollama", "omlx", "vllm", "llamacpp")
ASSIGNABLE = (("tier1", "Quick screen"), ("tier2", "Fit rating"))
PREFIX = "lan_"
_SLUG = re.compile(r"[^a-z0-9]+")
TEST_TIMEOUT_S = 8.0


@dataclass
class Server:
    name: str
    host: str
    base_url: str
    kind: str
    kind_label: str
    has_key: bool
    where: str  # a netloc location
    tiers: list[str]

    @property
    def label(self) -> str:
        return netloc.label_for(self.where, self.host)

    @property
    def where_words(self) -> str:
        """Where it runs, without the host (the row already names it)."""
        return netloc.label_for(self.where)

    @property
    def is_ollama(self) -> bool:
        return self.kind == "ollama"


def kind_label(kind: str | None) -> str:
    return KINDS.get(kind or "", {}).get("label", "the model server")


def backend_name(base_url: str) -> str:
    host = netloc.host_of(base_url) or "server"
    port = urlsplit(base_url).port
    return PREFIX + _SLUG.sub("_", host + (f"_{port}" if port else "")).strip("_")[:40]


def key_env(name: str) -> str:
    return name.upper() + "_API_KEY"


def normalize_url(raw: str) -> tuple[str, str]:
    """The address as the person typed it, made into a base URL ending in /v1: (url, "") or
    ("", the sentence for the person)."""
    text = (raw or "").strip()
    if not text:
        return "", "Enter the address of the server, for example http://192.168.1.20:8000."
    if "://" not in text:
        text = "http://" + text
    if any(c.isspace() or c in "\\\"'" or ord(c) < 32 for c in text) or "${" in text:
        return "", "Enter an address without spaces or quotes."
    try:
        parts = urlsplit(text)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        return "", "Enter a valid address, for example http://192.168.1.20:8000."
    if parts.scheme not in ("http", "https") or not host:
        return "", "Use an address that starts with http:// or https://."
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        return "", ("Use an address without a login, query or fragment. Put a key in the Key "
                    "box.")
    path = parts.path.rstrip("/") or "/v1"
    return f"{parts.scheme}://{parts.netloc}{path}", ""


def problem_for_location(base_url: str) -> str | None:
    """Why this address cannot be saved as a server on your network, else None. Only a literal IP
    address on this computer or the local network is accepted: a host name (even a .local one)
    counts as online, because a name can point anywhere (D-LLM-15)."""
    if netloc.classify_url(base_url) == netloc.INTERNET:
        return ("Use the server's IP address on your network, for example 192.168.2.25. A host "
                "name counts as online, and an address on the internet is an online service: "
                "add that under Online model, where Kestrel asks before anything is sent.")
    return None


def list_servers(settings: Settings) -> list[Server]:
    """Every model server the person has on their network: an http backend with an address on
    the local network (or one added here), other than the shipped online services."""
    from kestrel.llm import providers as prov

    used: dict[str, list[str]] = {}
    for tier, cfg in settings.llm.tiers.items():
        used.setdefault(cfg.backend, []).append(tier)
    out: list[Server] = []
    for name, b in settings.llm.backends.items():
        if b.engine != "http" or not (b.base_url or "").strip():
            continue
        where = b.location
        if where == netloc.INTERNET:
            continue
        if where == netloc.THIS_COMPUTER and not name.startswith(PREFIX):
            continue
        kind = b.server_kind or ("ollama" if b.requires_num_ctx_modelfile else "other")
        out.append(Server(
            name=name, host=netloc.host_of(b.base_url) or "", base_url=b.base_url, kind=kind,
            kind_label=kind_label(kind),
            has_key=bool(b.api_key) or prov.secret_present(b.api_key_env),
            where=where, tiers=used.get(name, []),
        ))
    return out


def backend_row(kind: str, base_url: str, name: str, *, with_key: bool) -> dict[str, Any]:
    preset = KINDS[kind if kind in KINDS else "other"]
    row: dict[str, Any] = {
        "base_url": base_url, "api_key": "ollama" if kind == "ollama" and not with_key else None,
        "json_mode": preset["json_mode"], "thinking_control": preset["thinking_control"],
        "requires_num_ctx_modelfile": bool(preset["num_ctx"]), "server_kind": kind,
        "external": False, "provider": "generic", "protocol": "chat_completions",
    }
    if with_key:
        row["api_key_env"] = key_env(name)
    return row


async def _put(session: AsyncSession, key: str, value: Any) -> None:
    from kestrel.db.base import utcnow_iso

    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow_iso()


async def save_server(
    session: AsyncSession, *, base_url: str, kind: str, api_key: str
) -> tuple[str, str, bool]:
    """Write the backend row (and the key, to the private key store). Returns (name, message,
    ok). The caller has checked the address with `problem_for_location`, and commits."""
    from kestrel.llm import providers as prov

    kind = kind if kind in KIND_ORDER else "other"
    name = backend_name(base_url)
    key = (api_key or "").strip()
    have_key = bool(key) or prov.secret_present(key_env(name))
    row = backend_row(kind, base_url, name, with_key=have_key)
    if key:
        shape = prov.key_shape_problem(key)
        if shape:
            return name, f"{shape}. Nothing was saved.", False
        try:
            prov.write_secret(key_env(name), key)
        except ValueError:
            return name, "The key could not be saved on this computer. Nothing was saved.", False
    await _put(session, f"llm.backends.{name}", row)
    return name, f"Saved {netloc.host_of(base_url)}.", True


async def assign_tier(
    session: AsyncSession, settings: Settings, tier: str, name: str, model: str
) -> tuple[bool, str]:
    """Point one step at a model on a saved server: (ok, the sentence to show)."""
    if tier not in dict(ASSIGNABLE):
        return False, "That step always runs where it is."
    backend = settings.llm.backends.get(name)
    model = (model or "").strip()[:200]
    if backend is None or not model:
        return False, "Pick a server and a model first."
    if backend.location == netloc.INTERNET:
        return False, "That server is not on your network."
    await _put(session, f"llm.tiers.{tier}.backend", name)
    await _put(session, f"llm.tiers.{tier}.model", model)
    step = dict(ASSIGNABLE)[tier]
    return True, f"{step} now uses {model} on {netloc.host_of(backend.base_url)}."


async def reset_tier(session: AsyncSession, tier: str) -> None:
    await session.execute(delete(Setting).where(Setting.key.in_(
        [f"llm.tiers.{tier}.backend", f"llm.tiers.{tier}.model"])))


async def remove_server(
    session: AsyncSession, settings: Settings, name: str
) -> tuple[bool, str]:
    if not name.startswith(PREFIX) or name not in settings.llm.backends:
        return False, "Kestrel can only remove a server added here."
    for tier, cfg in settings.llm.tiers.items():
        if cfg.backend == name:
            await reset_tier(session, tier)
    await session.execute(delete(Setting).where(Setting.key == f"llm.backends.{name}"))
    from kestrel.llm import providers as prov

    try:
        prov.write_secret(key_env(name), "")
    except ValueError:
        log.warning("lan_server_key_not_removed")
    return True, "Removed."


async def test_connection(base_url: str, api_key: str) -> dict[str, Any]:
    """One GET of the server's model list. Sends no profile, role or resume text. Returns
    {"ok", "text", "models"}; the text never holds the key."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    url = base_url.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=TEST_TIMEOUT_S, trust_env=False) as client:
            r = await client.get(url, headers=headers)
    except (httpx.HTTPError, OSError):
        return {"ok": False, "models": [], "text": (
            "Kestrel could not reach that server. Check the address and that the server is "
            "running and allows other computers.")}
    if r.status_code in (401, 403):
        return {"ok": False, "models": [], "text": "The server wants a key, or did not accept it."}
    if r.status_code != 200:
        return {"ok": False, "models": [],
                "text": f"The server answered with an error ({r.status_code})."}
    try:
        data = r.json()
        items = data.get("data") if isinstance(data, dict) else None
        models = sorted({str(m.get("id")) for m in (items or []) if isinstance(m, dict)
                         and m.get("id")})[:200]
    except ValueError:
        models = []
    if not models:
        return {"ok": True, "models": [], "text": (
            "The server answered, but it lists no models yet. Load one on that computer, then "
            "test again.")}
    return {"ok": True, "models": models, "text": f"Connected. {len(models)} model(s) found."}


def tier_where(settings: Settings, tier: str) -> tuple[str, str]:
    """(netloc location, host) of the backend a step runs on."""
    cfg = settings.llm.tiers.get(tier)
    b = settings.llm.backends.get(cfg.backend) if cfg else None
    if b is None:
        return netloc.THIS_COMPUTER, ""
    return b.location, netloc.host_of(b.base_url) or ""


def scoring_on_network(settings: Settings) -> tuple[bool, str]:
    """(True, host) when a scoring step runs on another computer on the network and none runs on
    this computer: the status rail then names that computer instead of probing local Ollama."""
    locs = [tier_where(settings, t) for t in ("tier1", "tier2") if t in settings.llm.tiers]
    if locs and all(loc == netloc.LOCAL_NETWORK for loc, _ in locs):
        return True, locs[-1][1]
    return False, ""


def only_non_ollama(settings: Settings) -> str:
    """The server kind label when every scoring step runs on a non-Ollama server, else ''. The
    download and build buttons are Ollama-only, so they hide then."""
    kinds = []
    for t in ("tier1", "tier2"):
        cfg = settings.llm.tiers.get(t)
        b = settings.llm.backends.get(cfg.backend) if cfg else None
        if b is None:
            return ""
        kinds.append(b.server_kind or ("ollama" if b.requires_num_ctx_modelfile else "other"))
    if kinds and all(k != "ollama" for k in kinds) and all(
        (settings.llm.backends[settings.llm.tiers[t].backend].location != netloc.INTERNET)
        for t in ("tier1", "tier2")
    ):
        return kind_label(kinds[0])
    return ""
