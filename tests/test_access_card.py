"""Settings > Access from other devices after turning it on (#133): the restart sentence is said
once, and a missing QR library degrades to a plain "use the address" line, never a 500."""

from __future__ import annotations

import sys

from fastapi.testclient import TestClient

SENTENCE = "Takes effect after Kestrel restarts."


def _client(monkeypatch) -> TestClient:
    from kestrel import paths as p
    from kestrel.db import migrate
    from kestrel.web import auth
    from kestrel.web.app import build_app

    migrate.run_migrations()
    monkeypatch.setattr(auth, "lan_host_ip", lambda: "192.168.1.20")
    p.ensure_dirs()
    (p.config_dir() / "settings.yml").write_text(
        (p.packaged_config() / "settings.yml").read_text(encoding="utf-8"), encoding="utf-8")
    return TestClient(build_app(worker=False))


def test_the_restart_sentence_appears_once_after_turning_access_on(monkeypatch):
    with _client(monkeypatch) as c:
        on = c.post("/settings/privacy/access", data={"on": "1", "ack": "on"})
        assert on.status_code == 200 and 'data-testid="lan-qr"' in on.text
        assert on.text.count(SENTENCE) == 1
        again = c.get("/settings/privacy/access")
        assert again.text.count(SENTENCE) == 1
        off = c.post("/settings/privacy/access", data={"on": "0"})
        assert off.text.count(SENTENCE) == 0  # back to what is running: nothing to restart


def test_an_environment_variable_that_wins_adds_its_own_line_not_a_second_restart_sentence(
        monkeypatch):
    monkeypatch.setenv("KESTREL_SERVER__BIND_ALL", "true")
    with _client(monkeypatch) as c:
        off = c.post("/settings/privacy/access", data={"on": "0"})
    assert 'data-testid="lan-bind-note"' in off.text
    assert "wins over this switch until you remove it" in off.text
    assert off.text.count(SENTENCE) == 0  # the variable already decides what is running


def test_a_missing_qr_library_says_to_use_the_address_and_never_breaks_the_page(monkeypatch):
    monkeypatch.setitem(sys.modules, "segno", None)  # `import segno` raises ImportError
    with _client(monkeypatch) as c:
        on = c.post("/settings/privacy/access", data={"on": "1", "ack": "on"})
        page = c.get("/settings")
    assert on.status_code == 200 and page.status_code == 200
    assert 'data-testid="lan-qr"' not in on.text
    assert "Scan with your phone" not in on.text
    assert 'data-testid="lan-no-qr"' in on.text
    assert "The code could not be drawn here. Use Copy address, then type or paste it" in on.text
    assert 'data-testid="copy-join-url"' in on.text  # the address is still one press away
    assert on.text.count(SENTENCE) == 1


def test_the_settings_mode_card_qualifies_local_ai_as_private_only_when_ollama_is_as_shipped():
    """#143: the On this computer line on Settings > How should Kestrel run? (real route, both
    the page and the card's own route). The old unqualified "Private" must not come back."""
    from kestrel.db import migrate
    from kestrel.db.engine import forget_engine
    from kestrel.web.app import build_app

    migrate.run_migrations()
    forget_engine()
    with TestClient(build_app(worker=False)) as c:
        for path in ("/settings/computer/mode", "/settings"):
            body = c.get(path).text
            card = body.split('id="st-mode"', 1)[1].split("</section>", 1)[0]
            local = card.split('data-testid="st-mode-local"', 1)[1].split("</button>", 1)[0]
            assert "<li>" in local and "Private when Ollama is set up as shipped" in local, path
            assert "Private</li>" not in local and "Works offline" not in local
            online = card.split('data-testid="st-mode-online"', 1)[1].split("</button>", 1)[0]
            assert "Private when Ollama" not in online
