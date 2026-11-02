"""D-LLM-9 (issue #104): bare qwen3:4b and qwen3:30b-a3b now resolve to Thinking-2507 builds that
cannot stop thinking, so every local profile pins an explicit instruct tag. Covers the catalog,
the upgrade path for an install with the old bases, the lone </think> strip, the doctor's
thinking check, and the clean rebuild Modelfile."""

from __future__ import annotations

import httpx
import pytest

from kestrel.config import get_settings
from kestrel.doctor import FAIL, PASS, WARN, DoctorReport, _check_thinking
from kestrel.llm import client as llm_client
from kestrel.llm import guard, profiles
from kestrel.services import model_tasks as mt
from kestrel.services import setup_state as ss
from tests.test_setup_page import HW_4080, FakeOllama, _ollama, quiet  # noqa: F401

INSTRUCT_4B = "qwen3:4b-instruct-2507-q4_K_M"
INSTRUCT_30B = "qwen3:30b-a3b-instruct-2507-q4_K_M"
BARE = {"qwen3:4b", "qwen3:30b-a3b"}


def _local_profiles():
    cat = profiles.load_catalog()
    return cat, [(n, cat.profile(n)) for n in cat.names() if not cat.profile(n).hosted]


# ------------------------------------------------------------------------ the catalog
def test_every_local_profile_pins_instruct_tags_and_none_uses_a_bare_tag():
    cat, local = _local_profiles()
    assert local
    for name, prof in local:
        for tier in ("tier1", "tier2", "tier3", "docsel"):
            base = prof.models.for_tier(tier)
            assert base not in BARE, (name, tier, base)
        assert prof.models.for_tier("tier1") == INSTRUCT_4B, name
        assert prof.models.for_tier("docsel") == prof.models.for_tier("tier2"), name  # D-LLM-10
        for tier in ("tier2", "tier3"):
            if "30b" in (prof.models.for_tier(tier) or ""):
                assert prof.models.for_tier(tier) == INSTRUCT_30B, name
        for tag in prof.models.base_tags():
            assert tag in cat.model_sizes_gb, (name, tag)
    assert cat.model_sizes_gb[INSTRUCT_4B] == 2.5 and cat.model_sizes_gb[INSTRUCT_30B] == 19.0
    assert not (BARE & set(cat.model_sizes_gb))


def test_shipped_modelfiles_use_the_pinned_base_and_tags_with_dashes_slug_cleanly():
    from importlib import resources

    text = (resources.files("kestrel.resources") / "models" / "Modelfile.tier1").read_text()
    assert f"FROM {INSTRUCT_4B}" in text
    from kestrel.web.models_view import model_billions
    from kestrel.web.pages.models_ui import derived_name

    assert model_billions(INSTRUCT_4B) == "4 billion"
    assert model_billions(INSTRUCT_30B) == "30 billion"
    assert derived_name("tier1", INSTRUCT_4B) == "kestrel-tier1-qwen3-4b-instruct-2507-q4-k-m"


# ------------------------------------------------------------------- the upgrade path
OLD_TAGS = ["qwen3:4b", "qwen3:14b", "nomic-embed-text",
            "kestrel-tier1", "kestrel-tier2", "kestrel-tier3", "kestrel-docsel"]


def test_an_install_with_the_old_bases_sees_one_missing_download_and_keeps_its_tags(quiet):  # noqa: F811
    profiles.save_active_profile("gpu_16gb", "built before the pin", HW_4080)  # no bases recorded
    _ollama(True, OLD_TAGS)
    before = list(FakeOllama.state["tags"])
    st = ss.assess(get_settings(), {})
    assert st.profile == "gpu_16gb"
    assert st.missing_tags == [INSTRUCT_4B]  # one download; qwen3:14b and the embedder are there
    assert st.stale_derived == ["kestrel-tier1"]
    assert set(st.stale_derived) <= set(st.missing_derived)  # the rebuild is on the checklist
    assert FakeOllama.state["tags"] == before  # nothing was removed or retagged
    from pathlib import Path

    src = (Path(mt.__file__)).read_text(encoding="utf-8")
    assert "/api/delete" not in src and "ollama rm" not in src


def test_a_rebuilt_install_is_no_longer_stale(quiet):  # noqa: F811
    profiles.save_active_profile(
        "gpu_16gb", "rebuilt", HW_4080, bases=profiles.plan_bases("gpu_16gb"))
    _ollama(True, OLD_TAGS + [INSTRUCT_4B])
    st = ss.assess(get_settings(), {})
    assert st.missing_tags == [] and st.stale_derived == [] and st.missing_derived == []


