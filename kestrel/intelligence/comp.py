"""The compensation-corpus query (SPEC-MODULES 6.1). Every observed comp figure was written at
ingestion; this is the read side. Queried by (normalized title, geo bucket, size band) it returns
median, p25, p75, AND the sample size, narrowed as tightly as five observations allow and widened
only when a tighter cut is too thin. The sample size is ALWAYS returned; under five observations it
reports insufficient rather than a precise-looking chart from three points (constraint 6).

This is the canonical comp query; the negotiation module (§4.2) imports it rather than duplicating
the arithmetic."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median, quantiles

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kestrel.db.models import CompObservation

MIN_OBSERVATIONS = 5  # hard rule 6: fewer than five is "insufficient", never rendered as solid


@dataclass
class CompRange:
    n: int
    sufficient: bool
    scope: str
    currency: str
    median: float | None = None
    low: float | None = None
    high: float | None = None
    p25: float | None = None
    p75: float | None = None
    interval: str = "year"  # every pooled observation shares this interval (never day with year)

    def summary(self) -> str:
        if not self.sufficient:
            return f"insufficient data (n={self.n}, need >={MIN_OBSERVATIONS})"
        band = f"${self.p25:,.0f}-${self.p75:,.0f}" if self.p25 is not None else ""
        return (
            f"median ${self.median:,.0f} per {self.interval} (n={self.n}, {self.scope}; "
            f"range ${self.low:,.0f}-${self.high:,.0f}{'; IQR ' + band if band else ''}), "
            "from the operator's own observed corpus"
        )

    def as_dict(self) -> dict:
        """Presentation payload. The UI renders a chart only when `sufficient`; otherwise it shows
        the insufficient-data message and the raw n, never a chart from a handful of points."""
        return {
            "n": self.n,
            "sufficient": self.sufficient,
            "scope": self.scope,
            "currency": self.currency,
            "interval": self.interval,
            "median": self.median,
            "p25": self.p25,
            "p75": self.p75,
            "low": self.low,
            "high": self.high,
            "insufficient_message": None if self.sufficient else self.summary(),
        }


def _mids(rows: list[CompObservation]) -> list[float]:
    out = []
    for r in rows:
        if r.min is not None and r.max is not None:
            out.append((r.min + r.max) / 2)
        elif r.max is not None:
            out.append(float(r.max))
        elif r.min is not None:
            out.append(float(r.min))
    return out


def _range_from(
    mids: list[float], scope: str, currency: str, interval: str = "year"
) -> CompRange:
    n = len(mids)
    if n < MIN_OBSERVATIONS:
        return CompRange(n=n, sufficient=False, scope=scope, currency=currency, interval=interval)
    p25 = p75 = None
    if n >= 4:
        q = quantiles(mids, n=4, method="inclusive")
        p25, p75 = q[0], q[2]
    return CompRange(
        n=n, sufficient=True, scope=scope, currency=currency, interval=interval,
        median=median(mids), low=min(mids), high=max(mids), p25=p25, p75=p75,
    )


def _same_interval(rows: list[CompObservation]) -> tuple[list[CompObservation], str, bool]:
    """Keep only observations that share one interval. A corpus of one interval (all annual, or
    all day rates) pools as it is; a MIXED corpus keeps the annual subset only, because a day rate
    pooled with a salary yields a median that looks solid and means nothing. A null interval is a
    legacy annual row. Returns (rows, interval, was_mixed)."""
    by_interval: dict[str, list[CompObservation]] = {}
    for r in rows:
        by_interval.setdefault(r.interval or "year", []).append(r)
    if len(by_interval) <= 1:
        interval = next(iter(by_interval), "year")
        return by_interval.get(interval, []), interval, False
    return by_interval.get("year", []), "year", True


async def comp_range(
    session: AsyncSession,
    *,
    normalized_title: str | None,
    geo_bucket: str | None = None,
    org_size_band: str | None = None,
    currency: str = "USD",
) -> CompRange:
    """The comp corpus range for a title, narrowed as tightly as MIN_OBSERVATIONS allows. Tries
    title+geo+size, then title+geo, then title alone; the first cut with enough observations wins,
    and the scope names which cut it was. Fewer than five at every level returns insufficient.
    Never pools across currencies: only the requested currency is kept (a null-currency legacy row
    is assumed to match), since a median over mixed currencies would look solid but mean nothing.
    Never pools across intervals either: a mixed corpus is reduced to its annual subset, and the
    returned n is that subset's (hard rule 6: under five stays insufficient)."""
    title = (normalized_title or "").lower().strip()
    if not title:
        return CompRange(n=0, sufficient=False, scope="no title", currency=currency)
    rows = (
        (
            await session.execute(
                select(CompObservation).where(CompObservation.normalized_title == title)
            )
        )
        .scalars()
        .all()
    )
    want = (currency or "").casefold()
    rows = [r for r in rows if (r.currency or currency or "").casefold() == want]
    rows, interval, mixed = _same_interval(rows)
    suffix = ", annual only" if mixed else ""

    def cut(pred, label: str) -> CompRange | None:
        rng = _range_from(_mids([r for r in rows if pred(r)]), label + suffix, currency, interval)
        return rng if rng.sufficient else None

    geo = geo_bucket or "unknown"
    candidates = []
    if org_size_band:
        candidates.append(
            (lambda r: (r.geo_bucket or "unknown") == geo and r.org_size_band == org_size_band,
             f"{geo}, {org_size_band}")
        )
    candidates.append((lambda r: (r.geo_bucket or "unknown") == geo, f"{geo}"))
    candidates.append((lambda r: True, "all geographies"))
    for pred, label in candidates:
        rng = cut(pred, label)
        if rng is not None:
            return rng
    return _range_from(_mids(rows), "all geographies" + suffix, currency, interval)
