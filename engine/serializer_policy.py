"""Deterministic policy identity for v3 serializer requests.

The serializer renders only a pre-certified immutable unit plan, so sampling
does not add useful variation.  The effective temperature is intentionally a
runtime value: operations may override it for an explicitly re-qualified run,
but both the prompt context and qualification receipt bind that value.
"""

from __future__ import annotations

import math
import os

SERIALIZER_TEMPERATURE_ENV = "HRAMATKA_GEN_TEMPERATURE"
DEFAULT_SERIALIZER_TEMPERATURE = 0.0
_MAX_SERIALIZER_TEMPERATURE = 2.0


def serializer_temperature() -> float:
    """Return the validated effective serializer temperature.

    Invalid settings fail closed rather than silently changing a qualified
    serializer's behavior.  Provider APIs accept the closed ``[0, 2]`` range.
    """
    raw = os.environ.get(SERIALIZER_TEMPERATURE_ENV)
    if raw is None:
        return DEFAULT_SERIALIZER_TEMPERATURE
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{SERIALIZER_TEMPERATURE_ENV} must be a number.") from error
    if not math.isfinite(value) or not 0.0 <= value <= _MAX_SERIALIZER_TEMPERATURE:
        raise ValueError(
            f"{SERIALIZER_TEMPERATURE_ENV} must be between 0 and {_MAX_SERIALIZER_TEMPERATURE}."
        )
    return value
