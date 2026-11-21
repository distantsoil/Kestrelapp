"""#207 / D-REL-26: the person's own LinkedIn address as a contact field. Invented handles only."""

from __future__ import annotations

import asyncio
import socket
import threading

import pytest
import yaml
from fastapi.testclient import TestClient

from kestrel import linkedin as li
from kestrel.services import profile_edit as pe

GOOD = "https://www.linkedin.com/in/pat-example"


@pytest.mark.parametrize("typed", [
    "https://www.linkedin.com/in/pat-example", "linkedin.com/in/pat-example",
    "www.linkedin.com/in/pat-example/", "HTTP://LinkedIn.com/in/pat-example",
    "https://www.linkedin.com/in/pat-example?utm_source=share&trk=public",
    "linkedin.com/in/pat-example#experience", "  linkedin.com/in/pat-example  ",
])
def test_the_shape_is_normalized_and_tracking_is_dropped(typed):
    r = li.normalize(typed)
    assert r.readable and r.display == GOOD and r.note == ""
    assert li.href(r.display) == GOOD and li.short(r.display) == "linkedin.com/in/pat-example"


@pytest.mark.parametrize("typed", [
    "my profile is on the site", "https://example.com/in/pat", "linkedin.com/company/acme",
    "linkedin.com/in/", "https://www.linkedin.com/in/pat/details/experience", "x" * 400,
])
def test_anything_else_is_kept_as_typed_and_never_a_link(typed):
    r = li.normalize(typed)
    assert not r.readable and r.display == typed.strip() and r.note == li.UNREADABLE_NOTE
    assert li.href(typed) == "" and li.short(typed) == typed.strip()


def test_empty_has_no_note():
    assert li.normalize("").note == "" and li.normalize(None).display == ""


def _boom(*a, **k):
    raise AssertionError("LinkedIn handling used the network")


def _close_the_network(monkeypatch) -> None:
    """Every connect and name lookup fails the test, except the connect made inside
    socket.socketpair(): on Windows that is two loopback TCP sockets, and the event loop makes
    one at start-up."""
    pairing = threading.local()
    real_connect, real_socketpair = socket.socket.connect, socket.socketpair

    def connect(self, *a, **k):
        if getattr(pairing, "on", False):
            return real_connect(self, *a, **k)
        return _boom()

    def socketpair(*a, **k):
        pairing.on = True
        try:
            return real_socketpair(*a, **k)
        finally:
            pairing.on = False

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "socketpair", socketpair)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.db.engine import forget_engine, reset_engine
    from kestrel.web import routes_control

    async def _down(*a, **k):
        return None

    monkeypatch.setattr(routes_control, "_probe_ollama", _down, raising=False)
    _close_the_network(monkeypatch)
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _save(c, **data):
    return c.post("/profile/employment/about", data={"first_name": "Pat", **data})


def _block() -> dict:
    return yaml.safe_load(pe.profile_path().read_text(encoding="utf-8"))["employment"]


def test_endpoint_normalizes_and_never_refuses():
    with _client() as c:
        r = c.post("/api/linkedin/normalize", data={"linkedin": "linkedin.com/in/pat-example?x=1"})
        assert r.status_code == 200 and r.json() == {"display": GOOD, "readable": True, "note": ""}
        r = c.post("/api/linkedin/normalize", data={"linkedin": "ask me"})
        assert r.status_code == 200 and r.json()["display"] == "ask me"
        assert r.json()["note"] == li.UNREADABLE_NOTE


def test_profile_save_stores_the_normalized_address_and_shows_it():
    with _client() as c:
        r = _save(c, linkedin_profile="linkedin.com/in/pat-example?trk=x")
        assert r.status_code == 200 and "pf-saved" in r.headers["hx-trigger"]
        assert _block()["linkedin_profile"] == GOOD
        page = c.get("/profile?tab=about").text
        assert f'value="{GOOD}"' in page and "data-linkedin-input" in page


def test_unreadable_text_is_kept_and_saving_is_not_blocked():
    with _client() as c:
        r = _save(c, linkedin_profile="on request")
        assert r.status_code == 200 and "pf-saved" in r.headers["hx-trigger"]
        assert _block()["linkedin_profile"] == "on request"


def _master(look=None, header=None):
    from kestrel.db.models import DocMaster

    return DocMaster(track="employment", name="r", doc_kind="resume", header=header or {},
                     look=look or {}, summary_variants=[], skill_groups=[])


def test_the_contact_line_prints_a_safe_link_and_the_per_document_choice_omits_it():
    from kestrel.docs.catalog import RenderDoc, document_header
    from kestrel.docs.letters import letter_html
    from kestrel.docs.render import render_html

    with _client() as c:
        _save(c, linkedin_profile="linkedin.com/in/pat-example", email="pat@example.com")
    h = document_header(_master())
    assert h["linkedin"] == "linkedin.com/in/pat-example" and h["linkedin_href"] == GOOD
    link = f'<a href="{GOOD}" rel="noopener noreferrer">linkedin.com/in/pat-example</a>'
    assert link in render_html(RenderDoc(header=h, blocks=[]))
    assert link in letter_html({"name": "Pat Example", **h}, "Dear Hiring Team,\n\nHello.", "cover")
    off = document_header(_master(look={"linkedin": "off"}))
    assert "linkedin" not in off and "linkedin_href" not in off
    assert "linkedin.com" not in render_html(RenderDoc(header=off, blocks=[]))


