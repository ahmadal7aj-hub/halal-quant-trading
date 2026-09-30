from decimal import Decimal
from pathlib import Path

import pytest

from halal_quant.core.config import (
    ConfigError,
    ShariaConfig,
    UniverseConfig,
    load_config,
)

ROOT = Path(__file__).parents[1]
SHARIA = ROOT / "config/sharia/aaoifi_v1.yaml"
UNIVERSE = ROOT / "config/universe/default.yaml"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "cfg.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def with_change(source: Path, old: str, new: str, tmp_path: Path) -> Path:
    text = source.read_text(encoding="utf-8")
    assert old in text
    return write(tmp_path, text.replace(old, new))


def test_repo_sharia_config_matches_approved_s1() -> None:
    loaded = load_config(SHARIA, ShariaConfig)
    cfg = loaded.config
    assert loaded.ref.version == "AAOIFI-v1" and cfg.status == "approved"
    assert cfg.max_debt_to_market_value == Decimal("0.30")
    assert cfg.max_cash_and_investments_to_market_value == Decimal("0.30")
    assert cfg.max_prohibited_income_to_revenue == Decimal("0.05")
    assert cfg.max_report_age_months == 16
    categories = {b.category for b in cfg.excluded_businesses}
    assert categories == {
        "conventional_finance",
        "alcohol",
        "tobacco",
        "gambling",
        "pork",
        "adult_entertainment",
    }


def test_repo_universe_config_loads_and_is_still_proposed() -> None:
    loaded = load_config(UNIVERSE, UniverseConfig)
    assert loaded.config.status == "proposed"  # owner decision OI-11 pending
    assert loaded.config.sharia_methodology == "AAOIFI-v1"


def test_hash_ignores_comments_and_line_endings(tmp_path: Path) -> None:
    original = load_config(SHARIA, ShariaConfig).ref.sha256
    text = SHARIA.read_text(encoding="utf-8")
    crlf = tmp_path / "crlf.yaml"
    crlf.write_bytes(("# extra comment\n" + text).replace("\n", "\r\n").encode())
    assert load_config(crlf, ShariaConfig).ref.sha256 == original


def test_hash_changes_when_a_value_changes(tmp_path: Path) -> None:
    changed = with_change(SHARIA, '"0.30"', '"0.33"', tmp_path)
    assert (
        load_config(changed, ShariaConfig).ref.sha256
        != load_config(SHARIA, ShariaConfig).ref.sha256
    )


# --- Fail closed -------------------------------------------------------------------------------


def test_missing_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="could not be read"):
        load_config(tmp_path / "missing.yaml", ShariaConfig)


def test_broken_yaml_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(write(tmp_path, "a: [unclosed"), ShariaConfig)


def test_non_mapping_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="mapping"):
        load_config(write(tmp_path, "- just\n- a list\n"), ShariaConfig)


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        ('max_debt_to_market_value: "0.30"', 'max_debt_to_market_value: "1.5"', "max_debt"),
        ('max_debt_to_market_value: "0.30"', 'max_debt_to_market_value: "0"', "max_debt"),
        ('max_debt_to_market_value: "0.30"', "max_debt_to_market_value: lots", "max_debt"),
        ('max_debt_to_market_value: "0.30"\n', "", "max_debt_to_market_value"),
        ("status: approved", "status: maybe", "status"),
        ("config_type: sharia_methodology", "config_type: universe", "config_type"),
        ("max_report_age_months: 16", "max_report_age_months: 16\nsurprise: 1", "surprise"),
        ("market_value_method: screening_date", "market_value_method: average", "market_value"),
    ],
)
def test_invalid_sharia_values_fail_closed_and_name_the_field(
    tmp_path: Path, old: str, new: str, field: str
) -> None:
    with pytest.raises(ConfigError, match=field):
        load_config(with_change(SHARIA, old, new, tmp_path), ShariaConfig)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('min_price_usd: "5.00"', 'min_price_usd: "-1"'),
        ("liquidity_window_trading_days: 20", "liquidity_window_trading_days: 0"),
        ("version: universe-v1", 'version: ""'),
    ],
)
def test_invalid_universe_values_fail_closed(tmp_path: Path, old: str, new: str) -> None:
    with pytest.raises(ConfigError, match="invalid"):
        load_config(with_change(UNIVERSE, old, new, tmp_path), UniverseConfig)
