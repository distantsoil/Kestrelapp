"""Which saved secret a source may send (D-SEC-21).

A source that needs its own key (USAJOBS, Adzuna, SAM.gov, RemoteRocketship) reads it from the
saved secrets by name, and the source's config may give that name (`api_key_env`, Adzuna's
`app_id_env` / `app_key_env`) so a person can keep two keys. The name decides WHICH secret is sent
to that service, so it is a fixed property of the adapter: it must start with the adapter's own
prefix (`USAJOBS_`, `ADZUNA_`, `SAM_GOV_`, `REMOTEROCKETSHIP_`). A name outside that family (an
online model key, the private service address, another source's key) is refused when the config
is validated AND again when the source runs, so a row a backup restored, or one written before
this rule, can never send another secret to that service.

Nothing here reads or sends a secret; the adapters do, with the name this module returns."""

from __future__ import annotations

import re

from kestrel.ingest.base import SourceConfigError

# The adapter kind and the prefix every secret name it may read starts with.
SECRET_PREFIX: dict[str, str] = {
    "usajobs": "USAJOBS_",
    "adzuna": "ADZUNA_",
    "sam_gov": "SAM_GOV_",
    "remoterocketship": "REMOTEROCKETSHIP_",
}
# The config fields that name a secret, for every adapter. A key holder may not write them
# (kestrel.web.pages.sources): which secret a source sends is this computer's choice.
SECRET_FIELDS = frozenset({"api_key_env", "app_id_env", "app_key_env"})
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def allowed(kind: str, name: object) -> bool:
    prefix = SECRET_PREFIX.get(kind)
    return (
        prefix is not None and isinstance(name, str) and bool(_ENV_NAME.match(name))
        and name.startswith(prefix) and len(name) > len(prefix)
    )


def secret_name(kind: str, config: dict, field: str, default: str) -> str:
    """The secret name this source sends, or SourceConfigError when the config names one the
    adapter may not read."""
    name = config.get(field)
    if name is None or name == "":
        return default
    if not allowed(kind, name):
        prefix = SECRET_PREFIX.get(kind, "")
        raise SourceConfigError(
            f"'{field}' must be the name of a saved key that starts with {prefix}"
        )
    return str(name)


def names_a_secret(config: object) -> bool:
    """Whether CONFIG sets any field that names a saved secret."""
    return isinstance(config, dict) and any(f in config for f in SECRET_FIELDS)
