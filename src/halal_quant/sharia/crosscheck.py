"""Compare our Halal list with Zoya's statuses (task 15; BRD §21; doc 03 S5; doc 01 §3).

    uv run python -m halal_quant.sharia.crosscheck [--as-of 2026-10-01] [--report path.md]
        [--save-snapshot path.json | --from-snapshot path.json]

Zoya is an outside opinion, not a source: nothing in our screening depends on it. The point is to
measure how far our own AAOIFI-v1 screen is from a professional AAOIFI-based one, and to list the
disagreements so each one can be explained (a real methodology difference, or a bug or data gap on
our side).

Pairs are grouped by what each side says. Zoya's QUESTIONABLE means scholars disagree, so it is
kept apart from COMPLIANT and NON_COMPLIANT. The headline agreement rate only counts the pairs
where both sides gave a firm answer (HALAL or NON_HALAL against COMPLIANT or NON_COMPLIANT).

The Personal Use licence allows non-public display only: the report lists counts and a few example
names per group and stays on this machine (`private/` is not tracked by git).
"""

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, and_, or_, select
from sqlalchemy.dialects.postgresql import distinct_on

from halal_quant.core.logging import configure_logging, correlation_scope
from halal_quant.core.settings import DbRole, get_settings
from halal_quant.data.security_master import security_table, security_ticker_table
from halal_quant.db.engine import make_engine
from halal_quant.sharia.classification import (
    COMMON_STOCK_CATEGORIES,
    PROVIDER,
    classification_table,
)
from halal_quant.sharia.zoya import ZoyaClient, ZoyaReport

EXAMPLES_PER_GROUP = 10
AGREE_HALAL = "agree_halal"
AGREE_NOT_HALAL = "agree_not_halal"
WE_HALAL_ZOYA_NOT = "we_halal_zoya_non_compliant"
WE_HALAL_ZOYA_QUESTIONABLE = "we_halal_zoya_questionable"
WE_NOT_ZOYA_COMPLIANT = "we_non_halal_zoya_compliant"
WE_NOT_ZOYA_QUESTIONABLE = "we_non_halal_zoya_questionable"
WE_UNKNOWN_ZOYA_DECIDED = "we_unknown_zoya_decided"
ZOYA_UNRATED = "we_decided_zoya_unrated"
BOTH_UNDECIDED = "both_undecided"
GROUP_ORDER = (
    AGREE_HALAL,
    AGREE_NOT_HALAL,
    WE_HALAL_ZOYA_NOT,
    WE_NOT_ZOYA_COMPLIANT,
    WE_HALAL_ZOYA_QUESTIONABLE,
    WE_NOT_ZOYA_QUESTIONABLE,
    WE_UNKNOWN_ZOYA_DECIDED,
    ZOYA_UNRATED,
    BOTH_UNDECIDED,
)
HARD_DISAGREEMENTS = (WE_HALAL_ZOYA_NOT, WE_NOT_ZOYA_COMPLIANT)


@dataclass(frozen=True)
class OurRecord:
    security_id: int
    ticker: str
    company_name: str
    status: str  # HALAL, NON_HALAL, UNKNOWN or PENDING_REVIEW
    reason: str


