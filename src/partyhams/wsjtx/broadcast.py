"""Outbound UDP log broadcast — mirror each logged QSO to another program.

Completely separate from the peer-to-peer sync in :mod:`partyhams.net`. That is a
two-way CRDT between PartyHams stations sharing one contest log; this is a
one-way, fire-and-forget announcement in *WSJT-X's* UDP dialect, so unrelated
software on the LAN — GridTracker, Log4OM, N3FJP, a homebrew display — can pick
up contacts as they are logged. Nothing is expected back, nothing is retried, and
a failure never touches the log.

The datagram is a WSJT-X ``LoggedADIF`` (type 12) message whose payload is a
miniature ADIF document: a header naming the format and this program, ``<EOH>``,
then the single QSO record. Receivers parse it exactly like an ADIF file.

Disabled by default; the address and port are set in **Logs → UDP Broadcast…**
and stored in the app-wide settings, not in any one log.
"""

from __future__ import annotations

import socket

from partyhams.export.adif import ADIF_VERSION
from partyhams.wsjtx.protocol import encode_logged_adif

#: What we call ourselves in the datagram's id field. Receivers key their
#: per-application settings off this, and many only recognize "WSJT-X" — so we
#: use it rather than our own name, which is what the reference implementation
#: this was modeled on does too.
BROADCAST_ID = "WSJT-X"

#: Program name declared in the payload header. Together with the shared
#: :data:`~partyhams.export.adif.ADIF_VERSION` imported above, this makes a
#: broadcast datagram and an exported ADIF file describe themselves
#: identically, so a receiver sees one consistent program and format version
#: whether a QSO arrives over UDP or by file import.
PROGRAM_ID = "PartyHams"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 2237  # the port WSJT-X itself broadcasts on, so receivers just work

#: Guard against a runaway record; a UDP datagram this large would fragment badly.
MAX_DATAGRAM = 60_000


def _tag(name: str, value: str) -> str:
    """One ADIF field: ``<name:len>value``, length in bytes."""
    return f"<{name}:{len(value.encode('utf-8'))}>{value}"


def build_payload(adif_record: str) -> str:
    """Wrap one ADIF record in the miniature ADIF document we transmit.

    Begins with a newline (as the reference implementation does — receivers skip
    leading whitespace, and it keeps the header readable in a packet dump), then
    the ADIF version, the program id, ``<EOH>``, the record, and a final newline.
    ``adif_record`` is a complete record ending in ``<EOR>`` — exactly what
    :func:`partyhams.export.adif.qso_to_adif` produces for the backup file.
    """
    return (
        "\n"
        + _tag("adif_ver", ADIF_VERSION)
        + "\n"
        + _tag("programid", PROGRAM_ID)
        + "\n"
        + "<EOH>\n"
        + adif_record
        + "\n"
    )


def build_datagram(adif_record: str) -> bytes:
    """The complete UDP payload for one logged QSO."""
    return encode_logged_adif(BROADCAST_ID, build_payload(adif_record))


class LogBroadcaster:
    """Sends one datagram per logged QSO to a configured address and port.

    Holds no socket between sends: a QSO every few seconds at most doesn't
    justify keeping one open, and a short-lived socket can't go stale when the
    machine changes network. ``send`` never raises — a broadcast that fails is
    reported by its return value and is never allowed to disturb logging.
    """

    def __init__(
        self,
        enabled: bool = False,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
    ) -> None:
        self.enabled = bool(enabled)
        self.host = host or DEFAULT_HOST
        self.port = int(port)
        #: Short reason the last send failed, for the status bar (None = fine).
        self.last_error: str | None = None

    def configure(self, enabled: bool, host: str, port: int) -> None:
        self.enabled = bool(enabled)
        self.host = host.strip() or DEFAULT_HOST
        self.port = int(port)
        self.last_error = None

    def send(self, adif_record: str) -> bool:
        """Broadcast one ADIF record. Returns True if the datagram went out.

        A no-op (False, no error) when disabled. Any socket failure sets
        :attr:`last_error` and returns False.
        """
        if not self.enabled:
            return False
        try:
            data = build_datagram(adif_record)
        except Exception as exc:  # noqa: BLE001 - a bad record must not break logging
            self.last_error = f"UDP broadcast: could not build datagram ({exc})"
            return False
        if len(data) > MAX_DATAGRAM:
            self.last_error = f"UDP broadcast: record too large ({len(data)} bytes)"
            return False
        return self._sendto(data)

    def _sendto(self, data: bytes) -> bool:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # Allow a broadcast address (255.255.255.255 or a subnet broadcast);
            # harmless for an ordinary unicast target.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(1.0)
            sock.sendto(data, (self.host, self.port))
        except OSError as exc:
            self.last_error = f"UDP broadcast to {self.host}:{self.port} failed ({exc})"
            return False
        except Exception:  # noqa: BLE001 - never let a broadcast disturb logging
            self.last_error = f"UDP broadcast to {self.host}:{self.port} failed"
            return False
        else:
            self.last_error = None
            return True
        finally:
            if sock is not None:
                sock.close()
