"""Internal shared validators.

Kept private so the public API stays minimal. These are imported from ``guard.py``
and ``mapping.py`` to enforce the same input rules on both the wrapper-level and
builder-level commit-amount paths.
"""

from __future__ import annotations

from runcycles_ap2._constants import MAX_USD_MICROS
from runcycles_ap2.exceptions import AP2MandateError


def validate_micros(amount: object, *, field: str) -> int:
    """Validate that ``amount`` is a non-negative ``int`` within the int64 ceiling.

    Uses ``type(amount) is int`` rather than ``isinstance(amount, int)`` deliberately:
    ``bool`` is a subclass of ``int``, and we do not want ``set_actual_micros(True)``
    to slip through and ship ``true`` over the wire. ``float`` is also rejected —
    pydantic would later raise during receipt construction (after the commit POST
    has already gone out), and that post-hoc failure mode is much harder to
    reconcile than a clean client-side reject.
    """
    if type(amount) is not int:
        raise AP2MandateError(f"{field} must be an int, got {type(amount).__name__}: {amount!r}")
    if amount < 0:
        raise AP2MandateError(f"{field} must be non-negative, got {amount}")
    if amount > MAX_USD_MICROS:
        raise AP2MandateError(f"{field} {amount} exceeds int64 USD micro-cent max ({MAX_USD_MICROS})")
    return amount
