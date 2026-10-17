"""Greenhouse job-board adapter (SPEC.md Appendix A, verified live 2026-07-30).

GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true -> {"jobs": [...]}.
GOTCHAS this adapter handles:
  - `location` is an OBJECT {"name": "..."}, not a string.
  - `content` is HTML-ESCAPED HTML; normalize.py unescapes then converts to markdown.
  - No pagination: one large payload (up to several MB), so a generous timeout and one board at a
    time. The shared client's default timeout (60s) covers it.
  - `metadata[]` is [{name, value}] and often carries useful custom fields (e.g. "Location Type").
"""

from __future__ import annotations

from typing import Any

from kestrel.ingest.base import RawPosting, SourceConfigError

_BASE = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseAdapter:
    kind = "greenhouse"

    def validate_config(self, config: dict) -> None:
        if not config.get("token"):
            raise SourceConfigError("greenhouse source requires a 'token' (the board token)")

    async def fetch(self, config: dict, client: Any) -> list[RawPosting]:
        self.validate_config(config)
        token = config["token"]
        url = f"{_BASE}/{token}/jobs?content=true"
        data = await client.get_json(url)
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        return [self._to_posting(j, token) for j in jobs if isinstance(j, dict)]

    def _to_posting(self, j: dict, token: str) -> RawPosting:
        loc = j.get("location")
        location_raw = (
            loc.get("name") if isinstance(loc, dict) else (loc if isinstance(loc, str) else None)
        )
        metadata = {
            m.get("name"): m.get("value")
            for m in (j.get("metadata") or [])
            if isinstance(m, dict) and m.get("name")
        }
        return RawPosting(
            external_id=str(j.get("id") or j.get("internal_job_id") or "").strip() or None,
            url=j.get("absolute_url"),
            title=j.get("title"),
            org_name=j.get("company_name") or token,
            location_raw=location_raw,
            description_html=j.get(
                "content"
            ),  # HTML-escaped HTML; normalize unescapes + markdownifies
            remote_hint=_str(metadata.get("Location Type") or metadata.get("Remote")),
            date_posted=_str(j.get("first_published") or j.get("updated_at")),
            size_band=_str(metadata.get("Company Size") or metadata.get("Employees")),
            sector=_str(metadata.get("Industry") or metadata.get("Sector")),
            ownership=_str(metadata.get("Ownership")),
            raw=j,
        )


def _str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None
