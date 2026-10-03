"""The owner's dashboard (Phase 6; PRD §26-§28, §34, §48): plain English, login required.

Start it with:  uv run streamlit run src/halal_quant/dashboard/app.py
It listens on this laptop only (127.0.0.1, see .streamlit/config.toml). Without a password hash in
`.env` (made by `python -m halal_quant.dashboard.auth`) it refuses everyone. Pages only read the
database through the read-only role; the two actions, deciding a proposal and the kill switch, use
the application role, need a reason, and are audited.
"""

from pathlib import Path
from typing import Any

import streamlit as st

from halal_quant.core.settings import DbRole, get_settings
from halal_quant.dashboard import queries
from halal_quant.dashboard.auth import LoginThrottle, verify_password
from halal_quant.db.engine import make_engine
from halal_quant.trading.records import (
    ProposalError,
    decide_proposal,
    engage_kill_switch,
    release_kill_switch,
)
from halal_quant.trading.risk import load_risk_limits

ACTOR = "owner:dashboard"
RISK_FILE = Path("config/risk/risk_v1.yaml")


@st.cache_resource
def engine(role: DbRole) -> Any:
    return make_engine(get_settings(), role)


def read() -> Any:
    return engine(DbRole.READONLY).connect()


def money(value: Any) -> str:
    return "n/a" if value is None else f"${float(value):,.2f}"


def pct(value: Any) -> str:
    return "n/a" if value is None else f"{float(value) * 100:.1f}%"


def table(rows: list[dict[str, Any]], empty: str = "Nothing to show yet.") -> None:
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.info(empty)


def overview() -> None:
    st.title("Overview")
    with read() as conn:
        risk = queries.risk_status(conn)
        health = queries.system_health(conn)
        waiting = queries.pending_approvals(conn)
        orders = queries.orders(conn, 5)
    if risk["kill_switch_engaged"]:
        st.error("TRADING IS STOPPED: the kill switch is on. No orders can be sent.")
    else:
        st.success("Trading allowed (paper account only). The kill switch is off.")
    a, b, c = st.columns(3)
    a.metric("Waiting for your approval", len(waiting))
    b.metric("Orders in doubt or failed", health["orders_failed_or_in_doubt"])
    c.metric("Database", health["database"])
    st.write(f"**Broker:** {health['broker']}")
    st.write("**Portfolio value, cash and daily profit** appear here once the broker is connected.")
    st.subheader("Latest orders")
    table(orders)


def holdings_page() -> None:
    st.title("Holdings")
    limits = load_risk_limits(RISK_FILE).config
    with read() as conn:
        rows = queries.holdings(conn, limits.approved_funds)
    table(
        [
            {
                "Symbol": h.symbol,
                "Kind": h.kind,
                "Shares": float(h.quantity),
                "Price": money(h.price),
                "Value": money(h.value),
                "Weight %": None if h.weight_pct is None else round(float(h.weight_pct), 1),
            }
            for h in rows
        ],
        "No holdings recorded yet. Holdings come from filled paper orders.",
    )


def strategy_page() -> None:
    st.title("Strategy and proposed trades")
    st.write(
        "Research found no trading strategy that beat simply holding a halal fund, so the plan "
        "is a core of an approved halal fund. Every proposed order and the risk engine's "
        "decision are listed here, including the refused ones and why."
    )
    with read() as conn:
        rows = queries.recent_proposals(conn)
    table(rows, "No trades have been proposed yet.")


def backtests_page() -> None:
    st.title("Backtests")
    st.write(
        "Stored research runs (each reproducible by its result hash). Out-of-sample is untouched."
    )
    with read() as conn:
        rows = queries.backtests(conn)
    for r in rows:
        for key in ("cagr", "volatility", "max_drawdown"):
            r[key] = pct(r[key])
        r["sharpe"] = None if r["sharpe"] is None else round(r["sharpe"], 2)
    table(rows)


def sharia_page() -> None:
    st.title("Sharia screening")
    with read() as conn:
        s = queries.sharia_summary(conn)
    st.write(f"**Method:** {s['methodology']} (AAOIFI-based). **Provider:** {s['provider']}.")
    st.write(f"**Latest screening date:** {s['latest_screening_date']}")
    if s["latest_universe"]:
        u = s["latest_universe"]
        st.write(f"**Eligible universe:** {u['member_count']} companies as of {u['as_of']}.")
    st.write("**Results at the latest screening:**")
    st.dataframe([{"Status": k, "Securities": v} for k, v in s["counts"].items()], hide_index=True)
    if s["unknown_count"]:
        st.warning(
            f"{s['unknown_count']} securities have no classification (UNKNOWN). "
            "They are never bought."
        )


def risk_page() -> None:
    st.title("Risk")
    limits = load_risk_limits(RISK_FILE)
    with read() as conn:
        r = queries.risk_status(conn)
    st.write(f"**Limits:** {limits.ref.version} ({limits.config.status}).")
    st.json(limits.config.model_dump(mode="json", exclude={"config_type"}))
    st.write("**Rules that have refused or flagged orders:**")
    table(r["rules_triggered"], "No rule has been triggered yet.")
    st.write(f"**Orders in doubt:** {r['orders_in_doubt']}")