@dataclass
class CrossCheck:
    as_of: date
    ours_total: int
    zoya_total: int
    matched: int = 0
    only_ours: int = 0
    only_zoya: int = 0
    groups: Counter[str] = field(default_factory=Counter)
    examples: dict[str, list[tuple[OurRecord, ZoyaReport]]] = field(default_factory=dict)
    ours_not_in_zoya: list[OurRecord] = field(default_factory=list)

    @property
    def firm_pairs(self) -> int:
        return sum(self.groups[g] for g in (AGREE_HALAL, AGREE_NOT_HALAL, *HARD_DISAGREEMENTS))

    @property
    def agreement_rate(self) -> Decimal | None:
        """Share of firm-vs-firm pairs on which both say the same thing."""
        if not self.firm_pairs:
            return None
        return Decimal(self.groups[AGREE_HALAL] + self.groups[AGREE_NOT_HALAL]) / self.firm_pairs

    @property
    def halal_precision(self) -> Decimal | None:
        """Of what we call HALAL and Zoya rates firmly, how much does Zoya call COMPLIANT?"""
        ours = self.groups[AGREE_HALAL] + self.groups[WE_HALAL_ZOYA_NOT]
        return None if not ours else Decimal(self.groups[AGREE_HALAL]) / ours

    @property
    def halal_recall(self) -> Decimal | None:
        """Of what Zoya calls COMPLIANT and we rate firmly, how much do we call HALAL?"""
        theirs = self.groups[AGREE_HALAL] + self.groups[WE_NOT_ZOYA_COMPLIANT]
        return None if not theirs else Decimal(self.groups[AGREE_HALAL]) / theirs


def normalise_symbol(symbol: str) -> str:
    """Providers write share classes differently (BRK.B, BRK-B): compare on one spelling."""
    return symbol.strip().upper().replace("-", ".").replace("/", ".")


def classify_pair(ours: str, zoya: str) -> str:
    """Which group a (our status, Zoya status) pair belongs to."""
    if ours == "HALAL":
        return {
            "COMPLIANT": AGREE_HALAL,
            "NON_COMPLIANT": WE_HALAL_ZOYA_NOT,
            "QUESTIONABLE": WE_HALAL_ZOYA_QUESTIONABLE,
        }.get(zoya, ZOYA_UNRATED)
    if ours == "NON_HALAL":
        return {
            "NON_COMPLIANT": AGREE_NOT_HALAL,
            "COMPLIANT": WE_NOT_ZOYA_COMPLIANT,
            "QUESTIONABLE": WE_NOT_ZOYA_QUESTIONABLE,
        }.get(zoya, ZOYA_UNRATED)
    return BOTH_UNDECIDED if zoya == "UNRATED" else WE_UNKNOWN_ZOYA_DECIDED


def compare(
    ours: Mapping[str, OurRecord], zoya_reports: Iterable[ZoyaReport], as_of: date
) -> CrossCheck:
    """Compare our records (keyed by normalised ticker) with Zoya's reports."""
    zoya = {normalise_symbol(r.symbol): r for r in zoya_reports}
    check = CrossCheck(as_of=as_of, ours_total=len(ours), zoya_total=len(zoya))
    for ticker in sorted(ours):
        record = ours[ticker]
        report = zoya.get(ticker)
        if report is None:
            check.only_ours += 1
            check.ours_not_in_zoya.append(record)
            continue
        check.matched += 1
        group = classify_pair(record.status, report.status)
        check.groups[group] += 1
        bucket = check.examples.setdefault(group, [])
        if len(bucket) < EXAMPLES_PER_GROUP:
            bucket.append((record, report))
    check.only_zoya = len(set(zoya) - set(ours))
    return check


def our_records(
    conn: Connection,
    as_of: date,
    methodology: str,
    categories: Sequence[str] = COMMON_STOCK_CATEGORIES,
) -> dict[str, OurRecord]:
    """Our classification in effect on `as_of`, keyed by the ticker each security used then."""
    c, s, t = classification_table.c, security_table.c, security_ticker_table.c
    rows = conn.execute(
        select(c.security_id, t.ticker, s.company_name, c.status, c.reason)
        .ext(distinct_on(c.security_id))
        .join_from(classification_table, security_table, c.security_id == s.security_id)
        .join(
            security_ticker_table,
            and_(
                t.security_id == c.security_id,
                t.valid_from <= as_of,
                or_(t.valid_to.is_(None), t.valid_to > as_of),
            ),
        )
        .where(
            c.provider == PROVIDER,
            c.methodology == methodology,
            c.effective_from <= as_of,
            c.effective_to >= as_of,
            s.category.in_(categories),
        )
        .order_by(c.security_id, c.screening_date.desc())
    )
    return {
        normalise_symbol(r.ticker): OurRecord(
            r.security_id, r.ticker, r.company_name, r.status, r.reason
        )
        for r in rows
    }


