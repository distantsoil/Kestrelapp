"""#207: the phone field through the real routes and documents (D-PHONE-1 to D-PHONE-5).
Invented numbers only."""

from __future__ import annotations

import ipaddress
import re
import socket

import pytest
import yaml
from fastapi.testclient import TestClient

from kestrel.services import profile_edit as pe


def _client() -> TestClient:
    from kestrel.web.app import build_app

    return TestClient(build_app(worker=False))


def _stored() -> dict:
    return yaml.safe_load(pe.profile_path().read_text(encoding="utf-8"))["employment"]


def _save(c: TestClient, **data):
    return c.post("/profile/employment/about", data={"first_name": "Pat", **data})


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    import asyncio

    from kestrel import paths
    from kestrel.db import migrate
    from kestrel.db.engine import forget_engine, reset_engine
    from kestrel.web import routes_control

    async def _down(*a, **k):
        return None

    monkeypatch.setattr(routes_control, "_probe_ollama", _down, raising=False)
    asyncio.run(reset_engine())
    for suffix in ("", "-wal", "-shm"):
        paths.db_path().with_name(paths.db_path().name + suffix).unlink(missing_ok=True)
    migrate.run_migrations()
    forget_engine()
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def connect(sock, address, *a, **k):
        if not _loopback(address[0] if isinstance(address, tuple) else address):
            raise AssertionError("phone handling used the network")
        return real_connect(sock, address, *a, **k)

    def getaddrinfo(host, *a, **k):
        if not _loopback(host):
            raise AssertionError("phone handling used the network")
        return real_getaddrinfo(host, *a, **k)

    # The Windows event loop connects a socket pair over loopback for its own wake-ups, so only
    # an address off this machine fails the test.
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def _loopback(host: object) -> bool:
    """True only for an address that cannot leave this machine: localhost or a loopback IP."""
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def test_the_guard_lets_loopback_through_as_the_windows_event_loop_needs():
    """The Windows proactor loop wakes itself through a socket pair joined over 127.0.0.1."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client.connect(server.getsockname())
        peer, _ = server.accept()
        peer.close()
        assert socket.getaddrinfo("localhost", 80)
    finally:
        client.close()
        server.close()


@pytest.mark.parametrize("address", [("203.0.113.7", 443), ("2001:db8::1", 443, 0, 0),
                                     ("example.invalid", 80)])
def test_the_guard_still_fails_any_connect_off_this_machine(address):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # the guard runs before the OS
    try:
        with pytest.raises(AssertionError, match="used the network"):
            sock.connect(address)
    finally:
        sock.close()
    with pytest.raises(AssertionError, match="used the network"):
        socket.getaddrinfo(address[0], address[1])


def test_format_endpoint_formats_and_never_refuses():
    with _client() as c:
        r = c.post("/api/phone/format", data={"phone": "(555) 555.0123", "country": "US"})
        assert r.status_code == 200
        assert r.json()["display"] == "555-555-0123" and r.json()["readable"] is True
        assert r.json()["e164"] == "+15555550123" and r.json()["note"] == ""
        r = c.post("/api/phone/format", data={"phone": "ask for Dana", "country": "US"})
        j = r.json()
        assert r.status_code == 200 and j["display"] == "ask for Dana" and j["readable"] is False
        assert j["note"] == ("Kestrel could not read this as a phone number; "
                             "it is kept as you typed it.")
        r = c.post("/api/phone/format", data={})
        assert r.status_code == 200 and r.json()["display"] == "" and r.json()["note"] == ""


@pytest.mark.parametrize("typed,country,shown,e164", [
    ("5555550123", "US", "555-555-0123", "+15555550123"),
    ("+1 555 555 0123", "US", "555-555-0123", "+15555550123"),
    ("020 7946 0123", "GB", "020 7946 0123", "+442079460123"),
    ("+49 30 123456", "DE", "030 123456", "+4930123456"),
    ("555.555.0123 ext 42", "US", "555-555-0123 x42", "+15555550123"),
])
def test_profile_save_stores_display_entered_and_canonical(typed, country, shown, e164):
    with _client() as c:
        r = _save(c, phone=typed, phone_country=country)
        assert r.status_code == 200 and "pf-saved" in r.headers["hx-trigger"]
        b = _stored()
        assert b["phone"] == shown and b["phone_e164"] == e164 and b["phone_entered"] == typed
        assert b["phone_country"] == country
        page = c.get("/profile?tab=about").text
        assert f'value="{shown}"' in page and 'name="phone_country"' in page
        assert f'<option value="{country}" selected>' in page


def test_profile_save_keeps_unreadable_text_exactly_and_saves():
    with _client() as c:
        r = _save(c, phone="  call after 5 (ask Dana)  ")
        assert r.status_code == 200 and "pf-saved" in r.headers["hx-trigger"]
        b = _stored()
        assert b["phone"] == "call after 5 (ask Dana)" and b["phone_e164"] == ""


def test_clearing_the_phone_clears_every_phone_field():
    with _client() as c:
        _save(c, phone="5555550123", phone_country="US")
        _save(c, phone="", phone_country="US")
        b = _stored()
        assert b["phone"] == "" and b["phone_e164"] == "" and b["phone_entered"] == ""


def test_documents_print_the_stored_number_with_its_digits_unchanged():
    from kestrel.db.models import DocMaster
    from kestrel.docs.catalog import RenderDoc, document_header
    from kestrel.docs.render import render_html

    with _client() as c:
        _save(c, phone="(555) 555 0123", phone_country="US")
    master = DocMaster(track="employment", name="r", doc_kind="resume", header={},
                       summary_variants=[], skill_groups=[])
    header = document_header(master)
    assert header["phone"] == "555-555-0123"
    html = render_html(RenderDoc(header=header, blocks=[]))
    assert "555-555-0123" in html
    assert re.sub(r"\D", "", header["phone"]) == "5555550123"  # digits never changed


def test_contact_phone_shows_formatted_and_the_stored_text_is_untouched():
    from kestrel.web import company_view

    assert company_view._phone_shown("5555550123") in ("555-555-0123", "+1 555-555-0123")
    assert company_view._phone_shown("front desk") == "front desk"
    assert company_view._phone_shown(None) is None


def test_a_foreign_number_keeps_its_plus_through_the_save_and_onto_the_resume():
    """Round 2 (HIGH): +44 typed on a US home used to save as 020 7946 0000, losing the +44."""
    from kestrel.db.models import DocMaster
    from kestrel.docs.catalog import RenderDoc, document_header
    from kestrel.docs.render import render_html

    with _client() as c:
        _save(c, phone="+44 20 7946 0000", phone_country="US")
        b = _stored()
        assert b["phone"] == "+44 20 7946 0000" and b["phone_e164"] == "+442079460000"
        assert b["phone_country"] == "US" and b["phone_entered"] == "+44 20 7946 0000"
        _save(c, phone=b["phone"], phone_country=b["phone_country"])  # saving it again
        assert _stored()["phone"] == "+44 20 7946 0000"
        assert 'value="+44 20 7946 0000"' in c.get("/profile?tab=about").text
        # The same number with the home country changed on purpose
        _save(c, phone="+44 20 7946 0000", phone_country="GB")
        assert _stored()["phone"] == "020 7946 0000" and _stored()["phone_e164"] == "+442079460000"
        _save(c, phone="+39 06 6982 0123", phone_country="US")
    master = DocMaster(track="employment", name="r", doc_kind="resume", header={},
                       summary_variants=[], skill_groups=[])
    header = document_header(master)
    assert header["phone"] == "+39 06 6982 0123"
    assert "+39 06 6982 0123" in render_html(RenderDoc(header=header, blocks=[]))
    assert re.sub(r"\D", "", header["phone"]) == re.sub(r"\D", "", _stored()["phone_e164"])


def test_the_endpoint_reformats_from_the_stored_number_when_the_home_country_changes():
    with _client() as c:
        r = c.post("/api/phone/format", data={"phone": "020 7946 0000", "country": "US",
                                              "e164": "+442079460000", "text_country": "GB"})
        assert r.json()["display"] == "+44 20 7946 0000" and r.json()["region"] == "GB"


def _hidden(page: str, name: str) -> str:
    return re.search(rf'name="{name}" value="([^"]*)"', page).group(1)


def test_the_page_pins_a_legacy_number_under_the_home_it_was_saved_with(monkeypatch):
    """Round 3 (1): a number stored before E.164 existed is resolved once on render."""
    from kestrel import phone

    monkeypatch.setattr(phone, "locale_region", lambda: "US")
    path = pe.profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"employment": {
        "track": "employment", "first_name": "Pat", "phone": "555-555-0123"}}), encoding="utf-8")
    with _client() as c:
        page = c.get("/profile?tab=about").text
        assert _hidden(page, "phone_e164") == "+15555550123"
        # The person then switches the home country to GB and saves, with no script at all.
        _save(c, phone="555-555-0123", phone_country="GB", phone_e164="+15555550123",
              )
        b = _stored()
        assert b["phone_e164"] == "+15555550123" and b["phone"] == "+1 555-555-0123"
        assert b["phone_country"] == "GB"


def test_the_submitted_e164_is_the_source_when_the_text_still_matches_it():
    """Round 3 (2): changing the country and saving before the reformat reply, or with no
    script, must not read the old text under the new country."""
    with _client() as c:
        _save(c, phone="5555550123", phone_country="US")
        page = c.get("/profile?tab=about").text
        e164, text = _hidden(page, "phone_e164"), "555-555-0123"
        assert e164 == "+15555550123"
        _save(c, phone=text, phone_country="GB", phone_e164=e164)
        assert _stored()["phone_e164"] == "+15555550123"  # not +445555550123
        # Text edited since the number was pinned: read afresh under the selector.
        _save(c, phone="020 7946 0000", phone_country="GB", phone_e164=e164)
        assert _stored()["phone_e164"] == "+442079460000"
        # An older form that sends no pinned number reads the text under the selector, as before.
        _save(c, phone="020 7946 0000", phone_country="GB")
        assert _stored()["phone_e164"] == "+442079460000"


def test_an_extension_survives_a_change_of_home_country():
    with _client() as c:
        _save(c, phone="555-555-0123 ext 42", phone_country="US")
        page = c.get("/profile?tab=about").text
        e164, text = _hidden(page, "phone_e164"), "555-555-0123 x42"
        assert e164 == "+15555550123;ext=42"
        _save(c, phone=text, phone_country="GB", phone_e164=e164)
        b = _stored()
        assert b["phone_e164"] == "+15555550123" and b["phone_ext"] == "42"
        assert b["phone"] == "+1 555-555-0123 x42"


def test_the_endpoint_returns_the_pinned_source_with_the_extension():
    with _client() as c:
        r = c.post("/api/phone/format", data={"phone": "+1 555-555-0123 x42", "country": "GB",
                                              "e164": "+15555550123;ext=42"})
        j = r.json()
        assert j["display"] == "+1 555-555-0123 x42" and j["source"] == "+15555550123;ext=42"
        assert j["e164"] == "+15555550123"


def test_a_mismatched_or_invalid_hint_never_wins_over_the_text():
    """Round 4 (2, 3): the page's E.164 is a hint, used only when it is the number typed."""
    with _client() as c:
        _save(c, phone="555-555-0123", phone_country="US", phone_e164="+447911123456")
        b = _stored()
        assert b["phone_e164"] == "+15555550123" and b["phone"] == "555-555-0123"  # not +44
        _save(c, phone="555-555-0123", phone_country="US", phone_e164="+1555")
        b = _stored()
        assert b["phone_e164"] == "+15555550123" and b["phone"] == "555-555-0123"  # not wiped


