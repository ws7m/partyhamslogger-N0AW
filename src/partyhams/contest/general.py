"""General logging — everyday contacts, no contest rules.

The activity you pick when you just want a logbook: ragchews, nets, a casual
evening on 40 m. Modeled as a :class:`~partyhams.contest.base.ContestDefinition`
like everything else so the whole logging core, sync, and export path works
unchanged, but with every contest-specific rule turned off:

* **Setup:** nothing beyond the station call — no class, section, or park, so the
  lower half of the new-log screen is empty.
* **Exchange:** none. A signal report and a free-text comment are entered per QSO
  on the entry row, and neither is a contest exchange field.
* **Dupes:** there are none. Working the same friend twice on the same band in
  one evening is a normal thing to log, not a mistake to warn about, so the dupe
  key is unique per QSO and the DUPE indicator never lights.
* **Scoring:** a plain QSO count — there is nothing to score.
* **Bands:** every amateur band, WARC included (no contest to exclude them).
"""

from __future__ import annotations

from collections.abc import Iterable

from partyhams.contest.base import (
    ConfigField,
    ContestConfig,
    ContestDefinition,
    ExchangeField,
    ScoreSummary,
)
from partyhams.contest.registry import register
from partyhams.core.models import BANDS, QSO, ModeGroup

# Cabrillo mode token by mode group. General logging has no Cabrillo submission,
# but the export layer still offers the format, so the mapping is here.
_CABRILLO_MODE: dict[ModeGroup, str] = {
    ModeGroup.CW: "CW",
    ModeGroup.PHONE: "PH",
    ModeGroup.DIGITAL: "DG",
}


@register
class General(ContestDefinition):
    id = "general"
    name = "General"
    cabrillo_name = ""  # not a contest — nothing to submit
    exchanges_rst = True  # a signal report is the whole exchange
    mult_label = "Mults"

    def config_fields(self) -> list[ConfigField]:
        # Nothing beyond the station call — the new-log screen's lower half is empty.
        return []

    def exchange_fields(self) -> list[ExchangeField]:
        # No contest exchange. RST and the comment are handled by the entry row,
        # not as exchange fields, so they aren't parsed positionally or validated.
        return []

    def allowed_bands(self) -> set[str]:
        # Everything, WARC included — there's no contest excluding them.
        return {band.label for band in BANDS}

    def dupe_key(self, qso: QSO) -> tuple:
        # Unique per QSO, so nothing ever collides and nothing is ever a dupe.
        # (A general log records every contact, including repeats with a friend.)
        return ("uuid", qso.uuid)

    def qso_points(self, qso: QSO) -> int:
        return 1

    def score(self, qsos: Iterable[QSO], config: ContestConfig) -> ScoreSummary:
        # No multipliers and no points — the "score" is just how many you worked.
        base = super().score(qsos, config)
        base.total = base.qso_count
        return base

    def cabrillo_qso_line(self, qso: QSO, config: ContestConfig) -> str:
        freq_khz = qso.freq_hz // 1000
        mode = _CABRILLO_MODE[qso.mode_group]
        date = qso.timestamp.strftime("%Y-%m-%d")
        time = qso.timestamp.strftime("%H%M")
        rst_sent = qso.rst_sent or "599"
        rst_rcvd = qso.rst_rcvd or "599"
        return (
            f"QSO: {freq_khz:>7} {mode:>2} {date} {time} "
            f"{config.my_call.upper():<10} {rst_sent:>3} "
            f"{qso.call.upper():<10} {rst_rcvd:>3}"
        ).rstrip()
