"""Performance analytics for a NAV series (P2-8; PRD §22).

Every function takes plain data (a list of (date, NAV) points) so it can be tested on small
examples and reused for the benchmarks. Returns are daily simple returns of the NAV; the
risk-free rate is taken as zero (stated in every report), so "Sharpe" here is return per unit of
volatility. Figures are floats: they describe results and are not part of the result hash.
"""

import math
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

TRADING_DAYS = 252
DAYS_PER_YEAR = 365.25


def daily_returns(nav: Sequence[tuple[date, Decimal]]) -> list[float]:
    """Simple daily returns of consecutive NAV points."""
    values = [float(v) for _, v in nav]
    return [b / a - 1 for a, b in zip(values, values[1:], strict=False) if a > 0]


def total_return(nav: Sequence[tuple[date, Decimal]]) -> float | None:
    if len(nav) < 2 or nav[0][1] <= 0:
        return None
    return float(nav[-1][1] / nav[0][1]) - 1


def cagr(nav: Sequence[tuple[date, Decimal]]) -> float | None:
    """Compound annual growth rate over the calendar span of the series."""
    growth = total_return(nav)
    if growth is None:
        return None
    years = (nav[-1][0] - nav[0][0]).days / DAYS_PER_YEAR
    if years <= 0:
        return None
    return (1 + growth) ** (1 / years) - 1 if growth > -1 else -1.0


def volatility(returns: Sequence[float]) -> float | None:
    """Annualised standard deviation of daily returns."""
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(TRADING_DAYS)


def sharpe(returns: Sequence[float]) -> float | None:
    """Annualised mean daily return over annualised volatility (risk-free rate taken as zero)."""
    vol = volatility(returns)
    if not vol:
        return None
    return (sum(returns) / len(returns)) * TRADING_DAYS / vol


def sortino(returns: Sequence[float]) -> float | None:
    """Like Sharpe, but only downside moves count as risk."""
    if len(returns) < 2:
        return None
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in returns) / len(returns))
    if not downside:
        return None
    return (sum(returns) / len(returns)) * TRADING_DAYS / (downside * math.sqrt(TRADING_DAYS))


def max_drawdown(nav: Sequence[tuple[date, Decimal]]) -> tuple[float, date | None, date | None]:
    """The worst peak-to-trough fall: (fraction, peak date, trough date)."""
    worst, peak_day, trough_day = 0.0, None, None
    peak, peak_date = None, None
    for day, value in nav:
        v = float(value)
        if peak is None or v > peak:
            peak, peak_date = v, day
        elif peak > 0 and v / peak - 1 < worst:
            worst, peak_day, trough_day = v / peak - 1, peak_date, day
    return worst, peak_day, trough_day


def monthly_returns(nav: Sequence[tuple[date, Decimal]]) -> list[tuple[str, float]]:
    """Return of each calendar month, measured from the last NAV of the month before."""
    last: dict[str, Decimal] = {}
    order: list[str] = []
    for day, value in nav:
        key = f"{day:%Y-%m}"
        if key not in last:
            order.append(key)
        last[key] = value
    out = []
    previous = nav[0][1] if nav else None
    for key in order:
        if previous and previous > 0:
            out.append((key, float(last[key] / previous) - 1))
        previous = last[key]
    return out


def summarize(
    nav: Sequence[tuple[date, Decimal]],
    traded_value: Decimal = Decimal(0),
    costs: Decimal = Decimal(0),
) -> dict[str, float | int | str | None]:
    """The headline figures for one NAV series, as plain JSON-friendly values."""
    returns = daily_returns(nav)
    drawdown, peak, trough = max_drawdown(nav)
    months = monthly_returns(nav)
    years = (nav[-1][0] - nav[0][0]).days / DAYS_PER_YEAR if len(nav) > 1 else 0.0
    average_nav = float(sum(v for _, v in nav) / len(nav)) if nav else 0.0
    return {
        "first_day": nav[0][0].isoformat() if nav else None,
        "last_day": nav[-1][0].isoformat() if nav else None,
        "trading_days": len(nav),
        "total_return": total_return(nav),
        "cagr": cagr(nav),
        "volatility": volatility(returns),
        "sharpe": sharpe(returns),
        "sortino": sortino(returns),
        "max_drawdown": drawdown,
        "max_drawdown_peak": peak.isoformat() if peak else None,
        "max_drawdown_trough": trough.isoformat() if trough else None,
        "months": len(months),
        "positive_months": sum(1 for _, r in months if r > 0),
        "best_month": max((r for _, r in months), default=None),
        "worst_month": min((r for _, r in months), default=None),
        "annual_turnover": (float(traded_value) / 2 / average_nav / years)
        if years > 0 and average_nav > 0
        else None,
        "costs_fraction_of_start_nav": float(costs / nav[0][1]) if nav and nav[0][1] else None,
    }
