"""Pure parser/encoder for the WSJT-X UDP message protocol.

WSJT-X broadcasts QDataStream-encoded datagrams describing what it is decoding,
transmitting, and logging (see ``NetworkMessage.hpp`` in the WSJT-X source). This
module is deliberately Qt-free and side-effect-free: :func:`parse_message`
decodes a datagram into one of the dataclasses below (or ``None`` for anything
malformed / unsupported), and :func:`encode_highlight_callsign` builds the
``HighlightCallsignInProgram`` reply so we can color stations whose section we
still need.

Wire format (all integers big-endian / network byte order):

* every datagram starts with magic ``0xadbccbda`` (uint32), then a schema
  number (uint32; WSJT-X uses 2 or 3), then a message-type id (uint32);
* a ``utf8`` string is a int32 byte-length followed by that many UTF-8 bytes,
  with ``0xffffffff`` (-1) meaning *null* (decoded as ``""``);
* a ``bool`` is a single byte;
* ``QTime`` is ``uint32`` milliseconds since midnight;
* ``QDateTime`` is ``qint64`` Julian day + ``uint32`` ms-since-midnight + a
  1-byte timespec (0=local, 1=UTC, 2=offset-from-UTC [+ int32 offset secs],
  3=time-zone).

The decoder is intentionally tolerant: it parses the fields documented for each
message and ignores any trailing bytes, so a newer WSJT-X that appends fields
still round-trips the parts we care about.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

MAGIC = 0xADBCCBDA
NULL_LEN = 0xFFFFFFFF  # int32 -1: a null QString/QByteArray

# Message type ids we understand (WSJT-X "out" messages + our reply).
TYPE_HEARTBEAT = 0
TYPE_STATUS = 1
TYPE_DECODE = 2
TYPE_CLEAR = 3
TYPE_REPLY = 4
TYPE_QSO_LOGGED = 5
TYPE_LOGGED_ADIF = 12  # an ADIF record for a logged QSO (we *send* this one)
TYPE_HIGHLIGHT_CALLSIGN = 13

# Julian Day Number for the Unix epoch (1970-01-01). QDateTime stores a Julian
# day; we convert to a Python aware datetime via this anchor.
_UNIX_EPOCH_JD = 2440588


@dataclass(frozen=True)
class Heartbeat:
    """Type 0 — periodic liveness ping identifying the WSJT-X instance."""

    id: str
    max_schema: int = 0
    version: str = ""
    revision: str = ""


@dataclass(frozen=True)
class Status:
    """Type 1 — the operator/transmit state of WSJT-X.

    ``tx_period_odd`` is ``True`` if WSJT-X is set to transmit in the odd Tx
    period (``None`` if the field wasn't present in this schema).
    """

    id: str
    dial_freq: int = 0  # Hz
    mode: str = ""
    dx_call: str = ""
    report: str = ""
    tx_mode: str = ""
    tx_enabled: bool = False
    transmitting: bool = False
    decoding: bool = False
    de_call: str = ""
    de_grid: str = ""
    dx_grid: str = ""
    tx_period_odd: bool | None = None


@dataclass(frozen=True)
class Decode:
    """Type 2 — a single decoded message in the band activity window."""

    id: str
    is_new: bool = True
    time_ms: int = 0  # ms since midnight UTC
    snr: int = 0
    delta_time: float = 0.0  # seconds
    delta_freq: int = 0  # Hz (audio offset)
    mode: str = ""
    message: str = ""


@dataclass(frozen=True)
class Clear:
    """Type 3 — WSJT-X cleared its decode window(s)."""

    id: str


@dataclass(frozen=True)
class QSOLogged:
    """Type 5 — a QSO the operator just logged in WSJT-X."""

    id: str
    date_time_off: datetime | None = None
    dx_call: str = ""
    dx_grid: str = ""
    tx_frequency: int = 0  # Hz
    mode: str = ""
    report_sent: str = ""
    report_recv: str = ""
    tx_power: str = ""
    comments: str = ""
    name: str = ""
    date_time_on: datetime | None = None
    operator_call: str = ""
    my_call: str = ""
    my_grid: str = ""
    exchange_sent: str = ""
    exchange_recv: str = ""
    extra: dict[str, str] = field(default_factory=dict)


WsjtxMessage = Heartbeat | Status | Decode | Clear | QSOLogged


# --------------------------------------------------------------------------- #
# low-level reader
# --------------------------------------------------------------------------- #
class _Reader:
    """A cursor over a datagram that raises on any under-read."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def _take(self, n: int) -> bytes:
        if n < 0 or self._pos + n > len(self._data):
            raise _Short()
        chunk = self._data[self._pos : self._pos + n]
        self._pos += n
        return chunk

    def u8(self) -> int:
        return self._take(1)[0]

    def u32(self) -> int:
        return int(struct.unpack(">I", self._take(4))[0])

    def i32(self) -> int:
        return int(struct.unpack(">i", self._take(4))[0])

    def u64(self) -> int:
        return int(struct.unpack(">Q", self._take(8))[0])

    def i64(self) -> int:
        return int(struct.unpack(">q", self._take(8))[0])

    def f64(self) -> float:
        return float(struct.unpack(">d", self._take(8))[0])

    def boolean(self) -> bool:
        return self.u8() != 0

    def utf8(self) -> str:
        length = self.u32()
        if length == NULL_LEN:
            return ""
        return self._take(length).decode("utf-8", errors="replace")

    def qtime(self) -> int:
        """A ``QTime`` as raw milliseconds since midnight."""
        return self.u32()

    def qdatetime(self) -> datetime | None:
        """Decode a ``QDateTime`` to an aware UTC datetime (None if invalid)."""
        jd = self.i64()
        ms = self.u32()
        spec = self.u8()
        offset = 0
        if spec == 2:  # OffsetFromUTC carries an explicit int32 second offset
            offset = self.i32()
        if jd <= 0:
            return None
        base = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(days=jd - _UNIX_EPOCH_JD)
        moment = base + timedelta(milliseconds=ms)
        # spec 0 (local) is ambiguous without a zone; treat its wall-clock as UTC.
        if spec == 2:
            moment -= timedelta(seconds=offset)
        return moment

    def at_end(self) -> bool:
        return self._pos >= len(self._data)


class _Short(Exception):
    """Raised internally when a datagram is truncated mid-field."""


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def parse_message(data: bytes) -> WsjtxMessage | None:
    """Decode a WSJT-X datagram, or return ``None`` if it isn't one we handle.

    Robust by contract: bad magic, a truncated buffer, a decode error, or an
    unknown message type all yield ``None`` rather than raising.
    """
    try:
        r = _Reader(data)
        if r.u32() != MAGIC:
            return None
        r.u32()  # schema — accepted as-is (WSJT-X uses 2 or 3)
        msg_type = r.u32()
        parser = _PARSERS.get(msg_type)
        if parser is None:
            return None
        return parser(r)
    except (_Short, struct.error, UnicodeDecodeError, ValueError, OverflowError):
        return None


def _parse_heartbeat(r: _Reader) -> Heartbeat:
    ident = r.utf8()
    max_schema = r.u32() if not r.at_end() else 0
    version = r.utf8() if not r.at_end() else ""
    revision = r.utf8() if not r.at_end() else ""
    return Heartbeat(id=ident, max_schema=max_schema, version=version, revision=revision)


def _parse_status(r: _Reader) -> Status:
    ident = r.utf8()
    dial_freq = r.u64()
    mode = r.utf8()
    dx_call = r.utf8()
    report = r.utf8()
    tx_mode = r.utf8()
    tx_enabled = r.boolean()
    transmitting = r.boolean()
    decoding = r.boolean()
    r.u32()  # Rx DF (audio offset, Hz) — unused
    r.u32()  # Tx DF — unused
    de_call = r.utf8()
    de_grid = r.utf8()
    dx_grid = r.utf8()
    tx_period_odd: bool | None = r.boolean() if not r.at_end() else None
    return Status(
        id=ident,
        dial_freq=dial_freq,
        mode=mode,
        dx_call=dx_call,
        report=report,
        tx_mode=tx_mode,
        tx_enabled=tx_enabled,
        transmitting=transmitting,
        decoding=decoding,
        de_call=de_call,
        de_grid=de_grid,
        dx_grid=dx_grid,
        tx_period_odd=tx_period_odd,
    )


def _parse_decode(r: _Reader) -> Decode:
    ident = r.utf8()
    is_new = r.boolean()
    time_ms = r.qtime()
    snr = r.i32()
    delta_time = r.f64()
    delta_freq = r.u32()
    mode = r.utf8()
    message = r.utf8()
    return Decode(
        id=ident,
        is_new=is_new,
        time_ms=time_ms,
        snr=snr,
        delta_time=delta_time,
        delta_freq=delta_freq,
        mode=mode,
        message=message,
    )


def _parse_clear(r: _Reader) -> Clear:
    return Clear(id=r.utf8())


def _parse_qso_logged(r: _Reader) -> QSOLogged:
    ident = r.utf8()
    date_time_off = r.qdatetime()
    dx_call = r.utf8()
    dx_grid = r.utf8()
    tx_frequency = r.u64()
    mode = r.utf8()
    report_sent = r.utf8()
    report_recv = r.utf8()
    tx_power = r.utf8()
    comments = r.utf8()
    name = r.utf8()
    # Fields below were added across WSJT-X schemas; tolerate their absence.
    date_time_on = r.qdatetime() if not r.at_end() else None
    operator_call = r.utf8() if not r.at_end() else ""
    my_call = r.utf8() if not r.at_end() else ""
    my_grid = r.utf8() if not r.at_end() else ""
    exchange_sent = r.utf8() if not r.at_end() else ""
    exchange_recv = r.utf8() if not r.at_end() else ""
    return QSOLogged(
        id=ident,
        date_time_off=date_time_off,
        dx_call=dx_call,
        dx_grid=dx_grid,
        tx_frequency=tx_frequency,
        mode=mode,
        report_sent=report_sent,
        report_recv=report_recv,
        tx_power=tx_power,
        comments=comments,
        name=name,
        date_time_on=date_time_on,
        operator_call=operator_call,
        my_call=my_call,
        my_grid=my_grid,
        exchange_sent=exchange_sent,
        exchange_recv=exchange_recv,
    )


_PARSERS: dict[int, Callable[[_Reader], WsjtxMessage]] = {
    TYPE_HEARTBEAT: _parse_heartbeat,
    TYPE_STATUS: _parse_status,
    TYPE_DECODE: _parse_decode,
    TYPE_CLEAR: _parse_clear,
    TYPE_QSO_LOGGED: _parse_qso_logged,
}


# --------------------------------------------------------------------------- #
# encoding (writer + HighlightCallsign reply)
# --------------------------------------------------------------------------- #
class _Writer:
    """Builds a QDataStream-compatible big-endian datagram."""

    def __init__(self) -> None:
        self._parts: list[bytes] = []

    def u32(self, value: int) -> _Writer:
        self._parts.append(struct.pack(">I", value & 0xFFFFFFFF))
        return self

    def u8(self, value: int) -> _Writer:
        self._parts.append(struct.pack(">B", value & 0xFF))
        return self

    def i32(self, value: int) -> _Writer:
        self._parts.append(struct.pack(">i", value))
        return self

    def f64(self, value: float) -> _Writer:
        self._parts.append(struct.pack(">d", value))
        return self

    def boolean(self, value: bool) -> _Writer:
        self._parts.append(b"\x01" if value else b"\x00")
        return self

    def utf8(self, value: str | None) -> _Writer:
        if value is None:
            self._parts.append(struct.pack(">I", NULL_LEN))
            return self
        raw = value.encode("utf-8")
        self._parts.append(struct.pack(">I", len(raw)))
        self._parts.append(raw)
        return self

    def qcolor(self, color: tuple[int, int, int, int] | None) -> _Writer:
        """Encode a ``QColor`` as WSJT-X does (spec byte + four 16-bit channels).

        ``color`` is ``(r, g, b, a)`` with 0..255 channels (a=255 opaque), or
        ``None`` for an *invalid* QColor (spec 0) meaning "reset to default".
        Valid colors use RGB spec (1) with each 8-bit channel scaled to 16 bits.
        """
        if color is None:
            # Invalid color: spec=Invalid(0), then zeroed alpha/r/g/b/pad.
            self._parts.append(struct.pack(">B", 0))
            self._parts.append(struct.pack(">HHHHH", 0, 0, 0, 0, 0))
            return self
        r, g, b, a = color
        self._parts.append(struct.pack(">B", 1))  # QColor::Rgb
        # 8-bit -> 16-bit channel (0xFF -> 0xFFFF); WSJT-X stores QColor as 16-bit.
        scaled = tuple((c & 0xFF) * 257 for c in (a, r, g, b))
        self._parts.append(struct.pack(">HHHHH", *scaled, 0))
        return self

    def getvalue(self) -> bytes:
        return b"".join(self._parts)


def encode_highlight_callsign(
    id: str,
    callsign: str,
    *,
    background: tuple[int, int, int, int] | None = None,
    foreground: tuple[int, int, int, int] | None = None,
    highlight_last: bool = False,
    schema: int = 2,
) -> bytes:
    """Build a ``HighlightCallsignInProgram`` (type 13) reply datagram.

    Sending this to WSJT-X's UDP port colors ``callsign`` in its decode windows.
    ``background``/``foreground`` are ``(r, g, b, a)`` tuples (or ``None`` for an
    invalid QColor = reset). ``highlight_last`` colors only the most recent
    period when set. Returns the bytes to ``sendto`` WSJT-X.
    """
    w = _Writer()
    w.u32(MAGIC).u32(schema).u32(TYPE_HIGHLIGHT_CALLSIGN)
    w.utf8(id)
    w.utf8(callsign)
    w.qcolor(background)
    w.qcolor(foreground)
    w.boolean(highlight_last)
    return w.getvalue()


def encode_logged_adif(id: str, adif: str, *, schema: int = 2) -> bytes:
    """Build a ``LoggedADIF`` (type 12) datagram carrying one ADIF record.

    This is the message WSJT-X emits when it logs a QSO, and the one third-party
    loggers and mappers (GridTracker, Log4OM, N3FJP, …) listen for. We *send* it
    rather than parse it, so the whole log can be mirrored to another program on
    the LAN — see :mod:`partyhams.wsjtx.broadcast`.

    Layout, matching the reference implementation byte for byte::

        0..3    ad bc cb da   magic
        4..7    00 00 00 02   schema (2)
        8..11   00 00 00 0c   message type (12 = LoggedADIF)
        12..15  00 00 00 06   length of the id string
        16..21  "WSJT-X"      id (identifies us to the receiver)
        22..25  <u32>         length of the ADIF payload, in bytes
        26..    <payload>     the ADIF text

    The payload length at 22..25 is **computed** from ``adif``. A fixed value
    there only holds for one exact record; anything longer is truncated by a
    conforming reader and anything shorter leaves it waiting on bytes that never
    arrive.
    """
    w = _Writer()
    w.u32(MAGIC).u32(schema).u32(TYPE_LOGGED_ADIF)
    w.utf8(id)
    w.utf8(adif)
    return w.getvalue()


def encode_reply(
    id: str,
    decode: Decode,
    *,
    low_confidence: bool = False,
    modifiers: int = 0,
    schema: int = 2,
) -> bytes:
    """Build a ``Reply`` (type 4) datagram that asks WSJT-X to answer ``decode``.

    It echoes the decode's time/snr/offset/message back — the UDP equivalent of
    double-clicking that line in WSJT-X, so WSJT-X starts calling that station.
    ``modifiers`` mirrors keyboard modifiers (0 = none). Unverified against live
    WSJT-X hardware. Returns the bytes to ``sendto`` WSJT-X.
    """
    w = _Writer()
    w.u32(MAGIC).u32(schema).u32(TYPE_REPLY)
    w.utf8(id)
    w.u32(decode.time_ms)
    w.i32(decode.snr)
    w.f64(decode.delta_time)
    w.u32(decode.delta_freq)
    w.utf8(decode.mode)
    w.utf8(decode.message)
    w.boolean(low_confidence)
    w.u8(modifiers)
    return w.getvalue()
