"""The in-process embed engine (SPEC.md 8.1.1, D-EMBED-1..5).

Offline throughout. The engine is exercised against a tiny deterministic stub (a real `tokenizers`
WordLevel tokenizer and a fake ONNX session) so pooling, masking, batching and normalization are
checked without weights. One test runs the real bundled model and is skipped when the files are
not installed (a fresh checkout or CI): `uv run python scripts/fetch_embed_model.py` installs them.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import importlib.util
import io
import json
import tarfile
import threading
from pathlib import Path

import numpy as np
import pytest
import respx
from sqlalchemy import select

from kestrel import setup as setup_mod
from kestrel.config import get_settings, load_settings
from kestrel.db.models import Label, Opportunity, OpportunityEmbedding, Setting, Source, Task
from kestrel.llm import inprocess_embed as ipe
from kestrel.llm import profiles
from kestrel.llm import providers as prov
from kestrel.llm.client import ExternalBackendError, LLMClient
from kestrel.llm.embeddings import to_bytes
from kestrel.pipeline.embed import enqueue_stale_embeddings, handle_embed
from kestrel.pipeline.worker import TierStats, Worker
from kestrel.services.setting_overrides import EMBED_SWEPT_MODEL_KEY, nest_overrides
from tests.pipeline_fakes import FakeLLM

ROOT = Path(__file__).resolve().parent.parent
MODEL = ipe.BUNDLED.model_id


def _inprocess_settings():
    s = get_settings()
    s.llm.tiers["embed"] = s.llm.tiers["embed"].model_copy(
        update={"backend": "inprocess", "model": MODEL}
    )
    return s


# ------------------------------------------------------------------------------ stub engine
_VOCAB = ["[PAD]", "[UNK]", "alpha", "beta", "gamma", "delta"]
_DIM = 4


def _stub_tokenizer_json() -> str:
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace

    tok = Tokenizer(WordLevel({w: i for i, w in enumerate(_VOCAB)}, unk_token="[UNK]"))
    tok.pre_tokenizer = Whitespace()
    return tok.to_str()


class _FakeSession:
    """Stands in for onnxruntime.InferenceSession: token id i -> a fixed one-hot-ish row, so the
    expected mean-pooled vector is computable by hand. Padding rows are huge, so any pooling that
    forgets the mask is visibly wrong."""

    def __init__(self, *_a, **_k) -> None:
        self.calls = 0

    def get_outputs(self):
        class _O:
            name = "last_hidden_state"

        return [_O()]

    def get_inputs(self):
        class _I:
            def __init__(self, name):
                self.name = name

        return [_I("input_ids"), _I("attention_mask"), _I("token_type_ids")]

    def run(self, _outputs, feed):
        self.calls += 1
        assert set(feed) == {"input_ids", "attention_mask", "token_type_ids"}
        ids = feed["input_ids"]
        table = np.zeros((len(_VOCAB), _DIM), dtype=np.float32)
        table[0] = 1000.0  # [PAD]: must be masked out
        for i in range(2, len(_VOCAB)):
            table[i, (i - 2) % _DIM] = 1.0
        return [table[ids]]


@pytest.fixture
def stub_model(tmp_path, monkeypatch):
    """A model directory whose files match a BUNDLED patched to their hashes, with the ONNX session
    faked. Yields the directory."""
    d = tmp_path / "embed_model"
    d.mkdir()
    (d / "tokenizer.json").write_text(_stub_tokenizer_json(), encoding="utf-8")
    (d / "model.onnx").write_bytes(b"not really onnx")
    (d / "config.json").write_text("{}", encoding="utf-8")
    files = {n: ipe.sha256_file(d / n) for n in ("model.onnx", "tokenizer.json", "config.json")}
    monkeypatch.setattr(ipe, "BUNDLED", dataclasses.replace(ipe.BUNDLED, files=files))
    monkeypatch.setattr(ipe, "model_dir", lambda: d)
    monkeypatch.setattr(ipe, "_cache", {})
    import onnxruntime

    monkeypatch.setattr(onnxruntime, "InferenceSession", _FakeSession)
    yield d


def test_stub_engine_mean_pools_over_the_mask_and_normalizes(stub_model):
    vecs = ipe.embed_sync(["alpha", "alpha beta", "gamma delta delta"], model=MODEL)
    assert len(vecs) == 3 and all(len(v) == _DIM for v in vecs)
    np.testing.assert_allclose(vecs[0], [1, 0, 0, 0], atol=1e-6)
    np.testing.assert_allclose(vecs[1], np.array([1, 1, 0, 0]) / np.sqrt(2), atol=1e-6)
    expect = np.array([0, 0, 1, 2]) / np.sqrt(5)  # padding (1000s) contributed nothing
    np.testing.assert_allclose(vecs[2], expect, atol=1e-6)
    for v in vecs:
        assert abs(np.linalg.norm(v) - 1.0) < 1e-6


def test_stub_engine_batches_and_is_deterministic(stub_model):
    texts = [f"alpha {'beta ' * (i % 3)}" for i in range(70)]
    a = ipe.embed_sync(texts, model=MODEL)
    b = ipe.embed_sync(texts, model=MODEL)
    assert len(a) == 70 and a == b
    assert ipe.get_embedder()._session.calls == 6  # 3 batches of 32 per call


def test_engine_refuses_another_model_id(stub_model):
    with pytest.raises(ipe.EmbedModelMismatch, match=MODEL):
        ipe.embed_sync(["alpha"], model="nomic-embed-text")


def test_missing_model_fails_clearly_and_names_the_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(ipe, "model_dir", lambda: tmp_path / "nowhere")
    monkeypatch.setattr(ipe, "_cache", {})
    with pytest.raises(ipe.EmbedModelMissing, match="fetch_embed_model.py"):
        ipe.embed_sync(["anything"], model=MODEL)


def test_an_altered_file_is_refused(stub_model):
    (stub_model / "model.onnx").write_bytes(b"tampered")
    with pytest.raises(ipe.EmbedModelMissing, match="sha256 mismatch"):
        ipe.embed_sync(["alpha"], model=MODEL)


# ------------------------------------------------------------------------------- the client
@respx.mock(assert_all_mocked=True)
async def test_client_routes_inprocess_off_the_event_loop_and_sends_nothing(monkeypatch):
    """No route is mocked, so ANY HTTP request fails the test; the engine runs in a worker
    thread, not on the loop's thread."""
    seen: dict = {}

    def fake(texts, *, model):
        seen["thread"] = threading.get_ident()
        seen["model"] = model
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(ipe, "embed_sync", fake)
    vecs = await LLMClient(_inprocess_settings()).embed(["operator text"])
    assert vecs == [[1.0, 0.0]]
    assert seen["model"] == MODEL
    assert seen["thread"] != threading.get_ident()


