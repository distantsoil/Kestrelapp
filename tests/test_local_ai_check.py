"""Issue #166 M1: Local AI settings spot check tests.
Verifies that the doctor's Local AI checks flag non-loopback backend addresses,
non-loopback OLLAMA_HOST, and cloud model tags."""

from __future__ import annotations

from unittest.mock import patch

from kestrel.doctor import (
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
