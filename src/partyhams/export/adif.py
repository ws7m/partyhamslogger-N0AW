"""ADIF export — the universal log interchange format (LoTW/QRZ/eQSL, etc.).

Emits ADIF 3.x. Each field is ``<NAME:len>value``; records end with ``<EOR>``
and the header ends with ``<EOH>``. Contest exchange fields are flattened into
the standard ``STX_STRING`` / ``SRX_STRING`` fields.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime

from partyhams import __version__
from partyhams.contest.base import ContestConfig, ContestDefinition
from partyhams.core.models import QSO, Mode


def timestamped_adif_name(call: str, contest_id: str, when: datetime) -> str:
    """A dated ADIF filename, e.g. ``W7ABC-arrl-field-day-20260607-143012.adi``.

    The timestamp encodes ``YYYYMMDD`` and ``HHMMSS`` so periodic auto-exports
    sort chronologically and never overwrite one another.
    """
    safe = re.sub(r"[^A-Za-z0-9]+", "_", call).strip("_") or "log"
    return f"{safe}-{contest_id}-{when.strftime('%Y%m%d-%H%M%S')}.adi"


def _safe_filename_part(value: str, *, keep_dash: bool = False) -> str:
    """Collapse anything not filename-safe into ``_`` (optionally keeping ``-``)."""
    allowed = "A-Za-z0-9-" if keep_dash else "A-Za-z0-9"
    return re.sub(rf"[^{allowed}]+", "_", value).strip("_")


def park_adif_name(call: str, park: str, when: datetime, *, at_sign: bool = True) -> str:
    """Default name for a manual ADIF export: ``CALL@PARK_YYYYMMDD.adif``.

    The ``@`` joins the callsign and the (POTA) park reference; on a filesystem
    that can't store ``@`` the caller passes ``at_sign=False`` and we substitute
    ``_`` instead (``CALL_PARK_YYYYMMDD.adif``). When no park is set the park
    segment is dropped entirely (``CALL_YYYYMMDD.adif``). Park references keep
    their dash (``US-1234``); other unsafe characters become ``_``.
    """
    call_part = _safe_filename_part(call) or "log"
    park_part = _safe_filename_part(park, keep_dash=True)
    date = when.strftime("%Y%m%d")
    if not park_part:
        return f"{call_part}_{date}.adif"
    sep = "@" if at_sign else "_"
    return f"{call_part}{sep}{park_part}_{date}.adif"


# our Mode -> ADIF (MODE, SUBMODE). ADIF treats some modes as submodes of a
# parent: FT4 is a submode of MFSK, PSK31 a submode of PSK. FT8 is its own
# top-level MODE (no submode). A None submode means "emit MODE only".
_ADIF_MODE: dict[Mode, tuple[str, str | None]] = {
    Mode.CW: ("CW", None),
    Mode.USB: ("SSB", None),
    Mode.LSB: ("SSB", None),
    Mode.FM: ("FM", None),
    Mode.AM: ("AM", None),
    Mode.RTTY: ("RTTY", None),
    Mode.PSK31: ("PSK", "PSK31"),
    Mode.FT8: ("FT8", None),
    Mode.FT4: ("MFSK", "FT4"),
}

# Reverse map for import: (MODE, SUBMODE) -> our Mode. The submode wins when it
# names a specific mode (e.g. MFSK/FT4 -> FT4); otherwise the MODE decides.
_MODE_FROM_ADIF: dict[tuple[str, str | None], Mode] = {
    (mode, sub): m for m, (mode, sub) in _ADIF_MODE.items()
}


def adif_to_mode(mode: str, submode: str = "") -> Mode | None:
    """Map an ADIF ``MODE``/``SUBMODE`` pair back to our :class:`Mode`.

    Reverses :data:`_ADIF_MODE`, so e.g. ``("MFSK", "FT4") -> Mode.FT4`` and
    ``("FT8", "") -> Mode.FT8``. Tries the submode first (it's more specific),
    then falls back to the bare MODE. Returns None if neither is recognized.
    """
    mode = mode.strip().upper()
    submode = submode.strip().upper()
    if submode and (mode, submode) in _MODE_FROM_ADIF:
        return _MODE_FROM_ADIF[(mode, submode)]
    return _MODE_FROM_ADIF.get((mode, None))


def _field(name: str, value: str) -> str:
    value = value or ""
    return f"<{name}:{len(value)}>{value}"


def _adif_band(label: str) -> str:
    # "20m" -> "20M"; passthrough for anything unexpected.
    return label.upper()


def _exchange_string(exchange: dict[str, str]) -> str:
    # "grid" is a separate concept (the FT8 grid square -> GRIDSQUARE), never part
    # of the contest exchange string.
    return " ".join(str(v) for k, v in exchange.items() if v and k != "grid")


def qso_to_adif(qso: QSO, config: ContestConfig, contest: ContestDefinition | None = None) -> str:
    parts = [
        _field("CALL", qso.call.upper()),
        _field("QSO_DATE", qso.timestamp.strftime("%Y%m%d")),
        _field("TIME_ON", qso.timestamp.strftime("%H%M%S")),
        _field("BAND", _adif_band(qso.band_label)),
        _field("FREQ", f"{qso.freq_hz / 1_000_000:.6f}"),
    ]
    adif_mode, adif_submode = _ADIF_MODE.get(qso.mode, (qso.mode.value, None))
    parts.append(_field("MODE", adif_mode))
    if adif_submode:
        parts.append(_field("SUBMODE", adif_submode))
    # Per-QSO station call (who the QSO was logged under), falling back to the
    # log's own call for records logged before the field existed.
    station_call = (qso.station_callsign or config.my_call).upper()
    parts += [
        _field("OPERATOR", qso.operator.upper()),
        _field("STATION_CALLSIGN", station_call),
    ]
    if qso.rst_sent:
        parts.append(_field("RST_SENT", qso.rst_sent))
    if qso.rst_rcvd:
        parts.append(_field("RST_RCVD", qso.rst_rcvd))
    # Activator's own park(s) for a POTA activation — comma-separated for an n-fer
    # (overlapping parks). Stored in config.extra["park"]; emitted as MY_POTA_REF.
    my_park = str(config.extra.get("park", "") or "").strip()
    if my_park:
        parts.append(_field("MY_POTA_REF", my_park))
    # Operating site (POTA): DX entity -> MY_COUNTRY, location code -> MY_STATE
    # (the part after the dash of a single "XX-YY" code, e.g. US-WA -> WA).
    my_entity = str(config.extra.get("entity", "") or "").strip()
    if my_entity:
        parts.append(_field("MY_COUNTRY", my_entity))
    my_loc = str(config.extra.get("location", "") or "").strip()
    if my_loc and "," not in my_loc and "-" in my_loc:
        parts.append(_field("MY_STATE", my_loc.split("-", 1)[1]))
    sent = _exchange_string(config.sent_exchange)
    # The received exchange is just the contest's exchange fields (class/section),
    # in order — not the grid, which is exported separately as GRIDSQUARE.
    if contest is not None:
        rcvd = " ".join(
            qso.exchange_rcvd.get(f.name, "") for f in contest.exchange_fields()
        ).strip()
    else:
        rcvd = _exchange_string(qso.exchange_rcvd)
    if sent:
        parts.append(_field("STX_STRING", sent))
    if rcvd:
        parts.append(_field("SRX_STRING", rcvd))
    grid = str(qso.exchange_rcvd.get("grid", "") or "").strip()
    if grid:
        parts.append(_field("GRIDSQUARE", grid))
    if qso.comment.strip():
        parts.append(_field("COMMENT", qso.comment.strip()))
    return "".join(parts) + "<EOR>"


def write_adif(
    qsos: Iterable[QSO],
    config: ContestConfig,
    contest: ContestDefinition | None = None,
) -> str:
    """Render a full ADIF document (header + records) as a string."""
    header_lines = [
        "PartyHams Logger ADIF export",
        _field("ADIF_VER", "3.1.4"),
        _field("PROGRAMID", "PartyHams"),
        _field("PROGRAMVERSION", __version__),
    ]
    if contest is not None and contest.cabrillo_name:
        header_lines.append(_field("CONTEST_ID", contest.cabrillo_name))
    header = "\n".join(header_lines) + "<EOH>\n"

    body = "\n".join(qso_to_adif(q, config, contest) for q in qsos if not q.deleted)
    return header + body + ("\n" if body else "")