def orders_page() -> None:
    st.title("Orders")
    with read() as conn:
        rows = queries.orders(conn)
        chosen = (
            st.selectbox("Show the history of order", [r["id"] for r in rows]) if rows else None
        )
        history = queries.order_history(conn, chosen) if chosen else []
    table(rows, "No orders yet.")
    if history:
        st.subheader(f"History of order {chosen}")
        table(history)


def approvals_page() -> None:
    st.title("Approve or reject trades")
    with read() as conn:
        waiting = queries.pending_approvals(conn)
    if not waiting:
        st.info("Nothing is waiting for you.")
        return
    for p in waiting:
        with st.container(border=True):
            st.subheader(f"#{p['id']}: {p['side']} {p['quantity']} {p['symbol']}")
            st.write(
                f"Price {money(p['ref_price'])}, value {money(p['notional'])}. "
                f"Why: {p['strategy_reason']}"
            )
            st.write(f"Risk engine: {p['risk_result']} ({p['reason_code']}): {p['explanation']}")
            reason = st.text_input("Your reason (required)", key=f"reason{p['id']}")
            yes, no = st.columns(2)
            for column, approve, label in ((yes, True, "APPROVE"), (no, False, "REJECT")):
                if column.button(label, key=f"{label}{p['id']}"):
                    try:
                        with engine(DbRole.APP).begin() as conn:
                            status = decide_proposal(conn, p["id"], approve, ACTOR, reason)
                        st.success(f"Recorded: {status}")
                        st.rerun()
                    except (ProposalError, ValueError) as exc:
                        st.error(str(exc))


def kill_switch_page() -> None:
    st.title("STOP TRADING (kill switch)")
    with read() as conn:
        risk = queries.risk_status(conn)
    engaged = risk["kill_switch_engaged"]
    st.write("**State:** " + ("STOPPED: no orders can be sent." if engaged else "Trading allowed."))
    reason = st.text_input("Reason (required)")
    sure = st.checkbox("I am sure")
    label = "RESUME trading" if engaged else "STOP all trading now"
    if st.button(label, type="primary"):
        if not (reason.strip() and sure):
            st.error("Give a reason and tick the box.")
        else:
            with engine(DbRole.APP).begin() as conn:
                (release_kill_switch if engaged else engage_kill_switch)(conn, ACTOR, reason)
            st.success("Done. The change is recorded.")
            st.rerun()
    st.subheader("Recent changes")
    table(risk["recent_kill_switch_events"], "The kill switch has never been used.")


def health_page() -> None:
    st.title("System health")
    with read() as conn:
        h = queries.system_health(conn)
    st.json(h, expanded=True)


def audit_page() -> None:
    st.title("Audit trail")
    with read() as conn:
        rows = queries.audit_trail(conn)
    table(rows)


def login() -> bool:
    """The login screen. Returns True once the owner is signed in for this browser session."""
    if st.session_state.get("signed_in"):
        return True
    throttle: LoginThrottle = st.session_state.setdefault("throttle", LoginThrottle())
    st.title("Halal Quant: sign in")
    stored = get_settings().dashboard_password_hash
    if stored is None or not stored.get_secret_value().strip():
        st.error(
            "No dashboard password is set, so nobody can sign in. Run "
            "`uv run python -m halal_quant.dashboard.auth` and put the line it prints in .env."
        )
        return False
    if not throttle.allowed():
        st.error(f"Too many wrong tries. Wait {throttle.seconds_left()} seconds.")
        return False
    password = st.text_input("Password", type="password")
    if st.button("Sign in"):
        ok = verify_password(password, stored.get_secret_value())
        throttle.record(ok)
        if ok:
            st.session_state["signed_in"] = True
            st.rerun()
        st.error("Wrong password.")
    return False


def main() -> None:
    st.set_page_config(page_title="Halal Quant", layout="wide")
    if not login():
        return
    pages = [
        st.Page(overview, title="Overview", url_path="overview", default=True),
        st.Page(holdings_page, title="Holdings", url_path="holdings"),
        st.Page(strategy_page, title="Strategy", url_path="strategy"),
        st.Page(approvals_page, title="Approvals", url_path="approvals"),
        st.Page(orders_page, title="Orders", url_path="orders"),
        st.Page(risk_page, title="Risk", url_path="risk"),
        st.Page(sharia_page, title="Sharia", url_path="sharia"),
        st.Page(backtests_page, title="Backtests", url_path="backtests"),
        st.Page(health_page, title="System health", url_path="health"),
        st.Page(audit_page, title="Audit trail", url_path="audit"),
        st.Page(kill_switch_page, title="STOP TRADING", url_path="stop"),
    ]
    st.navigation(pages).run()
    if st.sidebar.button("Sign out"):
        st.session_state.clear()
        st.rerun()


main()
