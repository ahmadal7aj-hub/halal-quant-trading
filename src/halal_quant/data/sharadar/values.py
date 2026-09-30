"""Reading Sharadar's text values: blanks and "N/A" mean "no value"."""

from datetime import date
from decimal import Decimal, InvalidOperation


def blank_to_none(value: str | None) -> str | None:
    text = (value or "").strip()
    return None if text.upper() in ("", "N/A") else text


def parse_date(value: str | None) -> date | None:
    """A date in ISO form, or None if blank. Raises ValueError for anything else."""
    text = blank_to_none(value)
    return date.fromisoformat(text) if text else None


def parse_decimal(value: str | None) -> Decimal | None:
    """A finite number, or None if blank. Raises ValueError for anything else."""
    text = blank_to_none(value)
    if text is None:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"not a number: {text!r}") from None
    if not number.is_finite():
        raise ValueError(f"not a finite number: {text!r}")
    return number
