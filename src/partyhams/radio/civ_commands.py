"""Shared CI-V command layer for Icom backends.

Both the serial (:class:`~partyhams.radio.icom_civ.IcomCIV`) and network
(:class:`~partyhams.radio.icom_net.IcomNet`) Icom backends speak the *same*
CI-V command set — they differ only in transport. This base captures the
command/response logic (read freq+mode, set freq/mode, PTT, CW) on top of an
abstract :meth:`_transact`, so the two transports can't drift apart.

A subclass must set ``civ_address`` and implement :meth:`_transact` (plus the
lifecycle ``connect``/``disconnect``).
"""

from __future__ import annotations

from partyhams.core.models import Mode
from partyhams.radio.base import Capability, Radio, RadioState, RadioUnsupported
from partyhams.radio.civ_protocol import (
    ACK_NG,
    ACK_OK,
    CIV_ADDR_IC7610,
    CMD_LEVEL,
    CMD_PTT,
    CMD_READ_FREQ,
    CMD_READ_MODE,
    CMD_SEND_CW,
    CMD_SET_FREQ,
    CMD_SET_MODE,
    SUB_KEYER_SPEED,
    bcd_to_freq,
    civ_to_mode,
    freq_to_bcd,
    mode_to_civ,
    wpm_to_level_bytes,
)


class CivRadio(Radio):
    """CI-V command behaviour shared by the serial and network Icom backends."""

    #: The radio's CI-V address (IC-705 ``0xA4``, IC-7610 ``0x98``).
    civ_address: int = 0

    @property
    def capabilities(self) -> Capability:
        caps = (
            Capability.FREQUENCY
            | Capability.MODE
            | Capability.VFO_AB
            | Capability.SPLIT
            | Capability.PTT
            | Capability.S_METER
            | Capability.RIT_XIT
            | Capability.SPECTRUM
            | Capability.SEND_CW
            | Capability.KEYER_SPEED
        )
        if self.civ_address == CIV_ADDR_IC7610:
            caps |= Capability.SUB_RECEIVER  # dual receive
        return caps

    # ------------------------------------------------------------------ #
    # Radio interface — built on _transact()
    # ------------------------------------------------------------------ #
    async def read_state(self) -> RadioState:
        freq_payload = await self._transact(bytes([CMD_READ_FREQ]), response_cmd=CMD_READ_FREQ)
        mode_payload = await self._transact(bytes([CMD_READ_MODE]), response_cmd=CMD_READ_MODE)
        freq = bcd_to_freq(freq_payload[1:6]) if freq_payload and len(freq_payload) >= 6 else 0
        mode = civ_to_mode(mode_payload[1]) if mode_payload and len(mode_payload) >= 2 else Mode.USB
        return RadioState(freq_hz=freq, mode=mode)

    async def set_frequency(self, freq_hz: int) -> None:
        await self._transact(bytes([CMD_SET_FREQ]) + freq_to_bcd(freq_hz), ack=True)

    async def set_mode(self, mode: Mode) -> None:
        civ = mode_to_civ(mode)
        if civ is None:
            raise RadioUnsupported(f"Icom CI-V has no mapping for {mode}")
        await self._transact(bytes([CMD_SET_MODE, civ]), ack=True)

    async def set_ptt(self, on: bool) -> None:
        await self._transact(bytes([CMD_PTT, 0x00, 0x01 if on else 0x00]), ack=True)

    async def set_wpm(self, wpm: int) -> None:
        # Fire-and-forget — no ACK wait.  Holding the lock for 1.5 s while waiting
        # for an ACK would block the polling loop and delay freq/mode updates.
        await self._transact(
            bytes([CMD_LEVEL, SUB_KEYER_SPEED]) + wpm_to_level_bytes(wpm), expect=False
        )

    async def send_cw(self, text: str, wpm: int | None = None) -> None:
        if wpm is not None:
            # Must wait for ACK before sending the CW text — the IC-7300 MK2 TCP
            # requires strict sequential cmd/response and ignores the CW frame if
            # two commands arrive back-to-back without a read between them.
            await self._transact(
                bytes([CMD_LEVEL, SUB_KEYER_SPEED]) + wpm_to_level_bytes(wpm), ack=True
            )
        # CI-V "send CW message" (0x17) + ASCII; the radio keys it at its set speed.
        await self._transact(bytes([CMD_SEND_CW]) + text.encode("ascii", "ignore"), expect=False)

    async def stop_tx(self) -> None:
        # 0x17 0xFF cancels CW; then drop PTT — best effort.
        try:
            await self._transact(bytes([CMD_SEND_CW, 0xFF]), expect=False)
        except OSError:
            pass
        try:
            await self._transact(bytes([CMD_PTT, 0x00, 0x00]), ack=True)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # transport — implemented per backend
    # ------------------------------------------------------------------ #
    async def _transact(
        self,
        payload: bytes,
        response_cmd: int | None = None,
        ack: bool = False,
        expect: bool = True,
    ) -> bytes | None:
        """Send a CI-V command payload and return the matching response payload.

        ``response_cmd`` selects a reply by command byte; ``ack`` instead accepts a
        bare ACK (``0xFB``)/NG (``0xFA``); ``expect=False`` sends fire-and-forget.
        Returns ``None`` on timeout or when nothing is expected.
        """
        raise NotImplementedError


def is_ack(payload: bytes) -> bool:
    """True if ``payload`` is a CI-V ACK (OK) or NG response byte."""
    return bool(payload) and payload[0] in (ACK_OK, ACK_NG)
