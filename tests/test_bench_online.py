"""`kestrel bench` and online models (issue #208 round 3, D-SCORE-30).

INVARIANT: nothing goes to an online model from `kestrel bench` without the same consent the app
requires: the operator is shown what is sent to which service and confirms this run, or names
that service with --allow-online when no terminal can ask; and Confidential mode redacts the
employer as Tier 2 does. Invented text only; nothing reaches a network.
"""

from __future__ import annotations

import json

from kestrel.bench import harness
from kestrel.config import load_settings
from kestrel.llm.client import ChatResult
from kestrel.privacy import Employer, EmployerMap


def _online():
    s = load_settings().model_copy(deep=True)
    s.llm.tiers["tier2"] = s.llm.tiers["tier2"].model_copy(update={"backend": "frontier"})
    return s


def test_a_local_tier_2_needs_nothing():
    asked = []
    s = load_settings()
    assert harness.online_bench_refusal(s, allow=None, interactive=False,
                                        confirm=asked.append, show=asked.append) is None
    assert asked == []


def test_online_without_a_terminal_refuses_unless_the_service_is_named():
    s = _online()
    asked: list[str] = []
    why = harness.online_bench_refusal(s, allow=None, interactive=False, confirm=asked.append,
                                       show=asked.append)
    assert why and "--allow-online openrouter.ai" in why and "Nothing was sent." in why
    assert asked == []  # never asked, never sent
    assert "Nothing was sent." in harness.online_bench_refusal(
        s, allow="example.com", interactive=False, confirm=asked.append, show=asked.append)
    assert harness.online_bench_refusal(s, allow="OpenRouter.ai", interactive=False,
                                        confirm=asked.append, show=asked.append) is None


def test_online_in_a_terminal_shows_what_is_sent_and_needs_a_yes():
    s = _online()
    shown: list[str] = []

    def no(text: str) -> bool:
        shown.append(text)
        return False

    assert harness.online_bench_refusal(s, allow=None, interactive=True, confirm=no,
                                        show=shown.append) == (
        "Not confirmed. Nothing was sent.")
    assert "openrouter.ai" in shown[0]
    # It lists what the active version sends: no resume lines under v3, the default.
    assert "lines from your current resume" not in shown[0]
    # A run of evaluate/v4 lists them, whatever the app's own setting says (round 4).
    harness.online_bench_refusal(s, allow=None, interactive=True, confirm=no,
                                 show=shown.append, prompt_versions=["evaluate/v4"])
    assert "lines from your current resume" in shown[1]
    assert harness.online_bench_refusal(s, allow=None, interactive=True,
                                        confirm=lambda _t: True, show=shown.append) is None


class _Capture:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.sent: list[str] = []

    async def complete(self, *, tier, messages, **kw):
        self.sent.append("\n".join(m["content"] for m in messages))
        body = {"blocks": {}, "dimensions": {}, "holistic": 3.0}  # invalid: quarantined, fine
        return ChatResult(content=json.dumps(body), prompt_tokens=1, completion_tokens=1,
                          latency_ms=1, model="m")


