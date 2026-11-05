"""Issue #166 M1: Local AI settings spot check tests.
Verifies that the doctor's Local AI checks flag non-loopback backend addresses,
non-loopback OLLAMA_HOST, and cloud model tags."""

from __future__ import annotations

import re
from unittest.mock import patch

import psutil
import pytest

from kestrel.doctor import (
    INFO,
    PASS,
    WARN,
    DoctorReport,
    _check_cloud_model_tags,
    _check_local_ai_settings,
    _is_loopback,
)


class _FakeClient:
    """Mock OllamaClient for testing cloud model tag detection."""

    def __init__(self, tags: list[str]):
        self._tags = tags

    def list_models(self, timeout: float = 10.0) -> list[str]:
        return self._tags


# ---------------------------------------------------------------------------
# _is_loopback
# ---------------------------------------------------------------------------


def test_is_loopback_127():
    assert _is_loopback("http://127.0.0.1:11434")


def test_is_loopback_localhost():
    assert _is_loopback("http://localhost:11434")


def test_is_loopback_ipv6():
    assert _is_loopback("http://[::1]:11434")


@pytest.mark.parametrize("value", ["::1", "::1:11434", "[::1]", "0:0:0:0:0:0:0:1"])
def test_a_bare_ipv6_literal_is_read_whole(value):
    """An unbracketed value with more than one colon is an IPv6 literal, not host:port."""
    assert _is_loopback(value) is (value != "::1:11434")


def test_a_bare_public_ipv6_literal_is_not_loopback():
    assert not _is_loopback("2001:db8::1")


def test_is_loopback_public_address():
    assert not _is_loopback("http://192.168.1.10:11434")


def test_is_loopback_domain():
    assert not _is_loopback("http://ollama.example.com:11434")


def test_is_loopback_0_0_0_0():
    # 0.0.0.0 is not loopback; it binds to all interfaces.
    assert not _is_loopback("http://0.0.0.0:11434")


# ---------------------------------------------------------------------------
# _check_local_ai_settings
# ---------------------------------------------------------------------------


def _settings(base_url: str = "http://127.0.0.1:11434"):
    """Build a minimal settings object with the attributes the check needs."""
    from types import SimpleNamespace

    return SimpleNamespace(
        llm=SimpleNamespace(
            backends={"local": SimpleNamespace(base_url=base_url)},
        )
    )


def test_backend_loopback_passes():
    rep = DoctorReport()
    s = _settings("http://127.0.0.1:11434")
    with patch.dict("os.environ", {}, clear=True):
        _check_local_ai_settings(rep, s)
    row = next(r for r in rep.results if r.name == "Local AI: backend address")
    assert row.status == PASS


def test_backend_public_warns():
    rep = DoctorReport()
    s = _settings("http://192.168.1.10:11434")
    with patch.dict("os.environ", {}, clear=True):
        _check_local_ai_settings(rep, s)
    row = next(r for r in rep.results if r.name == "Local AI: backend address")
    assert row.status == WARN
    assert "192.168.1.10" in row.detail


def test_ollama_host_unset_passes():
    rep = DoctorReport()
    s = _settings()
    with patch.dict("os.environ", {}, clear=True):
        _check_local_ai_settings(rep, s)
    row = next(r for r in rep.results if r.name == "Local AI: Ollama bind")
    assert row.status == PASS


def test_ollama_host_loopback_passes():
    rep = DoctorReport()
    s = _settings()
    with patch.dict("os.environ", {"OLLAMA_HOST": "127.0.0.1:11434"}):
        _check_local_ai_settings(rep, s)
    row = next(r for r in rep.results if r.name == "Local AI: Ollama bind")
    assert row.status == PASS


def test_ollama_host_non_loopback_warns():
    rep = DoctorReport()
    s = _settings()
    with patch.dict("os.environ", {"OLLAMA_HOST": "0.0.0.0:11434"}):
        _check_local_ai_settings(rep, s)
    row = next(r for r in rep.results if r.name == "Local AI: Ollama bind")
    assert row.status == WARN
    assert "0.0.0.0" in row.detail


# ---------------------------------------------------------------------------
# _check_cloud_model_tags
# ---------------------------------------------------------------------------