def test_the_server_trusts_its_own_stored_pin_not_the_pages():
    with _client() as c:
        _save(c, phone="+44 20 7946 0000", phone_country="US")
        stored = _stored()
        assert stored["phone_e164"] == "+442079460000"
        # The same text with a different number in the page's hint: the stored pin decides.
        _save(c, phone=stored["phone"], phone_country="US", phone_e164="+15555550123")
        assert _stored()["phone_e164"] == "+442079460000"


def test_an_edit_that_failed_to_save_is_not_replaced_by_the_old_number_on_resubmit():
    """Round 4 (1), Profile editor: the old E.164 rides along with newly typed text."""
    with _client() as c:
        _save(c, phone="5555550123", phone_country="US")
        old = _stored()["phone_e164"]
        assert old == "+15555550123"
        refused = c.post("/profile/employment/about", data={
            "first_name": "", "phone": "020 7946 0000", "phone_country": "GB",
            "phone_e164": old})
        assert "pf-save-refused" in refused.headers["hx-trigger"]
        assert _stored()["phone_e164"] == old  # a refused save changes nothing
        _save(c, phone="020 7946 0000", phone_country="GB", phone_e164=old)  # resubmitted
        b = _stored()
        assert b["phone_e164"] == "+442079460000" and b["phone"] == "020 7946 0000"


