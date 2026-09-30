"""The AAOIFI-v1 screening engine: point-in-time inputs in, status and reason out (S1; PRD §6).

Everything here is a pure function: no database, no clock, no network. The same inputs always
give the same answer, and the answer says which rule decided it and with which numbers.

Order of decision (S1 §1, §4):
1. A manual-list entry decides first (NON_HALAL or PENDING_REVIEW, with a reason).
2. A prohibited main business is NON_HALAL whatever the ratios are (ratios are not looked at).
3. Otherwise the three ratios are tested. A ratio that fails makes the stock NON_HALAL, even if
   other inputs are missing: it can no longer become HALAL. If nothing failed but an input is
   missing, invalid or out of date, the stock is UNKNOWN. Only when every test passes is it HALAL.

Only HALAL is ever eligible (BRD §9). A ratio exactly at its limit passes ("does not exceed").
Ratios are compared at full precision: nothing is rounded before the comparison.
"""

from calendar import monthrange
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from halal_quant.core.config import ExcludedBusiness, ShariaConfig

HUNDRED = Decimal(100)


class Status(StrEnum):
    HALAL = "HALAL"
    NON_HALAL = "NON_HALAL"
    UNKNOWN = "UNKNOWN"
    PENDING_REVIEW = "PENDING_REVIEW"

    @property
    def eligible(self) -> bool:
        """Only HALAL may ever be traded; PENDING_REVIEW, UNKNOWN and NON_HALAL never are."""
        return self is Status.HALAL


@dataclass(frozen=True)
class Filing:
    """The figures S1 needs from one as-reported filing (USD)."""

    dimension: str  # ARQ, ARY or ART
    filing_date: date
    period_end: date
    debt: Decimal | None = None
    cash_and_equivalents: Decimal | None = None
    investments: Decimal | None = None
    investments_current: Decimal | None = None
    investments_noncurrent: Decimal | None = None
    revenue: Decimal | None = None
    ebit: Decimal | None = None
    operating_income: Decimal | None = None


@dataclass(frozen=True)
class ManualEntry:
    """A named company the owner or reviewer has decided by hand (S1 §4 manual list)."""

    status: Status
    reason: str


@dataclass(frozen=True)
class ScreeningInputs:
    """What was public before `screening_date`, already chosen by the caller."""

    screening_date: date
    industry: str | None
    sic_code: int | None
    market_value: Decimal | None
    balance_sheet: Filing | None  # latest ARQ, else latest ARY, filed before the date
    income: Filing | None  # latest ART, else latest ARY, filed before the date
    manual: ManualEntry | None = None


@dataclass(frozen=True)
class ScreeningResult:
    status: Status
    reason: str
    details: dict[str, object] = field(default_factory=dict)


def choose_balance_sheet(arq: Filing | None, ary: Filing | None) -> Filing | None:
    """Quarterly report if the company has any, otherwise annual (S1 §3)."""
    return arq or ary


def choose_income_report(art: Filing | None, ary: Filing | None) -> Filing | None:
    """Trailing-twelve-month report if the company has any, otherwise annual (S1 §3)."""
    return art or ary


def months_before(day: date, months: int) -> date:
    """`day` moved back by whole calendar months (the day is clamped to the month's length)."""
    index = day.year * 12 + (day.month - 1) - months
    year, month = divmod(index, 12)
    return date(year, month + 1, min(day.day, monthrange(year, month + 1)[1]))


def matched_business(
    industry: str | None, sic_code: int | None, excluded: Iterable[ExcludedBusiness]
) -> str | None:
    """The prohibited category this industry or SIC code falls in, or None (S1 §4).

    Either signal is enough: the industry name, or the SIC code inside a listed range.
    """
    for business in excluded:
        if industry is not None and industry in business.industries:
            return business.category
        if sic_code is not None and any(lo <= sic_code <= hi for lo, hi in business.sic_ranges):
            return business.category
    return None


def _percent(ratio: Decimal) -> str:
    return f"{ratio * HUNDRED:.2f}%"


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _cash_and_investments(filing: Filing, problems: list[str]) -> Decimal | None:
    cash = filing.cash_and_equivalents
    investments = filing.investments
    if investments is None and (
        filing.investments_current is not None and filing.investments_noncurrent is not None
    ):
        investments = filing.investments_current + filing.investments_noncurrent
    if cash is None or investments is None:
        missing = [
            name
            for name, value in (("cashneq", cash), ("investments", investments))
            if value is None
        ]
        problems.append(f"Missing input: {', '.join(missing)}")
        return None
    if cash < 0 or investments < 0:
        problems.append("Invalid input: negative cash or investments")
        return None
    return cash + investments