def test_no_cloud_tags_pass():
    rep = DoctorReport()
    client = _FakeClient(["qwen3:8b", "llama3.1:7b", "nomic-embed-text"])
    _check_cloud_model_tags(rep, client)
    row = next(r for r in rep.results if r.name == "Local AI: cloud model tags")
    assert row.status == PASS


def test_cloud_tag_warns():
    rep = DoctorReport()
    client = _FakeClient(["qwen3:8b", "llama3.1:7b-cloud"])
    _check_cloud_model_tags(rep, client)
    row = next(r for r in rep.results if r.name == "Local AI: cloud model tags")
    assert row.status == WARN
    assert "llama3.1:7b-cloud" in row.detail


def test_multiple_cloud_tags_all_listed():
    rep = DoctorReport()
    client = _FakeClient(["a:1-cloud", "b:2-cloud", "c:3"])
    _check_cloud_model_tags(rep, client)
    row = next(r for r in rep.results if r.name == "Local AI: cloud model tags")
    assert row.status == WARN
    assert "a:1-cloud" in row.detail
    assert "b:2-cloud" in row.detail


def test_client_error_is_silent():
    """If Ollama is not reachable, the check is skipped (already reported elsewhere)."""

    class _BrokenClient:
        def list_models(self, timeout: float = 10.0) -> list[str]:
            raise ConnectionError("Ollama not running")

    rep = DoctorReport()
    _check_cloud_model_tags(rep, _BrokenClient())
    # No new rows added.
    assert all(r.name != "Local AI: cloud model tags" for r in rep.results)


# ---------------------------------------------------------------------------
# _check_local_ai_egress (issue #166 M1 connection spot check)
# ---------------------------------------------------------------------------


def test_egress_clean_result():
    """A clean egress result (no connections) reports PASS."""
    from kestrel.doctor import _check_local_ai_egress
    from kestrel.services.local_ai_egress import EgressResult, format_egress_result

    result = EgressResult(duration_s=3.0)
    assert result.clean
    text = format_egress_result(result)
    assert "No outbound connections" in text
    assert "spot check" in text

    rep = DoctorReport()
    with patch("kestrel.services.local_ai_egress.sample_connections", return_value=result):
        _check_local_ai_egress(rep)
    row = next(r for r in rep.results if r.name == "Local AI: egress")
    assert row.status == PASS


def test_egress_public_connection_warns():
    """A public connection is flagged as WARN."""
    from kestrel.doctor import _check_local_ai_egress
    from kestrel.services.local_ai_egress import (
        ConnectionSample,
        EgressResult,
        format_egress_result,
    )

    result = EgressResult(
        samples=[
            ConnectionSample(
                pid=1234, proc_name="ollama", remote_addr="93.184.216.34",
                remote_port=443, classification="public",
            )
        ],
        duration_s=3.0,
    )
    text = format_egress_result(result)
    assert "public address" in text
    assert "93.184.216.34" in text

    rep = DoctorReport()
    with patch("kestrel.services.local_ai_egress.sample_connections", return_value=result):
        _check_local_ai_egress(rep)
    row = next(r for r in rep.results if r.name == "Local AI: egress")
    assert row.status == WARN


def test_egress_private_connection_warns():
    """A private network connection is flagged."""
    from kestrel.doctor import _check_local_ai_egress
    from kestrel.services.local_ai_egress import (
        ConnectionSample,
        EgressResult,
        format_egress_result,
    )

    result = EgressResult(
        samples=[
            ConnectionSample(
                pid=1234, proc_name="ollama", remote_addr="192.168.1.50",
                remote_port=8080, classification="private",
            )
        ],
        duration_s=3.0,
    )
    text = format_egress_result(result)
    assert "another computer on your network" in text

    rep = DoctorReport()
    with patch("kestrel.services.local_ai_egress.sample_connections", return_value=result):
        _check_local_ai_egress(rep)
    row = next(r for r in rep.results if r.name == "Local AI: egress")
    assert row.status in (WARN, INFO)


