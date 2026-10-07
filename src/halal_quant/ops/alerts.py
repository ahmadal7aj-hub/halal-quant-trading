"""Alerts: what needs the owner's attention right now (Phase 7; PRD §32, doc 02 §9).

    uv run python -m halal_quant.ops.alerts

`evaluate` is a pure function from a snapshot of the system to a list of alerts in plain English,
so every rule is tested. `collect_state` builds the snapshot from the database and the backup
folder. The dashboard shows the alerts on its overview page. E-mail delivery is not built (it needs
the owner's mail account); until then alerts appear on the dashboard and in this command.
"""

import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final, Literal

from sqlalchemy import Connection, text

from halal_quant.data.calendar import previous_trading_day
from halal_quant.ops.backup import DEFAULT_DIR, latest_status
from halal_quant.trading.records import kill_switch_engaged

CRITICAL: Final = "CRITICAL"
WARNING: Final = "WARNING"
BACKUP_MAX_HOURS = 36
PRICE_MAX_TRADING_DAYS_OLD = 1


@dataclass(frozen=True)
class Alert:
    severity: Literal["CRITICAL", "WARNING"]
    code: str
    message: str


@dataclass(frozen=True)
class SystemState:
    today: date
    kill_switch_engaged: bool
    orders_in_doubt: int
    orders_failed: int
    reconciliation_mismatches_7d: int
    approvals_waiting: int
    latest_price_date: date | None
    backup: dict[str, Any]


def _trading_days_between(older: date, newer: date) -> int:
    count, day = 0, newer
    while day > older and count < 30:
        day = previous_trading_day(day)
        count += 1
    return count


def evaluate(state: SystemState) -> list[Alert]:
    """Alerts for a snapshot, most serious first."""
    alerts: list[Alert] = []
    if state.kill_switch_engaged:
        alerts.append(Alert(CRITICAL, "KILL_SWITCH", "Trading is STOPPED: the kill switch is on."))
    if state.orders_in_doubt:
        alerts.append(
            Alert(
                CRITICAL,
                "ORDER_IN_DOUBT",
                f"{state.orders_in_doubt} order(s) were sent but the broker never answered. "
                "Nothing new can be sent until they are checked with the broker.",
            )
        )
    if state.reconciliation_mismatches_7d:
        alerts.append(
            Alert(
                CRITICAL,
                "RECONCILIATION_MISMATCH",
                "Our records and the broker's positions disagreed in the last 7 days. "
                "Trading was stopped; check the audit trail.",
            )
        )
    if state.orders_failed:
        alerts.append(
            Alert(WARNING, "ORDERS_FAILED", f"{state.orders_failed} order(s) ended in FAILED.")
        )
    if state.approvals_waiting:
        alerts.append(
            Alert(
                WARNING,
                "APPROVALS_WAITING",
                f"{state.approvals_waiting} trade(s) are waiting for your approval.",
            )
        )
    if state.latest_price_date is None:
        alerts.append(Alert(WARNING, "NO_PRICES", "There is no market data at all."))
    else:
        old = _trading_days_between(state.latest_price_date, state.today)
        if old > PRICE_MAX_TRADING_DAYS_OLD:
            alerts.append(
                Alert(
                    WARNING,
                    "STALE_DATA",
                    f"Market data ends {state.latest_price_date} ({old} trading days old). "
                    "Refresh it before any paper trading; stale prices block orders.",
                )
            )
    alerts += _backup_alerts(state.backup)
    order = {CRITICAL: 0, WARNING: 1}
    return sorted(alerts, key=lambda a: order[a.severity])


def _backup_alerts(backup: dict[str, Any]) -> list[Alert]:
    if not backup.get("newest"):
        return [Alert(WARNING, "NO_BACKUP", "No backup has been made yet.")]
    out: list[Alert] = []
    if backup["age_hours"] > BACKUP_MAX_HOURS:
        out.append(
            Alert(
                WARNING,
                "BACKUP_OLD",
                f"The newest backup is {backup['age_hours']:.0f} hours old "
                f"(limit {BACKUP_MAX_HOURS}).",
            )
        )
    if not backup["verified"]:
        out.append(
            Alert(WARNING, "BACKUP_UNVERIFIED", "The newest backup has not passed a restore test.")
        )
    if backup["encrypted"] is False:
        out.append(
            Alert(
                WARNING,
                "BACKUP_NOT_ENCRYPTED",
                "The newest backup is not encrypted (set HQ_BACKUP_PASSPHRASE in .env).",
            )
        )
    return out


def _count(conn: Connection, sql: str) -> int:
    return int(conn.execute(text(sql)).scalar_one() or 0)


def collect_state(
    conn: Connection, backup_dir: Path = DEFAULT_DIR, today: date | None = None
) -> SystemState:
    latest: date | None = conn.execute(
        text("SELECT max(price_date) FROM hq.daily_price")
    ).scalar_one()
    return SystemState(
        today=today or datetime.now(UTC).date(),
        kill_switch_engaged=kill_switch_engaged(conn),
        orders_in_doubt=_count(
            conn, "SELECT count(*) FROM hq.order_current_state WHERE state = 'SUBMISSION_UNKNOWN'"
        ),
        orders_failed=_count(
            conn, "SELECT count(*) FROM hq.order_current_state WHERE state = 'FAILED'"
        ),
        reconciliation_mismatches_7d=_count(
            conn,
            "SELECT count(*) FROM hq.audit_event WHERE action = 'reconciliation.mismatch' "
            "AND occurred_at > now() - interval '7 days'",
        ),
        approvals_waiting=_count(
            conn,
            "SELECT count(*) FROM hq.proposal_current_status WHERE status = 'AWAITING_APPROVAL'",
        ),
        latest_price_date=latest,
        backup=latest_status(backup_dir),
    )


def main() -> int:
    from halal_quant.core.settings import DbRole, get_settings
    from halal_quant.db.engine import make_engine

    with make_engine(get_settings(), DbRole.READONLY).connect() as conn:
        alerts = evaluate(collect_state(conn))
    if not alerts:
        print("No alerts.")
        return 0
    for alert in alerts:
        print(f"[{alert.severity}] {alert.code}: {alert.message}")
    return 2 if any(a.severity == CRITICAL for a in alerts) else 1


if __name__ == "__main__":
    sys.exit(main())
