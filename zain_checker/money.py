import math
import re
from decimal import Decimal


ARABIC_INDIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
EASTERN_ARABIC_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
WESTERN_DIGITS = "0123456789"


def to_western_digits(value: object) -> str:
    translation = str.maketrans(
        ARABIC_INDIC_DIGITS + EASTERN_ARABIC_DIGITS,
        WESTERN_DIGITS + WESTERN_DIGITS,
    )
    return str(value).translate(translation)


def money_to_halalas(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("A boolean is not a valid amount.")

    if isinstance(value, int):
        return value * 100

    if isinstance(value, (float, Decimal)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"Invalid numeric amount: {value}")
        if isinstance(value, float):
            value = round(value, 2)
        return _decimal_to_halalas(Decimal(str(value)))

    if value is None or not str(value).strip():
        raise ValueError("Amount is empty.")

    normalized = to_western_digits(value)
    normalized = re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", normalized)
    normalized = normalized.replace("٬", ",").replace("٫", ".").strip()

    matches = re.findall(r"[+-]?\d[\d\s,.'’]*", normalized)
    if len(matches) != 1:
        raise ValueError(f"Could not read one amount from: {value}")

    token = re.sub(r"[\s'’]", "", matches[0])
    sign = -1 if token.startswith("-") else 1
    token = token.lstrip("+-")

    decimal_index = _find_decimal_separator_index(token)
    if decimal_index == -1:
        whole_part = token.replace(",", "").replace(".", "")
        fractional_part = ""
    else:
        whole_part = token[:decimal_index].replace(",", "").replace(".", "")
        fractional_part = token[decimal_index + 1 :].replace(",", "").replace(".", "")

    if not whole_part.isdigit() or (fractional_part and not fractional_part.isdigit()):
        raise ValueError(f"Invalid amount: {value}")

    if len(fractional_part) > 2 and any(digit != "0" for digit in fractional_part[2:]):
        raise ValueError(f"Amount has more than two decimal places: {value}")

    halalas = int(whole_part) * 100 + int((fractional_part + "00")[:2])
    return sign * halalas


def halalas_to_number(value: int) -> float:
    return value / 100


def _decimal_to_halalas(value: Decimal) -> int:
    halalas = value * Decimal(100)
    integral = halalas.to_integral_value()
    if halalas != integral:
        raise ValueError(f"Amount has more than two decimal places: {value}")
    return int(integral)


def _find_decimal_separator_index(token: str) -> int:
    last_dot = token.rfind(".")
    last_comma = token.rfind(",")

    if last_dot != -1 and last_comma != -1:
        return max(last_dot, last_comma)

    separator_index = max(last_dot, last_comma)
    if separator_index == -1:
        return -1

    separator = token[separator_index]
    occurrences = token.count(separator)
    digits_after_separator = len(token) - separator_index - 1

    if digits_after_separator in (1, 2):
        return separator_index

    if occurrences > 1 and 0 < digits_after_separator <= 2:
        return separator_index

    return -1

