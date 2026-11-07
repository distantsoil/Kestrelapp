"""#144 privacy: the addresses the Local AI connection check saw Ollama connect to never ride out
in a diagnostics bundle, in the normal or the conservative mode, while the row keeps its meaning.
The Ollama-service classification is covered here too (no network: the name lookup is mocked)."""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass

import pytest

from kestrel import doctor as doctor_mod
from kestrel.config import get_settings
from kestrel.services import diagnostics, local_ai_egress
from kestrel.services.local_ai_egress import (
    ConnectionSample,
    EgressResult,
    _classify_address,
    format_egress_result,
)

pytestmark = pytest.mark.usefixtures("_isolate_data_home")

PUBLIC = "203.0.113.9"
PRIVATE = "192.168.7.20"
SIX = "2001:db8::9"


@dataclass
class _Row:
    name: str
    status: str
    detail: str
    remediation: str = ""


@dataclass
class _Report:
    results: list
    has_blocking_failure: bool = False


def _observed_detail() -> str:
    result = EgressResult(samples=[
        ConnectionSample(1, "ollama", PUBLIC, 443, "public"),
        ConnectionSample(1, "ollama", PRIVATE, 8080, "private"),
        ConnectionSample(1, "ollama", SIX, 443, "public"),
    ])
    return format_egress_result(result)


def _bundle(tmp_path, monkeypatch, *, conservative: bool) -> dict[str, str]:
    from kestrel.services import setup_state

    monkeypatch.setattr(doctor_mod, "run_doctor", lambda *, quick=False: pytest.fail("saved"))
    setup_state.save_report(_Report([_Row("Local AI: connections", "WARN", _observed_detail())]))
    path = diagnostics.build_bundle(get_settings(), out_dir=tmp_path, conservative=conservative)
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n).decode("utf-8", "replace") for n in z.namelist()}


@pytest.mark.parametrize("conservative", [False, True])
def test_observed_addresses_never_appear_in_the_bundle(tmp_path, monkeypatch, conservative):
    members = _bundle(tmp_path, monkeypatch, conservative=conservative)
    for name, text in members.items():
        for needle in (PUBLIC, PRIVATE, SIX, "2001:db8"):
            assert needle not in text, f"{needle} leaked into {name}"
    detail = json.loads(members["doctor.json"])["results"][0]["detail"]
    assert "Ollama connected to a public address: <redacted>" in detail
    assert "Ollama connected to another computer on your network: <redacted>" in detail
    assert "(ollama)" in detail  # the rest of the sentence keeps its meaning


def test_the_scrubber_masks_addresses_and_ports_but_keeps_loopback_and_clock_times():
    scrub = diagnostics.make_scrubber(None)
    assert scrub(f"to {PUBLIC}:443 and [{SIX}]:11434 and {PRIVATE}") == (
        "to <redacted> and <redacted> and <redacted>")
    assert scrub("http://127.0.0.1:11434 and ::1 at 10:20:30 v10.0.19045.3693") == (
        "http://127.0.0.1:11434 and ::1 at 10:20:30 v10.0.19045.3693")


def test_weakening_the_ip_mask_would_leak(monkeypatch):
    """The guard check: with the mask off the same text leaks, so the test above is not vacuous."""
    monkeypatch.setattr(diagnostics, "mask_ip_addresses", lambda text: text)
    assert PUBLIC in diagnostics.make_scrubber(None)(f"to {PUBLIC}:443")


# ----------------------------------------------------------- Ollama's own services (no network)
def test_a_connection_to_an_ollama_service_ip_is_classified_apart():
    # (The documentation ranges such as 203.0.113.x count as private to the ipaddress module, so
    # the classification cases use globally routable example addresses.)
    real, other = "93.184.216.34", "8.8.8.8"
    service = frozenset({real})
    assert _classify_address(real, 443, service) == "ollama_service"
    assert _classify_address(real, 443) == "public"  # without the lookup: a plain public one
    assert _classify_address(other, 443, service) == "public"
    assert _classify_address(PRIVATE, 80, service) == "private"


