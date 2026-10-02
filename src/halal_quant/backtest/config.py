"""Phase 2 configuration: the research protocol and the strategy parameters (ADR-003).

Both live in versioned YAML files (PRD §29) and are loaded with the same strict, fail-closed loader
as the Sharia and universe configuration. The protocol is what makes the research honest: its
periods are fixed in advance, and `guard_period` refuses a run that touches the out-of-sample
period unless it is explicitly declared a final test.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from halal_quant.core.config import LoadedConfig, Status, _Strict, load_config

SLIPPAGE_CASES = ("optimistic", "base", "stress")


class DateRange(_Strict):
    first: date
    last: date

    @model_validator(mode="after")
    def _ordered(self) -> "DateRange":
        if self.last < self.first:
            raise ValueError("a period must end on or after the day it starts")
        return self

    def overlaps(self, first: date, last: date) -> bool:
        return first <= self.last and last >= self.first


class Periods(_Strict):
    burn_in: DateRange
    in_sample: DateRange
    validation: DateRange
    out_of_sample: DateRange

    @model_validator(mode="after")
    def _in_order_without_overlap(self) -> "Periods":
        ordered = [self.burn_in, self.in_sample, self.validation, self.out_of_sample]
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            if later.first <= earlier.last:
                raise ValueError("the research periods must follow each other without overlap")
        return self


class Costs(_Strict):
    commission_per_share: Decimal = Field(ge=0)
    commission_min_per_order: Decimal = Field(ge=0)
    commission_max_fraction: Decimal = Field(gt=0, le=1)
    slippage_bps: dict[str, Decimal]

    @model_validator(mode="after")
    def _all_cases(self) -> "Costs":
        if set(self.slippage_bps) != set(SLIPPAGE_CASES):
            raise ValueError(f"slippage_bps needs exactly these cases: {SLIPPAGE_CASES}")
        if any(v < 0 for v in self.slippage_bps.values()):
            raise ValueError("slippage cannot be negative")
        return self


class ResearchProtocol(_Strict):
    config_type: Literal["research_protocol"]
    version: str = Field(min_length=1)
    status: Status
    approved_by: str | None = None
    approved_on: date | None = None
    periods: Periods
    costs: Costs
    purification_annual_drag: Decimal = Field(ge=0, lt=1)


class MomentumConfig(_Strict):
    config_type: Literal["strategy_momentum"]
    version: str = Field(min_length=1)
    status: Status
    lookback_trading_days: int = Field(gt=0)
    skip_trading_days: int = Field(ge=0)
    top_n: int = Field(gt=0)
    weighting: Literal["equal"]
    rebalance: Literal["first_trading_day_of_month"]


class ProtocolError(Exception):
    """A run would break the research protocol."""


def load_protocol(path: Path) -> LoadedConfig[ResearchProtocol]:
    return load_config(path, ResearchProtocol)


def load_momentum(path: Path) -> LoadedConfig[MomentumConfig]:
    return load_config(path, MomentumConfig)


def period_name(protocol: ResearchProtocol, first: date, last: date) -> str:
    """Which protocol period a run covers: its name, or `custom` if it fits none exactly."""
    for name in ("burn_in", "in_sample", "validation", "out_of_sample"):
        period: DateRange = getattr(protocol.periods, name)
        if first >= period.first and last <= period.last:
            return name
    return "custom"


def guard_period(protocol: ResearchProtocol, first: date, last: date, final_test: bool) -> str:
    """Refuse a run that touches the out-of-sample period unless it is declared the final test.

    The out-of-sample period is touched once, at the end of Phase 3. Anything that overlaps it is
    out-of-sample, whatever it is called. Returns the period name for the run record.
    """
    if last < first:
        raise ProtocolError("The run ends before it starts.")
    touches = protocol.periods.out_of_sample.overlaps(first, last)
    if touches and not final_test:
        raise ProtocolError(
            "This run touches the out-of-sample period "
            f"({protocol.periods.out_of_sample.first} to {protocol.periods.out_of_sample.last}), "
            "which may be used once, at the end of Phase 3. Declare it with --final-test only "
            "when that is what this is."
        )
    if final_test and not touches:
        raise ProtocolError("--final-test is only for a run that covers the out-of-sample period.")
    if first < protocol.periods.burn_in.first:
        raise ProtocolError("The run starts before the first date with data.")
    return period_name(protocol, first, last)
