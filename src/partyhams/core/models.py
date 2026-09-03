"""Core data models: bands, modes, and the QSO record.

Deliberately framework-free (plain dataclasses + enums) so the contest engine,
networking, and persistence layers can all share them without pulling in Qt.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime


class ModeGroup(enum.Enum):
    """The coarse mode category used for scoring and dupe rules.

    Field Day (and many contests) treat CW, Phone, and Digital as distinct slots:
    20m CW and 20m Phone to the same station are *both* valid QSOs.
    """

    CW = "CW"
    PHONE = "PHONE"
    DIGITAL = "DIGITAL"


class Mode(enum.Enum):
    """A concrete on-air mode. Maps to a :class:`ModeGroup` via ``mode_group_for``."""

    CW = "CW"
    USB = "USB"
    LSB = "LSB"
    FM = "FM"
    AM = "AM"
    RTTY = "RTTY"
    PSK31 = "PSK31"
    FT8 = "FT8"
    FT4 = "FT4"


_MODE_GROUPS: dict[Mode, ModeGroup] = {
    Mode.CW: ModeGroup.CW,
    Mode.USB: ModeGroup.PHONE,
    Mode.LSB: ModeGroup.PHONE,
    Mode.FM: ModeGroup.PHONE,
    Mode.AM: ModeGroup.PHONE,
    Mode.RTTY: ModeGroup.DIGITAL,
    Mode.PSK31: ModeGroup.DIGITAL,
    Mode.FT8: ModeGroup.DIGITAL,
    Mode.FT4: ModeGroup.DIGITAL,
}


def mode_group_for(mode: Mode) -> ModeGroup:
    """Return the :class:`ModeGroup` for a concrete :class:`Mode`."""
    return _MODE_GROUPS[mode]


@dataclass(frozen=True)
class Band:
    """An amateur band, identified by its conventional label (e.g. ``"20m"``)."""

    label: str
    low_hz: int
    high_hz: int

    def contains(self, freq_hz: int) -> bool:
        return self.low_hz <= freq_hz <= self.high_hz


# Canonical HF/VHF/UHF bands. WARC bands (30/17/12 m) are included here because
# they are valid amateur bands; individual contests (e.g. Field Day) restrict the
# allowed subset in their own definition.
BANDS: tuple[Band, ...] = (
    Band("160m", 1_800_000, 2_000_000),
    Band("80m", 3_500_000, 4_000_000),
    Band("60m", 5_330_000, 5_405_000),
    Band("40m", 7_000_000, 7_300_000),
    Band("30m", 10_100_000, 10_150_000),
    Band("20m", 14_000_000, 14_350_000),
    Band("17m", 18_068_000, 18_168_000),
    Band("15m", 21_000_000, 21_450_000),
    Band("12m", 24_890_000, 24_990_000),
    Band("10m", 28_000_000, 29_700_000),
    Band("6m", 50_000_000, 54_000_000),
    Band("2m", 144_000_000, 148_000_000),
    Band("1.25m", 222_000_000, 225_000_000),
    Band("70cm", 420_000_000, 450_000_000),
)

_BANDS_BY_LABEL: dict[str, Band] = {b.label: b for b in BANDS}


def band_for_freq(freq_hz: int) -> Band | None:
    """Return the :class:`Band` containing ``freq_hz``, or ``None`` if out of band."""
    for b in BANDS:
        if b.contains(freq_hz):
            return b
    return None


def band_by_label(label: str) -> Band | None:
    return _BANDS_BY_LABEL.get(label)


def utcnow() -> datetime:
    """Timezone-aware current UTC time (QSOs are always logged in UTC)."""
    return datetime.now(UTC)


@dataclass
class QSO:
    """A single logged contact.

    Identity/merge fields (``uuid``, ``station_id``, ``lamport``, ``deleted``)
    support peer-to-peer sync; contest fields (``rst_*``, ``exchange_*``,
    ``serial_sent``) are filled per the active contest's exchange schema.
    """

    # --- identity & merge metadata ---
    uuid: str
    station_id: str  # the logging machine's per-session sync id (merge tiebreak)
    operator: str  # ADIF OPERATOR: the individual at the key (e.g. N0AW)
    station_callsign: str = ""  # ADIF STATION_CALLSIGN: the station call (e.g. W0CPH)
    lamport: int = 0
    deleted: bool = False

    # --- the contact ---
    call: str = ""  # the worked station's callsign
    timestamp: datetime = field(default_factory=utcnow)
    freq_hz: int = 0
    mode: Mode = Mode.CW

    # --- exchange ---
    rst_sent: str = ""
    rst_rcvd: str = ""
    serial_sent: int | None = None
    # Free-form per-contest exchange data, e.g. {"class": "3A", "section": "OR"}.
    exchange_rcvd: dict[str, str] = field(default_factory=dict)
    exchange_sent: dict[str, str] = field(default_factory=dict)
    #: Free-text operator note about the contact (ADIF ``COMMENT``). Entered in
    #: General logs; blank everywhere else, and on every QSO logged before the
    #: field existed.
    comment: str = ""

    @property
    def band(self) -> Band | None:
        return band_for_freq(self.freq_hz)

    @property
    def band_label(self) -> str:
        b = self.band
        return b.label if b else "?"

    @property
    def mode_group(self) -> ModeGroup:
        return mode_group_for(self.mode)