def test_text_kept_as_typed_prints_as_plain_text_not_a_link():
    from kestrel.docs.catalog import RenderDoc, document_header
    from kestrel.docs.render import render_html

    with _client() as c:
        _save(c, linkedin_profile='<b>on</b> request "x"')
    h = document_header(_master())
    assert "linkedin_href" not in h
    html = render_html(RenderDoc(header=h, blocks=[]))
    assert "&lt;b&gt;on&lt;/b&gt; request" in html and "<b>on</b>" not in html
    assert 'href="' not in html.split('class="contact"')[1].split("</div>")[0]


def test_a_master_that_already_links_it_does_not_print_it_twice():
    from kestrel.docs.catalog import document_header

    with _client() as c:
        _save(c, linkedin_profile="linkedin.com/in/pat-example")
    h = document_header(_master(header={"links": ["linkedin.com/in/Pat-Example"]}))
    assert "linkedin" not in h


def test_look_checkbox_saves_the_per_document_choice():
    from kestrel.db.models import DocMaster
    from kestrel.web.deps import sessionmaker

    async def make():
        async with sessionmaker()() as s:
            m = _master()
            s.add(m)
            await s.commit()
            return m.id

    async def look(mid):
        async with sessionmaker()() as s:
            return dict((await s.get(DocMaster, mid)).look or {})

    mid = asyncio.run(make())
    with _client() as c:
        c.post(f"/documents/{mid}/look", data={"show_linkedin_sent": "1"})
        assert asyncio.run(look(mid)).get("linkedin") == "off"
        c.post(f"/documents/{mid}/look", data={"show_linkedin_sent": "1", "show_linkedin": "on"})
        assert "linkedin" not in asyncio.run(look(mid))
        c.post(f"/documents/{mid}/look", data={"show_linkedin_sent": "1"})
        c.post(f"/documents/{mid}/look", data={})  # an older form leaves the choice alone
        assert asyncio.run(look(mid)).get("linkedin") == "off"


def test_the_model_facing_profile_never_carries_the_address_or_the_stored_phone_forms():
    """Structure, not a text scan (D-PHONE-7): load_profile, the only profile any rule or prompt
    reads, drops the contact-only keys, while the documents still read them from the editor."""
    from kestrel.pipeline.profile import CONTACT_ONLY, load_profile

    with _client() as c:
        _save(c, linkedin_profile="linkedin.com/in/pat-example", phone="5555550123",
              phone_country="US")
    profile = load_profile("employment")
    for key in CONTACT_ONLY:
        assert not hasattr(profile, key), key
    assert "pat-example" not in repr(profile)
    block = pe.load_state().tracks["employment"]  # the documents' source still has them
    assert block["linkedin_profile"] == GOOD and block["phone_e164"] == "+15555550123"


def test_a_master_link_to_a_longer_handle_does_not_hide_the_address():
    """Round 2: the whole handle is compared, so ann is not hidden by a link to anna."""
    from kestrel.docs.catalog import document_header

    with _client() as c:
        _save(c, linkedin_profile="linkedin.com/in/ann")
    other = document_header(_master(header={"links": ["linkedin.com/in/anna"]}))
    assert other["linkedin_href"] == "https://www.linkedin.com/in/ann"
    for link in ("linkedin.com/in/ann", "https://www.LinkedIn.com/in/Ann/?trk=x"):
        assert "linkedin" not in document_header(_master(header={"links": [link]}))
    assert li.handle_of("https://www.linkedin.com/in/anna/") == "anna"


def _loopback_pair(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0):
    """socket.socketpair as Windows builds it: a listener and a connect over 127.0.0.1."""
    lsock = socket.socket(family, type, proto)
    try:
        lsock.bind(("127.0.0.1", 0))
        lsock.listen()
        csock = socket.socket(family, type, proto)
        csock.connect(lsock.getsockname())
        ssock, _ = lsock.accept()
    finally:
        lsock.close()
    return ssock, csock


def test_the_network_guard_lets_an_event_loop_start_on_a_loopback_socketpair(monkeypatch):
    monkeypatch.undo()  # drop the autouse guard, then guard again over a Windows-style pair
    monkeypatch.setattr(socket, "socketpair", _loopback_pair)
    _close_the_network(monkeypatch)
    assert asyncio.run(asyncio.sleep(0, result="started")) == "started"


def test_the_network_guard_still_refuses_a_loopback_connect():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(AssertionError, match="used the network"):
            s.connect(("127.0.0.1", 9))
    finally:
        s.close()
