"""NotificationService records rows from Phase 1; Phase 16 adds delivery + batching."""

from __future__ import annotations

import httpx
import respx
from sqlalchemy import select

from kestrel.config import get_settings
from kestrel.db.base import utcnow_iso
from kestrel.db.models import Notification
from kestrel.services.delivery import PER_ITEM_KIND, deliver_pending
from kestrel.services.notifications import NotificationService


def _quiet_settings():
    s = get_settings()
    s.notify.enabled = True
    s.notify.toast = False  # no real win11toast in the test
    s.notify.ntfy_topic = None
    s.notify.webhook_url = None
    s.notify.events = {}
    s.notify.per_item_scored = False
    return s


async def test_notification_service_writes_a_row(sm):
    svc = NotificationService(sm)
    nid = await svc.notify(
        "reranker_ready",
        "Reranker awaiting approval",
        body="200 labels reached",
        severity="success",
    )
    async with sm() as s:
        note = await s.get(Notification, nid)
    assert note.kind == "reranker_ready"
    assert note.severity == "success"
    assert note.title == "Reranker awaiting approval"
    assert note.read_at is None
    assert note.delivered_channels == []  # recorded, not yet delivered


async def test_invalid_severity_falls_back_to_info(sm):
    svc = NotificationService(sm)
    nid = await svc.notify("x", "y", severity="bogus")
    async with sm() as s:
        assert (await s.get(Notification, nid)).severity == "info"


async def test_batching_collapses_a_burst_into_one(sm):
    async with sm() as s:
        for i in range(20):
            s.add(Notification(kind="high_score", title=f"Opportunity {i} scored 4.7",
                               severity="info", delivered_channels=[], created_at=utcnow_iso()))
        await s.commit()
    async with sm() as s:
        result = await deliver_pending(s, _quiet_settings())
    # 20 events in one window -> exactly ONE delivered message, collapsing all 20.
    assert result.sent == 1
    assert result.collapsed == 20
    assert "20 notifications" in result.title
    async with sm() as s:
        rows = (await s.execute(select(Notification))).scalars().all()
    assert all(r.delivered_channels for r in rows)  # every one marked handled
    async with sm() as s:  # nothing left to deliver next cycle
        again = await deliver_pending(s, _quiet_settings())
    assert again.sent == 0


async def test_per_item_scored_is_suppressed_by_default(sm):
    async with sm() as s:
        s.add(Notification(kind=PER_ITEM_KIND, title="item 1 scored", delivered_channels=[],
                           created_at=utcnow_iso()))
        s.add(Notification(kind="run_finished", title="Run 3 finished: 412 evaluated",
                           delivered_channels=[], created_at=utcnow_iso()))
        await s.commit()
    async with sm() as s:
        result = await deliver_pending(s, _quiet_settings())
    assert result.sent == 1 and result.collapsed == 1 and result.suppressed == 1
    assert result.title == "Run 3 finished: 412 evaluated"


async def test_disabled_event_kind_is_not_delivered(sm):
    settings = _quiet_settings()
    settings.notify.events = {"drift_detected": False}
    async with sm() as s:
        s.add(Notification(kind="drift_detected", title="Drift detected; reranker demoted",
                           delivered_channels=[], created_at=utcnow_iso()))
        await s.commit()
    async with sm() as s:
        result = await deliver_pending(s, settings)
    assert result.sent == 0 and result.suppressed == 1


# ------------------------------------------------------------ pushes leave the machine: two rules

_EMPLOYER = "Northwind Freight"


def test_dispatch_never_blocks_the_event_loop():
    """_dispatch runs on uvicorn's single loop, where the worker and every request session live.
    A synchronous httpx.post there froze the worker and the UI for up to five seconds per push."""
    import inspect

    from kestrel.services import delivery

    assert inspect.iscoroutinefunction(delivery._dispatch)