@respx.mock
async def test_embed_still_never_reaches_an_external_backend(monkeypatch):
    """The privacy invariant holds with the new engine in place: an external embed backend is
    refused before any request, and the in-process engine is not consulted as a fallback."""
    route = respx.post("https://openrouter.ai/api/v1/embeddings")
    monkeypatch.setattr(ipe, "embed_sync", lambda *a, **k: pytest.fail("engine used"))
    s = get_settings()
    s.llm.tiers["embed"] = s.llm.tiers["embed"].model_copy(update={"backend": "frontier"})
    s.llm.hosted_mode.enabled = True
    s.llm.hosted_mode.acknowledged_at = "2026-10-14T10:00:00+00:00"
    s.llm.hosted_mode.acknowledged_terms = s.llm.hosted_mode.disclosure_terms()
    with pytest.raises(ExternalBackendError, match="embeddings have no hosted path"):
        await LLMClient(s).embed(["operator text"])
    assert not route.called


async def test_inprocess_backend_cannot_complete_a_prompt():
    s = get_settings()
    s.llm.tiers["tier1"] = s.llm.tiers["tier1"].model_copy(update={"backend": "inprocess"})
    with pytest.raises(ValueError, match="serves llm.tiers.embed only"):
        await LLMClient(s).complete(tier="tier1", messages=[{"role": "user", "content": "x"}])


