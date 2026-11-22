"""D-TEST-817: which browser the browser tests try first."""

from __future__ import annotations

import pytest

from tests import browser


@pytest.fixture
def chromium_path(monkeypatch):
    monkeypatch.setattr(browser, "fallback_chromium", lambda: "C:/pw/chrome.exe")
    return "C:/pw/chrome.exe"


def test_edge_is_tried_first_by_default(monkeypatch, chromium_path):
    monkeypatch.delenv(browser.TEST_BROWSER_ENV, raising=False)
    assert browser._launch_kwargs() == [{"channel": "msedge"}, {"executable_path": chromium_path}]


def test_the_env_switch_puts_chromium_first_and_keeps_edge_as_the_fallback(
    monkeypatch, chromium_path
):
    monkeypatch.setenv(browser.TEST_BROWSER_ENV, "Chromium")
    assert browser._launch_kwargs() == [{"executable_path": chromium_path}, {"channel": "msedge"}]


def test_with_no_chromium_installed_the_switch_still_leaves_edge(monkeypatch):
    monkeypatch.setattr(browser, "fallback_chromium", lambda: None)
    monkeypatch.setenv(browser.TEST_BROWSER_ENV, "chromium")
    assert browser._launch_kwargs() == [{"channel": "msedge"}]
