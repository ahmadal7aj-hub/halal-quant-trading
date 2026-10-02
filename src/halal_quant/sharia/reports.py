"""Sharia reports from the stored classifications (task 15; BRD §21; doc 03 S4 and S5).

    uv run python -m halal_quant.sharia.reports [--start 1998-12] [--end 2026-09]
                                                [--methodology AAOIFI-v1] [--report path.md]

S4, the classification change log: how many securities changed status from one month-end
screening to the next, by kind of change and by year, who changed in the latest screening and
why, and which securities flip most often.
S5, the missing-data (UNKNOWN) report: how many securities could not be decided, why, how that
share moved over the years, and which securities have been UNKNOWN the longest.

Both read `hq.classification` only, so they describe exactly what the screen decided and why.
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from halal_quant.core.logging import configure_logging, correlation_scope
from halal_quant.core.settings import DbRole, get_settings
from halal_quant.db.engine import make_engine
from halal_quant.sharia.classification import PROVIDER

TOP = 15
STATUS_ORDER = ("HALAL", "NON_HALAL", "UNKNOWN", "PENDING_REVIEW")

_TRANSITIONS_BY_YEAR = text(
    """
    WITH seq AS (
      SELECT security_id, screening_date, status,
             lag(status) OVER (PARTITION BY security_id ORDER BY screening_date) AS previous
      FROM hq.classification
      WHERE provider = :provider AND methodology = :methodology
        AND screening_date BETWEEN :first AND :last
    )
    SELECT extract(year FROM screening_date)::int AS year, previous, status, count(*) AS n
    FROM seq WHERE previous IS NOT NULL AND previous <> status
    GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
    """
)
_SCREENED_BY_YEAR = text(
    """
    SELECT extract(year FROM screening_date)::int AS year, count(*) AS records,
           count(DISTINCT screening_date) AS screenings,
           count(*) FILTER (WHERE status = 'HALAL') AS halal,
           count(*) FILTER (WHERE status = 'UNKNOWN') AS unknown
    FROM hq.classification
    WHERE provider = :provider AND methodology = :methodology
      AND screening_date BETWEEN :first AND :last
    GROUP BY 1 ORDER BY 1
    """
)
_LATEST_CHANGES = text(
    """
    WITH latest AS (
      SELECT max(screening_date) AS d FROM hq.classification
      WHERE provider = :provider AND methodology = :methodology AND screening_date <= :last
    ), seq AS (
      SELECT c.security_id, c.screening_date, c.status, c.reason,
             lag(c.status) OVER (PARTITION BY c.security_id ORDER BY c.screening_date) AS previous
      FROM hq.classification c
      WHERE c.provider = :provider AND c.methodology = :methodology AND c.screening_date <= :last
    )
    SELECT seq.screening_date, s.company_name, t.ticker, seq.previous, seq.status,
           left(seq.reason, 140) AS why
    FROM seq JOIN latest ON seq.screening_date = latest.d
    JOIN hq.security s USING (security_id)
    LEFT JOIN hq.security_ticker t ON t.security_id = s.security_id
      AND t.valid_from <= seq.screening_date
      AND (t.valid_to IS NULL OR t.valid_to > seq.screening_date)
    WHERE seq.previous IS NOT NULL AND seq.previous <> seq.status
    ORDER BY seq.previous, seq.status, s.company_name
    """
)
_FLIP_FLOPPERS = text(
    """
    WITH seq AS (
      SELECT security_id, status,
             lag(status) OVER (PARTITION BY security_id ORDER BY screening_date) AS previous
      FROM hq.classification
      WHERE provider = :provider AND methodology = :methodology
        AND screening_date BETWEEN :first AND :last
    ), flips AS (
      SELECT security_id, count(*) AS changes FROM seq
      WHERE previous IS NOT NULL AND previous <> status GROUP BY 1
    )
    SELECT s.company_name, f.changes
    FROM flips f JOIN hq.security s USING (security_id)
    ORDER BY f.changes DESC, s.company_name LIMIT :top
    """
)
_UNKNOWN_REASONS = text(
    """
    SELECT CASE
        WHEN s.category LIKE '%Secondary Class' AND c.reason LIKE 'Missing input: market value%'
          THEN 'secondary share class (provider carries the data under the primary class)'
        WHEN c.reason LIKE 'Invalid input: revenue%' THEN 'revenue is zero or negative'
        WHEN c.reason LIKE 'Missing input: revenue%' THEN 'revenue or EBIT data missing'
        WHEN c.reason LIKE '%market value on the screening date%' THEN 'no market value on the date'
        WHEN c.reason LIKE 'Invalid input: market value%' THEN 'market value is zero or negative'
        WHEN c.reason LIKE 'Out of date%' THEN 'latest report older than 16 months'
        WHEN c.reason LIKE 'Missing input: no %report filed%' THEN 'no filing before the date'
        WHEN c.reason LIKE 'Invalid input: negative%' THEN 'negative debt, cash or investments'
        WHEN c.reason LIKE 'Missing input: %' THEN 'a balance-sheet figure is missing'
        ELSE 'other' END AS why, count(*) AS n
    FROM hq.classification c JOIN hq.security s USING (security_id)
    WHERE c.provider = :provider AND c.methodology = :methodology
      AND c.screening_date = :on AND c.status = 'UNKNOWN'
    GROUP BY 1 ORDER BY 2 DESC
    """
)
_LATEST_SCREENING = text(
    """
    SELECT max(screening_date) FROM hq.classification
    WHERE provider = :provider AND methodology = :methodology AND screening_date <= :last
    """
)
_LONGEST_UNKNOWN = text(
    """
    WITH runs AS (
      SELECT security_id, screening_date,
             (extract(year FROM screening_date)::int * 12 + extract(month FROM screening_date)::int)
               - row_number() OVER (PARTITION BY security_id ORDER BY screening_date) AS grp
      FROM hq.classification
      WHERE provider = :provider AND methodology = :methodology
        AND screening_date <= :on AND status = 'UNKNOWN'
    ), streaks AS (
      SELECT security_id, count(*) AS months, max(screening_date) AS last_seen
      FROM runs GROUP BY security_id, grp
    )
    SELECT s.company_name, st.months FROM streaks st JOIN hq.security s USING (security_id)
    WHERE st.last_seen = :on AND s.category NOT LIKE '%Secondary Class'
    ORDER BY st.months DESC, s.company_name LIMIT :top
    """
)


@dataclass
class ShariaReports:
    methodology: str
    first: date
    last: date
    by_year: list[Any]
    transitions: list[Any]
    latest_screening: date | None
    latest_changes: list[Any]
    flip_floppers: list[Any]
    unknown_reasons: list[Any]
    longest_unknown: list[Any]


def build_reports(
    conn: Connection, first: date, last: date, methodology: str, provider: str = PROVIDER
) -> ShariaReports:
    base = {"provider": provider, "methodology": methodology, "first": first, "last": last}
    latest = conn.execute(_LATEST_SCREENING, base).scalar_one_or_none()
    on = {**base, "on": latest, "top": TOP}
    return ShariaReports(
        methodology=methodology,
        first=first,
        last=last,
        by_year=list(conn.execute(_SCREENED_BY_YEAR, base)),
        transitions=list(conn.execute(_TRANSITIONS_BY_YEAR, base)),
        latest_screening=latest,
        latest_changes=list(conn.execute(_LATEST_CHANGES, base)) if latest else [],
        flip_floppers=list(conn.execute(_FLIP_FLOPPERS, {**base, "top": TOP})),
        unknown_reasons=list(conn.execute(_UNKNOWN_REASONS, on)) if latest else [],
        longest_unknown=list(conn.execute(_LONGEST_UNKNOWN, on)) if latest else [],
    )


def render_markdown(r: ShariaReports) -> str:
    """Both reports as one markdown document."""
    lines = [
        f"# Sharia reports, methodology {r.methodology}, {r.first} to {r.last}",
        "",
        "## S4 Classification change log",
        "",
        "Changes are between one month-end screening and the next, per security.",
        "",
        "| Year | Screenings | Records | HALAL records | Status changes | HALAL to NON_HALAL "
        "| NON_HALAL to HALAL |",
        "|---|---|---|---|---|---|---|",
    ]
    by_year: dict[int, dict[tuple[str, str], int]] = {}
    for row in r.transitions:
        by_year.setdefault(row.year, {})[(row.previous, row.status)] = row.n
    for row in r.by_year:
        moves = by_year.get(row.year, {})
        lines.append(
            f"| {row.year} | {row.screenings} | {row.records} | {row.halal} "
            f"| {sum(moves.values())} | {moves.get(('HALAL', 'NON_HALAL'), 0)} "
            f"| {moves.get(('NON_HALAL', 'HALAL'), 0)} |"
        )
    lines += ["", f"### Latest screening ({r.latest_screening}): who changed and why", ""]
    if not r.latest_changes:
        lines.append("No status changes.")
    kinds: dict[tuple[str, str], list[Any]] = {}
    for row in r.latest_changes:
        kinds.setdefault((row.previous, row.status), []).append(row)
    for (previous, status), rows in sorted(kinds.items()):
        lines += ["", f"**{previous} to {status}: {len(rows)}**", ""]
        lines += [f"- {x.ticker or '?'} ({x.company_name}): {x.why}" for x in rows[:TOP]]
        if len(rows) > TOP:
            lines.append(f"- ... and {len(rows) - TOP} more")
    lines += ["", "### Securities that changed status most often", ""]
    lines += [f"- {x.company_name}: {x.changes} changes" for x in r.flip_floppers] or ["None."]

    lines += [
        "",
        "## S5 Missing-data (UNKNOWN) report",
        "",
        "| Year | Records | UNKNOWN records | UNKNOWN share |",
        "|---|---|---|---|",
    ]
    for row in r.by_year:
        share = f"{100 * row.unknown / row.records:.1f}%" if row.records else "n/a"
        lines.append(f"| {row.year} | {row.records} | {row.unknown} | {share} |")
    lines += [
        "",
        f"### Why securities were UNKNOWN at the latest screening ({r.latest_screening})",
        "",
    ]
    lines += [f"- {x.n}: {x.why}" for x in r.unknown_reasons] or ["None."]
    lines += [
        "",
        "### Securities UNKNOWN for the most consecutive months (secondary share classes left out)",
        "",
    ]
    lines += [f"- {x.company_name}: {x.months} months" for x in r.longest_unknown] or ["None."]
    return "\n".join(lines) + "\n"


def _month(text_: str) -> date:
    try:
        return date.fromisoformat(f"{text_}-01")
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text_!r} is not a month like 2019-03") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sharia change log (S4) and UNKNOWN report (S5).")
    parser.add_argument("--start", type=_month, default=date(1998, 12, 1))
    parser.add_argument("--end", type=_month, default=date.today())
    parser.add_argument("--methodology", default="AAOIFI-v1")
    parser.add_argument("--report", type=Path, default=None, help="also write the reports here")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings, level=settings.log_level)
    last = date(args.end.year + args.end.month // 12, args.end.month % 12 + 1, 1)
    with correlation_scope(), make_engine(settings, DbRole.READONLY).connect() as conn:
        reports = build_reports(conn, args.start, last, args.methodology)
    document = render_markdown(reports)
    print(document)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(document, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
