from __future__ import annotations

import os

# One math thread per process, set before numpy or sklearn load. With several suites running at
# once on a small machine, BLAS's default thread pools oversubscribe the cores and the reranker
# tests stall; the app already fits with n_jobs=1, so this changes no result.
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_data_home(tmp_path_factory):
    """Point %LOCALAPPDATA%\\Kestrel at a throwaway dir so tests never touch real app data.
    Config still resolves: paths.config_file falls back to the packaged defaults when the (empty)
    config dir has nothing seeded.

    FUNCTION-scoped (not session): every test that uses paths.db_path() gets its OWN sqlite file.
    A session-shared file was the root cause of the deterministic Windows failure (AFTERACTION
    BLOCKING-3) -- test_web_phase13's Playwright server left an aiosqlite handle open on the shared
    file, so test_web_ui's _wipe_db could not unlink it. With per-test files a leaked handle on one
    test's file can never block another's. Each shared-db test already migrates/seeds per test, so
    the stronger isolation costs only a mktemp. The cached app engine is dropped on teardown so no
    test reuses an engine bound to a now-stale per-test path."""
    from kestrel import paths
    from kestrel.db.engine import forget_engine
    from tests import throwaway

    home = tmp_path_factory.mktemp("kestrel_home")
    os.environ["KESTREL_DATA_HOME"] = str(home)
    throwaway.mark(home)
    # The guard for every test: the data home and the database must resolve under pytest's own
    # temp root. If they do not (an environment override that wins, a path change), stop the whole
    # session before any test can touch an operator's real data.
    root = tmp_path_factory.getbasetemp().resolve()
    for name, found in (("data_home", paths.data_home()), ("db_path", paths.db_path())):
        if root not in found.resolve().parents:
            pytest.exit(
                f"paths.{name}() is {found}, outside pytest's temp root {root}; "
                "refusing to run tests against real data",
                returncode=3,
            )
    yield
    forget_engine()  # drop the cached engine WITHOUT awaiting dispose (its owning loop may be gone)


@pytest.fixture
def example_operator_profile():
    """Install the shipped fictional profile.example.yml as this test's operator profile.yml.
    With no profile.yml the app reads an EMPTY profile (D-PROF-4, issue #70), so a test that
    needs a populated profile (proof points, narrative, target titles) asks for this."""
    from kestrel import paths

    paths.ensure_dirs()
    src = paths.config_file("profile.example.yml")
    target = paths.config_dir() / "profile.yml"
    target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    yield target


@pytest.fixture(autouse=True)
def _no_first_run_redirect(monkeypatch):
    """A fresh install's GET / redirects to the onboarding wizard (D-UI-30). Almost every test
    hits / on an empty data home and expects Today, so the redirect is off by default here;
    tests/test_onboarding.py switches it back on to test it."""
    from kestrel.web.pages import setup as setup_page

    monkeypatch.setattr(setup_page, "FIRST_RUN_REDIRECT", False)


@pytest.fixture(autouse=True)
def _reset_status_caches():
    """The status rail caches its Ollama probe in a process-global dict; a probe one test faked
    must never answer the next test's request."""
    from kestrel.web.routes_control import reset_status_caches

    reset_status_caches()
    yield
    reset_status_caches()


@pytest.fixture(autouse=True)
def _no_credit_holds_between_tests():
    """An out-of-credit hold (kestrel/llm/credit.py) is process-global; one test's hold must
    never stop the next test's sends."""
    from kestrel.llm import credit

    credit.clear()
    yield
    credit.clear()


@pytest.fixture(autouse=True, scope="session")
def _no_pooled_db_connections_across_event_loops():
    """Tests run many short event loops (asyncio.run, TestClient), unlike the app's single one.
    With SQLAlchemy's default pool, an aiosqlite connection opened in one loop stays pooled after
    that loop closes; disposing or collecting it later makes its worker thread call into the dead
    loop ("RuntimeError: Event loop is closed", which CI annotates as errors), and a stale pooled
    connection can keep SQLite locked for the next test ("database is locked" on slow Windows
    runners). NullPool closes every connection when its session ends, inside its own loop.
    Production keeps the default pool: it has one loop for the life of the process. Session
    scoped, so a server thread that outlives its test still builds an unpooled engine."""
    from functools import partial

    from sqlalchemy.pool import NullPool

    from kestrel.db import engine as engine_mod

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            engine_mod,
            "create_async_engine",
            partial(engine_mod.create_async_engine, poolclass=NullPool),
        )
        yield


