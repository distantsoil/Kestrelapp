"""A source can only send its own service's key (D-SEC-21).

INVARIANT: a source's config never decides that another saved secret (an online model key, the
private service address, another source's key) is sent to that source's service, whoever wrote the
config: a key holder, a backup's restored rows, or an older build. The name must start with the
adapter's own prefix; it is checked when the config is validated and again when the source runs,
and a key holder may not add or change it. All values are invented; nothing touches the network."""

from __future__ import annotations

import asyncio
import json

import pytest

from kestrel import config as config_mod
from kestrel import paths
from kestrel.ingest import source_secrets
from kestrel.ingest.base import SourceConfigError
from kestrel.web import auth

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

FOREIGN = ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OTHER_SERVICE_URL_SECRET", "ADZUNA_APP_KEY")
KEY = "invented-shared-key"


class _Recorder:
    """A client that records any request and answers nothing."""

    user_agent = "invented"

    def __init__(self) -> None:
        self.calls: list = []

    async def request(self, method, url, **kw):
        self.calls.append((url, kw))
        raise RuntimeError("no network in tests")

    async def get_json(self, url, **kw):
        self.calls.append((url, kw))
        raise RuntimeError("no network in tests")

    async def get(self, url, **kw):
        self.calls.append((url, kw))
        raise RuntimeError("no network in tests")

    async def post_json(self, url, *a, **kw):
        self.calls.append((url, kw))
        raise RuntimeError("no network in tests")


@pytest.fixture
def secrets_read(monkeypatch):
    read: list[str] = []

    def fake(name):
        read.append(name)
        return "sk-invented-" + name

    monkeypatch.setattr(config_mod, "get_secret", fake)
    return read


def _adapters():
    from kestrel.ingest.adapters.adzuna import AdzunaAdapter
    from kestrel.ingest.adapters.remoterocketship import RemoteRocketshipAdapter
    from kestrel.ingest.adapters.usajobs import UsaJobsAdapter

    return [
        ("usajobs", UsaJobsAdapter(), "api_key_env", {"contact_email": "a@b.example"}),
        ("adzuna", AdzunaAdapter(), "app_key_env", {}),
        ("adzuna", AdzunaAdapter(), "app_id_env", {}),
        ("remoterocketship", RemoteRocketshipAdapter(), "api_key_env", {}),
    ]


@pytest.mark.parametrize("foreign", FOREIGN)
def test_no_adapter_accepts_or_reads_a_foreign_secret_name(foreign, secrets_read):
    for kind, adapter, field, base in _adapters():
        cfg = {**base, field: foreign}
        if foreign.startswith(source_secrets.SECRET_PREFIX[kind]):
            continue  # an ADZUNA_ name is Adzuna's own
        with pytest.raises(SourceConfigError):
            adapter.validate_config(cfg)
        client = _Recorder()
        with pytest.raises(SourceConfigError):
            asyncio.run(adapter.fetch(cfg, client))
        assert foreign not in secrets_read, (kind, field)
        assert client.calls == []


def test_sam_gov_never_reads_a_foreign_secret_name(secrets_read):
    from kestrel.ingest.signal_sources import sam_gov

    with pytest.raises(SourceConfigError):
        sam_gov._resolve_api_key({"api_key_env": "OPENROUTER_API_KEY"})
    assert "OPENROUTER_API_KEY" not in secrets_read
    assert sam_gov._resolve_api_key({"api_key_env": "SAM_GOV_SECOND_KEY"})


def test_an_adapters_own_names_still_work(secrets_read):
    for kind, adapter, field, base in _adapters():
        prefix = source_secrets.SECRET_PREFIX[kind]
        adapter.validate_config({**base, field: prefix + "SECOND_KEY"})
    assert not source_secrets.allowed("usajobs", "USAJOBS_")
    assert not source_secrets.allowed("usajobs", "usajobs_key")
    assert not source_secrets.allowed("rss", "RSS_KEY")


# ------------------------------------------------------------------------- a key holder
def _app_client(monkeypatch, peer):
    from fastapi.testclient import TestClient

    from kestrel.db import migrate
    from kestrel.web.app import build_app

    migrate.run_migrations()
    auth.set_cached_key(KEY)
    return TestClient(build_app(worker=False), base_url="http://127.0.0.1",
                      raise_server_exceptions=False, client=(peer, 12345))


def _source_configs() -> list[dict]:
    import sqlite3

    con = sqlite3.connect(paths.db_path())
    try:
        return [json.loads(c) for (c,) in con.execute("SELECT config FROM source")]
    finally:
        con.close()


FORM = {"name": "Invented federal search", "track": "employment", "kind": "usajobs",
        "cadence_hours": "24", "enabled": "1"}


def test_a_key_holder_cannot_name_which_secret_a_source_sends(monkeypatch):
    try:
        with _app_client(monkeypatch, "192.168.1.50") as c:
            r = c.post("/sources", headers={"X-Kestrel-Key": KEY}, data={
                **FORM, "config": json.dumps({"api_key_env": "USAJOBS_SECOND_KEY",
                                              "contact_email": "a@b.example"})})
            assert r.status_code == 403
            assert r.text == auth.LOCAL_ONLY_CHANGE
            assert not any("api_key_env" in c for c in _source_configs())
            # Without a secret name the same key holder may add the source.
            r = c.post("/sources", headers={"X-Kestrel-Key": KEY}, data={
                **FORM, "config": json.dumps({"contact_email": "a@b.example"})})
            assert r.status_code == 200
    finally:
        auth.reset_cache()


def test_this_computer_may_name_its_own_second_key(monkeypatch):
    try:
        with _app_client(monkeypatch, "127.0.0.1") as c:
            r = c.post("/sources", data={**FORM, "config": json.dumps(
                {"api_key_env": "USAJOBS_SECOND_KEY", "contact_email": "a@b.example"})})
            assert r.status_code == 200
            r = c.post("/sources", data={**FORM, "config": json.dumps(
                {"api_key_env": "OPENROUTER_API_KEY", "contact_email": "a@b.example"})})
            assert r.status_code == 400
    finally:
        auth.reset_cache()
    assert [c.get("api_key_env") for c in _source_configs()] == ["USAJOBS_SECOND_KEY"]