async def test_confidential_mode_redacts_the_employer_in_a_bench_send(tmp_path):
    rows = [{"track": "employment", "title": "Planner", "org": "Gullwing Ports",
             "description_md": "Plan the cranes at Gullwing Ports for a busy harbor season.",
             "location": "Remote, US", "comp_min": 1, "comp_max": 2, "remote_type": "remote",
             "gold_verdict": "consider", "gold_score": 3.0}]
    (tmp_path / "goldset.employment.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    clients: list[_Capture] = []

    def make(s):
        clients.append(_Capture(s))
        return clients[-1]

    mapping = EmployerMap(employers=[Employer(name="Gullwing Ports", shown_as="a port operator")])
    await harness.run_bench(settings=load_settings(), models=["m"], tracks=("employment",),
                            gold_dir=tmp_path, make_client=make, redaction=mapping)
    sent = "\n".join(t for c in clients for t in c.sent)
    assert sent and "Gullwing" not in sent


def test_the_bench_command_refuses_an_online_run_with_no_terminal_and_sends_nothing(
    monkeypatch,
):
    from typer.testing import CliRunner

    from kestrel import cli

    async def online_settings():
        return _online()

    called: list[dict] = []

    async def no_run(**kw):
        called.append(kw)
        raise AssertionError("run_bench must not run")

    monkeypatch.setattr(cli, "_db_settings", online_settings)
    monkeypatch.setattr(harness, "run_bench", no_run)
    out = CliRunner().invoke(cli.app, ["bench", "--models", "some-model"])
    assert out.exit_code == 2, out.output
    assert "--allow-online openrouter.ai" in out.output and "Nothing was sent." in out.output
    assert called == []


def test_allow_online_still_shows_and_logs_what_is_sent_and_asks_nothing():
    """Round 5: --allow-online skips the question, never the disclosure."""
    from structlog.testing import capture_logs

    s = _online()
    shown: list[str] = []
    asked: list[str] = []
    with capture_logs() as logs:
        assert harness.online_bench_refusal(
            s, allow="openrouter.ai", interactive=False, confirm=asked.append,
            show=shown.append, prompt_versions=["evaluate/v4"]) is None
    assert asked == [] and len(shown) == 1
    assert "openrouter.ai" in shown[0] and "lines from your current resume" in shown[0]
    assert "each gold posting" in shown[0]
    logged = [e for e in logs if e["event"] == "bench_online_disclosure"]
    assert len(logged) == 1 and logged[0]["disclosure"] == shown[0]
    assert logged[0]["resume_lines"] is True
    # A v3 run says what v3 sends: no resume lines.
    harness.online_bench_refusal(s, allow="openrouter.ai", interactive=False,
                                 confirm=asked.append, show=shown.append,
                                 prompt_versions=["evaluate/v3"])
    assert "lines from your current resume" not in shown[1]
    # A wrong service name shows nothing and sends nothing.
    assert "Nothing was sent." in harness.online_bench_refusal(
        s, allow="example.com", interactive=False, confirm=asked.append, show=shown.append)
    assert len(shown) == 2


def test_the_bench_command_prints_the_disclosure_before_the_first_send(monkeypatch):
    """Round 5: a non-interactive v4 run with --allow-online prints exactly what goes where
    (including resume lines) before run_bench sends anything."""
    import typer
    from typer.testing import CliRunner

    from kestrel import cli
    from kestrel.pipeline import _shared, resume_digest

    async def online_settings():
        return _online()

    events: list[str] = []
    real_echo = typer.echo

    def echo(message=None, *a, **kw):
        events.append(f"echo:{message}")
        return real_echo(message, *a, **kw)

    class _Plan:
        refuse = None
        mapping = None

    async def plan(*_a, **_kw):
        return _Plan()

    async def digests():
        return {}

    async def stop_run(**kw):
        events.append("send")
        raise RuntimeError("stop here")

    monkeypatch.setattr(cli, "_db_settings", online_settings)
    monkeypatch.setattr(cli, "_stdin_is_terminal", lambda: False)
    monkeypatch.setattr(typer, "echo", echo)
    monkeypatch.setattr(_shared, "plan_hosted_send", plan)
    monkeypatch.setattr(resume_digest, "digests_for_tracks", digests)
    monkeypatch.setattr(harness, "run_bench", stop_run)
    monkeypatch.setattr(cli, "_warn_unfit_models", lambda *_a, **_kw: None)  # no local probe
    out = CliRunner().invoke(cli.app, ["bench", "--models", "some-model", "--prompt-version",
                                       "evaluate/v4", "--allow-online", "openrouter.ai"])
    assert "send" in events, (events, out.output, out.exception)
    before = events[: events.index("send")]
    shown = [e for e in before if "lines from your current resume" in e]
    assert shown and "openrouter.ai" in shown[0]