def test_shipped_settings_define_the_inprocess_backend():
    b = get_settings().llm.backends["inprocess"]
    assert b.engine == "inprocess" and not b.external and b.base_url == ""
    assert get_settings().llm.tiers["embed"].backend == "local"  # the Ollama path is unchanged


# ------------------------------------------------------------------------- hosted_only profile
def _picks():
    return {
        "tier1": prov.ModelInfo(id="b/gratis:free"),
        "tier2": prov.ModelInfo(id="a/paid"),
        "docsel": prov.ModelInfo(id="b/gratis:free"),
    }


def test_hosted_only_profile_resolves_embed_to_the_inprocess_backend():
    assert profiles.load_catalog().profile("hosted_only").embed_backend == "inprocess"
    assert profiles.embed_route("hosted_only") == ("inprocess", MODEL)
    for name in profiles.load_catalog().names():
        if name != "hosted_only":
            assert profiles.embed_route(name) is None, name


def test_hosted_setup_rows_leave_no_tier_on_ollama():
    """What `kestrel setup --hosted` and Settings > Hosted write for hosted_only, resolved through
    the real settings chain: no tier names the `local` (Ollama) backend, which is what lets the
    ollama_needed() check report False for a BYOK install."""
    backend = get_settings().llm.backends["frontier"]
    rows = setup_mod.hosted_rows(
        "frontier", backend, _picks(), "2026-10-14T10:00", profile="hosted_only"
    )
    assert rows["llm.tiers.embed.backend"] == "inprocess"
    assert rows["llm.tiers.embed.model"] == MODEL
    s = load_settings(db_overrides=nest_overrides(rows))
    assert s.llm.mode == "hosted"
    assert s.llm.tiers["embed"].backend == "inprocess"
    assert [t for t, c in s.llm.tiers.items() if c.backend == "local"] == []
    # and the local tier set (mode local) keeps embed in-process too: no Ollama in either mode
    local = load_settings(db_overrides=nest_overrides(rows), mode="local")
    assert local.llm.tiers["embed"].backend == "inprocess"


def test_hosted_rows_without_the_hosted_profile_leave_embed_alone():
    backend = get_settings().llm.backends["frontier"]
    for profile in (None, "gpu_16gb", "no-such-profile"):
        rows = setup_mod.hosted_rows("frontier", backend, _picks(), "t", profile=profile)
        assert not any(k.startswith("llm.tiers.embed") for k in rows), profile


def test_catalog_rejects_an_inprocess_profile_that_names_an_ollama_embed_tag(monkeypatch):
    import yaml

    raw = yaml.safe_load((Path(str(profiles.paths.packaged_config())) / "models.yml").read_text())
    raw["profiles"]["hosted_only"]["models"]["embed"] = "nomic-embed-text"
    monkeypatch.setattr(profiles, "_catalog", {"value": None})

    class _Fake:
        def __truediv__(self, name):
            class _F:
                def read_text(self, encoding="utf-8"):
                    return yaml.safe_dump(raw)

            return _F()

    monkeypatch.setattr(profiles.paths, "packaged_config", lambda: _Fake())
    with pytest.raises(ValueError, match="takes no models.embed tag"):
        profiles.load_catalog()


# ------------------------------------------------------------------- model change re-embeds
async def _seed(sm, n: int, model: str) -> list[int]:
    async with sm() as s:
        src = Source(track="employment", kind="greenhouse", name="emp", enabled=True)
        s.add(src)
        await s.flush()
        ids = []
        for i in range(n):
            opp = Opportunity(
                track="employment", opp_type="job_posting", source_id=src.id,
                title=f"VP of IT #{i}", org_name=f"Beacon {i}", stage="new",
                description_md=f"Own IT leadership. Item {i}.",
            )
            s.add(opp)
            await s.flush()
            ids.append(opp.id)
            s.add(
                OpportunityEmbedding(
                    opportunity_id=opp.id, model=model, vector=to_bytes([0.5] * 768), dims=768,
                    text_hash="old",
                )
            )
        await s.commit()
    return ids


