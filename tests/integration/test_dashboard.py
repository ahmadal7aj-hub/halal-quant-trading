"""Phase 6: the dashboard's data layer and the app itself, on made-up records."""

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine
from streamlit.testing.v1 import AppTest

from halal_quant.core.settings import DbRole, get_settings
from halal_quant.dashboard import queries
from halal_quant.dashboard.auth import hash_password
from halal_quant.trading import risk
from halal_quant.trading.broker import SimulatedBroker
from halal_quant.trading.orders import submit_approved_proposal
from halal_quant.trading.records import (
    AWAITING_APPROVAL,
    engage_kill_switch,
    release_kill_switch,
    submit_proposal,
)

D = Decimal
APP = str(Path(__file__).resolve().parents[2] / "src" / "halal_quant" / "dashboard" / "app.py")
OK = risk.RiskDecision(risk.APPROVED, risk.OK, "All risk checks passed.")
MANUAL = risk.RiskDecision(risk.REQUIRES_MANUAL_REVIEW, risk.MANUAL_REVIEW_SIZE, "above $20,000")
REFUSED = risk.RiskDecision(risk.REJECTED, risk.STALE_PRICE, "price too old")


@pytest.fixture
def conn(engines: dict[DbRole, Engine]) -> Iterator[Connection]:
    with engines[DbRole.APP].connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def order(symbol: str = "SPUS", qty: int = 10) -> risk.OrderRequest:
    return risk.OrderRequest(symbol, "BUY", D(qty), D(100), "fund core")


def test_pending_approvals_and_recent_proposals_show_what_the_owner_needs(
    conn: Connection,
) -> None:
    waiting_id, _ = submit_proposal(conn, order(qty=300), MANUAL, "risk-v1", "system:test")
    submit_proposal(conn, order(), REFUSED, "risk-v1", "system:test")
    pending = {p["id"]: p for p in queries.pending_approvals(conn)}
    assert waiting_id in pending
    row = pending[waiting_id]
    assert row["symbol"] == "SPUS" and row["notional"] == D("30000.00")
    assert "above $20,000" in row["explanation"] and row["strategy_reason"] == "fund core"
    recent = queries.recent_proposals(conn, 5)
    assert {r["status"] for r in recent} >= {AWAITING_APPROVAL, "RISK_REJECTED"}


def test_orders_and_holdings_come_from_the_fills(conn: Connection) -> None:
    broker = SimulatedBroker()
    first, _ = submit_proposal(conn, order(qty=10), OK, "risk-v1", "system:test")
    submit_approved_proposal(conn, broker, first)
    second, _ = submit_proposal(
        conn, risk.OrderRequest("SPUS", "SELL", D(4), D(100), "trim"), OK, "risk-v1", "system:test"
    )
    submit_approved_proposal(conn, broker, second)
    rows = queries.orders(conn, 5)
    assert [(r["symbol"], r["side"], r["state"], r["filled"]) for r in rows[:2]] == [
        ("SPUS", "SELL", "FILLED", D(4)),
        ("SPUS", "BUY", "FILLED", D(10)),
    ]
    held = {h.symbol: h for h in queries.holdings(conn, ["SPUS", "HLAL"])}
    assert held["SPUS"].quantity == D(6) and held["SPUS"].kind == "approved fund"
    assert held["SPUS"].price is not None  # priced from the newest complete vintage
    assert held["SPUS"].value == held["SPUS"].quantity * held["SPUS"].price
    history = queries.order_history(conn, rows[1]["id"])
    assert [h["state"] for h in history] == ["APPROVED", "SUBMITTED", "FILLED"]


def test_risk_status_follows_the_kill_switch_and_counts_triggered_rules(
    conn: Connection,
) -> None:
    submit_proposal(conn, order(), REFUSED, "risk-v1", "system:test")
    engage_kill_switch(conn, "owner", "test")
    r = queries.risk_status(conn)
    assert r["kill_switch_engaged"] is True
    assert {"reason_code": "STALE_PRICE"}.items() <= next(
        x for x in r["rules_triggered"] if x["reason_code"] == "STALE_PRICE"
    ).items()
    release_kill_switch(conn, "owner", "over")
    assert queries.risk_status(conn)["kill_switch_engaged"] is False


def test_sharia_backtest_health_and_audit_pages_read_the_real_data(conn: Connection) -> None:
    s = queries.sharia_summary(conn)
    assert s["latest_screening_date"] is not None and s["counts"].get("HALAL", 0) > 0
    assert "not reviewed" in s["provider"] and s["latest_universe"]["member_count"] > 0
    assert queries.backtests(conn, 3)
    health = queries.system_health(conn)
    assert health["database"] == "connected" and health["schema_revision"]
    assert any(v["status"] == "complete" for v in health["price_vintages"])
    assert queries.audit_trail(conn, 3)


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> AppTest:
    monkeypatch.setenv("HQ_DASHBOARD_PASSWORD_HASH", hash_password("a long test password", 1_000))
    get_settings.cache_clear()
    return AppTest.from_file(APP, default_timeout=30)


def test_the_app_refuses_everyone_when_no_password_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HQ_DASHBOARD_PASSWORD_HASH", "")
    get_settings.cache_clear()
    try:
        at = AppTest.from_file(APP, default_timeout=30).run()
        assert not at.exception
        assert any("nobody can sign in" in e.value for e in at.error)
        assert not at.sidebar.button  # no navigation, no pages
    finally:
        get_settings.cache_clear()


def test_a_wrong_password_stays_signed_out_and_the_right_one_opens_the_pages(app: AppTest) -> None:
    try:
        at = app.run()
        assert not at.exception and at.text_input[0].label == "Password"
        at.text_input[0].set_value("wrong password").run()
        at.button[0].click().run()
        assert any("Wrong password" in e.value for e in at.error)
        at.text_input[0].set_value("a long test password").run()
        at.button[0].click().run()
        assert not at.exception
        assert at.session_state["signed_in"] is True
        assert any("Overview" in t.value for t in at.title)
    finally:
        get_settings.cache_clear()