def _check_report_age(
    name: str, filing: Filing | None, screening_date: date, max_months: int, problems: list[str]
) -> bool:
    if filing is None:
        problems.append(f"Missing input: no {name} report filed before {screening_date}")
        return False
    if filing.period_end < months_before(screening_date, max_months):
        problems.append(
            f"Out of date: {name} report period ended {filing.period_end}, "
            f"more than {max_months} months before {screening_date}"
        )
        return False
    return True


def screen(inputs: ScreeningInputs, config: ShariaConfig) -> ScreeningResult:
    """Decide HALAL, NON_HALAL, UNKNOWN or PENDING_REVIEW for one security on one date."""
    if inputs.manual is not None:
        return ScreeningResult(
            inputs.manual.status,
            f"Manual list: {inputs.manual.reason}",
            {"rule": "manual_list"},
        )

    category = matched_business(inputs.industry, inputs.sic_code, config.excluded_businesses)
    if category is not None:
        return ScreeningResult(
            Status.NON_HALAL,
            f"Prohibited main business: {category} "
            f"(industry {inputs.industry!r}, SIC {inputs.sic_code})",
            {"rule": "business", "category": category},
        )

    problems: list[str] = []
    mv = inputs.market_value
    if mv is None:
        problems.append("Missing input: market value on the screening date")
    elif mv <= 0:
        problems.append(f"Invalid input: market value {mv} is not positive")
        mv = None

    sheet, income = inputs.balance_sheet, inputs.income
    sheet_ok = _check_report_age(
        "balance-sheet", sheet, inputs.screening_date, config.max_report_age_months, problems
    )
    income_ok = _check_report_age(
        "income", income, inputs.screening_date, config.max_report_age_months, problems
    )

    debt_ratio = cash_ratio = income_ratio = None
    if sheet is not None and sheet_ok:
        if sheet.debt is None:
            problems.append("Missing input: debt")
        elif sheet.debt < 0:
            problems.append("Invalid input: negative debt")
        elif mv is not None:
            debt_ratio = sheet.debt / mv
        cash_total = _cash_and_investments(sheet, problems)
        if cash_total is not None and mv is not None:
            cash_ratio = cash_total / mv
    if income is not None and income_ok:
        missing = [
            name
            for name, value in (
                ("revenue", income.revenue),
                ("ebit", income.ebit),
                ("opinc", income.operating_income),
            )
            if value is None
        ]
        if missing:
            problems.append(f"Missing input: {', '.join(missing)}")
        elif income.revenue is not None and income.revenue <= 0:
            problems.append("Invalid input: revenue is not positive, so the income test cannot run")
        elif (
            income.revenue is not None
            and income.ebit is not None
            and income.operating_income is not None
        ):
            non_core = max(Decimal(0), income.ebit - income.operating_income)
            income_ratio = non_core / income.revenue

    limits = (
        ("debt", debt_ratio, config.max_debt_to_market_value, "of market value"),
        (
            "cash and investments",
            cash_ratio,
            config.max_cash_and_investments_to_market_value,
            "of market value",
        ),
        (
            "prohibited income",
            income_ratio,
            config.max_prohibited_income_to_revenue,
            "of revenue",
        ),
    )
    parts = [
        f"{name} {_percent(ratio)} {basis} (limit {_percent(limit)})"
        for name, ratio, limit, basis in limits
        if ratio is not None
    ]
    failed = [name for name, ratio, limit, _ in limits if ratio is not None and ratio > limit]
    details: dict[str, object] = {
        "rule": "ratios",
        "debt_to_market_value": _text(debt_ratio),
        "cash_and_investments_to_market_value": _text(cash_ratio),
        "prohibited_income_to_revenue": _text(income_ratio),
        "market_value": _text(inputs.market_value),
        "problems": problems,
    }

    if failed:
        note = f"; also: {'; '.join(problems)}" if problems else ""
        return ScreeningResult(
            Status.NON_HALAL, f"Failed: {', '.join(failed)}. " + "; ".join(parts) + note, details
        )
    if problems:
        return ScreeningResult(Status.UNKNOWN, "; ".join(problems), details)
    return ScreeningResult(Status.HALAL, "; ".join(parts), details)


def describe_sources(filings: Sequence[Filing | None]) -> list[dict[str, str]]:
    """Which filings a result used, for the stored source reference."""
    return [
        {
            "dimension": f.dimension,
            "filing_date": f.filing_date.isoformat(),
            "period_end": f.period_end.isoformat(),
        }
        for f in filings
        if f is not None
    ]
