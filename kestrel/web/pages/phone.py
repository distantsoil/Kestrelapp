"""The phone formatter's one stateless endpoint (#207, D-PHONE-1 to D-PHONE-5).

It formats the text the person typed and returns it. It stores nothing, calls nothing outside
this computer, and never refuses: an unreadable entry comes back as typed, with the note."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from kestrel import linkedin, phone

router = APIRouter()


@router.post("/api/phone/format")
async def format_phone(request: Request) -> JSONResponse:
    form = await request.form()
    # `text_country` is the country the shown text was last formatted under, so a change of home
    # country reformats the pinned number. The page only uses the answer to show it: what is
    # saved is decided again from the server's own record.
    res = phone.format_phone(form.get("phone"), form.get("country"), form.get("e164"),
                             read_as=form.get("text_country"))
    return JSONResponse({
        "display": res.display, "region": res.region, "readable": res.readable,
        "e164": res.e164, "source": phone.source_of(res.e164, res.ext), "note": res.note,
    })


@router.post("/api/linkedin/normalize")
async def normalize_linkedin(request: Request) -> JSONResponse:
    """Shape only, no lookup: the address is the person's own text and is never fetched."""
    form = await request.form()
    res = linkedin.normalize(form.get("linkedin"))
    return JSONResponse({"display": res.display, "readable": res.readable, "note": res.note})