@pytest.mark.asyncio
async def test_the_rebuild_writes_only_the_rendered_modelfile(sm):
    """The owner's rebuild copied the old Thinking model's TEMPLATE onto the instruct base and
    the reply came back as "assistant\\nhi". A rebuild sends render_modelfile's text only."""
    async with sm() as s:
        await mt.queue_builds(s, "gpu_16gb", "upgrade")
        tasks = await mt.live_tasks(s)
    builds = [t for t in tasks if t.type == mt.CREATE_TYPE]
    assert len(builds) == len(profiles.DERIVED_TIERS)
    for t in builds:
        text = t.payload["modelfile"]
        assert text == profiles.render_modelfile(t.payload["tier"], "gpu_16gb")
        assert t.payload["base"] and f"FROM {t.payload['base']}" in text
        keys = {ln.split()[0] for ln in text.splitlines() if ln.strip() and ln[0] != "#"}
        assert keys <= {"FROM", "PARAMETER"}, keys
        assert "TEMPLATE" not in text and "SYSTEM" not in text and "LICENSE" not in text


def test_the_build_path_never_asks_ollama_for_the_old_modelfile():
    from pathlib import Path

    src = Path(mt.__file__).read_text(encoding="utf-8")
    assert "modelfile" in src
    assert "api/show" not in src  # nothing is copied from `ollama show --modelfile`


# --------------------------------------------------------------- the lone </think> strip
def test_a_lone_closing_think_drops_everything_before_it():
    raw = "Hmm, the user wants a word.\nI should say hi.</think>hi"
    assert llm_client.strip_think_text(raw) == "hi"
    assert llm_client.OllamaClient.strip_think(raw) == "hi"
    assert guard.strip_think(raw) == "hi"
    assert llm_client.LLMClient.strip_think(raw) == "hi"


def test_paired_spans_and_plain_text_still_strip_as_before():
    assert llm_client.strip_think_text("<think>a</think>answer") == "answer"
    assert llm_client.strip_think_text('{"a": 1}') == '{"a": 1}'
    assert llm_client.strip_think_text("x <think>a</think> y</think>z") == "z"


# --------------------------------------------------------------- the doctor thinking check
class _Client:
    def __init__(self, reply=None, tokens=2, exc=None):
        self.reply, self.tokens, self.exc = reply, tokens, exc
        self.sent: list[dict] = []

    def chat_with_usage(self, model, messages, **kw):
        self.sent.append({"messages": messages, **kw})
        if self.exc:
            raise self.exc
        return self.reply, self.tokens

    @staticmethod
    def strip_think(text):
        return llm_client.strip_think_text(text)


def _run(client):
    s = get_settings()
    present = {s.llm.tiers["tier1"].model}
    rep = DoctorReport()
    _check_thinking(rep, client, s, present)
    return [r for r in rep.results if r.name.startswith("thinking off")]


def test_thinking_check_fails_on_reasoning_prose():
    client = _Client("Hmm, the user asked me to reply with just the word hi, so I should", 32)
    rows = _run(client)
    assert rows and all(r.status == FAIL for r in rows)
    assert "thinks before answering" in rows[0].detail
    assert "corrected model" in rows[0].remediation
    sent = client.sent[0]
    assert sent["messages"][0]["content"] == "Reply with just the word hi"
    assert sent["max_tokens"] <= 64 and sent["reasoning_effort"] == "none"


def test_thinking_check_fails_when_the_word_is_right_but_the_tokens_are_far_too_many():
    assert all(r.status == FAIL for r in _run(_Client("hi", tokens=60)))


def test_thinking_check_passes_on_hi():
    assert all(r.status == PASS for r in _run(_Client(" Hi.\n", tokens=2)))


def test_thinking_check_only_warns_on_a_5xx_or_a_timeout():
    req = httpx.Request("POST", "http://x/v1/chat/completions")
    boom = httpx.HTTPStatusError("500", request=req, response=httpx.Response(500, request=req))
    assert all(r.status == WARN for r in _run(_Client(exc=boom)))
    assert all(r.status == WARN for r in _run(_Client(exc=httpx.ReadTimeout("slow"))))


def test_every_profiles_docsel_base_equals_its_tier2_base_and_pulls_once():
    """D-LLM-10: the import model is the profile's own tier2 base. Two derived models built from
    one base pull it once (base_tags stays unique)."""
    cat, local = _local_profiles()
    for name, prof in local:
        assert prof.models.for_tier("docsel") == prof.models.for_tier("tier2"), name
        tags = prof.models.base_tags()
        assert len(tags) == len(set(tags)), name
        assert tags.count(prof.models.for_tier("docsel")) == 1, name
    assert cat.profile("gpu_16gb").models.for_tier("docsel") == "qwen3:14b"
    assert cat.profile("cpu_8gb").models.for_tier("docsel") == "qwen3:8b"
    assert cat.profile("gpu_24gb").models.for_tier("docsel") == INSTRUCT_30B


