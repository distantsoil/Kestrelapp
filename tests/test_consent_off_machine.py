"""D-SEC-10: a backend whose base URL is not this computer always gets the consent dialog,
whatever its `external` flag says (the flag is a setting a restored backup could carry).

Invented hosts only. Nothing reaches the network."""

from __future__ import annotations

import pytest

from kestrel.config import load_settings
from kestrel.services.online_consent import backend_is_off_machine, tier_goes_online

pytestmark = pytest.mark.usefixtures("_isolate_data_home")


def _settings_with_local_url(url: str, *, external: bool = False):
    s = load_settings()
    s.llm.backends["local"].base_url = url
    s.llm.backends["local"].external = external
    return s


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:11434/v1", "http://localhost:11434/v1", "http://[::1]:11434/v1",
    "http://LOCALHOST./v1",
])
def test_the_documented_local_backends_stay_local(url):
    s = _settings_with_local_url(url)
    assert tier_goes_online(s, "tier1") is False


@pytest.mark.parametrize("url", [
    "https://models.invalid.example/v1",   # an internet host with external false
    "http://localhost.invalid.example/v1",  # a name that only starts like localhost
    "http://127.0.0.1@models.invalid.example/v1",  # userinfo trick
])
def test_any_internet_url_is_online_even_when_flagged_local(url):
    s = _settings_with_local_url(url, external=False)
    assert s.llm.backends["local"].external is True  # the address decides (D-LLM-15)
    assert tier_goes_online(s, "tier1") is True


def test_the_in_process_engine_and_an_empty_url_never_leave():
    s = load_settings()
    inproc = [b for b in s.llm.backends.values() if b.engine == "inprocess"]
    assert inproc
    assert [backend_is_off_machine(b) for b in inproc] == [False] * len(inproc)
