"""The error code registry: every code a person can see is defined here and nowhere else.

A code is `AREA-NNN` (SPEC-INTERFACES.md §2.7, D-ERR-1). Codes are permanent: a code is never
renumbered, reused or deleted. A code no longer used moves, whole, from `_ALL` to `_RETIRED_ALL`
with the date it was retired, so an old stored row can still show it. `tests/test_error_codes.py`
holds the frozen list of every code ever issued, and `scripts/error_code_base.py` (run by
`scripts/check.py`) compares this file with the one at the branch's merge base with main.

Entries are grouped by area, one block per area in `AREAS` order, and a branch appends only to the
blocks of the areas it owns (D-ERR-4), so parallel branches never edit the same lines.

Titles, meanings and fixes are plain American English with no em dash, and never hold a path, a
key, an address, an organization name, exception text or a class name. `render_table` builds the
help table that `scripts/error_codes_table.py` writes to `docs/guide/error-codes.md`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorCode:
    code: str
    area: str
    title: str
    meaning: str
    fix: str
    since: str


AREAS: dict[str, str] = {
    "STA": "Starting Kestrel",
    "DAT": "Your data, backup and restore",
    "MDL": "AI models",
    "ONL": "Online models and consent",
    "SRC": "Job sources and searches",
    "RUN": "Runs and scoring",
    "IMP": "Importing files",
    "DOC": "Documents and PDFs",
    "NET": "Other devices and access",
    "MCP": "The MCP server",
    "UPD": "Updates",
    "CHK": "Health checks",
    "APP": "Anything else",
}


@dataclass(frozen=True)
class RetiredCode:
    entry: ErrorCode
    retired: str  # YYYY-MM-DD


def _code(code: str, title: str, meaning: str, fix: str, since: str = "0.7.7") -> ErrorCode:
    return ErrorCode(code=code, area=code[:3], title=title, meaning=meaning, fix=fix, since=since)


def _retired(entry: ErrorCode, retired: str) -> RetiredCode:
    return RetiredCode(entry=entry, retired=retired)


_ALL: tuple[ErrorCode, ...] = (
    # ---- STA, Starting Kestrel. Seeded from the startup card's own codes, in this order:
    # server_still_closing, safe_mode_offer, newer_database, migrations_failed, no_port,
    # health_never_up (kestrel/desktop/shell.py, kestrel/startup_health.py).
    _code(
        "STA-001",
        "The last start is still closing",
        "Kestrel's local server from the last attempt was still closing, so Kestrel did not "
        "start a second one.",
        "Wait a moment, then choose Try again.",
    ),
    _code(
        "STA-002",
        "Kestrel did not start twice in a row",
        "Kestrel could not start the last two times.",
        "Choose Start with optional features off. Your roles, settings and files stay as they are.",
    ),
    _code(
        "STA-003",
        "Your data is from a newer version",
        "Your data was saved by a newer version of Kestrel than the one you opened.",
        "Choose Download latest and install it, then open Kestrel again. Your data stays as it is.",
    ),
    _code(
        "STA-004",
        "Kestrel could not open your data",
        "Kestrel could not open your data on this computer while it was starting.",
        "Choose Try again. If it stops again, choose Show details to see the step that stopped, "
        "and include the log file from Open logs folder when you ask for help.",
    ),
    _code(
        "STA-005",
        "Another app is using the connection",
        "Another app was using the connection Kestrel needs on this computer.",
        "Close other apps, then choose Try again.",
    ),
    _code(
        "STA-006",
        "The local server did not answer",
        "Kestrel's local server did not answer in time, or stopped while it was starting.",
        "If the card says the server did not answer in time, choose Try again. If it says the "
        "server stopped while starting, fix the cause named in Show details first, then choose "
        "Try again.",
    ),
    _code(
        "STA-007",
        "The last session is still closing",
        "A relaunch waited a minute, but the last Kestrel session had not finished closing.",
        "Wait a moment, then choose Try again.",
    ),
    _code(
        "STA-008",
        "The start was canceled",
        "You chose Cancel while Kestrel was starting, so it stopped on purpose.",
        "Choose Try again to start Kestrel, or Quit to close it.",
    ),
    # ---- DAT, Your data, backup and restore.
    # ---- MDL, AI models.
    _code(
        "MDL-001",
        "The AI request did not finish",
        "A request to an AI model did not finish, and Kestrel could not tell the exact cause "
        "from the kind of failure.",
        "Try again. If it keeps happening, run the checks on the Models page and include the "
        "details when you ask for help.",
    ),
    _code(
        "MDL-002",
        "The local AI stopped while loading",
        "The AI engine on this computer stopped while it was loading the model.",
        "Restart Ollama, then try again. If it keeps happening, pick a smaller model on the "
        "Models page.",
    ),
    _code(
        "MDL-003",
        "The AI took too long to answer",
        "A request to an AI model waited longer than Kestrel allows, so Kestrel stopped waiting.",
        "Try again. A large model on a busy computer can be slow, and a smaller model on the "
        "Models page answers sooner.",
    ),
    _code(
        "MDL-004",
        "Kestrel could not reach the AI",
        "Kestrel could not connect to the service that runs the AI model.",
        "If you use models on this computer, open Ollama. If you use an online model, check that "
        "this computer is online. Then try again.",
    ),
    _code(
        "MDL-005",
        "The AI service asked Kestrel to slow down",
        "The service that runs the model said it had too many requests and asked Kestrel to wait.",
        "Wait a minute, then try again.",
    ),
    _code(
        "MDL-006",
        "A model download or build stalled",
        "The AI engine on this computer sent no progress on a model download or build for a long "
        "time, so Kestrel stopped waiting.",
        "Choose Try again. If it stalls again, check that this computer is online and that Ollama "
        "is still open.",
    ),
    _code(
        "MDL-007",
        "The local AI engine reported an error",
        "The AI engine on this computer answered a model download or build with an error of its "
        "own.",
        "Read the message beside the code, check the model name and the free disk space, then "
        "choose Try again.",
    ),
    # ---- ONL, Online models and consent.
    _code(
        "ONL-001",
        "The online service did not accept the key",
        "The online service said the API key Kestrel sent is not valid or not allowed.",
        "Check the key for that service under Settings, Hosted, then try again.",
    ),
    _code(
        "ONL-002",
        "The online account cannot pay for this request",
        "The online service said the account is out of credit, over its limit, or short of the "
        "credit this request needs.",
        "Add credit or raise the limit with the provider, then try again.",
    ),
    _code(
        "ONL-003",
        "Nothing was sent: the send was not confirmed",
        "Kestrel sent nothing online because the dialog that says what will be sent was not "
        "confirmed.",
        "Open the dialog again, read what would be sent, tick the box and confirm.",
    ),
    _code(
        "ONL-004",
        "Nothing was sent: the destination changed",
        "Kestrel sent nothing online because the model or online service changed after the "
        "dialog opened.",
        "Press the button again to see what would be sent now, then confirm.",
    ),
    _code(
        "ONL-005",
        "Nothing was sent: confidential mode held it",
        "Kestrel sent nothing online because confidential mode is on and no current employer is "
        "chosen.",
        "Choose your current employer in Settings, then try again.",
    ),
    _code(
        "ONL-006",
        "Nothing was sent: Hosted mode is off",
        "Kestrel sent nothing online because online models need Hosted mode, which is off.",
        "Turn on Hosted mode under Settings, Hosted, or pick a model on this computer.",
    ),
    # ---- SRC, Job sources and searches.
    _code(
        "SRC-001",
        "This source's setup is not valid",
        "Kestrel could not use how this source is set up, so it did not check it.",
        "Open the source, fix its setup, then check it again. Remove it and add it again if "
        "you are not sure what is wrong.",
    ),
    _code(
        "SRC-002",
        "The site asked Kestrel to slow down",
        "The site said it was getting too many requests, so this check stopped early.",
        "Do nothing. Kestrel tries again on the next check and waits longer between requests.",
    ),
    _code(
        "SRC-003",
        "The site had a server problem",
        "The site answered with a server error, so this check stopped early.",
        "Do nothing. Kestrel tries again on the next check. If it keeps happening, the site "
        "may be down.",
    ),
    _code(
        "SRC-004",
        "The site took too long to answer",
        "The site did not answer in time, so this check stopped early.",
        "Do nothing. Kestrel tries again on the next check. Check your internet connection if "
        "every source does this.",
    ),
    _code(
        "SRC-005",
        "The site does not allow reading this page",
        "The site's rules ask programs not to read this page, so Kestrel did not open it.",
        "Open the posting in your browser instead, or turn this source off.",
    ),
    _code(
        "SRC-006",
        "This source needs a key of your own",
        "This board needs a key of your own, and it is missing or the board turned it down.",
        "Open Sources, find the board under Sources that need your own key, and save a key the "
        "board accepts.",
    ),
    _code(
        "SRC-007",
        "The daily limit for this source is used",
        "This source allows only a few checks a day, and today's are used.",
        "Do nothing. Kestrel checks again tomorrow.",
    ),
    _code(
        "SRC-008",
        "The page was not found",
        "Kestrel could not find this page or board at the saved address. It may have moved.",
        "Open the site in your browser and add the source again from its current address.",
    ),
    _code(
        "SRC-009",
        "Part of the check did not finish",
        "This check stopped before it read everything the source offered.",
        "Do nothing. Kestrel tries again on the next check.",
    ),
    _code(
        "SRC-010",
        "Kestrel could not read this source",
        "The last check of this source did not work.",
        "Do nothing. Kestrel tries again on the next check. If it keeps failing, remove the "
        "source and add it again.",
    ),
    _code(
        "SRC-011",
        "A posting could not be saved",
        "Kestrel read a posting from this source but could not save it.",
        "Do nothing. Kestrel tries again on the next check. The other postings were saved.",
    ),
    _code(
        "SRC-012",
        "A source hit a problem Kestrel did not expect",
        "Something unexpected stopped the check of this source.",
        "Do nothing. Kestrel tries again on the next check. If it keeps happening, use Show "
        "details when you ask for help.",
    ),
    _code(
        "SRC-013",
        "A search has no job boards left",
        "This search named only a board that Kestrel does not search, so it was turned off.",
        "Open the search and choose Use Kestrel's default boards.",
    ),
    _code(
        "SRC-014",
        "No posting could be saved",
        "Kestrel read the postings from this source but could not save any of them.",
        "Do nothing. Kestrel tries again on the next check. If it keeps happening, use Show "
        "details when you ask for help.",
    ),
    # ---- RUN, Runs and scoring.
    _code(
        "RUN-001",
        "The run did not run",
        "No search could run, so the run ended without checking any source.",
        "Do nothing. Nothing was lost and the searches run again. Choose Run now to try sooner.",
    ),
    _code(
        "RUN-002",
        "Kestrel closed before the run ended",
        "Kestrel was closed or stopped while this run was still going.",
        "Do nothing. Nothing was lost. Choose Run now to start a new one.",
    ),
    _code(
        "RUN-003",
        "A search was cut off",
        "Kestrel was closed while this search was still going, so it did not finish.",
        "Do nothing. The search runs again on the next run.",
    ),
    # ---- IMP, Importing files.
    # ---- DOC, Documents and PDFs.
    # ---- NET, Other devices and access.
    # ---- MCP, The MCP server.
    # ---- UPD, Updates.
    # ---- CHK, Health checks.
    _code(
        "CHK-001",
        "Python is the wrong version",
        "Kestrel's check found a Python version it does not support.",
        "Install the Python version the check names, then run the check again.",
    ),
    _code(
        "CHK-002",
        "The uv tool was not found",
        "The check could not find the tool Kestrel uses to install its parts.",
        "Install uv from its own website, then run the check again.",
    ),
    _code(
        "CHK-003",
        "A settings or rules file is not valid",
        "The check could not read one of Kestrel's settings or rules files.",
        "Fix the line the check names, or restore the file from a backup, then run the check "
        "again.",
    ),
    _code(
        "CHK-004",
        "Your profile is missing or incomplete",
        "The check could not find a finished profile.",
        "Open Profile and fill in the missing parts, then run the check again.",
    ),
    _code(
        "CHK-005",
        "The AI engine is not running",
        "The check could not reach the AI engine on this computer.",
        "Start Ollama, then run the check again.",
    ),
    _code(
        "CHK-006",
        "A model is not downloaded",
        "The check found a model that Kestrel needs but this computer does not have.",
        "Open Models and download the missing model, then run the check again.",
    ),
    _code(
        "CHK-007",
        "A model has too little room to read",
        "A model is set up to read less text than a step needs, so long postings would be cut "
        "short.",
        "Open Models and choose Fix it, or pick a model with more room.",
    ),
    _code(
        "CHK-008",
        "A model failed the room-to-read test",
        "A model did not find a word hidden near the end of a long posting, so long postings "
        "may be cut short.",
        "Open Models and choose Fix it, then run the check again.",
    ),
    _code(
        "CHK-009",
        "A model does not turn thinking off",
        "A model keeps thinking before it answers, which slows every role.",
        "Open Models and choose Fix it, or pick a different model.",
    ),
    _code(
        "CHK-010",
        "A model does not answer in the right format",
        "A model did not return data in the shape Kestrel needs.",
        "Open Models and choose Fix it, or pick a different model.",
    ),
    _code(
        "CHK-011",
        "No browser was found for PDFs",
        "The check could not find a browser that Kestrel can use to make PDFs.",
        "Install Microsoft Edge or Google Chrome, then run the check again.",
    ),
    _code(
        "CHK-012",
        "The connector port is in use",
        "Another program is using the port that Kestrel's AI connector wants.",
        "Close the other program, or change the connector port in Kestrel's settings.",
    ),
    _code(
        "CHK-013",
        "An Ollama setting could be better",
        "An Ollama setting is not what Kestrel works best with, which can slow scoring.",
        "Follow the suggestion beside the check, then restart Ollama.",
    ),
    _code(
        "CHK-014",
        "The AI address is not on this computer",
        "Kestrel is set to use an AI engine that is not on this computer.",
        "Open Models and point Kestrel back at the engine on this computer.",
    ),
    _code(
        "CHK-015",
        "Ollama listens beyond this computer",
        "Ollama accepts connections from other computers, not only this one.",
        "Set Ollama to listen only on this computer, then restart it.",
    ),
    _code(
        "CHK-016",
        "A cloud-hosted model is installed",
        "Ollama has a model that runs on someone else's computer, not yours.",
        "Remove that model in Ollama, or keep it if you chose it on purpose.",
    ),
    _code(
        "CHK-017",
        "Ollama made a connection you did not expect",
        "During the check, Ollama connected to something besides this computer.",
        "Open the details of the check to see what it was. Turn off Ollama's cloud features "
        "if you did not choose them.",
    ),
    _code(
        "CHK-018",
        "A model is not one Kestrel tested",
        "A model on this computer differs from the version Kestrel tested.",
        "Download the model again from Models, or keep it if you chose it on purpose.",
    ),
    _code(
        "CHK-019",
        "No contact is set for reading job sites",
        "The contact line Kestrel sends to the sites it reads is still a placeholder.",
        "Add your real contact email to the contact line in Kestrel's settings, then run the "
        "check again.",
    ),
    _code(
        "CHK-020",
        "A company page did not look as expected",
        "The check read a company page and found it laid out differently than Kestrel expects.",
        "Check that source on Sources, or remove it and add it again.",
    ),
    _code(
        "CHK-021",
        "Your data could not be fully checked",
        "The check could not confirm that Kestrel's data is up to date.",
        "Close and reopen Kestrel. If it keeps happening, make a backup in Settings before "
        "anything else.",
    ),
    _code(
        "CHK-022",
        "An online model is not set up fully",
        "The check found an online model choice that is missing a setting or a key.",
        "Open Models and finish setting up the online model, or switch back to this computer.",
    ),
    _code(
        "CHK-023",
        "The search model is not ready",
        "The model Kestrel uses to compare roles is not set up or is not available.",
        "Follow the suggestion beside the check, then run it again.",
    ),
    _code(
        "CHK-024",
        "Your saved settings could not be read",
        "The check could not read the settings you changed in the app, so it used the defaults.",
        "Close and reopen Kestrel. Make a backup in Settings if it keeps happening.",
    ),
    _code(
        "CHK-025",
        "Model tests were skipped during a run",
        "The check skipped its model tests because a run is in progress.",
        "Run the check again when the run has finished.",
    ),
    _code(
        "CHK-026",
        "A model may be too large for this computer",
        "The largest model a profile uses may not fit in this computer's memory, so it could run "
        "slowly or fail to load.",
        "Follow the suggestion beside the check: pick a smaller model or a lighter profile.",
    ),
    _code(
        "CHK-027",
        "The check could not run",
        "The check could not finish, so it does not say whether anything is wrong.",
        "Run the check again. If it keeps happening, use Show details when you ask for help.",
    ),
    _code(
        "CHK-028",
        "A model has not been built",
        "Kestrel's version of a model has not been built on this computer yet.",
        "Open Models and choose Fix it, or download and build the missing model.",
    ),
    _code(
        "CHK-029",
        "Your profile file could not be read",
        "Kestrel found your profile file but could not read it, so roles are scored against an "
        "empty profile.",
        "Open Profile and choose Start fresh. Your old file is kept.",
    ),
    _code(
        "CHK-030",
        "A browser path you set points at nothing",
        "A browser location you set does not exist, so Kestrel ignores it and tries the usual "
        "browsers.",
        "Fix the browser location in Kestrel's settings, or remove it.",
    ),
    _code(
        "CHK-031",
        "Your data needs updating",
        "Kestrel's data is not at the version this app expects.",
        "Close and reopen Kestrel so it can update the data. If it says the data is from a newer "
        "version, install the newest Kestrel.",
    ),
    # ---- APP, Anything else.
    _code(
        "APP-001",
        "Something went wrong on this page",
        "Kestrel could not load a page because of a problem it did not expect.",
        "Choose Try again. If it keeps happening, choose Show details and include those details "
        "when you ask for help.",
    ),
)

CODES: dict[str, ErrorCode] = {entry.code: entry for entry in _ALL}

# A code no longer used, moved here whole with the date it was retired: `_retired(_code(...),
# "YYYY-MM-DD")`. A retired code is never issued again and never returns to `_ALL`.
_RETIRED_ALL: tuple[RetiredCode, ...] = ()

RETIRED: dict[str, RetiredCode] = {item.entry.code: item for item in _RETIRED_ALL}


def get(code: str) -> ErrorCode:
    """The registry entry for `code`. An unknown or retired code raises KeyError, so a code in
    use that was never registered fails loudly instead of showing a blank."""
    try:
        return CODES[code]
    except KeyError:
        raise KeyError(f"unregistered error code: {code!r}") from None


def lookup(code: str) -> ErrorCode | None:
    """The entry for a live or a retired code, or None for anything else. For showing a code read
    from a stored row: a retired code still shows (an old row keeps its meaning), and text that
    is not a registered code shows nothing, never a guess."""
    if code in CODES:
        return CODES[code]
    if code in RETIRED:
        return RETIRED[code].entry
    return None


def anchor(code: str) -> str:
    """The one anchor scheme for a code, in the help table and the in-app Help article alike:
    `STA-006` becomes `sta-006`."""
    return code.lower()


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_table() -> str:
    """The Markdown help table, grouped by area in `AREAS` order, one anchor per code."""
    lines = [
        "# Error codes",
        "",
        "Each code below names one problem Kestrel can report, such as `STA-006`. Find the code "
        "to see what happened and what you can do. A code never changes meaning and is never "
        "reused.",
        "",
        "This page is generated from Kestrel's list of codes by `scripts/error_codes_table.py`. "
        "Do not edit it by hand.",
    ]
    for area, name in AREAS.items():
        entries = sorted((e for e in CODES.values() if e.area == area), key=lambda e: e.code)
        if not entries:
            continue
        lines += [
            "",
            f"## {name} ({area})",
            "",
            "| Code | What happened | What to do |",
            "|---|---|---|",
        ]
        for e in entries:
            lines.append(
                f'| <a id="{anchor(e.code)}"></a>{e.code} | **{_cell(e.title)}.** '
                f"{_cell(e.meaning)} | {_cell(e.fix)} |"
            )
    if RETIRED:
        lines += [
            "",
            "## Retired codes",
            "",
            "Kestrel no longer shows these codes, but an older message may still carry one.",
            "",
            "| Code | What happened | Retired |",
            "|---|---|---|",
        ]
        for code in sorted(RETIRED):
            e = RETIRED[code].entry
            lines.append(
                f'| <a id="{anchor(code)}"></a>{code} | **{_cell(e.title)}.** {_cell(e.meaning)} '
                f"| {RETIRED[code].retired} |"
            )
    return "\n".join(lines) + "\n"
