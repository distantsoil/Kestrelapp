from __future__ import annotations

import os
import threading

# One math thread per process, set before numpy or sklearn load. With several suites running at
# once on a small machine, BLAS's default thread pools oversubscribe the cores and the reranker
# tests stall; the app already fits with n_jobs=1, so this changes no result.
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402


def _host_configuration_vars() -> list[str]:
    """The developer's own configuration in os.environ (D-TEST-572): every KESTREL_<setting>
    variable (its first `__` part names a Settings field, so pydantic-settings would read it,
    case-insensitively) and every OLLAMA_* variable. The harness's own switches
    (KESTREL_LIVE_BROWSER, KESTREL_DATA_HOME, the stress clock) name no setting and are kept."""
    from kestrel.config import Settings

    settings_vars = {f"KESTREL_{field}".upper() for field in Settings.model_fields}
    return [
        name for name in os.environ
        if name.upper().startswith("OLLAMA_") or name.upper().split("__", 1)[0] in settings_vars
    ]


def _isolate_the_process_from_the_host() -> None:
    """D-TEST-574: before any test module is imported, the process gets a throwaway data home and
    loses the host's configuration variables. Module imports (tests/test_route_crawl.py builds its
    app at import), collection and module- or session-scoped fixtures all run before the per-test
    `_isolate_data_home`; without this they read the developer's real data home and shell. Every
    test still gets its own data home from that fixture."""
    import atexit
    import shutil
    import tempfile
    from pathlib import Path

    from tests import throwaway

    for name in _host_configuration_vars():
        os.environ.pop(name, None)
    # D-TEST-792 (carried over from the consent outbound branch): uvicorn's "auto" loop is uvloop
    # wherever it is installed (Linux, macOS). uvloop resolves names and connects in libuv, never
    # through Python's socket module, so the served app in every browser test
    # (web.serve.running_server) went around _no_outbound_network: on Linux a dialog reached
    # openrouter.ai unseen, while Windows (no uvloop, the guarded Proactor loop) failed the same
    # tests. With uvloop unimportable uvicorn falls back to asyncio, whose every lookup and connect
    # the guard sees.
    import sys

    sys.modules["uvloop"] = None  # type: ignore[assignment]
    home = Path(tempfile.mkdtemp(prefix="kestrel-collect-"))
    throwaway.mark(home)
    os.environ["KESTREL_DATA_HOME"] = str(home)
    _COLLECT_HOME.append(home)
    atexit.register(shutil.rmtree, home, ignore_errors=True)


_COLLECT_HOME: list = []


def pytest_unconfigure(config):
    """Remove this process's collection-time data home (D-TEST-574). Runs in every xdist worker
    as well as the controller; atexit stays as the fallback."""
    import shutil

    for home in _COLLECT_HOME:
        shutil.rmtree(home, ignore_errors=True)


_isolate_the_process_from_the_host()


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