def test_changing_the_country_with_no_script_and_no_hint_keeps_the_stored_number():
    """The server's own record pins the number: the same text saved under a new home country
    does not become a different country's number."""
    with _client() as c:
        _save(c, phone="5555550123", phone_country="US")
        assert _stored()["phone"] == "555-555-0123"
        _save(c, phone="555-555-0123", phone_country="GB")  # nothing but the text and the country
        b = _stored()
        assert b["phone_e164"] == "+15555550123" and b["phone"] == "+1 555-555-0123"
        assert b["phone_country"] == "GB"


def _write_block(**fields) -> None:
    path = pe.profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"employment": {
        "track": "employment", "first_name": "Pat", **fields}}), encoding="utf-8")


def test_a_stored_e164_that_disagrees_with_its_text_is_rebuilt_never_adopted():
    """Round 5 (1): an imported, restored or hand-edited profile.yml whose E.164 is not the
    number its own text reads as must not save the E.164."""
    _write_block(phone="555-555-0123", phone_e164="+442079460000", phone_country="US")
    with _client() as c:
        _save(c, phone="555-555-0123", phone_country="US")
        assert _stored()["phone_e164"] == "+15555550123"
    _write_block(phone="555-555-0123", phone_e164="+442079460000", phone_country="US")
    with _client() as c:  # and under a different selector the text is still read as written
        _save(c, phone="555-555-0123", phone_country="GB")
        b = _stored()
        assert b["phone_e164"] == "+15555550123" and b["phone"] == "+1 555-555-0123"


