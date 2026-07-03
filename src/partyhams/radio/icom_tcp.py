"""Icom CI-V over TCP backend — IC-7300 MK2 LAN control.

The IC-7300 MK2 exposes its CI-V control interface over a plain TCP socket
(no Icom proprietary UDP handshake, no username/password).  Raw CI-V frames
are sent and received exactly as over a serial connection, just wrapped in a
TCP stream instead.

The CI-V command set (read/set freq/mode, PTT, CW…) is shared with the serial
and UDP-native backends via :class:`~partyhams.radio.civ_commands.CivRadio`.
This module only provides the TCP transport.

Frame-filtering note: on a direct TCP link the radio may use ``to_addr=0x00``
(broadcast) in responses, so we cannot filter by ``to_addr != CONTROLLER_ADDR``
as the serial backend does.  Instead we skip only our own echoed command —
identified by ``from_addr == CONTROLLER_ADDR`` — and accept any frame whose
command byte matches what we asked for.

Debug log: written to ~/.partyhams/radio-debug.log on every launch.
"""

from __future__ import annotations

import asyncio
import logging

from partyhams.core.models import Mode
from partyhams.radio.base import RadioState, RadioUnsupported
from partyhams.radio.civ_commands import CivRadio
from partyhams.radio.civ_protocol import (
    ACK_OK,
    CIV_ADDR_IC7300MK2,
    CMD_READ_FREQ,
    CMD_READ_MODE,
    CONTROLLER_ADDR,
    END,
    bcd_to_freq,
    build_frame,
    civ_to_mode,
    parse_frames,
)
from partyhams.radio.registry import register

_log = logging.getLogger(__name__)

DEFAULT_PORT = 50001
_CONNECT_TIMEOUT = 10.0
_TRANSACT_TIMEOUT = 1.5


def verify_connectivity(host: str, port: int = DEFAULT_PORT, timeout: float = 2.0) -> bool:
    """True if a TCP connection to the IC-7300 MK2 control port succeeds."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@register
class IcomTCP(CivRadio):
    """CI-V over TCP transport for the IC-7300 MK2 LAN interface."""

    backend_id = "icom-tcp"
    backend_name = "Icom CI-V (TCP)"

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        civ_address: int = CIV_ADDR_IC7300MK2,
    ) -> None:
        self.host = host
        self.port = port
        self.civ_address = civ_address
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    def description(self) -> str:
        return f"Icom IC-7300 MK2 @ {self.host}:{self.port} (TCP)"

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=_CONNECT_TIMEOUT,
        )

    async def disconnect(self) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    # read_state — IC-7300 MK2 LAN quirk
    # ------------------------------------------------------------------ #
    async def read_state(self) -> RadioState:
        """Read freq and mode, accounting for the IC-7300 MK2 LAN protocol quirk.

        The IC-7300 MK2 TCP CI-V server responds to a CMD_READ_MODE (0x04) query
        with a frame whose command byte is 0x01 (the legacy "send mode data"
        command) rather than echoing back 0x04.  We therefore look for 0x01 in
        the mode response instead of 0x04.
        """
        freq_payload = await self._transact(bytes([CMD_READ_FREQ]), response_cmd=CMD_READ_FREQ)
        # IC-7300 MK2 over LAN answers the 0x04 mode query with cmd byte 0x01.
        mode_payload = await self._transact(bytes([CMD_READ_MODE]), response_cmd=0x01)

        freq = bcd_to_freq(freq_payload[1:6]) if freq_payload and len(freq_payload) >= 6 else 0
        mode = civ_to_mode(mode_payload[1]) if mode_payload and len(mode_payload) >= 2 else Mode.USB
        return RadioState(freq_hz=freq, mode=mode)

    # ------------------------------------------------------------------ #
    # transport
    # ------------------------------------------------------------------ #
    async def _transact(
        self,
        payload: bytes,
        response_cmd: int | None = None,
        ack: bool = False,
        expect: bool = True,
    ) -> bytes | None:
        if self._writer is None or self._reader is None:
            raise RadioUnsupported("Icom TCP backend is not connected")
        async with self._lock:
            frame_out = build_frame(self.civ_address, CONTROLLER_ADDR, payload)
            _log.debug("_transact tx: %s", frame_out.hex())
            self._writer.write(frame_out)
            await self._writer.drain()
            if not expect:
                return None
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _TRANSACT_TIMEOUT
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    _log.debug("_transact: timeout waiting for cmd=0x%02X", response_cmd or 0)
                    return None
                try:
                    raw = await asyncio.wait_for(
                        self._reader.readuntil(bytes([END])),
                        timeout=remaining,
                    )
                except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                    _log.debug("_transact: read error waiting for cmd=0x%02X", response_cmd or 0)
                    return None
                _log.debug("_transact raw rx: %s", raw.hex())
                frames, _ = parse_frames(raw)
                for frame in frames:
                    if not frame.payload:
                        continue
                    _log.debug(
                        "_transact frame: to=0x%02X from=0x%02X payload=%s",
                        frame.to_addr,
                        frame.from_addr,
                        frame.payload.hex(),
                    )
                    if frame.from_addr == CONTROLLER_ADDR:
                        _log.debug("_transact: skipping echo (from_addr=0xE0)")
                        continue
                    if ack and frame.payload[0] in (ACK_OK, 0xFA):
                        return frame.payload
                    if not ack and response_cmd is not None and frame.payload[0] == response_cmd:
                        _log.debug("_transact: matched cmd=0x%02X", response_cmd)
                        return frame.payload
                    _log.debug(
                        "_transact: discarding non-matching frame (want 0x%02X got 0x%02X)",
                        response_cmd or 0,
                        frame.payload[0],
                    )
