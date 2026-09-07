"""E.164 phone-number validation for NahaLabs regional deployments."""

import re

# South Africa: +27 followed by 9 national digits.
_ZA_RE = re.compile(r"\+27[1-8]\d{8}")
# Lesotho: +266 followed by 8 national digits.
_LS_RE = re.compile(r"\+266[5-8]\d{7}")


def validate_phone_number(number: str) -> str:
    """Validate a supported South African or Lesotho E.164 destination."""
    if not isinstance(number, str):
        raise ValueError("phone_number must be a string")
    number = number.strip()
    if _ZA_RE.fullmatch(number) or _LS_RE.fullmatch(number):
        return number
    raise ValueError(
        f"Invalid phone number {number!r}: expected South Africa (+27) or Lesotho (+266) E.164 format"
    )


def validate_caller_id(caller_id: str | None) -> str | None:
    """Validate a configured caller ID; ownership must be verified by the carrier."""
    if caller_id is None:
        return None
    return validate_phone_number(caller_id)
