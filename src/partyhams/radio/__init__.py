"""Radio CAT abstraction (decision #3): Hamlib base + native Flex / Icom CI-V.

A single :class:`~partyhams.radio.base.Radio` interface with pluggable backends.
Backends self-register; the UI lists :func:`~partyhams.radio.registry.available`
and builds the chosen one. Validated against real hardware (IDEAS.md §3.1):
FlexRadio 6500 → ``flex``, Icom IC-705/IC-7610 → ``icom-civ``, Yaesu FT-891 →
``hamlib``.
"""

from partyhams.radio import flex as _flex  # noqa: F401

# Import backends so they register.
from partyhams.radio import hamlib as _hamlib  # noqa: F401
from partyhams.radio import icom_civ as _icom  # noqa: F401
from partyhams.radio import icom_net as _icom_net  # noqa: F401
from partyhams.radio import icom_tcp as _icom_tcp  # noqa: F401
from partyhams.radio.base import (
    Capability,
    Radio,
    RadioState,
    RadioUnsupported,
)
from partyhams.radio.registry import available, get_backend, register

__all__ = [
    "Capability",
    "Radio",
    "RadioState",
    "RadioUnsupported",
    "available",
    "get_backend",
    "register",
]
