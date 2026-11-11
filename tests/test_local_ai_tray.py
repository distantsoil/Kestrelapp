"""#166 (owner, 2026-11-11): the Local AI spot check watches Ollama's server and model runners,
never the tray app, whose own update check would otherwise read as a model connection."""

from __future__ import annotations

from types import SimpleNamespace

import psutil

from kestrel.services import local_ai_egress as egress


def _procs(*names: str):
    return [SimpleNamespace(info={"pid": i + 10, "name": n}) for i, n in enumerate(names)]


def test_the_tray_app_is_left_out_and_the_server_and_runners_are_watched(monkeypatch):
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: _procs(
        "ollama.exe", "ollama app.exe", "ollama_llama_server", "Ollama", "ollama", "notepad.exe"))
    names = [n for _pid, n in egress._find_ollama_pids()]
    assert names == ["ollama.exe", "ollama_llama_server", "ollama"]
