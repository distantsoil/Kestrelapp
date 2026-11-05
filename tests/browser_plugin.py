"""The fixture every *_browser.py test file imports (D-TEST-8): test mode on (toasts do not
auto-dismiss, so a slow machine cannot lose one a test is about to press) and a per-test folder
for a failure's screenshots and Playwright traces. Import it as

    from tests.browser_plugin import browser_artifacts  # noqa: F401

It registers a small report hook with pytest the first time it runs, so tests/conftest.py is not
touched. A failing test prints the folder, under the pytest tmp dir."""

from __future__ import annotations

import os
import re

import pytest

from tests import browser


class _FailureHook:
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):  # noqa: ANN001
        outcome = yield
        report = outcome.get_result()
        tracker = browser._TRACKER  # noqa: SLF001
        if tracker is None or report.passed or report.skipped:
            return
        tracker.failed = True
        tracker.capture_open()
        if tracker.saved:
            report.sections.append(("Browser failure artifacts", str(tracker.folder)))


@pytest.fixture(autouse=True)
def browser_artifacts(request, monkeypatch, tmp_path_factory):
    if "1" not in (os.environ.get("KESTREL_LIVE_BROWSER"), os.environ.get("KESTREL_LAYOUT_CRAWL")):
        yield
        return
    monkeypatch.setenv("KESTREL_TEST_MODE", "1")
    pm = request.config.pluginmanager
    if not pm.has_plugin("kestrel-browser-failure-hook"):
        pm.register(_FailureHook(), "kestrel-browser-failure-hook")
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.nodeid)[-120:]
    folder = tmp_path_factory.getbasetemp() / "browser-artifacts" / name
    browser.set_tracker(browser.ArtifactTracker(folder))
    try:
        yield
    finally:
        browser.set_tracker(None)
