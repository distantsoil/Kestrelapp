"""Tier 3 escalation UI (SPEC.md 6.4, hard rule 1). The "Deeper read with a stronger model"
button (D-UI-45; formerly "Escalate to Tier 3") POSTs to `/opportunities/{id}/escalate`, which
shows a PREVIEW: the payload,
the destination (provider, base URL, model), whether confidential redaction was applied, and an
estimated token cost. NOTHING is transmitted until the operator submits the confirm form, whose
hidden confirmation_token binds the send to the previewed payload byte-for-byte. If the frontier API
key is not configured the preview renders WITHOUT a confirm button. Tier 3 is the only path that
sends operator data off the machine, and it is never automatic."""

from __future__ import annotations

import html

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from kestrel.llm import client as llm_client
from kestrel.llm.service_name import service_name
from kestrel.pipeline.tier3 import build_escalation_preview, transmit_escalation
from kestrel.services.setting_overrides import load_settings_with_db
from kestrel.web.deps import sessionmaker
from kestrel.web.fit import fit_word_for_verdict

router = APIRouter()


def _esc(s: str | None) -> str:
    return html.escape(s or "")


@router.post("/opportunities/{opp_id}/escalate", response_class=HTMLResponse)
async def escalate_preview(opp_id: int):
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        preview = await build_escalation_preview(s, settings, opp_id)
    if preview.error:
        return HTMLResponse(
            f'<div class="warnbox">No deeper read possible: {_esc(preview.error)}</div>'
        )
    from kestrel.web.routes_pages import _cost_line

    cost = _cost_line(preview.model, preview.prompt_tokens_estimate)
    service = service_name(preview.base_url)

    payload = "\n\n".join(f"[{m['role']}]\n{_esc(m['content'])}" for m in preview.messages)
    conf = (
        '<span class="badge">employer redacted</span>'
        if preview.confidential
        else '<span class="muted">confidential mode off</span>'
    )
    # Plain words (D-UI-45): what is sent, where, and what it costs, before the one button.
    warn = (
        f'<div class="warnbox">Sending would put the text below on '
        f'{_esc(service)} ({_esc(preview.base_url)}), off this computer. '
        f"{_esc(preview.unavailable)}</div>"
        if preview.unavailable
        else '<div class="warnbox">Sending puts the text below on '
        f'{_esc(service)} ({_esc(preview.base_url)}), off this computer. '
        "Read it first; nothing goes until you press Send.</div>"
    )
    # Plain summary above the payload: what leaves, where, and that it
    # includes the job + your profile
    summary = (
        f'<p class="muted" style="margin:.3rem 0 .6rem">This sends the job posting and your '
        f'profile to {_esc(service)}, using the model '
        f'<span class="mono">{_esc(preview.model)}</span>. '
        f"Confidential redaction: {('on' if preview.confidential else 'off')}.</p>"
    )
    confirm_form = (
        ""
        if preview.unavailable
        else (
            f'<form hx-post="/opportunities/{opp_id}/escalate/confirm" hx-target="#t3-result" '
            f'hx-swap="innerHTML">'
            f'<input type="hidden" name="confirmation_token" '
            f'value="{_esc(preview.confirmation_token)}">'
            f'<button type="submit" class="danger">Send for a deeper read</button>'
            f'</form>'
        )
    )
    return HTMLResponse(
        f'<div class="panel" id="t3-preview">'
        f"<h4>Deeper read with a stronger model: what would be sent</h4>{warn}{summary}"
        f'<div class="muted">To <span class="mono">{_esc(preview.base_url)}</span> '
        f"&middot; model <span class=\"mono\">{_esc(preview.model)}</span> &middot; "
        f"about {preview.prompt_tokens_estimate} tokens in, {_esc(cost)} &middot; {conf}</div>"
        f'<pre style="max-height:22rem;overflow:auto;white-space:pre-wrap">{payload}</pre>'
        f"{confirm_form}"
        f'<div id="t3-result"></div>'
        f"</div>"
    )


@router.post("/opportunities/{opp_id}/escalate/confirm", response_class=HTMLResponse)
async def escalate_confirm(opp_id: int, request: Request):
    form = await request.form()
    token = str(form.get("confirmation_token") or "")
    async with sessionmaker()() as s:
        settings = await load_settings_with_db(s)
        async with httpx.AsyncClient() as http:
            client = llm_client.LLMClient(settings, http=http)
            res = await transmit_escalation(
                s, client, settings, opp_id, confirmation_token=token
            )
    if res.sent and res.evaluation_id:
        word = fit_word_for_verdict(res.verdict, res.score) or res.verdict
        return HTMLResponse(
            f'<div class="panel">Sent (record #{res.transmission_id}). The stronger model says '
            f"<strong>{_esc(word)}</strong>. "
            "Reload to see its read.</div>"
        )
    if res.sent:
        return HTMLResponse(
            f'<div class="warnbox">Sent (record #{res.transmission_id}) but no usable '
            f"answer came back: {_esc(res.error)}</div>"
        )
    return HTMLResponse(f'<div class="warnbox">Nothing was sent: {_esc(res.error)}</div>')
