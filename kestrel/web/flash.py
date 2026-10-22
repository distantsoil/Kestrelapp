"""One-line notices carried by a redirect as `?msg=<code>` (WP-08, #21).

The query string is attacker-controllable (a crafted LAN link), so the page never prints it. The
code is looked up here and the authored sentence is shown; an unknown code shows nothing."""

from __future__ import annotations

from urllib.parse import quote

from fastapi.responses import RedirectResponse

FLASH_MESSAGES: dict[str, str] = {
    "reset_confirm_needed": "Nothing was cleared: type RESET in the box to confirm.",
    "reset_stopped": (
        "The reset stopped part way. Your backup is in the Kestrel-backups folder beside your "
        "data folder."
    ),
    "reset_busy": (
        "Kestrel is busy with a run, a restore or another reset. Nothing was deleted. Try "
        "again in a minute."
    ),
    "reset_backup_failed": "The backup before the reset failed, so nothing was deleted.",
    "reset_files_in_use": (
        "Kestrel was reset, but some files were in use and stayed. Close them and they can be "
        "deleted by hand."
    ),
    "mode_local": "Kestrel runs on local models. Steps 2 to 5 apply.",
    "mode_both": (
        "Local models for daily runs, a hosted set ready to switch to. Steps 2 to 7 apply."
    ),
    "mode_hosted": "Kestrel runs on hosted models.",
    "mode_hosted_pending": (
        "Hosted mode will apply once step 6 is complete under Settings > Hosted."
    ),
    "profile_recorded": "Hardware profile recorded.",
}


_KNOWN_CODES: dict[str, str] = {k: k for k in FLASH_MESSAGES}


def flash_text(code: str | None) -> str | None:
    """The authored sentence for a known code, else None (unknown codes are ignored silently)."""
    return FLASH_MESSAGES.get(code or "")


def flash_redirect(path: str, code: str, anchor: str = "") -> RedirectResponse:
    """A 303 to `path` carrying only a KNOWN code (an unknown one is dropped, so form input never
    rides into the URL); `anchor` is the fragment without its '#'."""
    # The URL carries the allowlist's own copy of the code, never the caller's string, so nothing
    # derived from form input reaches the redirect (CodeQL on #14).
    known = _KNOWN_CODES.get(code)
    query = f"?msg={quote(known)}" if known else ""
    return RedirectResponse(f"{path}{query}{'#' + anchor if anchor else ''}", status_code=303)
