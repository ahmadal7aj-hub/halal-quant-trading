import pytest
from pydantic import ValidationError

from halal_quant.data.security_master import SecurityInfo, SecurityMasterError, normalize_ticker


def test_tickers_are_normalized_to_upper_case_without_spaces() -> None:
    assert normalize_ticker("  brk.b ") == "BRK.B"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_ticker_is_refused(blank: str) -> None:
    with pytest.raises(SecurityMasterError, match="empty"):
        normalize_ticker(blank)


def test_security_needs_a_source_id_and_name() -> None:
    with pytest.raises(ValidationError):
        SecurityInfo(source="sharadar", source_id=" ", company_name="Example Corp")
    with pytest.raises(ValidationError):
        SecurityInfo(source="sharadar", source_id="1", company_name="")


def test_unknown_security_fields_are_refused() -> None:
    with pytest.raises(ValidationError):
        SecurityInfo(source="sharadar", source_id="1", company_name="X", ticker="X")  # type: ignore[call-arg]
