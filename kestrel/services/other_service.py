"""The custom online service: private address and key, existing hosted client and consent."""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, field_validator

from kestrel.config import BackendConfig

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from kestrel.config import Settings

URL_ENV = "OTHER_SERVICE_URL_SECRET"
KEY_ENV = "OTHER_SERVICE_API_KEY"
BACKEND = "other"
PRESET = {
    "backend": BACKEND,
    "label": "Other service",
    "key_prefix": "",
    "console": "/settings#computer",
    "privacy": "/settings#computer",
}


class Connection(BaseModel):
    base_url: str = Field(repr=False)
    api_key: str = Field(default="", repr=False)

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if (
            "${" in value
            or not value
            or any(c.isspace() or c in "\\\"'" or ord(c) < 32 for c in value)
        ):
            raise ValueError("Enter a service address without spaces or quotes.")
        try:
            url = urlsplit(value)
            host = url.hostname
            port = url.port
        except ValueError:
            raise ValueError("Enter a valid service address.") from None
        if (
            not host
            or url.username is not None
            or url.password is not None
            or "?" in value
            or "#" in value
            or (port is not None and port == 0)
        ):
            raise ValueError("Use a service address without a login, query or fragment.")
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.lower() == "localhost"
        if url.scheme != "https" and not (url.scheme == "http" and loopback):
            raise ValueError("Use HTTPS. HTTP is allowed only on this computer.")
        try:
            return str(httpx.URL(value)).rstrip("/")
        except httpx.InvalidURL:
            raise ValueError("Enter a valid service address.") from None

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value: str) -> str:
        if (
            any(c in value for c in "\r\n")
            or "${" in value
            or any(c.isspace() or ord(c) < 32 or c in "\\\"'" for c in value.strip())
        ):
            raise ValueError("Enter a key without spaces, quotes or control characters.")
        return value.strip()

    def backend(self) -> BackendConfig:
        return BackendConfig(
            base_url=self.base_url,
            api_key=self.api_key or None,
            api_key_env=KEY_ENV,
            external=True,
            provider="generic",
            protocol="chat_completions",
        )


def apply(settings: Settings) -> Settings:
    """Read only the private .env store, never a YAML/DB URL or a system variable."""
    from kestrel.config import _dotenv_state

    existing = settings.llm.backends.pop(BACKEND, None)
    value = _dotenv_state["secrets"].get(URL_ENV)
    if value:
        from kestrel.private_logging import install

        install()
        # A damaged entry holds sends instead of falling back to a previously configured URL.
        from pydantic import ValidationError

        try:
            backend = Connection(base_url=value).backend()
        except ValidationError:
            backend = BackendConfig(external=True, api_key_env=KEY_ENV)
        settings.llm.backends[BACKEND] = backend
    elif existing or any(
        t.backend == BACKEND
        for t in (*settings.llm.tiers.values(), *settings.llm.hosted_tiers.values())
    ):
        settings.llm.backends[BACKEND] = BackendConfig(external=True, api_key_env=KEY_ENV)
    return settings


async def key_action(
    op: str, base_url: str, api_key: str, session: AsyncSession, settings: Settings
) -> dict:
    """Test writes nothing. Save stores both values privately; no model sends or consent."""
    from pydantic import ValidationError

    from kestrel.llm import providers as prov
    from kestrel.services import hosted_models
    from kestrel.services.hosted_connect import _set

    try:
        candidate = Connection(base_url=base_url, api_key=api_key)
    except ValidationError as exc:
        # ValidationError itself contains input values. Only our authored reason is safe.
        return {
            "ok": False,
            "text": str(exc.errors(include_input=False)[0]["msg"]).removeprefix("Value error, "),
        }
    if not candidate.api_key and not prov.secret_present(KEY_ENV):
        return {"ok": False, "text": "Paste your service key first."}
    if op == "test":
        from kestrel.private_logging import private_values

        with private_values(candidate.base_url, candidate.api_key):
            status = await prov.verify_key(candidate.backend())
        return {
            "ok": status.ok,
            "text": "The connection works. Press Save to keep it."
            if status.ok
            else "The connection failed. Check the address and key and try again.",
        }
    if op != "save":
        return {"ok": False, "text": "Choose Test connection or Save."}
    old = settings.llm.backends.get(BACKEND)
    # Hold existing assignments before changing their destination, including on a partial write.
    if old and old.base_url != candidate.base_url:
        await _set(session, "llm.hosted_mode.acknowledged_at", None)
        await session.commit()
    try:
        if candidate.api_key:
            prov.write_secret(KEY_ENV, candidate.api_key)
        prov.write_secret(URL_ENV, candidate.base_url)
    except (ValueError, OSError):
        return {"ok": False, "text": "The connection was not saved. Try again."}
    hosted_models.clear_cache()
    return {
        "ok": True,
        "text": "Service address and key saved. Choose models and approve "
        "what is sent on Models before using this service.",
    }
