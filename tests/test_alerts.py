"""Alert rules on made-up snapshots."""

from dataclasses import replace
from datetime import date

from halal_quant.ops.alerts import Alert, SystemState, evaluate

GOOD_BACKUP = {"newest": "b.dump", "age_hours": 5.0, "verified": True, "encrypted": True}
CALM = SystemState(
    today=date(2026, 10, 7),  # a Wednesday
    kill_switch_engaged=False,
    orders_in_doubt=0,
    orders_failed=0,
    reconciliation_mismatches_7d=0,
    approvals_waiting=0,
    latest_price_date=date(2026, 10, 6),
    backup=GOOD_BACKUP,
)


def codes(state: SystemState) -> list[str]:
    return [a.code for a in evaluate(state)]


def test_a_calm_system_has_no_alerts() -> None:
    assert evaluate(CALM) == []


def test_the_critical_alerts_come_first_and_say_what_to_do() -> None:
    state = replace(
        CALM,
        kill_switch_engaged=True,
        orders_in_doubt=1,
        reconciliation_mismatches_7d=2,
        orders_failed=3,
        approvals_waiting=1,
    )
    alerts = evaluate(state)
    assert [a.severity for a in alerts] == ["CRITICAL"] * 3 + ["WARNING"] * 2
    assert {a.code for a in alerts[:3]} == {
        "KILL_SWITCH",
        "ORDER_IN_DOUBT",
        "RECONCILIATION_MISMATCH",
    }
    assert isinstance(alerts[0], Alert) and "STOPPED" in next(
        a.message for a in alerts if a.code == "KILL_SWITCH"
    )


def test_market_data_age_is_counted_in_trading_days() -> None:
    assert codes(replace(CALM, latest_price_date=date(2026, 10, 5))) == ["STALE_DATA"]  # 2 days
    # Monday's data on a Tuesday is one trading day old: fine
    assert codes(replace(CALM, today=date(2026, 10, 6), latest_price_date=date(2026, 10, 5))) == []
    assert codes(replace(CALM, latest_price_date=None)) == ["NO_PRICES"]


def test_backup_rules() -> None:
    assert codes(replace(CALM, backup={"newest": None})) == ["NO_BACKUP"]
    old = {**GOOD_BACKUP, "age_hours": 40.0}
    assert codes(replace(CALM, backup=old)) == ["BACKUP_OLD"]
    unverified = {**GOOD_BACKUP, "verified": False}
    assert codes(replace(CALM, backup=unverified)) == ["BACKUP_UNVERIFIED"]
    plain = {**GOOD_BACKUP, "encrypted": False}
    assert codes(replace(CALM, backup=plain)) == ["BACKUP_NOT_ENCRYPTED"]
    everything = {"newest": "b", "age_hours": 99.0, "verified": False, "encrypted": False}
    assert len(evaluate(replace(CALM, backup=everything))) == 3