@pytest.fixture(autouse=True)
def _no_provider_keys_from_the_host(monkeypatch):
    """A hosted-provider key in the host environment (or one a previous test wrote through the
    .env loader) must never leak into a test: pages and preflight render differently with a key
    present. Tests that need a key set it themselves with monkeypatch.setenv."""
    from kestrel.config import _dotenv_state

    for name in (
        "OPENROUTER_API_KEY",
        "OPENMODEL_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "SAM_GOV_API_KEY",
        "REMOTEROCKETSHIP_API_KEY",
        "USAJOBS_API_KEY",
        "ADZUNA_APP_ID_KEY",
        "ADZUNA_APP_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    _dotenv_state["owned"].clear()  # type: ignore[union-attr]
    _dotenv_state["secrets"].clear()  # type: ignore[union-attr]
    _dotenv_state["path"] = None
    # Tests hand keys over with monkeypatch.setenv, so they run with the opt-out that lets a
    # process-environment key count (D-BYOK-11). The isolation tests unset it themselves.
    monkeypatch.setenv("KESTREL_USE_SYSTEM_KEYS", "1")


@pytest.fixture(autouse=True)
def _testclient_presents_a_loopback_host(monkeypatch):
    """Make TestClient present itself as a loopback client.

    The app validates the Host header (TrustedHostMiddleware) so DNS rebinding cannot turn its
    predictable port into a readable same-origin surface. Starlette's TestClient defaults to
    `http://testserver`, which is correctly refused with a 400.

    This is a HARNESS fix, not a relaxed assertion: `testserver` is not a host the app should ever
    accept, so the right answer is for the test client to speak as the loopback client it stands in
    for, rather than for production to allowlist a fictional hostname. Tests that want to exercise
    the guard itself pass an explicit Host header, which still overrides this.
    """
    from starlette.testclient import TestClient

    original = TestClient.__init__

    def _init(self, *args, **kwargs):
        kwargs.setdefault("base_url", "http://127.0.0.1")
        # Also present a loopback PEER. The LAN shared-key middleware exempts loopback, and
        # Starlette's default peer is the string "testclient", which is neither loopback nor a real
        # LAN address, so without this every TestClient request looks like an unauthenticated device
        # and gets a 401. Same reasoning as base_url: the test client stands in for the operator at
        # the machine, so it should speak as that, rather than production allowlisting a fictional
        # peer. Tests that exercise the LAN boundary pass `client=` explicitly and still override.
        kwargs.setdefault("client", ("127.0.0.1", 12345))
        original(self, *args, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", _init)


# Test modules whose job is to exercise Alembic itself (the real upgrade path, older revisions,
# restore and doctor against real migrations). They always run the real migration; every other test
# gets a copy of one database that the real migration built (see _migrated_template_db).
_REAL_MIGRATION_MODULES = frozenset(
    {
        "test_overhaul_migration",
        "test_db_schema",
        "test_doctor_schema",
        "test_cli_db",
        "test_restore_while_open",
    }
)


@pytest.fixture(autouse=True, scope="session")
def _migrated_template_db(tmp_path_factory):
    """Run the real Alembic upgrade ONCE per xdist worker and hand every later "migrate an empty
    database" call a byte copy of the result, instead of replaying every revision per test
    (about 150 to 250 ms each, in roughly a third of the suite's tests).

    Per-test isolation is unchanged: each test still gets its own new file. Only a call whose
    target is missing or empty is served from the template; a database that already has content
    (an older-revision fixture, a restore) always goes through the real upgrade, and so does every
    test in _REAL_MIGRATION_MODULES. tests/test_migration_template.py proves the copy equals a
    real migration. KESTREL_REAL_MIGRATIONS=1 turns this off for a whole run."""
    import shutil
    from pathlib import Path

    from alembic import command

    from kestrel import paths
    from kestrel.db import engine as engine_mod
    from kestrel.db import migrate

    real = migrate.run_migrations

    # The pieces a real upgrade runs through. If a test has replaced any of them when it calls
    # run_migrations (to inject a failure, count calls, or point at other scripts), the copy would
    # hide that, so such a call always takes the real path.
    def _current() -> tuple:
        return (
            migrate.command,
            command.upgrade,
            migrate._config,
            migrate.paths,
            engine_mod._apply_pragmas,
        )

    pristine = _current()

    def _untouched() -> bool:
        return all(a is b for a, b in zip(pristine, _current(), strict=True))

    # Built here, in the fixture's own setup before any test runs, so no test's patches or
    # environment can reach it. The real upgrade calls paths.ensure_dirs(), so point the data home
    # at a scratch directory for the build and never at an operator's real one.
    template = tmp_path_factory.mktemp("migrated_template") / "template.db"
    with pytest.MonkeyPatch.context() as build_env:
        build_env.setenv("KESTREL_DATA_HOME", str(template.parent / "home"))
        real(template)

    def run_migrations(db_path=None):
        target = Path(db_path) if db_path else paths.db_path()
        test = os.environ.get("PYTEST_CURRENT_TEST", "")
        module = test.split("::", 1)[0].replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".py")
        empty = not target.exists() or target.stat().st_size == 0
        if (
            not empty
            or module in _REAL_MIGRATION_MODULES
            or os.environ.get("KESTREL_REAL_MIGRATIONS") == "1"
            or not _untouched()
        ):
            return real(db_path)
        paths.ensure_dirs()
        target.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("-wal", "-shm"):  # never pair a fresh file with a stale journal
            target.with_name(target.name + suffix).unlink(missing_ok=True)
        shutil.copyfile(template, target)

    run_migrations.real = real  # type: ignore[attr-defined]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(migrate, "run_migrations", run_migrations)
        yield


@pytest.fixture(autouse=True, scope="session")
def _yaml_settings_parse_cache():
    """Settings are rebuilt on every get_settings() call (SPEC 4.6) and each build parses
    settings.yml with pure-Python PyYAML, about 30 ms, many times per test. Cache the parsed result
    by the file's exact text and hand out a deep copy: a changed file has different text, so it is
    always re-read, and no test can see another's mutations. A file that fails to parse is not
    cached and raises exactly as before."""
    import copy

    from pydantic_settings.sources.providers.yaml import YamlConfigSettingsSource

    original = YamlConfigSettingsSource._read_file
    cache: dict[tuple[str, str], object] = {}

    def _read_file(self, file_path):
        try:
            text = file_path.read_text(encoding=self.yaml_file_encoding or "utf-8")
        except OSError:
            return original(self, file_path)
        key = (self.yaml_file_encoding or "", text)
        if key not in cache:
            cache[key] = original(self, file_path)
        return copy.deepcopy(cache[key])

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(YamlConfigSettingsSource, "_read_file", _read_file)
        yield


@pytest_asyncio.fixture
async def sm(tmp_path):
    """A fresh, migrated SQLite database per test: a copy of the database the real programmatic
    migration built once for this worker (see _migrated_template_db), so every test gets the full
    schema in its own file, and a WAL engine so concurrency behaves like production."""
    from kestrel.db import migrate
    from kestrel.db.engine import make_async_engine

    db = tmp_path / "test.db"
    migrate.run_migrations(db)  # command.upgrade -> full schema
    engine = make_async_engine(db)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield sessionmaker
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _no_real_alert_sound(monkeypatch):
    """No test makes a sound or runs a sound player: a delivery cycle plays Kestrel's alert sound
    (kestrel.services.alert_sound), and a developer's speakers must not chime through the suite.
    tests/test_alert_sound.py takes the real functions at import time, before this replaces them."""
    from kestrel.services import alert_sound

    monkeypatch.setattr(alert_sound, "play", lambda name, volume, **kw: False)


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    """No test resolves a name (hard rule: tests never touch the network). The SSRF guard for
    operator-supplied links (kestrel.ingest.http.assert_public_url) resolves a host, so every
    name resolves to one public documentation-range address. A test of the guard itself
    overrides this seam."""
    async def _public(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr("kestrel.ingest.http._resolve", _public)


@pytest.fixture(autouse=True)
def _worker_unpaused():
    """The worker's pause flag is process-global (kestrel.web.app._paused). A test that pauses
    and fails before resuming would leave every later test paused, so reset it after each test."""
    from kestrel.web import app

    original = app._paused
    yield
    app._paused = original  # a test may have swapped the flag object itself
    app.set_paused(False)


@pytest.fixture(autouse=True)
def _reset_desktop_bridge():
    """The desktop_bridge handlers are process-global. A test that registers a handler (e.g. via
    shell.run_desktop) must not leak it to subsequent tests."""
    from kestrel.web import desktop_bridge

    yield
    desktop_bridge.set_focus_handler(None)
    desktop_bridge.set_quit_handler(None)
    desktop_bridge.set_reveal_handler(None)
    desktop_bridge.set_folder_handler(None)
    desktop_bridge.set_window_frame(False)


@pytest.fixture
def browser_fallback(monkeypatch):
    """Point KESTREL_BROWSER_PATH at a fallback Chromium when Edge is absent; skip only if there
    is none (tests/browser.py). For tests that render through the app's own launch ladder."""
    from tests.browser import use_browser_fallback

    return use_browser_fallback(monkeypatch)


# pytest puts each running test's id in the PYTEST_CURRENT_TEST environment variable, and Windows
# caps a variable at 32,767 characters: a parametrized id built from a huge value errors before the
# test runs, on Windows CI only. Fail at collection everywhere instead (keep ids short with `ids=`).
_MAX_TEST_ID = 30_000  # under the 32,767 cap, with room for " (setup)"


def pytest_collection_modifyitems(config, items):
    too_long = [item.nodeid[:120] + "..." for item in items if len(item.nodeid) > _MAX_TEST_ID]
    if too_long:
        raise pytest.UsageError(
            f"test ids longer than {_MAX_TEST_ID} characters break Windows; give the parametrize "
            f"short ids: {too_long[:3]}"
        )
