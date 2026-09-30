"""Golden tests for the AAOIFI-v1 screening engine, built from S1's worked examples (section 6).

The made-up company is screened on 31 Jan 2020, in USD millions: debt 2,000, cash 1,500,
investments 500, revenue 8,000, EBIT 2,300, operating income 2,200, market value 10,000.
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from halal_quant.core.config import ShariaConfig, load_config
from halal_quant.sharia.screening import (
    Filing,
    ManualEntry,
    ScreeningInputs,
    Status,
    choose_balance_sheet,
    choose_income_report,
    describe_sources,
    matched_business,
    months_before,
    screen,
)

CONFIG = load_config(
    Path(__file__).parents[1] / "config" / "sharia" / "aaoifi_v1.yaml", ShariaConfig
).config
D = date(2020, 1, 31)


def sheet(**kw: object) -> Filing:
    base: dict[str, object] = {
        "dimension": "ARQ",
        "filing_date": date(2019, 11, 1),
        "period_end": date(2019, 9, 30),
        "debt": Decimal(2000),
        "cash_and_equivalents": Decimal(1500),
        "investments": Decimal(500),
    }
    return Filing(**{**base, **kw})  # type: ignore[arg-type]


def income(**kw: object) -> Filing:
    base: dict[str, object] = {
        "dimension": "ART",
        "filing_date": date(2019, 11, 1),
        "period_end": date(2019, 9, 30),
        "revenue": Decimal(8000),
        "ebit": Decimal(2300),
        "operating_income": Decimal(2200),
    }
    return Filing(**{**base, **kw})  # type: ignore[arg-type]


def inputs(**kw: object) -> ScreeningInputs:
    base: dict[str, object] = {
        "screening_date": D,
        "industry": "Software - Application",
        "sic_code": 7372,
        "market_value": Decimal(10000),
        "balance_sheet": sheet(),
        "income": income(),
    }
    return ScreeningInputs(**{**base, **kw})  # type: ignore[arg-type]


def test_the_worked_example_is_halal_and_the_reason_lists_all_three_ratios() -> None:
    result = screen(inputs(), CONFIG)
    assert result.status is Status.HALAL and result.status.eligible
    assert "debt 20.00% of market value (limit 30.00%)" in result.reason
    assert "cash and investments 20.00% of market value (limit 30.00%)" in result.reason
    assert "prohibited income 1.25% of revenue (limit 5.00%)" in result.reason
    assert result.details["debt_to_market_value"] == "0.2"


def test_e1_debt_exactly_at_the_limit_passes() -> None:
    assert screen(inputs(balance_sheet=sheet(debt=Decimal(3000))), CONFIG).status is Status.HALAL


def test_e2_debt_just_over_the_limit_fails_and_the_reason_records_it() -> None:
    result = screen(inputs(balance_sheet=sheet(debt=Decimal(3001))), CONFIG)
    assert result.status is Status.NON_HALAL and not result.status.eligible
    assert "Failed: debt" in result.reason and "30.01%" in result.reason


def test_e3_non_core_income_exactly_five_percent_passes() -> None:
    assert screen(inputs(income=income(ebit=Decimal(2600))), CONFIG).status is Status.HALAL


def test_e4_non_core_income_just_over_five_percent_fails() -> None:
    result = screen(inputs(income=income(ebit=Decimal(2601))), CONFIG)
    assert result.status is Status.NON_HALAL and "prohibited income 5.01%" in result.reason


def test_e5_negative_non_core_income_counts_as_zero() -> None:
    result = screen(inputs(income=income(ebit=Decimal(2100))), CONFIG)
    assert result.status is Status.HALAL and "prohibited income 0.00%" in result.reason


def test_the_cash_test_uses_cash_plus_investments_and_fails_above_the_limit() -> None:
    result = screen(inputs(balance_sheet=sheet(cash_and_equivalents=Decimal(2501))), CONFIG)
    assert result.status is Status.NON_HALAL and "Failed: cash and investments" in result.reason


def test_e6_a_missing_cash_figure_is_unknown() -> None:
    result = screen(inputs(balance_sheet=sheet(cash_and_equivalents=None)), CONFIG)
    assert result.status is Status.UNKNOWN and "Missing input: cashneq" in result.reason
    assert not result.status.eligible


def test_investments_fall_back_to_current_plus_non_current() -> None:
    parts = sheet(
        investments=None, investments_current=Decimal(200), investments_noncurrent=Decimal(300)
    )
    result = screen(inputs(balance_sheet=parts), CONFIG)
    assert result.status is Status.HALAL and "cash and investments 20.00%" in result.reason


def test_investments_missing_with_no_fallback_is_unknown() -> None:
    result = screen(inputs(balance_sheet=sheet(investments=None)), CONFIG)
    assert result.status is Status.UNKNOWN and "Missing input: investments" in result.reason


def test_e7_a_report_older_than_sixteen_months_is_unknown() -> None:
    old = sheet(period_end=date(2018, 3, 31), filing_date=date(2018, 5, 1))
    result = screen(inputs(balance_sheet=old), CONFIG)
    assert result.status is Status.UNKNOWN and "Out of date" in result.reason


def test_a_report_exactly_sixteen_months_old_is_still_usable() -> None:
    edge = sheet(period_end=date(2018, 9, 30))  # 16 months before 31 Jan 2020 is 30 Sep 2018
    assert screen(inputs(balance_sheet=edge), CONFIG).status is Status.HALAL
    assert screen(inputs(balance_sheet=sheet(period_end=date(2018, 9, 29))), CONFIG).status is (
        Status.UNKNOWN
    )


def test_e8_a_filing_made_on_the_screening_date_is_choosable_only_if_filed_before() -> None:
    # Choosing is the caller's job (filings filed on or after the date are never passed in):
    # the engine then uses what it is given. The database-level test covers the date rule.
    used = choose_balance_sheet(sheet(), None)
    assert used is not None and used.filing_date == date(2019, 11, 1)


def test_e9_a_prohibited_business_is_non_halal_whatever_the_ratios() -> None:
    result = screen(inputs(industry="Beverages - Brewers", balance_sheet=None, income=None), CONFIG)
    assert result.status is Status.NON_HALAL
    assert "alcohol" in result.reason and result.details["rule"] == "business"


def test_e10_zero_or_negative_revenue_is_unknown() -> None:
    for revenue in (Decimal(0), Decimal(-5)):
        result = screen(inputs(income=income(revenue=revenue)), CONFIG)
        assert result.status is Status.UNKNOWN and "revenue is not positive" in result.reason


def test_e11_a_missing_market_value_is_unknown() -> None:
    result = screen(inputs(market_value=None), CONFIG)
    assert result.status is Status.UNKNOWN and "market value on the screening date" in result.reason


def test_a_market_value_of_zero_or_less_is_unknown() -> None:
    for value in (Decimal(0), Decimal(-1)):
        result = screen(inputs(market_value=value), CONFIG)
        assert result.status is Status.UNKNOWN and "not positive" in result.reason


def test_negative_debt_cash_or_investments_is_unknown() -> None:
    assert screen(inputs(balance_sheet=sheet(debt=Decimal(-1))), CONFIG).status is Status.UNKNOWN
    negative_cash = sheet(cash_and_equivalents=Decimal(-1))
    assert screen(inputs(balance_sheet=negative_cash), CONFIG).status is Status.UNKNOWN
    negative_investments = sheet(investments=Decimal(-1))
    assert screen(inputs(balance_sheet=negative_investments), CONFIG).status is Status.UNKNOWN


def test_no_report_at_all_is_unknown() -> None:
    result = screen(inputs(balance_sheet=None, income=None), CONFIG)
    assert result.status is Status.UNKNOWN
    assert "no balance-sheet report" in result.reason and "no income report" in result.reason


def test_missing_income_fields_are_unknown() -> None:
    result = screen(inputs(income=income(ebit=None, operating_income=None)), CONFIG)
    assert result.status is Status.UNKNOWN and "Missing input: ebit, opinc" in result.reason


def test_a_failed_test_wins_over_a_missing_input_but_says_so() -> None:
    bad = sheet(debt=Decimal(4000), cash_and_equivalents=None)
    result = screen(inputs(balance_sheet=bad), CONFIG)
    assert result.status is Status.NON_HALAL
    assert "Failed: debt" in result.reason and "Missing input: cashneq" in result.reason


def test_the_manual_list_decides_first() -> None:
    for status in (Status.NON_HALAL, Status.PENDING_REVIEW):
        manual = ManualEntry(status, "Reviewer flagged this company")
        result = screen(inputs(manual=manual), CONFIG)
        assert result.status is status and not result.status.eligible
        assert result.reason == "Manual list: Reviewer flagged this company"
    # ... even when the ratios would have passed.
    assert screen(inputs(), CONFIG).status is Status.HALAL


def test_a_missing_debt_figure_is_unknown() -> None:
    result = screen(inputs(balance_sheet=sheet(debt=None)), CONFIG)
    assert result.status is Status.UNKNOWN and "Missing input: debt" in result.reason


def test_only_halal_is_eligible() -> None:
    assert {s for s in Status if s.eligible} == {Status.HALAL}


@pytest.mark.parametrize(
    ("industry", "sic", "category"),
    [
        ("Banks - Regional", None, "conventional_finance"),
        ("Savings & Cooperative Banks", 6035, "conventional_finance"),  # caught by SIC
        ("Software - Application", 6500, None),  # 6500 is not in a listed range
        (None, 2085, "alcohol"),
        ("Tobacco", None, "tobacco"),
        ("Resorts & Casinos", None, "gambling"),
        ("Gambling", None, "gambling"),
        ("Software - Application", 7372, None),
        (None, None, None),
    ],
)
def test_business_matching_uses_the_industry_name_or_the_sic_range(
    industry: str | None, sic: int | None, category: str | None
) -> None:
    assert matched_business(industry, sic, CONFIG.excluded_businesses) == category


def test_every_industry_name_in_the_config_is_matched() -> None:
    for business in CONFIG.excluded_businesses:
        for industry in business.industries:
            assert matched_business(industry, None, CONFIG.excluded_businesses) == business.category


def test_the_annual_report_is_used_only_when_there_is_no_quarterly_or_ttm_report() -> None:
    arq, ary, art = sheet(), sheet(dimension="ARY"), income()
    assert choose_balance_sheet(arq, ary) is arq and choose_balance_sheet(None, ary) is ary
    assert choose_income_report(art, ary) is art and choose_income_report(None, ary) is ary
    assert choose_balance_sheet(None, None) is None


def test_months_before_clamps_the_day_and_crosses_years() -> None:
    assert months_before(date(2020, 1, 31), 16) == date(2018, 9, 30)
    assert months_before(date(2020, 3, 31), 1) == date(2020, 2, 29)
    assert months_before(date(2020, 1, 15), 1) == date(2019, 12, 15)


def test_the_same_inputs_always_give_the_same_result() -> None:
    assert screen(inputs(), CONFIG) == screen(replace(inputs()), CONFIG)


def test_describe_sources_lists_only_the_filings_used() -> None:
    described = describe_sources([sheet(), None, income()])
    assert [d["dimension"] for d in described] == ["ARQ", "ART"]
    assert described[0]["filing_date"] == "2019-11-01"