async def test_a_model_change_reembeds_the_corpus_once(sm):
    ids = await _seed(sm, 3, model="nomic-embed-text")
    settings = _inprocess_settings()
    worker = Worker(sm, FakeLLM(settings), settings)

    stats = TierStats()
    await worker.drain_embed(stats)
    assert stats.completed == 3 and stats.errors == 0
    async with sm() as s:
        rows = (await s.execute(select(OpportunityEmbedding))).scalars().all()
        swept = await s.get(Setting, EMBED_SWEPT_MODEL_KEY)
    assert {r.opportunity_id for r in rows} == set(ids)
    assert all(r.model == MODEL and r.dims != 768 for r in rows)
    assert swept is not None and swept.value.startswith(MODEL)  # open until the next check
    assert await enqueue_stale_embeddings(sm, settings) == 0  # drained: closes, adds nothing
    async with sm() as s:
        assert (await s.get(Setting, EMBED_SWEPT_MODEL_KEY)).value == MODEL

    # Once per model: the next cycle adds nothing, so the serve loop's 5 s cadence costs a read.
    assert await enqueue_stale_embeddings(sm, settings) == 0
    # Switching back is another model change and sweeps again.
    back = get_settings()
    assert await enqueue_stale_embeddings(sm, back) == 3


async def test_the_sweep_backfills_missing_vectors_and_never_duplicates_a_live_task(sm):
    async with sm() as s:
        src = Source(track="employment", kind="greenhouse", name="emp", enabled=True)
        s.add(src)
        await s.flush()
        a = Opportunity(track="employment", opp_type="job_posting", source_id=src.id,
                        title="CTO", stage="new")
        b = Opportunity(track="employment", opp_type="job_posting", source_id=src.id,
                        title="CIO", stage="new")
        s.add_all([a, b])
        await s.flush()
        s.add(Task(type="embed", payload={"opportunity_id": a.id}))  # already queued
        await s.commit()
    assert await enqueue_stale_embeddings(sm, _inprocess_settings()) == 1
    async with sm() as s:
        tasks = (await s.execute(select(Task).where(Task.type == "embed"))).scalars().all()
    assert sorted(t.payload["opportunity_id"] for t in tasks) == sorted([a.id, b.id])


async def test_handle_embed_uses_no_nomic_prefix_in_process(sm):
    ids = await _seed(sm, 1, model="nomic-embed-text")
    settings = _inprocess_settings()
    sent: list[str] = []
    client = FakeLLM(settings)
    real = client.embed

    async def spy(texts, **k):
        sent.extend(texts)
        return await real(texts, **k)

    client.embed = spy  # type: ignore[method-assign]
    out = await handle_embed(sm, client, settings, opportunity_id=ids[0])
    assert out.action == "embedded"
    assert sent and not sent[0].startswith("search_document: ")


async def test_readers_ignore_vectors_from_another_model(sm):
    """Stale rows never meet current ones in a cosine: search and few-shot see only the current
    model's rows, so a mixed-width corpus cannot crash or silently mis-rank."""
    from kestrel.intelligence.search import search_opportunities
    from kestrel.pipeline.tier2 import _gather_few_shot

    ids = await _seed(sm, 2, model="nomic-embed-text")
    settings = _inprocess_settings()
    async with sm() as s:
        s.add(Label(opportunity_id=ids[1], track="employment", verdict="keep", score=4.0))
        await s.commit()
        hits = await search_opportunities(s, FakeLLM(settings), "VP of IT")
        assert hits == []
        opp = await s.get(Opportunity, ids[0])
        assert await _gather_few_shot(s, opp, settings) == ([], [])


# ---------------------------------------------------------------------- real weights, if present
_REAL = not ipe.missing_files()