def test_egress_no_ollama_processes():
    """If no Ollama processes are found, the result has an error message."""
    from kestrel.doctor import _check_local_ai_egress
    from kestrel.services.local_ai_egress import EgressResult, format_egress_result

    result = EgressResult(duration_s=3.0, error="No Ollama processes found.")
    text = format_egress_result(result)
    assert "Could not check" in text

    rep = DoctorReport()
    with patch("kestrel.services.local_ai_egress.sample_connections", return_value=result):
        _check_local_ai_egress(rep)
    row = next(r for r in rep.results if r.name == "Local AI: egress")
    assert row.status == WARN


def test_classify_address_loopback():
    from kestrel.services.local_ai_egress import _classify_address

    assert _classify_address("127.0.0.1", 8080) == "loopback"
    assert _classify_address("::1", 8080) == "loopback"


def test_classify_address_private():
    from kestrel.services.local_ai_egress import _classify_address

    assert _classify_address("192.168.1.50", 8080) == "private"
    assert _classify_address("10.0.0.1", 443) == "private"


def test_classify_address_public():
    from kestrel.services.local_ai_egress import _classify_address

    assert _classify_address("8.8.8.8", 443) == "public"
    assert _classify_address("1.1.1.1", 53) == "public"


# ---------------------------------------------------------------------------
# _check_model_digests (issue #166 M2)
# ---------------------------------------------------------------------------


def _catalog() -> dict:
    import json
    from importlib.resources import files

    text = files("kestrel.resources.config").joinpath("model_digests.json").read_text("utf-8")
    return json.loads(text)["models"]


def _bare(digest: str) -> str:
    """Ollama's /api/tags reports a digest as bare hex, with no "sha256:" prefix."""
    return digest.removeprefix("sha256:")


def test_digests_all_match():
    """Every installed model carries the catalog's own digest: PASS."""
    from kestrel.doctor import _check_model_digests

    catalog = _catalog()

    class _Client:
        def list_models_with_digests(self, timeout: float = 10.0):
            return [{"name": name, "digest": meta["digest"]} for name, meta in catalog.items()]

    rep = DoctorReport()
    _check_model_digests(rep, _Client())
    row = next(r for r in rep.results if r.name == "Local AI: model digests")
    assert row.status == PASS
    assert "match" in row.detail.lower()


def test_digests_in_bare_hex_as_ollama_reports_them_match():
    """The owner's PC returns qwen3:8b as bare hex; the catalog stores "sha256:<hex>"."""
    from kestrel.doctor import _check_model_digests

    catalog = _catalog()
    assert "qwen3:8b" in catalog
    assert _bare(catalog["qwen3:8b"]["digest"]) == (
        "500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41"
    )

    class _Client:
        def list_models_with_digests(self, timeout: float = 10.0):
            return [{"name": n, "digest": _bare(m["digest"]).upper() if i == 0 else
                     _bare(m["digest"])} for i, (n, m) in enumerate(catalog.items())]

    rep = DoctorReport()
    _check_model_digests(rep, _Client())
    row = next(r for r in rep.results if r.name == "Local AI: model digests")
    assert row.status == PASS, row.detail
    assert f"{len(catalog)} model(s) match" in row.detail


def test_digests_mismatch_warns():
    """A local digest differs from the catalog: WARN (tag has moved)."""
    from kestrel.doctor import _check_model_digests

    catalog = _catalog()
    moved = next(iter(catalog))

    class _Client:
        def list_models_with_digests(self, timeout: float = 10.0):
            return [
                {"name": name,
                 "digest": "ab12" * 16 if name == moved else _bare(meta["digest"])}
                for name, meta in catalog.items()
            ]

    rep = DoctorReport()
    _check_model_digests(rep, _Client())
    row = next(r for r in rep.results if r.name == "Local AI: model digests")
    assert row.status == WARN
    assert "differ" in row.detail.lower()
    assert moved in row.detail


def test_digests_ollama_unreachable():
    """If Ollama is not reachable, the check is skipped."""
    from kestrel.doctor import _check_model_digests

    class _BrokenClient:
        def list_models_with_digests(self, timeout: float = 10.0):
            raise ConnectionError("Ollama not running")

    rep = DoctorReport()
    _check_model_digests(rep, _BrokenClient())
    # No new row for digests (Ollama not reachable is already reported).
    assert all(r.name != "Local AI: model digests" for r in rep.results)


