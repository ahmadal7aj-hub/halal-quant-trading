"""Stored backtest runs: inputs, hashes, NAV, trades and rebalances (P2-6; PRD §18, §20).

A run is identified by the hash of its inputs (`run_key_sha256`). Running the same inputs again
must reproduce the same `result_sha256`; if it does not, something changed (the code, the data
or a bug) and the run fails loudly instead of quietly storing a second answer.
"""

import hashlib
import json
import subprocess  # nosec B404
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Connection,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB

from halal_quant.backtest.engine import BacktestResult
from halal_quant.db.engine import metadata

NAV_PLACES = Decimal("0.00000001")

backtest_run_table = Table(
    "backtest_run",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("run_key_sha256", Text, nullable=False),
    Column(
        "vintage_id",
        Text,
        ForeignKey("price_vintage.vintage_id", name="fk_backtest_run_vintage"),
        nullable=False,
    ),
    Column("strategy_version", Text, nullable=False),
    Column("strategy_params", JSONB, nullable=False),
    Column("strategy_sha256", Text, nullable=False),
    Column("protocol_version", Text, nullable=False),
    Column("protocol_sha256", Text, nullable=False),
    Column("universe_version", Text, nullable=False),
    Column("universe_sha256", Text, nullable=False),
    Column("methodology", Text, nullable=False),
    Column("slippage_case", Text, nullable=False),
    Column("slippage_bps", Numeric(10, 4), nullable=False),
    Column("period_name", Text, nullable=False),
    Column("first_day", Date, nullable=False),
    Column("last_day", Date, nullable=False),
    Column("final_test", Boolean, nullable=False),
    Column("code_version", Text, nullable=False),
    Column("result_sha256", Text, nullable=False),
    Column("metrics", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("run_key_sha256", name="uq_backtest_run_key"),
    CheckConstraint("last_day >= first_day", name="ck_backtest_run_period"),
)

backtest_nav_table = Table(
    "backtest_nav",
    metadata,
    Column(
        "run_id",
        BigInteger,
        ForeignKey("backtest_run.id", name="fk_backtest_nav_run"),
        nullable=False,
    ),
    Column("nav_date", Date, nullable=False),
    Column("nav", Numeric(24, 8), nullable=False),
    PrimaryKeyConstraint("run_id", "nav_date", name="pk_backtest_nav"),
)

backtest_trade_table = Table(
    "backtest_trade",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column(
        "run_id",
        BigInteger,
        ForeignKey("backtest_run.id", name="fk_backtest_trade_run"),
        nullable=False,
    ),
    Column("trade_date", Date, nullable=False),
    Column(
        "security_id",
        BigInteger,
        ForeignKey("security.security_id", name="fk_backtest_trade_security"),
        nullable=False,
    ),
    Column("side", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("shares", Numeric(24, 4), nullable=False),
    Column("price", Numeric(30, 6), nullable=False),
    Column("notional", Numeric(24, 2), nullable=False),
    Column("cost", Numeric(24, 4), nullable=False),
    Column("reason", Text, nullable=False),
    CheckConstraint("side IN ('BUY', 'SELL')", name="ck_backtest_trade_side"),
    Index("ix_backtest_trade_run", "run_id", "trade_date"),
)

backtest_rebalance_table = Table(
    "backtest_rebalance",
    metadata,
    Column(
        "run_id",
        BigInteger,
        ForeignKey("backtest_run.id", name="fk_backtest_rebalance_run"),
        nullable=False,
    ),
    Column("decision_date", Date, nullable=False),
    Column("eligible", Integer, nullable=False),
    Column("scored", Integer, nullable=False),
    Column("selected", Integer, nullable=False),
    Column("turnover", Numeric(12, 6), nullable=False),
    Column("costs", Numeric(24, 4), nullable=False),
    Column("nav_before", Numeric(24, 8), nullable=False),
    Column("nav_after", Numeric(24, 8), nullable=False),
    PrimaryKeyConstraint("run_id", "decision_date", name="pk_backtest_rebalance"),
)


class ReproducibilityError(Exception):
    """The same inputs gave a different result than the one stored: do not trust either."""


@dataclass(frozen=True)
class RunInputs:
    vintage_id: str
    strategy_version: str
    strategy_params: dict[str, Any]
    strategy_sha256: str
    protocol_version: str
    protocol_sha256: str
    universe_version: str
    universe_sha256: str
    methodology: str
    slippage_case: str
    slippage_bps: Decimal
    period_name: str
    first_day: date
    last_day: date
    final_test: bool


def _sha(payload: object) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def run_key(inputs: RunInputs) -> str:
    """The hash of everything that determines a result (not the code version or period name)."""
    return _sha(
        {
            "vintage": inputs.vintage_id,
            "strategy": inputs.strategy_sha256,
            "protocol": inputs.protocol_sha256,
            "universe": inputs.universe_sha256,
            "first": inputs.first_day.isoformat(),
            "last": inputs.last_day.isoformat(),
            "slippage_bps": str(inputs.slippage_bps),
        }
    )


def result_hash(result: BacktestResult) -> str:
    """The hash of a result: NAV, trades (with reasons) and rebalances, in exact decimals."""
    return _sha(
        {
            "nav": [[d.isoformat(), str(v.quantize(NAV_PLACES))] for d, v in result.nav],
            "trades": [
                [
                    t.trade_date.isoformat(),
                    t.security_id,
                    t.side,
                    t.kind,
                    str(t.shares),
                    str(t.price),
                    str(t.notional),
                    str(t.cost),
                    t.reason,
                ]
                for t in result.trades
            ],
            "rebalances": [
                [
                    r.decision_date.isoformat(),
                    r.eligible,
                    r.scored,
                    r.selected,
                    str(r.turnover.quantize(Decimal("0.000001"))),
                    str(r.costs.quantize(Decimal("0.0001"))),
                    str(r.nav_before.quantize(NAV_PLACES)),
                    str(r.nav_after.quantize(NAV_PLACES)),
                ]
                for r in result.rebalances
            ],
        }
    )


def code_version() -> str:
    """The git commit the code is at, or `unknown` when git is not available (fixed command)."""
    try:
        out = subprocess.run(  # noqa: S603  # nosec B603 B607
            ["git", "rev-parse", "--short=12", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=10,
            cwd=Path(__file__).parent,
            check=True,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def stored_result_hash(conn: Connection, key: str) -> tuple[int, str] | None:
    r = backtest_run_table.c
    row = conn.execute(select(r.id, r.result_sha256).where(r.run_key_sha256 == key)).first()
    return None if row is None else (int(row.id), str(row.result_sha256))


def save_run(
    conn: Connection,
    inputs: RunInputs,
    result: BacktestResult,
    metrics: dict[str, Any],
    version: str,
) -> tuple[int, str]:
    """Store a new run; returns (run id, result hash). The caller has checked the key is new."""
    digest = result_hash(result)
    run_id: int = conn.execute(
        backtest_run_table.insert()
        .values(
            run_key_sha256=run_key(inputs),
            vintage_id=inputs.vintage_id,
            strategy_version=inputs.strategy_version,
            strategy_params=inputs.strategy_params,
            strategy_sha256=inputs.strategy_sha256,
            protocol_version=inputs.protocol_version,
            protocol_sha256=inputs.protocol_sha256,
            universe_version=inputs.universe_version,
            universe_sha256=inputs.universe_sha256,
            methodology=inputs.methodology,
            slippage_case=inputs.slippage_case,
            slippage_bps=inputs.slippage_bps,
            period_name=inputs.period_name,
            first_day=inputs.first_day,
            last_day=inputs.last_day,
            final_test=inputs.final_test,
            code_version=version,
            result_sha256=digest,
            metrics=metrics,
        )
        .returning(backtest_run_table.c.id)
    ).scalar_one()
    conn.execute(
        backtest_nav_table.insert(),
        [{"run_id": run_id, "nav_date": d, "nav": v.quantize(NAV_PLACES)} for d, v in result.nav],
    )
    if result.trades:
        conn.execute(
            backtest_trade_table.insert(),
            [
                {
                    "run_id": run_id,
                    "trade_date": t.trade_date,
                    "security_id": t.security_id,
                    "side": t.side,
                    "kind": t.kind,
                    "shares": t.shares,
                    "price": t.price,
                    "notional": t.notional,
                    "cost": t.cost,
                    "reason": t.reason,
                }
                for t in result.trades
            ],
        )
    conn.execute(
        backtest_rebalance_table.insert(),
        [
            {
                "run_id": run_id,
                "decision_date": r.decision_date,
                "eligible": r.eligible,
                "scored": r.scored,
                "selected": r.selected,
                "turnover": r.turnover.quantize(Decimal("0.000001")),
                "costs": r.costs.quantize(Decimal("0.0001")),
                "nav_before": r.nav_before.quantize(NAV_PLACES),
                "nav_after": r.nav_after.quantize(NAV_PLACES),
            }
            for r in result.rebalances
        ],
    )
    return run_id, digest
