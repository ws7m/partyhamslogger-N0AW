"""The peer-to-peer sync engine.

Ties a :class:`~partyhams.net.transport.Transport` to a
:class:`~partyhams.net.sync.LogMerge` and a Lamport clock. Responsibilities:

* **Join** — announce ourselves (:class:`Hello`) so peers send us what we're missing.
* **Log** — stamp a local QSO with a fresh lamport, merge it, broadcast it.
* **Receive** — merge remote QSOs; answer catch-up requests; reconcile on divergence.
* **Heartbeat** — periodically advertise ``(count, log_hash)`` so a peer that has
  drifted (dropped packet, late join after a partition) notices and re-syncs.

The receive handling is split so tests can drive it deterministically:
``join()`` + manual :meth:`pump_once` (no background tasks), while ``start()``
runs the real background receive + heartbeat loops.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from partyhams.core.clock import LamportClock, new_uuid
from partyhams.core.models import QSO, Mode, utcnow
from partyhams.hunters.models import HunterRecord
from partyhams.net.clocksync import clock_offset_seconds, is_clock_off
from partyhams.net.protocol import (
    Chat,
    ChatSyncResponse,
    FullLogRequest,
    Heartbeat,
    Hello,
    HunterMessage,
    HunterSyncResponse,
    Message,
    QsoMessage,
    StationStatus,
    SyncRequest,
    SyncResponse,
)
from partyhams.net.sync import LogMerge
from partyhams.net.transport import HEARTBEAT_INTERVAL_S, Transport

STATUS_INTERVAL_S = 3.0  # how often we re-broadcast our operating state


class SyncEngine:
    def __init__(
        self,
        transport: Transport,
        *,
        operator: str,
        call: str,
        log: LogMerge | None = None,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_S,
        status_interval: float = STATUS_INTERVAL_S,
        on_qso: Callable[[QSO], None] | None = None,
        on_status: Callable[[], None] | None = None,
        on_chat: Callable[[Chat, str], None] | None = None,
        on_hunter: Callable[[HunterRecord], None] | None = None,
        on_clock_off: Callable[[str, float], None] | None = None,
    ) -> None:
        self.transport = transport
        self.station_id = transport.station_id
        self.operator = operator
        self.call = call
        self.log = log if log is not None else LogMerge()
        self.clock = LamportClock()
        self.heartbeat_interval = heartbeat_interval
        self.status_interval = status_interval
        # Fired whenever a QSO is applied locally or from a peer (state changed).
        # The app layer uses this to persist + refresh the UI.
        self.on_qso = on_qso
        self.on_status = on_status  # a peer's presence/state changed
        self.on_chat = on_chat  # (Chat, sender_station_id) for an incoming message
        # All chat we've seen, keyed by uuid (durable + synced like QSOs).
        self.chats: dict[str, Chat] = {}
        # POTA hunter roster records we've seen, keyed by (call, station_id).
        # Only populated for a POTA log — see LogSession.
        self.on_hunter = on_hunter  # a hunter record was applied (persist it)
        self.hunters: dict[tuple[str, str], HunterRecord] = {}
        # Fired (debounced) when a peer's apparent clock offset crosses the
        # off-threshold: on_clock_off(operator_label, offset_seconds).
        self.on_clock_off = on_clock_off
        # station_id -> operator label, for the legacy "who's on" count.
        self.peers: dict[str, str] = {}
        # station_id -> {operator, call, freq_hz, mode, last_heard} for peers.
        self.stations: dict[str, dict] = {}
        # Our own current operating state, broadcast periodically.
        # power_w/swr default 0 (unknown); ft_tx_even -1 (unknown).
        self._status = {
            "operator": operator,
            "call": call,
            "freq_hz": 0,
            "mode": Mode.CW.value,
            "power_w": 0.0,
            "swr": 0.0,
            "ft_tx_even": -1,
        }
        self._tasks: list[asyncio.Task] = []
        self._running = False

    def _notify(self, qso: QSO) -> None:
        if self.on_qso is not None:
            self.on_qso(qso)

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def join(self) -> None:
        """Open the transport and announce ourselves (no background loops)."""
        await self.transport.start()
        await self._announce()

    async def start(self) -> None:
        """Join and run the background receive + heartbeat loops."""
        await self.join()
        self._running = True
        self._tasks = [
            asyncio.create_task(self._recv_loop()),
            asyncio.create_task(self._heartbeat_loop()),
            asyncio.create_task(self._status_loop()),
        ]
        # On (re)join, pull a complete copy of everyone's log so a station that
        # quit and came back ends up with all QSOs locally.
        await self.request_full_log()

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []
        await self.transport.stop()

    async def _announce(self) -> None:
        await self.transport.send(
            Hello(operator=self.operator, call=self.call, high_water=self.log.high_water())
        )

    # ------------------------------------------------------------------ #
    # local actions
    # ------------------------------------------------------------------ #
    def record(
        self,
        *,
        call: str,
        freq_hz: int,
        mode: Mode,
        exchange_rcvd: dict[str, str] | None = None,
        rst_sent: str = "599",
        rst_rcvd: str = "599",
        timestamp: datetime | None = None,
        uuid: str | None = None,
    ) -> QSO:
        """Create + store a new QSO locally (synchronous, no network).

        Returns immediately so the UI updates instantly; broadcast the result with
        :meth:`broadcast` as a separate, best-effort step. ``timestamp`` overrides
        the default "now" (e.g. WSJT-X reports the QSO's actual completion time).
        ``uuid`` overrides the random id — pass a content-derived one to make a
        re-delivered source (e.g. a duplicated WSJT-X UDP packet) idempotent.
        """
        when = {} if timestamp is None else {"timestamp": timestamp}
        qso = QSO(
            uuid=uuid or new_uuid(),
            station_id=self.station_id,
            operator=self.operator,
            station_callsign=self.call,
            lamport=self.clock.tick(),
            call=call.upper(),
            freq_hz=freq_hz,
            mode=mode,
            rst_sent=rst_sent,
            rst_rcvd=rst_rcvd,
            exchange_rcvd=exchange_rcvd or {},
            **when,
        )
        self.log.apply(qso)
        self._notify(qso)
        return qso

    def set_operator(self, operator: str) -> None:
        """Change who is at the key. New QSOs are stamped with this operator and
        presence broadcasts reflect it."""
        self.operator = operator
        self._status["operator"] = operator

    def amend(self, qso: QSO) -> QSO:
        """Re-merge an existing QSO (an edit or a tombstone) under a fresh lamport.

        Keeps the original ``uuid`` so it replaces the prior version; the new
        lamport is strictly greater than anything seen, so it wins last-writer-wins
        on every peer. Returns the amended record (broadcast it like a new QSO).
        """
        amended = replace(qso, lamport=self.clock.tick())
        self.log.apply(amended)
        self._notify(amended)
        return amended

    async def broadcast(self, qso: QSO) -> None:
        """Send a previously-recorded QSO to peers."""
        await self.transport.send(QsoMessage(qso=qso))

    async def request_full_log(self) -> None:
        """Ask every peer to send its entire log (anti-entropy from scratch)."""
        await self.transport.send(FullLogRequest())

    async def log_qso(self, **kwargs) -> QSO:
        """Record and broadcast a QSO (convenience for tests/headless callers)."""
        qso = self.record(**kwargs)
        await self.broadcast(qso)
        return qso

    async def send_heartbeat(self) -> None:
        await self.transport.send(
            Heartbeat(
                count=len(self.log),
                log_hash=self.log.log_hash(),
                lamport_max=self.clock.value,
                sender_utc=utcnow().isoformat(),
            )
        )

    # ------------------------------------------------------------------ #
    # presence + chat
    # ------------------------------------------------------------------ #
    def update_status(
        self,
        *,
        freq_hz: int,
        mode: str,
        operator: str | None = None,
        power_w: float | None = None,
        swr: float | None = None,
        ft_tx_even: int | None = None,
    ) -> None:
        """Update our current operating state (broadcast by the presence loop).

        ``power_w``/``swr``/``ft_tx_even`` are optional; ``None`` leaves the
        previously-broadcast value untouched so a caller that only knows the
        freq/mode doesn't wipe a power reading it set earlier.
        """
        if operator:
            self._status["operator"] = operator
            self.operator = operator
        self._status["freq_hz"] = freq_hz
        self._status["mode"] = mode
        if power_w is not None:
            self._status["power_w"] = power_w
        if swr is not None:
            self._status["swr"] = swr
        if ft_tx_even is not None:
            self._status["ft_tx_even"] = ft_tx_even

    async def send_status(self) -> None:
        await self.transport.send(
            StationStatus(
                operator=self._status["operator"],
                call=self.call,
                freq_hz=int(self._status["freq_hz"]),
                mode=str(self._status["mode"]),
                power_w=float(self._status["power_w"]),
                swr=float(self._status["swr"]),
                ft_tx_even=int(self._status["ft_tx_even"]),
            )
        )

    async def send_chat(self, to_op: str, text: str) -> Chat:
        msg = Chat(
            from_op=self.operator,
            to_op=to_op,
            text=text,
            ts=utcnow().isoformat(),
            uuid=new_uuid(),
            station_id=self.station_id,
        )
        self.chats[msg.uuid] = msg
        await self.transport.send(msg)
        return msg

    def apply_chat(self, chat: Chat) -> bool:
        """Record a chat for sync/dedup. Returns True if it was new (by uuid)."""
        if not chat.uuid or chat.uuid in self.chats:
            return False
        self.chats[chat.uuid] = chat
        return True

    def apply_hunter(self, record: HunterRecord) -> bool:
        """Merge a hunter record into the in-memory roster (True if it changed).

        Ordering is by ``(lamport, worked_count)`` within a ``(call, station_id)``
        key. Only the owning station writes its own records, so this is enough to
        make a re-delivered or reordered datagram a no-op — see
        :mod:`partyhams.hunters.models` for why the roster is keyed per station.
        """
        key = (record.call, record.station_id)
        existing = self.hunters.get(key)
        if existing is not None and (record.lamport, record.worked_count) <= (
            existing.lamport,
            existing.worked_count,
        ):
            return False
        self.hunters[key] = record
        return True

    def drop_hunter(self, call: str) -> None:
        """Forget every in-memory roster record for ``call``.

        Used after a local rename so a peer requesting a full sync is not served
        the superseded callsign. Purely local: the protocol has no roster
        tombstone, so a peer that still holds the old record keeps it until it
        renames too.
        """
        for key in [k for k in self.hunters if k[0] == call]:
            del self.hunters[key]

    async def broadcast_hunter(self, record: HunterRecord) -> None:
        """Send one already-applied hunter record to peers."""
        await self.transport.send(HunterMessage(hunter=record))

    async def _status_loop(self) -> None:
        while self._running:
            await self.send_status()
            await asyncio.sleep(self.status_interval)

    # ------------------------------------------------------------------ #
    # receive
    # ------------------------------------------------------------------ #
    async def pump_once(self) -> bool:
        """Drain and handle all currently-queued messages. Returns True if any."""
        handled = False
        while not self.transport.inbox.empty():
            sender, message = self.transport.inbox.get_nowait()
            await self._handle(sender, message)
            handled = True
        return handled

    async def _recv_loop(self) -> None:
        while self._running:
            sender, message = await self.transport.inbox.get()
            await self._handle(sender, message)

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.heartbeat_interval)
            await self.send_heartbeat()

    async def _handle(self, sender: str, message: Message) -> None:
        self.peers.setdefault(sender, sender)

        if isinstance(message, QsoMessage):
            self.clock.update(message.qso.lamport)
            if self.log.apply(message.qso):
                self._notify(message.qso)

        elif isinstance(message, Hello):
            self.peers[sender] = message.call or message.operator or sender
            self.stations.setdefault(
                sender,
                {"operator": message.operator, "call": message.call, "freq_hz": 0, "mode": ""},
            )["last_heard"] = utcnow()
            if self.on_status is not None:
                self.on_status()
            # The newcomer told us what it has; send back anything it's missing,
            # and let it see our current state right away.
            await self._send_diff(message.high_water)
            await self.send_status()

        elif isinstance(message, StationStatus):
            self.stations[sender] = {
                "operator": message.operator,
                "call": message.call,
                "freq_hz": message.freq_hz,
                "mode": message.mode,
                "power_w": message.power_w,
                "swr": message.swr,
                "ft_tx_even": message.ft_tx_even,
                "last_heard": utcnow(),
            }
            self.peers[sender] = message.call or message.operator or sender
            if self.on_status is not None:
                self.on_status()

        elif isinstance(message, Chat):
            # Dedup by uuid (legacy messages without a uuid always fire once).
            is_new = message.uuid == "" or self.apply_chat(message)
            if is_new and self.on_chat is not None:
                self.on_chat(message, sender)

        elif isinstance(message, HunterMessage):
            self.clock.update(message.hunter.lamport)
            if self.apply_hunter(message.hunter) and self.on_hunter is not None:
                self.on_hunter(message.hunter)

        elif isinstance(message, HunterSyncResponse):
            for record in message.hunters:
                self.clock.update(record.lamport)
                if self.apply_hunter(record) and self.on_hunter is not None:
                    self.on_hunter(record)

        elif isinstance(message, SyncRequest):
            await self._send_diff(message.high_water)

        elif isinstance(message, FullLogRequest):
            # Reply with our entire log (including tombstones) so the requester
            # gets a guaranteed-complete copy, not just a high-water delta.
            everything = self.log.qsos(include_deleted=True)
            if everything:
                await self.transport.send(SyncResponse(qsos=everything))
            # ...and all of our chat, so a (re)joiner ends up with everyone's.
            if self.chats:
                await self.transport.send(ChatSyncResponse(chats=list(self.chats.values())))
            # ...and the POTA hunter roster, so a (re)joiner gets everyone's tallies.
            if self.hunters:
                await self.transport.send(
                    HunterSyncResponse(hunters=list(self.hunters.values()))
                )

        elif isinstance(message, ChatSyncResponse):
            for chat in message.chats:
                if self.apply_chat(chat) and self.on_chat is not None:
                    self.on_chat(chat, sender)

        elif isinstance(message, SyncResponse):
            for qso in message.qsos:
                self.clock.update(qso.lamport)
                if self.log.apply(qso):
                    self._notify(qso)

        elif isinstance(message, Heartbeat):
            self.clock.update(message.lamport_max)
            self._note_clock(sender, message.sender_utc)
            # Divergence backstop: if our merged state differs, ask for the delta.
            if message.log_hash != self.log.log_hash():
                await self.transport.send(SyncRequest(high_water=self.log.high_water()))

    def _note_clock(self, sender: str, sender_utc: str) -> None:
        """Record a peer's apparent clock offset and (debounced) flag if it's off.

        The offset is the peer's advertised UTC minus our own ``utcnow()`` and is
        stored on ``self.stations[sender]`` as ``clock_offset`` / ``clock_off`` so
        the roster can surface it. ``on_clock_off`` is fired only on a *material*
        transition (newly off, recovered, or the magnitude changed by >=1s) so a
        5s heartbeat doesn't spam the same warning. See ``net.clocksync`` for the
        latency caveat — small apparent offsets are usually transit, not drift.
        """
        offset = clock_offset_seconds(sender_utc, utcnow())
        info = self.stations.get(sender)
        if info is None:
            return
        prev_off = bool(info.get("clock_off"))
        prev_offset = info.get("clock_offset")
        off = is_clock_off(offset)
        info["clock_offset"] = offset
        info["clock_off"] = off
        if offset is None or self.on_clock_off is None:
            return
        moved = prev_offset is None or abs(offset - prev_offset) >= 1.0
        if off and (not prev_off or moved):
            label = info.get("operator") or info.get("call") or sender
            self.on_clock_off(label, offset)

    async def _send_diff(self, remote_high_water: dict[str, int]) -> None:
        missing = self.log.diff_since(remote_high_water)
        if missing:
            await self.transport.send(SyncResponse(qsos=missing))