# ---------------------------------------------------------------------------
# Through the doctor's public entry point (run_doctor), with the machine faked
# ---------------------------------------------------------------------------


def _no_ollama(monkeypatch) -> None:
    """A computer where Ollama does not answer: the settings and digest rows still run."""
    import httpx

    from kestrel import doctor as d

    def refused(self, *a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(d.OllamaClient, "version", refused)
    monkeypatch.setattr(d.OllamaClient, "list_models", refused)
    monkeypatch.setattr(d.OllamaClient, "list_models_with_digests", refused)
    monkeypatch.setattr(d, "_check_browser", lambda rep: rep.add("Browser (PDF)", PASS, "x"))


def _bind_row(monkeypatch, host: str):
    from kestrel import doctor as d

    _no_ollama(monkeypatch)
    monkeypatch.setenv("OLLAMA_HOST", host)
    rep = d.run_doctor(quick=True)
    return next(r for r in rep.results if r.name == "Local AI: Ollama bind")


@pytest.mark.parametrize("host", [
    "http://localhost.evil.example:11434",
    "http://127.0.0.1.evil.example:11434",
    "http://localhost@evil.example:11434",
    "http://[::1].evil.example:11434",
    # OLLAMA_HOST is often set without a scheme; a prefix match must not pass these.
    "localhost.evil.example:11434",
    "127.0.0.1.evil.example:11434",
])
def test_a_look_alike_host_is_not_reported_as_local(monkeypatch, host):
    row = _bind_row(monkeypatch, host)
    assert row.status == WARN, f"{host} was treated as local: {row.detail}"


@pytest.mark.parametrize("host", [
    "http://127.0.0.1:11434", "http://[::1]:11434", "http://localhost:11434", "127.0.0.1:11434",
])
def test_real_loopback_hosts_are_reported_as_local(monkeypatch, host):
    assert _bind_row(monkeypatch, host).status == PASS


def _ollama_up_for_digests(monkeypatch) -> None:
    """Ollama answers, so the digest row runs; other checks that would ask Ollama are no-ops."""
    from kestrel import doctor as d

    _no_ollama(monkeypatch)
    monkeypatch.setattr(d, "_check_ollama", lambda rep, client: True)
    monkeypatch.setattr(d, "_check_models_present", lambda rep, client, s: [])
    monkeypatch.setattr(d, "_check_num_ctx", lambda *a, **k: None)
    monkeypatch.setattr(d, "check_model_fit", lambda *a, **k: None)
    monkeypatch.setattr(d, "_check_cloud_model_tags", lambda rep, client: None)

def test_every_shipped_digest_is_a_real_looking_sha256():
    models = _catalog()
    assert models
    for tag, meta in models.items():
        digest = meta["digest"]
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest), f"{tag}: {digest!r}"
        body = digest.split(":", 1)[1]
        assert len(set(body)) > 1, f"{tag}: placeholder digest {digest!r}"


def test_only_the_models_kestrel_builds_are_exempt_from_unknown(monkeypatch):
    from kestrel import doctor as d
    from kestrel.llm.profiles import DERIVED_TIERS, derived_name

    _ollama_up_for_digests(monkeypatch)
    catalog = _catalog()
    installed = [{"name": n, "digest": _bare(m["digest"])} for n, m in catalog.items()]
    installed += [{"name": f"{derived_name(t)}:latest", "digest": "7b1e" * 16}
                  for t in DERIVED_TIERS]
    # A model picked on the Models page, and the document selection model older builds made.
    own_extra = ["kestrel-tier2-example-model-7b", "kestrel-docsel"]
    installed += [{"name": f"{n}:latest", "digest": "7b1e" * 16} for n in own_extra]
    installed.append({"name": "kestrel-impostor:latest", "digest": "5a3c" * 16})
    monkeypatch.setattr(d.OllamaClient, "list_models_with_digests",
                        lambda self, timeout=10.0: installed)
    rep = d.run_doctor(quick=True)
    row = next(r for r in rep.results if r.name == "Local AI: model digests")
    text = f"{row.detail} {row.remediation or ''}"
    assert "kestrel-impostor" in text and "unknown" in text.lower()
    for t in DERIVED_TIERS:
        assert derived_name(t) not in text
    for n in own_extra:
        assert n not in text