def test_service_connections_are_listed_apart_and_are_not_a_public_address_warning():
    result = EgressResult(samples=[ConnectionSample(1, "ollama", PUBLIC, 443, "ollama_service")])
    assert result.public_connections == [] and len(result.ollama_service_connections) == 1
    text = format_egress_result(result)
    assert "Ollama's own services: 203.0.113.9:443" in text
    assert "public address" not in text


def test_the_service_name_lookup_resolves_the_three_hosts_and_is_mocked_here(monkeypatch):
    local_ai_egress._ollama_service_ips.cache_clear()
    looked_up: list[str] = []

    def fake_getaddrinfo(host, port, *a, **k):
        looked_up.append(host)
        return [(2, 1, 6, "", (PUBLIC, 0))]

    monkeypatch.setattr(local_ai_egress.socket, "getaddrinfo", fake_getaddrinfo)
    ips = local_ai_egress._ollama_service_ips()
    local_ai_egress._ollama_service_ips.cache_clear()
    assert ips == frozenset({PUBLIC})
    assert set(looked_up) == set(local_ai_egress._OLLAMA_SERVICE_HOSTS)


# ------------------------------------------- forms the first mask missed (0.7.6 review, item 1)
# An unbracketed full IPv6 address with its port, an address glued to a word character, and
# leading-zero IPv4 octets. Every one of them is an observed address and must never ride out.
HARD_FORMS = (
    "2a00:1450:4001:82f:1:2:3:4:443",
    "2606:4700:10:a:b:c:d:e:443",
    "ip_8.8.8.8",
    "ip_2001:db8::9",
    "08.08.08.08",
    "8.8.8.8_x",
)


@pytest.mark.parametrize("form", HARD_FORMS)
def test_hard_address_forms_are_masked(form):
    out = diagnostics.make_scrubber(None)(f"seen {form} today")
    for needle in ("8.8.8", "08.08", "2a00", "1450", "2606", "4700", "2001:db8"):
        assert needle not in out, f"{needle} survived in {out!r}"
    assert out.startswith("seen ") and out.endswith(" today")


def test_hard_forms_never_appear_in_the_bundle(tmp_path, monkeypatch):
    from kestrel.services import setup_state

    monkeypatch.setattr(doctor_mod, "run_doctor", lambda *, quick=False: pytest.fail("saved"))
    detail = "Could not check: " + " ".join(HARD_FORMS)
    setup_state.save_report(_Report([_Row("Local AI: connections", "WARN", detail)]))
    path = diagnostics.build_bundle(get_settings(), out_dir=tmp_path, conservative=False)
    with zipfile.ZipFile(path) as z:
        members = {n: z.read(n).decode("utf-8", "replace") for n in z.namelist()}
    for name, text in members.items():
        for needle in ("8.8.8.8", "08.08.08.08", "2a00:1450", "2606:4700", "2001:db8"):
            assert needle not in text, f"{needle} leaked into {name}"


def test_loopback_clock_times_and_versions_stay_intact():
    scrub = diagnostics.make_scrubber(None)
    kept = "127.0.0.1:11434 ::1 [::1]:80 0.0.0.0 at 10:20:30 2026-11-06T10:20:30.5 v1.2.3 1.2.3"
    assert scrub(kept) == kept
    assert scrub("127.000.000.001") == "127.000.000.001"


def test_the_egress_text_brackets_an_ipv6_address_before_its_port():
    full = "2a00:1450:4001:82f:1:2:3:4"
    text = format_egress_result(EgressResult(samples=[
        ConnectionSample(1, "ollama", full, 443, "public"),
        ConnectionSample(1, "ollama", PUBLIC, 443, "public"),
    ]))
    assert f"[{full}]:443 (ollama)" in text
    assert f"{PUBLIC}:443 (ollama)" in text