@pytest.mark.skipif(
    not _REAL, reason="bundled model not installed; run scripts/fetch_embed_model.py"
)
def test_real_bundled_model_embeds_offline_and_ranks_sensibly(monkeypatch):
    monkeypatch.setattr(ipe, "_cache", {})
    texts = [
        "Chief Technology Officer, fintech startup",
        "VP Engineering at a payments company",
        "Line cook, evening shift, downtown restaurant",
    ]
    vecs = np.asarray(ipe.embed_sync(texts, model=MODEL))
    assert vecs.shape == (3, ipe.BUNDLED.dims)
    np.testing.assert_allclose(np.linalg.norm(vecs, axis=1), 1.0, atol=1e-5)
    sims = vecs @ vecs.T
    assert sims[0, 1] > sims[0, 2] and sims[0, 1] > sims[1, 2]
    again = np.asarray(asyncio.run(ipe.embed_async(texts[:1], model=MODEL)))
    np.testing.assert_allclose(again[0], vecs[0], atol=1e-6)


# ------------------------------------------------------------------- build-time fetch script
def _load_fetch_script():
    spec = importlib.util.spec_from_file_location(
        "fetch_embed_model", ROOT / "scripts" / "fetch_embed_model.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _archive(tmp_path: Path, files: dict[str, bytes], extra: dict[str, bytes] | None = None):
    path = tmp_path / "model.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for name, body in {**files, **(extra or {})}.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return path


def _pinned(tmp_path, monkeypatch, *, tamper: str | None = None):
    bodies = {"model.onnx": b"weights", "tokenizer.json": b"{}", "config.json": b"{\"a\":1}"}
    pins = {n: hashlib.sha256(b).hexdigest() for n, b in bodies.items()}
    shipped = dict(bodies)
    if tamper:
        shipped[tamper] = b"evil"
    arch = _archive(
        tmp_path,
        {f"onnx/{n}": b for n, b in shipped.items()},
        extra={"../escape.txt": b"x", "onnx/ignored.bin": b"x"},
    )
    bundled = dataclasses.replace(
        ipe.BUNDLED, files=pins, archive_root="onnx",
        archive_sha256=hashlib.sha256(arch.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(ipe, "BUNDLED", bundled)
    return arch


def test_fetch_installs_only_the_pinned_files_and_a_notice(tmp_path, monkeypatch):
    fetch = _load_fetch_script()
    arch = _pinned(tmp_path, monkeypatch)
    dest = tmp_path / "out" / "embed_model"
    assert fetch.main(["--archive", str(arch), "--dest", str(dest)]) == 0
    assert sorted(p.name for p in dest.iterdir()) == [
        "NOTICE.txt", "config.json", "model.onnx", "tokenizer.json",
    ]
    assert not (tmp_path / "out" / "escape.txt").exists()
    notice = (dest / "NOTICE.txt").read_text(encoding="utf-8")
    assert MODEL in notice and "Apache-2.0" in notice
    assert fetch.main(["--check", "--dest", str(dest)]) == 0


def test_fetch_refuses_a_file_that_does_not_match_its_pin(tmp_path, monkeypatch):
    fetch = _load_fetch_script()
    arch = _pinned(tmp_path, monkeypatch, tamper="tokenizer.json")
    dest = tmp_path / "embed_model"
    with pytest.raises(SystemExit, match="tokenizer.json sha256 mismatch"):
        fetch.main(["--archive", str(arch), "--dest", str(dest)])
    assert not any(p.name in ipe.BUNDLED.files for p in dest.iterdir())
    assert not list(dest.glob(".*.part"))


def test_fetch_refuses_an_archive_that_does_not_match_its_pin(tmp_path, monkeypatch):
    fetch = _load_fetch_script()
    arch = _pinned(tmp_path, monkeypatch)
    monkeypatch.setattr(ipe, "BUNDLED", dataclasses.replace(ipe.BUNDLED, archive_sha256="0" * 64))
    with pytest.raises(SystemExit, match="archive sha256 mismatch"):
        fetch.main(["--archive", str(arch), "--dest", str(tmp_path / "d")])


# ---------------------------------------------------------------------------- packaging guards
def test_spec_requires_the_embed_model_files_the_runtime_pins():
    spec = (ROOT / "packaging" / "kestrel.spec").read_text(encoding="utf-8")
    line = next(ln for ln in spec.splitlines() if ln.startswith("EMBED_MODEL_FILES"))
    named = set(json.loads(line.split("=", 1)[1].strip().replace("(", "[").replace(")", "]")))
    assert set(ipe.BUNDLED.files) | {ipe.NOTICE_FILENAME} == named
    assert "model_notice=EMBED_MODEL_DIR" in spec
    assert "fetch_embed_model.py" in spec


def test_release_fetches_the_model_before_every_build():
    import yaml

    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())
    for job in ("linux", "macos", "windows"):
        names = [st.get("name", "") for st in wf["jobs"][job]["steps"]]
        runs = [st.get("run", "") for st in wf["jobs"][job]["steps"]]
        fetch = next(i for i, r in enumerate(runs) if "scripts/fetch_embed_model.py" in r)
        build = next(i for i, n in enumerate(names) if n.startswith("Build"))
        assert fetch < build, job


def test_notices_carry_the_model_notice_and_onnxruntime_third_party_notices(tmp_path):
    from kestrel.services import notices

    note = tmp_path / "NOTICE.txt"
    note.write_text("Bundled embedding model: test-model\nLicense: Apache-2.0\n", encoding="utf-8")
    text = notices.build_notices(note)
    assert "Bundled embedding model: test-model" in text
    assert "--- onnxruntime ThirdPartyNotices.txt ---" in text
    names = {d.metadata["Name"].lower() for d in notices.runtime_closure()}
    assert {"onnxruntime", "tokenizers"} <= names


def test_model_files_are_never_committed():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "kestrel/resources/embed_model/" in gitignore


# ------------------------------------------------------------------------------------- doctor
def test_doctor_reports_the_inprocess_engine(stub_model, monkeypatch):
    from kestrel import doctor as d

    rep = d.DoctorReport()
    d._check_inprocess_embed(rep, _inprocess_settings())
    row = rep.results[-1]
    assert row.status == d.PASS and MODEL in row.detail

    monkeypatch.setattr(ipe, "model_dir", lambda: stub_model.parent / "gone")
    rep = d.DoctorReport()
    d._check_inprocess_embed(rep, _inprocess_settings())
    row = rep.results[-1]
    assert row.status == d.WARN and "fetch_embed_model.py" in row.remediation


# --------------------------------------------------------------------- review follow-ups
async def test_an_incomplete_sweep_is_retried_later_and_marked_done_only_when_drained(sm,
                                                                                    monkeypatch):
    """Tasks that fail for good (missing model files) must not leave the corpus unembedded
    forever: the sweep stays open and re-queues after RESWEEP_S, and records done only when
    nothing is stale (hard rule 5)."""
    from kestrel.pipeline import embed as embed_mod

    async with sm() as s:
        src = Source(track="employment", kind="greenhouse", name="emp", enabled=True)
        s.add(src)
        await s.flush()
        s.add(Opportunity(track="employment", opp_type="job_posting", source_id=src.id,
                          title="CTO", stage="new"))
        await s.commit()
    settings = _inprocess_settings()
    assert await enqueue_stale_embeddings(sm, settings) == 1
    async with sm() as s:
        state = (await s.get(Setting, EMBED_SWEPT_MODEL_KEY)).value
        for t in (await s.execute(select(Task).where(Task.type == "embed"))).scalars():
            t.status = "failed"  # the model was missing; retries ran out
        await s.commit()
    assert state.startswith(MODEL + "|")  # not done: the backlog is not empty
    assert await enqueue_stale_embeddings(sm, settings) == 0  # inside RESWEEP_S: quiet
    now = embed_mod.time.time()
    monkeypatch.setattr(embed_mod.time, "time", lambda: now + embed_mod.RESWEEP_S + 1)
    assert await enqueue_stale_embeddings(sm, settings) == 1  # the fixed install heals


def test_doctor_reports_the_inprocess_engine_on_a_byok_install(stub_model):
    """Routing, not just the helper: with no tier on Ollama the in-process row still appears."""
    from kestrel import doctor as d

    s = _inprocess_settings()
    for name, tier in s.llm.tiers.items():
        if name != "embed":
            tier.backend = "frontier"
    assert not profiles.ollama_needed(s)
    src = Path(d.__file__).read_text(encoding="utf-8")
    assert "if embed_is_inprocess(s) and not (needed and hosted):" in src


def test_a_gpu_profile_still_on_the_inprocess_engine_is_named_by_the_doctor(stub_model,
                                                                            monkeypatch):
    from kestrel import doctor as d

    monkeypatch.setattr(profiles, "load_active_profile",
                        lambda: profiles.ActiveProfile(profile="gpu_16gb", reason="t"))
    rep = d.DoctorReport()
    d._check_inprocess_embed(rep, _inprocess_settings())
    notes = " ".join(f"{r.detail} {r.remediation}" for r in rep.results)
    assert "gpu_16gb" in notes and "llm.tiers.embed.backend" in notes


# --------------------------------------------------------- Hugging Face source (D-EMBED-6)
_HF_FILES = {"onnx/model.onnx": b"onnx-bytes", "tokenizer.json": b'{"t": 1}',
             "config.json": b'{"c": 1}'}
_COMMIT = "a" * 40


def _mock_hf(tamper: str | None = None):
    repo = "sentence-transformers/all-MiniLM-L6-v2"
    siblings = []
    for path, data in _HF_FILES.items():
        if path.endswith(".onnx"):
            siblings.append({"rfilename": path,
                             "lfs": {"sha256": hashlib.sha256(data).hexdigest()}})
        else:
            blob = hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()
            siblings.append({"rfilename": path, "blobId": blob})
    respx.get(f"https://huggingface.co/api/models/{repo}/revision/main?blobs=true").mock(
        return_value=httpx_response(200, json={"sha": _COMMIT, "siblings": siblings}))
    for path, data in _HF_FILES.items():
        body = b"tampered" if path == tamper else data
        respx.get(f"https://huggingface.co/{repo}/resolve/{_COMMIT}/{path}").mock(
            return_value=httpx_response(200, content=body))


def httpx_response(status, **kw):
    from httpx import Response

    return Response(status, **kw)


@respx.mock
def test_pin_resolves_a_fixed_commit_and_prints_verified_hashes():
    mod = _load_fetch_script()
    _mock_hf()
    out = mod.pin_from_huggingface()
    assert f"at commit {_COMMIT}" in out and f"/resolve/{_COMMIT}/onnx/model.onnx" in out
    assert hashlib.sha256(b"onnx-bytes").hexdigest() in out  # pinned by content, not by name
    assert '"model.onnx":' in out and "/resolve/main/" not in out  # never a moving branch


@respx.mock
def test_pin_refuses_bytes_that_disagree_with_hugging_faces_index():
    mod = _load_fetch_script()
    _mock_hf(tamper="onnx/model.onnx")
    with pytest.raises(SystemExit, match="Hugging Face lists"):
        mod.pin_from_huggingface()
    respx.reset()
    _mock_hf(tamper="tokenizer.json")
    with pytest.raises(SystemExit, match="git blob"):
        mod.pin_from_huggingface()


@respx.mock
def test_file_mode_installs_only_when_every_file_matches(tmp_path):
    mod = _load_fetch_script()
    _mock_hf()
    base = "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/" + _COMMIT
    good = dataclasses.replace(
        ipe.BUNDLED,
        files={"model.onnx": hashlib.sha256(b"onnx-bytes").hexdigest(),
               "tokenizer.json": hashlib.sha256(b'{"t": 1}').hexdigest()},
        file_urls={"model.onnx": f"{base}/onnx/model.onnx",
                   "tokenizer.json": f"{base}/tokenizer.json"},
    )
    mod.install_files(tmp_path / "ok", good)
    assert (tmp_path / "ok" / "model.onnx").read_bytes() == b"onnx-bytes"
    bad = dataclasses.replace(good, files={**good.files, "tokenizer.json": "0" * 64})
    with pytest.raises(SystemExit, match="tokenizer.json sha256 mismatch"):
        mod.install_files(tmp_path / "bad", bad)
    assert not (tmp_path / "bad" / "model.onnx").exists()  # nothing installed on a mismatch
    assert not list((tmp_path / "bad").glob(".*.part"))