@pytest.fixture(autouse=True)
def _downloads_in_tmp(request, tmp_path_factory, monkeypatch):
    """No test writes into the real Downloads folder or the real home (D-TEST-800, issue #237).
    paths.downloads_dir() is where the diagnostics save puts its zip, so every test gets its own
    Downloads folder under pytest's temp root, and the Windows Known Folder call
    (paths._windows_downloads) answers None. Yields the folder. A test that exercises
    downloads_dir() itself carries @pytest.mark.real_downloads_dir and patches what the function
    reads (sys.platform, Path.home, _windows_downloads) the way it always did."""
    from kestrel import paths

    folder = tmp_path_factory.mktemp("downloads") / "Downloads"
    folder.mkdir()
    if request.node.get_closest_marker("real_downloads_dir") is None:
        monkeypatch.setattr(paths, "downloads_dir", lambda: folder)
        monkeypatch.setattr(paths, "_windows_downloads", lambda: None)
    yield folder


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
def _reset_process_globals():
    """Process-global state that a test, or the app code it drives, changes must never reach the
    next test (D-TEST-25): the enabled-tracks cache and the count cache (filled by any app
    startup or page render), and safe mode, which the app's own safe-start button writes to
    os.environ where monkeypatch cannot see it. The stress run's random order found all three.
    Also structlog's configuration (D-TEST-30): a test that runs the real serve() calls
    configure_logging(), whose INFO filter then hid every later test's captured debug line.
    Also the queue's first-claim stamp (D-TEST-32): queue._CLAIMS_SINCE is set once per process
    by the first lease, so under the stress run's shifted or 10x clock an earlier test's later
    stamp made a later test's own lease look like an earlier process's, and a clean quit then
    kept its attempt (test_quit_grace)."""
    import structlog

    from kestrel import logging_setup, startup_health
    from kestrel.queue import queue as queue_mod
    from kestrel.services import count_cache, tracks

    saved = os.environ.get(startup_health.SAFE_MODE_ENV)
    saved_log = structlog.get_config()
    saved_configured = logging_setup._configured

    from kestrel.services import setup_state

    def reset() -> None:
        tracks._cache["value"] = None
        count_cache.clear()
        queue_mod._CLAIMS_SINCE.clear()

    # D-TEST-576: the detected hardware is cached once per process. A test that fakes a machine
    # (an RTX 4080, a weak laptop) and fills the cache must not leave that machine for the next
    # test, so the cache is put back as it was.
    saved_hw = setup_state._hw_cache["value"]
    reset()
    yield
    reset()
    setup_state._hw_cache["value"] = saved_hw
    structlog.configure(**saved_log)
    logging_setup._configured = saved_configured
    if saved is None:
        os.environ.pop(startup_health.SAFE_MODE_ENV, None)
    else:
        os.environ[startup_health.SAFE_MODE_ENV] = saved


_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "testserver", "0.0.0.0", "::", ""}