def test_docsel_calls_the_tier2_derived_model_and_no_profile_builds_a_docsel_model():
    """D-LLM-10 amendment: two derived models with different contexts load as two runners, so
    document selection calls kestrel-tier2 (whose num_ctx exceeds its budget)."""
    from kestrel.doctor import required_num_ctx

    s = get_settings()
    assert s.llm.tiers["docsel"].model == profiles.derived_name("tier2")
    assert "docsel" not in profiles.DERIVED_TIERS
    cat, local = _local_profiles()
    for name, prof in local:
        assert "kestrel-docsel" not in [profiles.derived_name(t) for t in profiles.DERIVED_TIERS]
        assert prof.num_ctx.for_tier("tier2") >= required_num_ctx("docsel", s), name
    from importlib import resources

    assert not (resources.files("kestrel.resources") / "models" / "Modelfile.docsel").is_file()


# ------------------------------------------- the upgrade notice, the owed rebuild (D-LLM-9)
def _old_install_app():
    from tests.test_setup_page import _fresh_app

    profiles.save_active_profile("gpu_16gb", "built before the pin", HW_4080)
    _ollama(True, OLD_TAGS)
    return _fresh_app()


def test_the_upgrade_notice_has_a_button_that_queues_the_download_and_owes_the_rebuild(quiet):  # noqa: F811
    client = _old_install_app()
    html = client.get("/announcements").text
    assert "answers without thinking out loud" in html and "About 2.5 GB" in html
    assert 'hx-post="/announcements/model-upgrade/download"' in html  # a button, not a link
    assert "<a " not in html.split("announcement-action")[0].split("sf-notice")[-1]
    shown = client.post("/announcements/model-upgrade/download").text
    assert "Downloading" in shown
    assert "model-upgrade/download" not in shown  # no second press while it runs
    import asyncio

    from kestrel.db.engine import get_sessionmaker

    async def read():
        async with get_sessionmaker()() as s:
            return await mt.live_tasks(s), await mt.rebuild_owed(s)

    tasks, owed = asyncio.run(read())
    assert [t.payload["tag"] for t in tasks if t.type == mt.PULL_TYPE] == [INSTRUCT_4B]
    assert owed is True


def test_dismissing_the_upgrade_notice_hides_only_the_notice(quiet):  # noqa: F811
    client = _old_install_app()
    assert "model-upgrade" in client.get("/announcements").text
    client.post("/announcements/model-upgrade/dismiss")
    assert "answers without thinking out loud" not in client.get("/announcements").text


async def test_dismissing_the_notice_never_hides_the_general_models_ask(sm, quiet):  # noqa: F811
    from kestrel.analytics.today import system_asks
    from kestrel.db.models import Setting
    from kestrel.web import announcements

    _ollama(True, ["qwen3:14b", INSTRUCT_4B, "nomic-embed-text"])  # derived models missing
    async with sm() as s:
        s.add(Setting(key=announcements.DISMISSED_KEY, value=["model-upgrade"]))
        await s.commit()
        asks = await system_asks(s, get_settings(), None)
    assert "models" in [a["key"] for a in asks]


async def test_a_tier_set_to_the_persons_own_model_is_not_asked_to_download(quiet):  # noqa: F811
    from kestrel.analytics.today import model_upgrade_gb

    profiles.save_active_profile("gpu_16gb", "built before the pin", HW_4080)
    _ollama(True, OLD_TAGS)
    assert await model_upgrade_gb(get_settings()) == 2.5
    mine = get_settings().model_copy(deep=True)
    mine.llm.tiers["tier1"].model = "my-own-model"
    assert await model_upgrade_gb(mine) == 0.0


async def test_the_owed_rebuild_waits_for_the_pull_survives_a_restart_and_clears(sm, quiet):  # noqa: F811
    from sqlalchemy.ext.asyncio import async_sessionmaker

    profiles.save_active_profile("gpu_16gb", "built before the pin", HW_4080)
    _ollama(True, OLD_TAGS)  # the corrected model is not here yet
    async with sm() as s:
        await mt.set_rebuild_owed(s, True)
    restarted = async_sessionmaker(sm.kw["bind"], expire_on_commit=False)  # a fresh process
    assert await mt.finish_owed_rebuild(restarted) is False
    async with restarted() as s:
        assert await mt.rebuild_owed(s) is True  # still owed
    _ollama(True, OLD_TAGS + [INSTRUCT_4B])  # the pull finished
    ss.forget_ollama_probe()
    assert await mt.finish_owed_rebuild(restarted) is True
    async with restarted() as s:
        assert await mt.rebuild_owed(s) is False
        builds = [t for t in await mt.live_tasks(s) if t.type == mt.CREATE_TYPE]
    assert len(builds) == len(profiles.DERIVED_TIERS)
    assert await mt.finish_owed_rebuild(restarted) is False  # nothing owed any more