async def _seed_confidential(sm) -> None:
    from kestrel.db.models import DocMaster, DocSection, Setting

    async with sm() as s:
        master = DocMaster(track="employment", name="Resume", doc_kind="resume")
        s.add(master)
        await s.flush()
        s.add(DocSection(doc_master_id=master.id, section_kind="experience", sort_order=0,
                         org_label=_EMPLOYER, org_label_redacted="[Confidential]"))
        s.add(Setting(key="confidential_mode", value=True))
        s.add(Notification(kind="high_score", title=f"{_EMPLOYER} scored 4.7",
                           body=f"VP of IT at {_EMPLOYER}: apply", severity="info",
                           delivered_channels=[], created_at=utcnow_iso()))
        await s.commit()


@respx.mock
async def test_push_channels_are_redacted_in_confidential_mode(sm):
    """ntfy and the webhook are the two places a notification LEAVES the machine. Confidential
    mode must redact the current employer there, or a push titled "Northwind Freight scored 4.7"
    hands the employer the operator is confidentially leaving to a third-party push service."""
    ntfy = respx.post("https://ntfy.sh/kestrel-test").mock(return_value=httpx.Response(200))
    hook = respx.post("https://hooks.example/kestrel").mock(return_value=httpx.Response(200))
    await _seed_confidential(sm)
    settings = _quiet_settings()
    settings.notify.ntfy_topic = "kestrel-test"
    settings.notify.webhook_url = "https://hooks.example/kestrel"

    async with sm() as s:
        result = await deliver_pending(s, settings)

    assert result.sent == 1 and result.channels == ["ntfy", "webhook"]
    assert ntfy.called and hook.called
    ntfy_req = ntfy.calls[0].request
    hook_req = hook.calls[0].request
    assert _EMPLOYER not in ntfy_req.content.decode("utf-8")
    assert _EMPLOYER not in ntfy_req.headers["Title"]
    assert _EMPLOYER not in hook_req.content.decode("utf-8")
    assert "[Confidential]" in hook_req.content.decode("utf-8")
    assert _EMPLOYER not in (result.title or "")


@respx.mock
async def test_one_bad_settings_row_neither_stops_a_push_nor_unredacts_it(sm):
    """When a settings row no longer validates the delivery loop runs on the settings file
    (kestrel.web.app). Confidential mode is a row too, so it is read by itself: the push still
    goes, and it still hides the employer."""
    from kestrel.db.models import Setting

    ntfy = respx.post("https://ntfy.sh/kestrel-test").mock(return_value=httpx.Response(200))
    await _seed_confidential(sm)
    async with sm() as s:
        s.add(Setting(key="thresholds.apply", value="not a number"))
        await s.commit()
    settings = _quiet_settings()
    settings.notify.ntfy_topic = "kestrel-test"

    async with sm() as s:
        result = await deliver_pending(s, settings)

    assert result.sent == 1 and result.channels == ["ntfy"]
    sent = ntfy.calls[0].request
    assert _EMPLOYER not in sent.content.decode("utf-8")
    assert _EMPLOYER not in sent.headers["Title"]


@respx.mock
async def test_push_channels_carry_the_real_name_when_confidential_mode_is_off(sm):
    hook = respx.post("https://hooks.example/kestrel").mock(return_value=httpx.Response(200))
    async with sm() as s:
        s.add(Notification(kind="high_score", title=f"{_EMPLOYER} scored 4.7", severity="info",
                           delivered_channels=[], created_at=utcnow_iso()))
        await s.commit()
    settings = _quiet_settings()
    settings.notify.webhook_url = "https://hooks.example/kestrel"
    async with sm() as s:
        await deliver_pending(s, settings)
    assert _EMPLOYER in hook.calls[0].request.content.decode("utf-8")


@respx.mock
async def test_a_failed_channel_never_raises(sm):
    respx.post("https://hooks.example/kestrel").mock(side_effect=httpx.ConnectError("down"))
    async with sm() as s:
        s.add(Notification(kind="high_score", title="x scored 4.7", severity="info",
                           delivered_channels=[], created_at=utcnow_iso()))
        await s.commit()
    settings = _quiet_settings()
    settings.notify.webhook_url = "https://hooks.example/kestrel"
    async with sm() as s:
        result = await deliver_pending(s, settings)
    assert result.sent == 1 and result.channels == []  # handled, delivered nowhere
