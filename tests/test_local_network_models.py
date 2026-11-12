"""D-LLM-15: where a model server runs is decided by its ADDRESS: this computer, the local network
(no Hosted mode, no consent dialog) or the internet (online exactly as before). Invented hosts;
nothing reaches the network."""

from __future__ import annotations

import pytest

from kestrel import netloc
from kestrel.config import BackendConfig, load_settings
from kestrel.services.online_consent import backend_is_off_machine, tier_goes_online

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Classification uses no DNS and no socket at all (D-LLM-15): any call fails the test."""
    import socket

    def boom(*a, **k):
        raise AssertionError("netloc touched the network")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket, "gethostbyname", boom)


@pytest.mark.parametrize("url,where", [
    ("http://localhost:11434/v1", netloc.THIS_COMPUTER),
    ("http://127.0.0.1:11434/v1", netloc.THIS_COMPUTER),
    ("http://[::1]:11434/v1", netloc.THIS_COMPUTER),
    ("http://192.168.2.25:8000/v1", netloc.LOCAL_NETWORK),
    ("http://10.1.2.3/v1", netloc.LOCAL_NETWORK),
    ("http://172.16.0.9/v1", netloc.LOCAL_NETWORK),
    ("http://172.31.255.1/v1", netloc.LOCAL_NETWORK),
    ("http://172.32.0.1/v1", netloc.INTERNET),
    ("http://[fd00::1]:8000/v1", netloc.LOCAL_NETWORK),
    ("http://[fe80::1]:8000/v1", netloc.LOCAL_NETWORK),
    ("http://studio.local:8000/v1", netloc.INTERNET),      # a name is never trusted
    ("http://nas:8000/v1", netloc.INTERNET),
    ("http://box.example.invalid/v1", netloc.INTERNET),    # even if it would resolve to a LAN IP
    ("http://8.8.8.8/v1", netloc.INTERNET),
    ("http://192.168.1.20@8.8.8.8/v1", netloc.INTERNET),   # userinfo trick
])
def test_classify_by_literal_address_only(url, where):
    assert netloc.classify_url(url) == where


def test_classify_never_touches_the_network():
    for url in ("http://a.example.invalid/v1", "http://x.local/v1", "http://192.168.0.2/v1"):
        assert netloc.classify_url(url) in (netloc.INTERNET, netloc.LOCAL_NETWORK)


def _with_url(url: str, *, external: bool = False):
    s = load_settings()
    s.llm.backends["local"].base_url = url
    s.llm.backends["local"].external = external
    return s


def test_lan_address_needs_no_consent_and_no_hosted_mode():
    s = _with_url("http://192.168.2.25:8000/v1")
    assert not s.llm.hosted_mode.active
    assert tier_goes_online(s, "tier1") is False
    assert tier_goes_online(s, "tier2") is False


def test_public_address_is_online_whatever_the_flag_says():
    s = _with_url("http://8.8.8.8:11434/v1", external=False)
    assert s.llm.backends["local"].external is True
    assert tier_goes_online(s, "tier1") is True


def test_a_host_name_is_online_whatever_it_would_resolve_to():
    for name in ("http://a.example.invalid/v1", "http://studio.local/v1"):
        assert tier_goes_online(_with_url(name), "tier1") is True


def test_localhost_is_this_computer():
    b = _with_url("http://localhost:11434/v1").llm.backends["local"]
    assert b.location == netloc.THIS_COMPUTER and not backend_is_off_machine(b)


def test_the_spec_example_mini_backend_is_local_network():
    mini = BackendConfig(base_url="http://192.168.2.25:8000/v1", api_key_env="OMLX_API_KEY",
                         json_mode="json_object", thinking_control="chat_template_kwargs")
    assert mini.location == netloc.LOCAL_NETWORK
    assert not mini.external and not backend_is_off_machine(mini)


def test_an_internet_address_still_runs_only_through_consent():
    """The invariant: a model server on the internet is online, held by the consent check."""
    from kestrel.services.online_consent import approval_for

    s = _with_url("https://203.0.113.9/v1")
    assert approval_for(s, ["tier1"]).get("tier1")
    lan = _with_url("http://192.168.2.25:8000/v1")
    assert approval_for(lan, ["tier1"]) == {}


def test_a_local_backend_never_carries_an_online_provider_key():
    """D-SEC-11 defense in depth: a backend on this computer or the local network drops a
    reference to any online provider's key name at load; an external one keeps it."""
    lan = BackendConfig(base_url="http://192.168.2.25:8000/v1", api_key_env="OPENROUTER_API_KEY")
    assert lan.api_key_env is None
    other = BackendConfig(base_url="http://localhost:1/v1", api_key_env="OTHER_SERVICE_API_KEY")
    assert other.api_key_env is None
    own = BackendConfig(base_url="http://192.168.2.25:8000/v1", api_key_env="OMLX_API_KEY")
    assert own.api_key_env == "OMLX_API_KEY"
    online = BackendConfig(base_url="https://openrouter.ai/api/v1",
                           api_key_env="OPENROUTER_API_KEY", external=True)
    assert online.api_key_env == "OPENROUTER_API_KEY"


def test_the_llm_client_uses_a_no_proxy_client_for_a_local_backend():
    import httpx

    from kestrel.llm.client import LLMClient

    s = _with_url("http://192.168.2.25:8000/v1")
    shared, local = httpx.AsyncClient(), httpx.AsyncClient(trust_env=False)
    c = LLMClient(s, http=shared, http_local=local)
    assert c._shared(s.llm.backends["local"]) is local
    online = _with_url("https://203.0.113.9/v1").llm.backends["local"]
    assert c._shared(online) is shared
    assert LLMClient(s)._new_client(s.llm.backends["local"])._trust_env is False
    assert LLMClient(s)._new_client(online)._trust_env is True


def test_a_page_client_with_only_a_shared_http_never_sends_local_traffic_through_it():
    """Every page and CLI path builds LLMClient(settings, http=httpx.AsyncClient()), a client that
    reads a system proxy. A local-network or this-computer backend must not use it: it gets a
    call-own trust_env=False client instead (D-LLM-15)."""
    import httpx

    from kestrel.llm.client import LLMClient

    s = _with_url("http://192.168.2.25:8000/v1")
    shared = httpx.AsyncClient()
    c = LLMClient(s, http=shared)
    assert c._shared(s.llm.backends["local"]) is None
    assert c._new_client(s.llm.backends["local"])._trust_env is False
    online = _with_url("https://203.0.113.9/v1").llm.backends["local"]
    assert c._shared(online) is shared