def test_an_installed_tag_missing_from_the_catalog_is_reported_unknown(monkeypatch):
    from kestrel import doctor as d

    _ollama_up_for_digests(monkeypatch)
    catalog = _catalog()
    installed = [{"name": n, "digest": m["digest"]} for n, m in catalog.items()]
    installed.append({"name": "mystery-model:1b", "digest": "sha256:" + "5a3c" * 16})
    monkeypatch.setattr(d.OllamaClient, "list_models_with_digests",
                        lambda self, timeout=10.0: installed)
    rep = d.run_doctor(quick=True)
    row = next(r for r in rep.results if r.name == "Local AI: model digests")
    text = f"{row.detail} {row.remediation or ''}".lower()
    assert "mystery-model:1b" in text and "unknown" in text


def test_kestrels_own_derived_models_are_never_reported_unknown(monkeypatch):
    from kestrel import doctor as d

    _ollama_up_for_digests(monkeypatch)
    catalog = _catalog()
    installed = [{"name": n, "digest": m["digest"]} for n, m in catalog.items()]
    installed.append({"name": "kestrel-tier1:latest", "digest": "sha256:" + "7b1e" * 16})
    monkeypatch.setattr(d.OllamaClient, "list_models_with_digests",
                        lambda self, timeout=10.0: installed)
    rep = d.run_doctor(quick=True)
    row = next(r for r in rep.results if r.name == "Local AI: model digests")
    assert "kestrel-tier1" not in f"{row.detail} {row.remediation or ''}"


class _DeniedProcess:
    """An Ollama process whose connections the operating system will not let Kestrel list."""

    pid = 4242
    info = {"pid": 4242, "name": "ollama"}

    def name(self):
        return "ollama"

    def __getattr__(self, attr):
        def denied(*a, **k):
            raise psutil.AccessDenied(pid=4242, name="ollama")

        return denied


def test_when_connections_cannot_be_listed_the_egress_row_says_unverified(monkeypatch):
    from kestrel import doctor as d

    _no_ollama(monkeypatch)
    monkeypatch.setattr(d.OllamaClient, "version", lambda self, timeout=5.0: "0.5.0")
    monkeypatch.setattr(d.OllamaClient, "list_models", lambda self, timeout=10.0: [])
    monkeypatch.setattr(d.OllamaClient, "list_models_with_digests", lambda self, timeout=10.0: [])
    monkeypatch.setattr(d, "_check_models_present", lambda rep, client, s: set())
    monkeypatch.setattr(d, "_check_num_ctx", lambda *a, **k: None)
    monkeypatch.setattr(d, "_run_probes", lambda *a, **k: None)
    monkeypatch.setattr(d, "check_model_fit", lambda *a, **k: None)
    monkeypatch.setattr(
        psutil, "process_iter", lambda attrs=None, *a, **k: iter([_DeniedProcess()]))
    monkeypatch.setattr(psutil, "Process", lambda pid=None: _DeniedProcess())
    monkeypatch.setattr(psutil, "net_connections", _raise_denied, raising=False)
    rep = d.run_doctor(quick=False)
    row = next(r for r in rep.results if r.name == "Local AI: egress")
    assert row.status != PASS
    assert "unverified" in f"{row.detail}".lower()


def _raise_denied(*a, **k):
    raise psutil.AccessDenied(pid=4242, name="ollama")


def test_a_stopped_ollama_is_asked_once_not_once_per_local_ai_check(monkeypatch):
    import httpx

    from kestrel import doctor as d

    _no_ollama(monkeypatch)
    asked: list[str] = []

    def refused(self, *a, **k):
        asked.append("list")
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(d.OllamaClient, "list_models", refused)
    monkeypatch.setattr(d.OllamaClient, "list_models_with_digests", refused)
    rep = d.run_doctor(quick=True)
    assert asked == []
    rows = [r for r in rep.results if r.name == d._NEEDS_OLLAMA_ROW]
    assert len(rows) == 1 and "unverified" in rows[0].detail
