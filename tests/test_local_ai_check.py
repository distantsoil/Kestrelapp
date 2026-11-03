"""Issue #166 M1: Local AI settings spot check tests.
Verifies that the doctor's Local AI checks flag non-loopback backend addresses,
non-loopback OLLAMA_HOST, and cloud model tags."""

from __future__ import annotations

from unittest.mock import patch

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
    from kestrel.services.local_ai_egress import EgressResult, format_egrade_result

    result = EgressResult(duration_s=3.0)
    assert result.clean
    text = format_egrade_result(result)
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
        format_egrade_result,
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
    text = format_egrade_result(result)
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
        format_egrade_result,
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
    text = format_egrade_result(result)
    assert "another computer on your network" in text

    rep = DoctorReport()
    with patch("kestrel.services.local_ai_egress.sample_connections", return_value=result):
        _check_local_ai_egress(rep)
    row = next(r for r in rep.results if r.name == "Local AI: egress")
    assert row.status in (WARN, INFO)


def test_egress_no_ollama_processes():
    """If no Ollama processes are found, the result has an error message."""
    from kestrel.doctor import _check_local_ai_egress
    from kestrel.services.local_ai_egress import EgressResult, format_egrade_result

    result = EgressResult(duration_s=3.0, error="No Ollama processes found.")
    text = format_egrade_result(result)
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


def test_digests_all_match():
    """All local digests match the catalog: PASS."""
    from kestrel.doctor import _check_model_digests

    _MATCH_DIGEST = "sha256:" + "1" * 64

    class _Client:
        def list_models_with_digests(self, timeout: float = 10.0):
            return [
                {"name": "qwen3:8b", "digest": _MATCH_DIGEST},
                {"name": "qwen3:4b-instruct-2507-q4_K_M",
                 "digest": "sha256:" + "0" * 64},
                {"name": "qwen3:14b", "digest": "sha256:" + "2" * 64},
                {"name": "qwen3:30b-a3b-instruct-2507-q4_K_M",
                 "digest": "sha256:" + "3" * 64},
                {"name": "nomic-embed-text", "digest": "sha256:" + "4" * 64},
            ]

    rep = DoctorReport()
    _check_model_digests(rep, _Client())
    row = next(r for r in rep.results if r.name == "Local AI: model digests")
    assert row.status == PASS
    assert "match" in row.detail.lower()


def test_digests_mismatch_warns():
    """A local digest differs from the catalog: WARN (tag has moved)."""
    from kestrel.doctor import _check_model_digests

    class _Client:
        def list_models_with_digests(self, timeout: float = 10.0):
            return [
                {"name": "qwen3:8b", "digest": "sha256:" + "9" * 64},
                {"name": "qwen3:4b-instruct-2507-q4_K_M",
                 "digest": "sha256:" + "0" * 64},
                {"name": "qwen3:14b", "digest": "sha256:" + "2" * 64},
                {"name": "qwen3:30b-a3b-instruct-2507-q4_K_M",
                 "digest": "sha256:" + "3" * 64},
                {"name": "nomic-embed-text", "digest": "sha256:" + "4" * 64},
            ]

    rep = DoctorReport()
    _check_model_digests(rep, _Client())
    row = next(r for r in rep.results if r.name == "Local AI: model digests")
    assert row.status == WARN
    assert "differ" in row.detail.lower()


def test_digests_unknown_tag_not_in_catalog():
    """A local model not in the catalog is not flagged (only catalog models are checked)."""
    from kestrel.doctor import _check_model_digests

    class _Client:
        def list_models_with_digests(self, timeout: float = 10.0):
            return [
                {"name": "llama3:8b", "digest": "sha256:abcdef"},
            ]

    rep = DoctorReport()
    _check_model_digests(rep, _Client())
    row = next(r for r in rep.results if r.name == "Local AI: model digests")
    # No catalog models are installed; all are "not installed".
    assert "not installed" in row.detail.lower() or "not installed" in row.name


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