def _pct(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def render_markdown(check: CrossCheck) -> str:
    """The cross-check report: counts and a few examples per group (no full lists)."""
    lines = [
        f"# Sharia cross-check against Zoya, as of {check.as_of}",
        "",
        "Zoya is an outside opinion. Their QUESTIONABLE means scholars disagree and is kept apart.",
        "Personal Use licence: counts and a few examples only; this file stays on this machine.",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Our securities (US common stock) | {check.ours_total} |",
        f"| Zoya US stock reports | {check.zoya_total} |",
        f"| Matched by ticker | {check.matched} |",
        f"| Ours with no Zoya report | {check.only_ours} |",
        f"| Zoya reports not in our universe | {check.only_zoya} |",
        f"| Pairs where both sides are firm | {check.firm_pairs} |",
        f"| **Agreement on firm pairs** | **{_pct(check.agreement_rate)}** |",
        f"| Of our HALAL, Zoya says COMPLIANT | {_pct(check.halal_precision)} |",
        f"| Of Zoya's COMPLIANT, we say HALAL | {_pct(check.halal_recall)} |",
        "",
        "| Group | Count |",
        "|---|---|",
    ]
    lines += [f"| {group} | {check.groups.get(group, 0)} |" for group in GROUP_ORDER]
    for group in GROUP_ORDER:
        pairs = check.examples.get(group, [])
        if not pairs or group in (AGREE_HALAL, AGREE_NOT_HALAL):
            continue
        lines += ["", f"## {group}: {check.groups[group]} (first {len(pairs)})", ""]
        for ours, theirs in pairs:
            why = ours.reason.replace("\n", " ")[:110]
            lines.append(
                f"- {ours.ticker} ({ours.company_name}): we say {ours.status} ({why}); "
                f"Zoya says {theirs.status}"
            )
    return "\n".join(lines) + "\n"


def save_snapshot(path: Path, reports: Sequence[ZoyaReport]) -> None:
    rows: list[dict[str, Any]] = [
        {
            "symbol": r.symbol,
            "name": r.name,
            "exchange": r.exchange,
            "status": r.status,
            "purification_ratio": None
            if r.purification_ratio is None
            else str(r.purification_ratio),
            "report_date": None if r.report_date is None else r.report_date.isoformat(),
        }
        for r in reports
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows), encoding="utf-8")


def load_snapshot(path: Path) -> list[ZoyaReport]:
    return [
        ZoyaReport(
            symbol=row["symbol"],
            name=row["name"],
            exchange=row["exchange"],
            status=row["status"],
            purification_ratio=(
                None if row["purification_ratio"] is None else Decimal(row["purification_ratio"])
            ),
            report_date=None
            if row["report_date"] is None
            else date.fromisoformat(row["report_date"]),
        )
        for row in json.loads(path.read_text(encoding="utf-8"))
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare our Halal list with Zoya's statuses.")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--methodology", default="AAOIFI-v1")
    parser.add_argument("--report", type=Path, default=None, help="write the report here")
    parser.add_argument("--save-snapshot", type=Path, default=None, help="keep Zoya's reports here")
    parser.add_argument("--from-snapshot", type=Path, default=None, help="use saved reports")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings, level=settings.log_level)
    if args.from_snapshot:
        reports = load_snapshot(args.from_snapshot)
    else:
        if settings.zoya_api_key is None:
            print("HQ_ZOYA_API_KEY is not set. Add it to .env (never in chat or git).")
            return 1
        reports = ZoyaClient(settings.zoya_api_key).fetch_us_reports()
        if args.save_snapshot:
            save_snapshot(args.save_snapshot, reports)
    with correlation_scope(), make_engine(settings, DbRole.READONLY).connect() as conn:
        ours = our_records(conn, args.as_of, args.methodology)
    text = render_markdown(compare(ours, reports, args.as_of))
    print(text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