def test_the_region_is_the_servers_never_a_forged_hint(monkeypatch):
    """Round 5 (2): a hint cannot pick the country the text is read under."""
    with _client() as c:
        _save(c, phone="020 7946 0000", phone_country="US", phone_e164="+442079460000")
        b = _stored()  # read under the US selector, as the server sees it: not a UK number
        assert b["phone_e164"] != "+442079460000" and b["phone"] == "020 7946 0000"
    # A number stored before E.164 existed is read under the stored home, not a hint.
    _write_block(phone="555-555-0123", phone_country="US")
    with _client() as c:
        _save(c, phone="555-555-0123", phone_country="GB", phone_e164="+447911123456")
        assert _stored()["phone_e164"] == "+15555550123"
    # No stored home at all: the default is the computer's locale (patched here), then pinned.
    monkeypatch.setattr("kestrel.phone.locale_region", lambda: "US")
    _write_block(phone="555-555-0123")
    with _client() as c:
        _save(c, phone="555-555-0123", phone_country="GB")
        b = _stored()
        assert b["phone_e164"] == "+15555550123" and b["phone_country"] == "GB"
        _save(c, phone=b["phone"], phone_country="DE")
        assert _stored()["phone_e164"] == "+15555550123"


def test_a_client_sent_phone_entered_is_validated_never_stored_as_sent():
    """Round 8: the raw spelling kept beside a number is metadata. A page can only supply a short
    spelling of the SAME number; a long value, markup or a different number is replaced by the
    typed text, and nothing the page sends can bloat or contradict the stored record."""
    eighty = "555" + "-" * 70 + "5550123"
    assert len(eighty) == 80

    def entered_after(c, sent: str) -> str:
        _save(c, phone="", phone_country="US")  # new text each time: the stored record is empty
        _save(c, phone="555-555-0123", phone_country="US", phone_entered=sent)
        return _stored()["phone_entered"]

    with _client() as c:
        assert entered_after(c, "X" * 5000 + "<script>") == "555-555-0123"
        assert entered_after(c, "+1 999 9999999") == "555-555-0123"  # a different number
        assert entered_after(c, "(555) 555.0123") == "(555) 555.0123"  # the same number, respelled
        # The bound is its own hatch: 80 characters pass, 81 do not.
        assert entered_after(c, eighty + "-") == "555-555-0123"
        assert entered_after(c, eighty) == eighty
        # Text that did not change keeps the SERVER's stored spelling, whatever the page sends.
        _save(c, phone="555-555-0123", phone_country="US", phone_entered="(555) 555.0123")
        assert _stored()["phone_entered"] == eighty


def test_a_stored_phone_entered_that_is_not_the_stored_number_is_rebuilt():
    """The server's own record is rebuilt, not adopted, when it is oversized or disagrees."""
    _write_block(phone="555-555-0123", phone_e164="+15555550123", phone_country="US",
                 phone_entered="<script>" + "Y" * 5000)
    with _client() as c:
        _save(c, phone="555-555-0123", phone_country="US")
        assert _stored()["phone_entered"] == "555-555-0123"
    _write_block(phone="555-555-0123", phone_e164="+15555550123", phone_country="US",
                 phone_entered="(555) 555.0123")
    with _client() as c:
        _save(c, phone="555-555-0123", phone_country="US")
        assert _stored()["phone_entered"] == "(555) 555.0123"  # a good stored spelling is kept


def test_each_page_loads_the_phone_assets_exactly_once():
    """Round 8: phone_field.html and linkedin_field.html each emitted their own tags."""
    with _client() as c:
        page = c.get("/profile?tab=about").text
        assert page.count("js/phone.js") == 1 and page.count("css/pages/phone.css") == 1
        assert "data-linkedin-input" in page and "data-phone-input" in page