def _is_loopback(host: object) -> bool:
    """True for this machine only: loopback names and addresses (127.0.0.0/8, ::1)."""
    import ipaddress

    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    name = str(host).strip("[]").lower()
    if name in _LOOPBACK_NAMES or name in _this_machine():
        return True
    try:
        return ipaddress.ip_address(name.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


_THIS_MACHINE: set[str] = set()


def _this_machine() -> set[str]:
    """This machine's own host names and the addresses they resolve to (the LAN address the MCP
    tests bind to). Looked up once, before any guard is installed."""
    import socket

    if not _THIS_MACHINE:
        names = {socket.gethostname(), socket.getfqdn()}
        _THIS_MACHINE.update(n.lower() for n in names if n)
        for n in names:
            try:
                for info in socket.getaddrinfo(n, None):
                    _THIS_MACHINE.add(str(info[4][0]).split("%", 1)[0].lower())
            except OSError:
                continue
        _THIS_MACHINE.add("\0")  # never empty, so the lookup runs once
    return _THIS_MACHINE


# Ollama's own default port, the one the shipped settings name (ollama_native_url).
# tests/test_host_isolation.py fails if the shipped settings stop naming it.
HOST_OLLAMA_PORT = 11434


class OutboundNetworkInTest(RuntimeError):
    """A test tried to reach a host other than this machine (CLAUDE.md: never call a live job
    board, ATS or SEC endpoint from a test)."""


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch):
    """Refuse any DNS lookup or connection to a host other than this machine, so a test that runs
    the real worker (board lookups, update checks, notification pushes) fails loudly instead of
    calling a live service. Loopback stays open for the app under test, Ollama fakes and the
    browser tests' local server, except the host's own Ollama port (D-TEST-571). Unix sockets are
    not affected."""
    import errno
    import socket

    _this_machine()
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not _is_loopback(host):
            raise OutboundNetworkInTest(f"test tried to look up {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    def _check(sock, address) -> bool:
        """True when the connect must be refused as the host's own Ollama (see below)."""
        # A UDP connect only sets the default peer and sends nothing (kestrel/web/auth.py uses
        # one to learn the LAN address), so only stream connections are checked.
        if (sock.family in (socket.AF_INET, socket.AF_INET6) and sock.type == socket.SOCK_STREAM
                and isinstance(address, tuple)):
            if not _is_loopback(address[0]):
                raise OutboundNetworkInTest(f"test tried to connect to {address[0]!r}")
            # D-TEST-571 (#205): the machine's own Ollama is host state. A test that reached it
            # passed in the cloud and failed on a computer with Ollama running. Its port is
            # refused, exactly as on a machine without Ollama; a test that needs a model server
            # fakes one (a mocked client, or a fake server on a port of its own).
            return len(address) >= 2 and address[1] == HOST_OLLAMA_PORT
        return False

    def guarded_connect(self, address):
        if _check(self, address):
            raise ConnectionRefusedError(errno.ECONNREFUSED, "test: the host's Ollama is closed")
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        if _check(self, address):
            return errno.ECONNREFUSED
        return real_connect_ex(self, address)

    # A proxy on this machine would turn every public request into a loopback connection, so
    # the proxy settings are dropped for the test (a test never needs one).
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy",
                "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    # D-TEST-579: on Windows, asyncio's default Proactor loop connects through IOCP ConnectEx
    # and never calls socket.connect, so every httpx.AsyncClient request went around the checks
    # above. Its one entry point is guarded with the same rule (the Selector loop used elsewhere
    # calls socket.connect and is already covered).
    from asyncio import proactor_events

    real_sock_connect = proactor_events.BaseProactorEventLoop.sock_connect

    async def guarded_sock_connect(self, sock, address):
        if _check(sock, address):
            raise ConnectionRefusedError(errno.ECONNREFUSED, "test: the host's Ollama is closed")
        return await real_sock_connect(self, sock, address)

    monkeypatch.setattr(proactor_events.BaseProactorEventLoop, "sock_connect",
                        guarded_sock_connect)


@pytest.fixture(autouse=True)
def _no_run_state_between_tests():
    """The serve loop records the mode of its latest cycle and the settings a run in flight was
    confirmed on in process-global dicts (kestrel/web/app.py). A worker-loop test that left
    "local" behind made a later run-card test read the wrong mode; reset both around each test."""
    from kestrel.desktop import linux_check
    from kestrel.web import app as app_mod

    app_mod._run_mode["mode"] = None
    app_mod._release_run()
    linux_check.clear()  # a desktop test's "the window could not open" must not reach the next
    yield
    app_mod._run_mode["mode"] = None
    app_mod._release_run()
    linux_check.clear()


@pytest.fixture(autouse=True)
def _no_credit_holds_between_tests():
    """An out-of-credit hold (kestrel/llm/credit.py) is process-global; one test's hold must
    never stop the next test's sends."""
    from kestrel.llm import credit

    credit.clear()
    yield
    credit.clear()


_REAL_ENGINE_PROBE: dict = {}


@pytest.fixture
def real_engine_probe(monkeypatch):
    """Put the production worker.probe_engine back (setup_state.probe_ollama and its cache),
    for a test that fakes only the network beneath it."""
    from kestrel.pipeline import worker as worker_mod
    from kestrel.services.setup_state import forget_ollama_probe

    forget_ollama_probe()
    monkeypatch.setattr(worker_mod, "probe_engine", _REAL_ENGINE_PROBE["probe_engine"])
    yield
    forget_ollama_probe()


@pytest.fixture(autouse=True)
def _no_engine_preflight(monkeypatch):
    """The worker's preflight (D-WORKER-6) probes Ollama before each Tier 1 and Tier 2 drain and
    waits while it is down. The suite has no Ollama, so by default the probe reports "not
    probed" and drains run as before; the preflight's own tests set the probe explicitly. The
    record of why scoring waits is process-global and is cleared for each test."""
    from kestrel.pipeline import worker as worker_mod

    _REAL_ENGINE_PROBE.setdefault("probe_engine", worker_mod.probe_engine)
    monkeypatch.setattr(worker_mod, "probe_engine", lambda _settings, **_kw: None)
    from kestrel.llm import engine_cycle

    engine_cycle.stop()
    worker_mod._ENGINE_WAIT.clear()
    yield
    worker_mod._ENGINE_WAIT.clear()


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
    # D-TEST-572 (#205): the host's own configuration never reaches a test either (doctor reads
    # OLLAMA_HOST and OLLAMA_NUM_PARALLEL; pydantic-settings reads KESTREL_<setting>). Removed
    # from the process at import (D-TEST-574), and again here, for good: one an earlier test
    # left in os.environ directly (a .env load puts KESTREL_LOG_LEVEL there) is removed, not put
    # back afterwards. A test that needs one sets it with monkeypatch.setenv.
    for name in _host_configuration_vars():
        os.environ.pop(name, None)
    _dotenv_state["owned"].clear()  # type: ignore[union-attr]
    _dotenv_state["secrets"].clear()  # type: ignore[union-attr]
    _dotenv_state["path"] = None
    # Tests hand keys over with monkeypatch.setenv, so they run with the opt-out that lets a
    # process-environment key count (D-BYOK-11). The isolation tests unset it themselves.
    monkeypatch.setenv("KESTREL_USE_SYSTEM_KEYS", "1")


@pytest.fixture(autouse=True)
def _no_setup_action_outlives_its_test(
    monkeypatch, _reset_process_globals, _reset_status_caches, _isolate_data_home
):
    """D-TEST-578: a Setup action (pull, build, verify) runs on a daemon thread named "setup-<name>"
    (kestrel.services.setup_state.start_action). One that outlived its test ran doctor against
    the NEXT test's data home while that test deleted and re-created its database, and the next
    test read "no such table: source" (stress run, 10x clock). A test that starts an action waits
    for it itself (Action.join). Any such thread still running when the test returns fails that
    test, naming the action (D-TEST-581): it is never waited for silently. It is still joined
    first, so it cannot reach the next test, before this test's patches are undone and before
    the process-global caches and the data home are reset: this fixture asks for those
    fixtures, so it is set up after them and torn down first. Threads are compared as objects,
    never by ident, which the OS reuses."""
    import threading
    import time

    before = set(threading.enumerate())
    yield
    left = [t for t in threading.enumerate()
            if t.name.startswith("setup-") and t not in before and t.is_alive()]
    if not left:
        return
    # Well under pytest-timeout's 60 s for the whole test, so a stuck thread fails here with
    # its own message instead of a timeout dump (or, on Windows, a lost worker).
    deadline = time.monotonic() + 15.0
    for t in left:
        t.join(max(0.0, deadline - time.monotonic()))
    still = [t.name for t in left if t.is_alive()]
    pytest.fail(
        f"the test returned while Setup action thread(s) {[t.name for t in left]} still ran; "
        "wait for the action in the test (Action.join)"
        + (f"; still running after 15 s: {still}" if still else ""),
        pytrace=False,
    )


class _SplashWarningTap:
    """Forward structlog calls, keeping splash_update_failed events for the guard (D-CI-601)."""

    def __init__(self, inner, events: list[dict]) -> None:
        self._inner = inner
        self._events = events

    def warning(self, event: str, *args, **kwargs):
        if event == "splash_update_failed":
            self._events.append(dict(kwargs, event=event, _thread=threading.current_thread()))
        return self._inner.warning(event, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


class _SplashTimers:
    """A `threading.Timer` stand-in that remembers the splash delivery timers of one test.

    shell.py starts a `threading.Timer` per splash update and for its window geometry. Only the
    delivery timers are kept, with the callback and arguments, so teardown knows exactly which
    timers this test started and whether the callback ran. It is installed on the threading
    module itself, so a shell module reloaded during the test still gets it."""

    def __init__(self) -> None:
        self._real = threading.Timer
        self.deliveries: list[dict] = []

    def __call__(self, interval, function, args=None, kwargs=None):
        if getattr(function, "__name__", "") != "_check_delivered":
            return self._real(interval, function, args, kwargs)
        record = {"function": function, "args": args or (), "kwargs": kwargs or {},
                  "ran": False, "started": False, "timer": None,
                  "creator": threading.current_thread()}
        real_function = record["function"]
        real_args, real_kwargs = record["args"], record["kwargs"]

        def run_once() -> None:
            record["ran"] = True
            real_function(*real_args, **real_kwargs)

        timer = self._real(interval, run_once)
        real_start = timer.start

        def start() -> None:
            real_start()
            record["started"] = True

        timer.start = start
        record["timer"] = timer
        self.deliveries.append(record)
        return timer


@pytest.fixture(autouse=True)
def _no_splash_update_failure_goes_unnoticed(request, monkeypatch):
    """D-CI-601 (CLAUDE.md: a CI warning becomes a failing local test in the same PR).

    kestrel.desktop.shell._Splash logs splash_update_failed when an update the loading page did
    not take would leave the window on "Opening your data" (shell.py `_js`, `_check_delivered`).
    A startup test that lets that happen is not exercising a real loading page, and the warning
    is the defect, so it fails the test here: give the test a Channel its page fetches, a splash
    stub, or mark it splash_update_failed_is_the_point with the reason it asserts the warning.

    `_js` checks delivery from a daemon timer DELIVERY_WAIT_S (10 s) later, after the test has
    returned. `threading.Timer` is replaced for the test by a recorder, so teardown knows exactly
    which delivery timers this test started and whether each callback ran. A timer whose page
    already fetched is cancelled; one that would warn is joined for the rest of its own grace so
    it logs as production does. Both are waits on the thread, never a sleep, and a timer that did
    not warn never reaches the next test. A warning is blamed on this test only when it came from
    this test's own thread or a thread that did not exist when the test began, so a timer left
    behind by another test is not attributed here.
    """
    import time

    import structlog

    from kestrel.desktop import shell

    events: list[dict] = []
    real_logger = structlog.get_logger

    def tapped_logger(*args, **kwargs):
        # tests/test_platform.py reloads the shell module; its fresh logger must be tapped too.
        return _SplashWarningTap(real_logger(*args, **kwargs), events)

    monkeypatch.setattr(structlog, "get_logger", tapped_logger)
    monkeypatch.setattr(shell, "log", _SplashWarningTap(shell.log, events))
    timers = _SplashTimers()
    monkeypatch.setattr(threading, "Timer", timers)
    expected = request.node.get_closest_marker("splash_update_failed_is_the_point")
    if expected is not None and not (expected.args and str(expected.args[0]).strip()):
        pytest.fail(
            "the splash_update_failed_is_the_point marker needs the one-line reason it asserts "
            "the warning (D-CI-601).",
            pytrace=False,
        )
    before = set(threading.enumerate())
    yield
    stuck: list[str] = []
    for record in timers.deliveries:
        timer, function = record["timer"], record["function"]
        # Constructed and never started: nothing can be logged. A live thread is settled even
        # when the starting thread has not returned from Thread.start yet (D-CI-602 review).
        if not record["started"] and not timer.is_alive():
            continue
        args = record["args"]
        splash = getattr(function, "__self__", None)
        last_fetch = getattr(getattr(splash, "channel", None), "last_fetch", None)
        posted = args[2]
        if expected is not None or (last_fetch is not None and last_fetch >= posted):
            timer.cancel()
            timer.join(timeout=1.0)
        else:
            grace = getattr(splash, "DELIVERY_WAIT_S", 10.0)
            timer.join(timeout=max(0.0, posted + grace - time.monotonic()) + 1.0)
            if not record["ran"]:
                timer.cancel()
                timer.join(timeout=1.0)
                if not record["ran"]:  # the canceled thread never started it: run it now
                    function(*args)
        if timer.is_alive():
            timer.cancel()
            timer.join(timeout=1.0)
        if timer.is_alive():
            stuck.append(getattr(function, "__name__", "?"))
    if stuck:
        pytest.fail(
            f"splash delivery timer(s) {stuck} did not finish at teardown; the guard cannot "
            "judge their warning (D-CI-601).",
            pytrace=False,
        )
    creators = {r["timer"]: r["creator"] for r in timers.deliveries}
    own = [e for e in events
           if e["_thread"] is threading.current_thread()
           or creators.get(e["_thread"], e["_thread"]) not in before]
    if own and expected is None:
        said = "; ".join(f"fn={e.get('fn')} cause={e.get('cause')}" for e in own)
        pytest.fail(
            "the test logged splash_update_failed: the loading page never took an update "
            f"({said}). Give the test a Channel its page fetches or a splash stub, or mark it "
            "splash_update_failed_is_the_point with the reason (D-CI-601).",
            pytrace=False,
        )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "splash_update_failed_is_the_point(reason): the test asserts a splash_update_failed "
        "warning on purpose, so the autouse guard (D-CI-601) lets it through; state the reason",
    )
    config.addinivalue_line(
        "markers",
        "real_hardware_detection: run kestrel.llm.profiles.detect_hardware itself (the test fakes "
        "the subprocess and platform calls it makes); see _no_hardware_from_the_host",
    )
    config.addinivalue_line(
        "markers",
        "real_downloads_dir: run kestrel.paths.downloads_dir itself (the test patches what it "
        "reads underneath); see _downloads_in_tmp (D-TEST-800)",
    )


# The machine every test runs on unless it says otherwise (D-TEST-580): the cloud runner's own
# answer (Linux, no supported GPU, 16 GB), so a test's result does not depend on whether the
# developer's computer has an NVIDIA card, Apple silicon or more memory.
TEST_MACHINE_NOTES = ("nvidia-smi not found or reported no GPU",)


@pytest.fixture(autouse=True)
def _no_hardware_from_the_host(request, monkeypatch):
    """D-TEST-580: hardware detection (kestrel.llm.profiles.detect_hardware) runs nvidia-smi,
    sysctl and uname and reads /proc/meminfo on the host. Every caller goes through that one
    module attribute, so it answers with a fixed Linux machine with no GPU. A test that wants
    another machine patches it (or setup_state.detected_hardware) itself; a test of the detector
    is marked real_hardware_detection and fakes the calls the detector makes."""
    if request.node.get_closest_marker("real_hardware_detection"):
        return
    from kestrel.llm import profiles

    machine = profiles.Hardware(os="linux", ram_gb=16.0, notes=TEST_MACHINE_NOTES)
    monkeypatch.setattr(profiles, "detect_hardware", lambda: machine)


@pytest.fixture(autouse=True)
def _no_git_config_from_the_host(monkeypatch):
    """D-TEST-577: the git a test runs reads no global or system config. A developer's
    commit.gpgsign, core.hooksPath or init.templateDir made the temporary repositories of the
    check, ship and export tests sign, run hooks or start from a template. Tests that commit set
    their own user.name and user.email in their repository."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


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


@pytest.fixture(autouse=True)
def _no_content_security_policy_violations(request):
    """D-INST-8: a browser test fails on any Content-Security-Policy violation, console report
    of one, or Alpine expression error on any page it visited (recorded by tests/browser.py's
    watch on every context launch_sync makes). A test about the policy itself takes the record
    with take_csp_violations() and asserts on it; nothing else may leave one behind."""
    import sys

    yield
    browser_mod = sys.modules.get("tests.browser")
    if browser_mod is None:
        return
    found = browser_mod.take_csp_violations()
    if found:
        pytest.fail(browser_mod.describe_csp_violations(found), pytrace=False)


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
    _timeout_for_browser_files(items)


BROWSER_TEST_TIMEOUT_SECONDS = 180  # live browser tests; every other test gets pyproject's 60 s


def _timeout_for_browser_files(items) -> None:
    """D-TEST-8: browser test files get a longer per-test timeout than the 60 s default."""
    for item in items:
        if item.path.name.endswith("_browser.py") and item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(BROWSER_TEST_TIMEOUT_SECONDS))
