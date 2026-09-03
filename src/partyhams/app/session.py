"""LogSession — the contest logging controller the UI binds to.

Owns the active contest, the station config, the peer-to-peer
:class:`~partyhams.net.engine.SyncEngine`, and the SQLite store, and exposes a
clean API for the entry window: log a QSO, check dupes, validate the exchange,
read the live score, list peers, and export. Every applied QSO — whether logged
here or received from a peer — is persisted and fans out to UI listeners.

Qt-free on purpose, so it's fully unit-testable.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from partyhams.contest import get as get_contest
from partyhams.contest.base import ContestConfig, ContestDefinition, ScoreSummary
from partyhams.contest.fd_bonus import BONUS_SELECTIONS_KEY
from partyhams.core.clock import new_station_id, new_uuid
from partyhams.core.models import QSO, Mode, ModeGroup, mode_group_for, utcnow
from partyhams.db.store import SqliteLog
from partyhams.export import write_adif, write_cabrillo, write_fieldday_summary
from partyhams.hunters import HUNTERS_DB, Hunter, HunterRecord, HunterStore
from partyhams.net.engine import SyncEngine
from partyhams.net.protocol import Chat
from partyhams.net.transport import MulticastTransport, NullTransport

RATE_WINDOWS_MIN = (15, 30, 60)  # QSO-rate windows shown in the network panel

# Presence thresholds (seconds since a peer's last StationStatus). A station
# broadcasts status every 3s, so "stale" dims it after a few missed beats,
# "silent" flags it as probably-gone after two minutes, and "gone" strikes it
# through after five — escalating confidence that the station has dropped off.
STALE_AFTER_S = 20
SILENT_AFTER_S = 120
GONE_AFTER_S = 300


def default_rst(mode: Mode) -> str:
    """Sensible default report: ``59`` for phone, ``599`` otherwise."""
    return "59" if mode_group_for(mode) == ModeGroup.PHONE else "599"


class LogSession:
    def __init__(
        self,
        *,
        contest: ContestDefinition,
        config: ContestConfig,
        engine: SyncEngine,
        store: SqliteLog,
        hunters: HunterStore | None = None,
    ) -> None:
        self.contest = contest
        self.config = config
        self.engine = engine
        self.store = store
        #: POTA hunter roster (None for any non-POTA contest — see
        #: :mod:`partyhams.hunters`). Populated as QSOs are logged.
        self.hunters = hunters if contest.id == "pota" else None
        self._listeners: list[Callable[[], None]] = []
        self._roster_listeners: list[Callable[[], None]] = []
        self._chat_listeners: list[Callable[[dict], None]] = []
        self._chat_log: list[dict] = []
        self._chat_seen: set[str] = set()  # uuids already in _chat_log (dedup)
        self._pending_broadcast: dict[str, Chat] = {}  # posted, awaiting broadcast
        self._pending_hunters: list[HunterRecord] = []  # applied, awaiting broadcast
        self._dupe_keys: set[tuple] = set()
        self._mult_keys: set[tuple[str, str]] = set()

        engine.on_qso = self._on_applied
        engine.on_status = self._on_roster_change
        engine.on_chat = self._on_chat
        engine.on_hunter = self._on_hunter
        engine.on_clock_off = self._on_clock_off
        # Load the persisted log into the in-memory merge, clock, and indexes.
        for qso in store.all(include_deleted=True):
            engine.log.apply(qso)
            engine.clock.update(qso.lamport)
        self._rebuild_indexes()
        # Replay persisted chat so chat_messages() returns history on open and
        # the engine can serve it to (re)joiners requesting a full log.
        for row in store.all_chat():
            self._chat_seen.add(row["uuid"])
            self._chat_log.append({**row, "incoming": True})
            engine.apply_chat(
                Chat(
                    from_op=row["from_op"],
                    to_op=row["to_op"],
                    text=row["text"],
                    ts=row["ts"],
                    uuid=row["uuid"],
                    station_id=row["station_id"],
                )
            )
        self._chat_log.sort(key=lambda e: (e["ts"], e["uuid"]))
        # Replay the persisted hunter roster so the engine can serve it to a
        # peer requesting a full sync, and so our own lamport clock never
        # reissues a value an existing record already used.
        if self.hunters is not None:
            for record in self.hunters.records():
                engine.apply_hunter(record)
                engine.clock.update(record.lamport)

    # ------------------------------------------------------------------ #
    # listeners / lifecycle
    # ------------------------------------------------------------------ #
    def add_listener(self, callback: Callable[[], None]) -> None:
        """Register a no-arg callback fired whenever the log changes."""
        self._listeners.append(callback)

    def _emit(self) -> None:
        for callback in self._listeners:
            callback()

    async def start(self) -> None:
        await self.engine.start()

    async def stop(self) -> None:
        await self.engine.stop()

    # ------------------------------------------------------------------ #
    # network panel: roster (presence + rates) and chat
    # ------------------------------------------------------------------ #
    def add_roster_listener(self, callback: Callable[[], None]) -> None:
        self._roster_listeners.append(callback)

    def add_chat_listener(self, callback: Callable[[dict], None]) -> None:
        self._chat_listeners.append(callback)

    def _on_roster_change(self) -> None:
        for callback in self._roster_listeners:
            callback()

    def _on_clock_off(self, operator: str, offset: float) -> None:
        """Announce (once, debounced by the engine) that a peer's clock drifted.

        Posted as a system chat to everyone so the whole network sees it. The
        offset is *apparent* (includes network transit latency) — see
        ``partyhams.net.clocksync`` — so the wording stays advisory.
        """
        self.post_chat("*", f"⏰ {operator}'s clock is off by {offset:+.1f}s — check time sync")

    def set_local_status(
        self,
        freq_hz: int,
        mode: Mode,
        *,
        power_w: float | None = None,
        swr: float | None = None,
        ft_tx_even: int | None = None,
    ) -> None:
        """Push our current frequency/mode so peers see what we're on.

        ``power_w``/``swr``/``ft_tx_even`` are optional best-effort extras (from
        the radio or WSJT-X); ``None`` leaves any previously-set value intact.
        """
        self.engine.update_status(
            freq_hz=freq_hz,
            mode=mode.value,
            power_w=power_w,
            swr=swr,
            ft_tx_even=ft_tx_even,
        )

    @property
    def operator(self) -> str:
        """The operator currently at the key."""
        return self.engine.operator

    def set_operator(self, operator: str) -> None:
        """Change the current operator (persisted, so it survives a reopen) and
        refresh listeners so the log re-colors by the new operator."""
        op = operator.strip().upper()
        if not op or op == self.engine.operator:
            return
        self.engine.set_operator(op)
        self.store.set_meta("operator", op)
        self._emit()

    def update_config(
        self,
        *,
        my_call: str,
        operator: str,
        sent_exchange: dict[str, str],
        extra: dict[str, object],
    ) -> None:
        """Edit the log's station config in place and persist it to the log file.

        Covers the station call, operator, sent exchange, and contest-specific
        fields (e.g. POTA park/entity/location). The contest type and sync network
        are fixed when the log is created and are not changed here. Affects future
        QSOs + presence; already-logged QSOs keep the call/operator they were
        recorded under.
        """
        my_call = my_call.strip().upper()
        operator = operator.strip().upper() or my_call
        # Merge over the existing extra so values not exposed in the editor (e.g.
        # bonus_points, set at creation) are preserved.
        self.config.my_call = my_call
        self.config.sent_exchange = dict(sent_exchange)
        self.config.extra = {**self.config.extra, **extra}
        # Station identity for new QSOs + presence broadcasts.
        self.engine.call = my_call
        self.engine.operator = operator
        self.engine._status["call"] = my_call
        self.engine._status["operator"] = operator
        self.store.set_meta("my_call", my_call)
        self.store.set_meta("operator", operator)
        self.store.set_meta("sent_exchange", json.dumps(self.config.sent_exchange))
        self.store.set_meta("extra", json.dumps(self.config.extra))
        self._emit()

    def station_rates(self, station_id: str, now=None) -> dict[int, int]:
        """Cumulative QSO counts for a station within each rate window."""
        now = now or utcnow()
        counts = {w: 0 for w in RATE_WINDOWS_MIN}
        for qso in self.engine.log.qsos():
            if qso.station_id != station_id:
                continue
            age_min = (now - qso.timestamp).total_seconds() / 60.0
            for window in RATE_WINDOWS_MIN:
                if age_min <= window:
                    counts[window] += 1
        return counts

    def station_total(self, station_id: str) -> int:
        """Total QSOs logged by a station across the entire log (no time window)."""
        return sum(1 for qso in self.engine.log.qsos() if qso.station_id == station_id)

    def station_stats(self, station_id: str) -> dict:
        """Per-station breakdown for the expanded view: hourly + by-mode counts."""
        qsos = [q for q in self.engine.log.qsos() if q.station_id == station_id]
        by_hour = [0] * 24
        by_mode: dict[str, int] = {}
        for qso in qsos:
            by_hour[qso.timestamp.hour] += 1
            by_mode[qso.mode.value] = by_mode.get(qso.mode.value, 0) + 1
        times = [q.timestamp for q in qsos]
        return {
            "total": len(qsos),
            "by_hour": by_hour,  # index = UTC hour 0..23
            "by_mode": by_mode,
            "first": min(times) if times else None,
            "last": max(times) if times else None,
        }

    def roster(self) -> list[dict]:
        """All known stations (self first), with operating state and QSO rates.

        Live presence (``engine.stations``, from StationStatus broadcasts) is merged
        with every station that has logged QSOs in the (persisted) log, so the panel
        survives a restart: peers who logged but haven't re-announced yet appear with
        their last-known identity + stats, flagged offline until they're heard again.
        """
        now = utcnow()
        rows = [
            self._station_row(
                self.engine.station_id,
                {
                    "operator": self.engine.operator,
                    "call": self.engine.call,
                    "freq_hz": int(self.engine._status["freq_hz"]),
                    "mode": str(self.engine._status["mode"]),
                    "power_w": self.engine._status["power_w"],
                    "swr": self.engine._status["swr"],
                    "ft_tx_even": self.engine._status["ft_tx_even"],
                    "last_heard": now,
                },
                is_self=True,
                now=now,
            )
        ]
        # Identities from the log (last-known operator/call per station), so ops who
        # logged earlier still show up. Live presence wins where both exist.
        from_log = self._log_station_identities()
        seen = {self.engine.station_id}
        for sid in [*self.engine.stations, *from_log]:
            if sid in seen:
                continue
            seen.add(sid)
            info = self.engine.stations.get(sid) or from_log.get(sid, {})
            rows.append(self._station_row(sid, info, is_self=False, now=now))
        return rows

    def _log_station_identities(self) -> dict[str, dict]:
        """Map each station_id in the log to its last-known operator/call (by the
        highest-lamport QSO), so a station that's logged QSOs has a roster identity
        even when it isn't currently broadcasting presence."""
        latest: dict[str, tuple[int, dict]] = {}
        for qso in self.engine.log.qsos():
            sid = qso.station_id
            if not sid:
                continue
            prev = latest.get(sid)
            if prev is None or qso.lamport >= prev[0]:
                latest[sid] = (
                    qso.lamport,
                    {"operator": qso.operator, "call": qso.station_callsign},
                )
        return {sid: ident for sid, (_, ident) in latest.items()}

    def _station_row(self, sid: str, info: dict, is_self: bool, now) -> dict:
        last_heard = info.get("last_heard")
        # Seconds since we last heard a presence beat (None for self / never-heard).
        silent_secs = (
            None if (is_self or last_heard is None) else (now - last_heard).total_seconds()
        )
        stale = silent_secs is not None and silent_secs > STALE_AFTER_S
        # Probably gone: no presence for SILENT_AFTER_S (also true if never heard).
        silent = (not is_self) and (last_heard is None or silent_secs > SILENT_AFTER_S)
        # Almost certainly dropped: no presence for GONE_AFTER_S (five minutes).
        gone = (not is_self) and (last_heard is None or silent_secs > GONE_AFTER_S)
        return {
            "station_id": sid,
            "operator": info.get("operator", ""),
            "call": info.get("call", ""),
            "freq_hz": int(info.get("freq_hz", 0) or 0),
            "mode": info.get("mode", ""),
            "is_self": is_self,
            "stale": stale,
            # No presence beat for over two minutes — likely offline. ``silent_secs``
            # is how long it's been (None if self/never-heard) for the tooltip.
            "silent": silent,
            "silent_secs": silent_secs,
            # No presence beat for over five minutes — render struck-through.
            "gone": gone,
            # Apparent clock offset vs us (seconds, peer-ahead positive) and whether
            # it exceeds the sync threshold. None/False for self and unheard peers.
            # NB: offset includes network latency — see partyhams.net.clocksync.
            "clock_offset": None if is_self else info.get("clock_offset"),
            "clock_off": (not is_self) and bool(info.get("clock_off")),
            # Last-known transmit power (W) and SWR; 0 => unknown. For FT8/FT4,
            # ft_tx_even is -1 unknown / 0 odd / 1 even (the Tx sequence).
            "power_w": float(info.get("power_w", 0.0) or 0.0),
            "swr": float(info.get("swr", 0.0) or 0.0),
            "ft_tx_even": int(info.get("ft_tx_even", -1)),
            "rates": self.station_rates(sid, now),
            "total": self.station_total(sid),
        }

    def operators(self) -> list[str]:
        """Distinct peer operator names (for the chat recipient list)."""
        seen = {
            info.get("operator", "")
            for info in self.engine.stations.values()
            if info.get("operator")
        }
        seen.discard(self.engine.operator)
        return sorted(seen)

    def post_chat(self, to_op: str, text: str) -> dict:
        """Record a chat message locally, persist it, and notify listeners.

        The message gets a stable uuid + our station_id (via the engine) so it
        dedups and syncs across machines. Pair with :meth:`broadcast_chat`.
        """
        msg = Chat(
            from_op=self.engine.operator,
            to_op=to_op,
            text=text,
            ts=utcnow().isoformat(),
            uuid=new_uuid(),
            station_id=self.engine.station_id,
        )
        self.engine.apply_chat(msg)
        self._pending_broadcast[msg.uuid] = msg
        self._record_chat(msg, incoming=False)
        return self._chat_log[-1]

    async def broadcast_chat(self, to_op: str, text: str) -> None:
        """Broadcast the message most recently produced by :meth:`post_chat`."""
        msg = next(
            (
                m
                for m in self._pending_broadcast.values()
                if m.to_op == to_op and m.text == text
            ),
            None,
        )
        if msg is None:
            msg = await self.engine.send_chat(to_op, text)
        else:
            self._pending_broadcast.pop(msg.uuid, None)
            await self.engine.transport.send(msg)

    def _on_chat(self, message, sender: str) -> None:
        # Show broadcasts and messages addressed to us; ignore others' DMs.
        addressed_to_all = message.to_op in ("", "*")
        if not (addressed_to_all or message.to_op == self.engine.operator):
            return
        self._record_chat(message, incoming=True)

    def _record_chat(self, message, incoming: bool) -> None:
        """Append to the in-memory log, persist, and notify (dedup by uuid)."""
        uuid = getattr(message, "uuid", "") or new_uuid()
        if uuid in self._chat_seen:
            return
        self._chat_seen.add(uuid)
        entry = {
            "uuid": uuid,
            "from_op": message.from_op,
            "to_op": message.to_op,
            "text": message.text,
            "ts": message.ts,
            "station_id": getattr(message, "station_id", ""),
            "incoming": incoming,
        }
        self.store.add_chat(entry)
        self._chat_log.append(entry)
        self._chat_log.sort(key=lambda e: (e["ts"], e["uuid"]))
        self._emit_chat(entry)

    def _emit_chat(self, entry: dict) -> None:
        for callback in self._chat_listeners:
            callback(entry)

    def chat_messages(self) -> list[dict]:
        return list(self._chat_log)

    # ------------------------------------------------------------------ #
    # apply hook (local + remote QSOs)
    # ------------------------------------------------------------------ #
    def _on_applied(self, qso: QSO) -> None:
        self.store.upsert(qso)
        self._rebuild_indexes()
        self._emit()

    def wipe_log(self) -> None:
        """Delete every QSO from this log (memory + disk), keeping the contest
        config. A hard local reset — no tombstones, so it won't propagate as a
        delete to peers (and peers can re-share what they have)."""
        self.engine.log.clear()
        self.store.wipe_qsos()
        self._rebuild_indexes()
        self._emit()

    def _rebuild_indexes(self) -> None:
        qsos = self.engine.log.qsos()
        self._dupe_keys = {self.contest.dupe_key(q) for q in qsos}
        self._mult_keys = set()
        for qso in qsos:
            self._mult_keys |= self.contest.multipliers(qso)

    # ------------------------------------------------------------------ #
    # logging
    # ------------------------------------------------------------------ #
    def record_qso(
        self,
        *,
        call: str,
        freq_hz: int,
        mode: Mode,
        exchange: dict[str, str],
        rst_sent: str | None = None,
        rst_rcvd: str = "599",
        timestamp: datetime | None = None,
        uuid: str | None = None,
    ) -> QSO:
        """Log a QSO locally and synchronously (UI updates immediately).

        Returns the recorded QSO; broadcast it to peers with :meth:`broadcast`.
        ``timestamp`` overrides "now" (WSJT-X reports the QSO's actual time).
        ``uuid`` overrides the random id (a content-derived id makes a re-delivered
        source — e.g. a duplicated WSJT-X packet — idempotent).
        """
        if self.contest.exchanges_rst:
            rs, rr = rst_sent or default_rst(mode), rst_rcvd
        else:
            rs = rr = ""  # contests like Field Day exchange no signal report
        qso = self.engine.record(
            call=call,
            freq_hz=freq_hz,
            mode=mode,
            exchange_rcvd=exchange,
            rst_sent=rs,
            rst_rcvd=rr,
            timestamp=timestamp,
            uuid=uuid,
        )
        self._note_hunter(qso)
        return qso

    def delete_qso(self, qso: QSO) -> QSO:
        """Tombstone a QSO (locally + CRDT). Returns the tombstone; broadcast it
        with :meth:`broadcast` so peers drop it too."""
        return self.engine.amend(replace(qso, deleted=True))

    def update_qso(
        self,
        qso: QSO,
        *,
        call: str,
        freq_hz: int,
        mode: Mode,
        exchange: dict[str, str],
        rst_sent: str | None = None,
        rst_rcvd: str | None = None,
        timestamp: datetime | None = None,
    ) -> QSO:
        """Edit a QSO's details in place (same uuid, fresh lamport). Returns the
        amended QSO; broadcast it with :meth:`broadcast`."""
        if self.contest.exchanges_rst:
            rs = rst_sent if rst_sent is not None else qso.rst_sent
            rr = rst_rcvd if rst_rcvd is not None else qso.rst_rcvd
        else:
            rs = rr = ""
        amended = replace(
            qso,
            call=call.upper(),
            freq_hz=freq_hz,
            mode=mode,
            exchange_rcvd=exchange,
            rst_sent=rs,
            rst_rcvd=rr,
            timestamp=timestamp or qso.timestamp,
            deleted=False,
        )
        return self.engine.amend(amended)

    async def broadcast(self, qso: QSO) -> None:
        await self.engine.broadcast(qso)
        await self.broadcast_hunters()

    async def request_full_log(self) -> None:
        """Ask every networked station to send its entire log."""
        await self.engine.request_full_log()

    async def log_qso(self, **kwargs) -> QSO:
        """Record + broadcast (convenience for tests/headless callers)."""
        qso = self.record_qso(**kwargs)
        await self.broadcast(qso)
        return qso

    # ------------------------------------------------------------------ #
    # POTA hunter roster
    # ------------------------------------------------------------------ #
    def _note_hunter(self, qso: QSO) -> None:
        """Add or increment the roster entry for a just-logged POTA QSO.

        A no-op for every non-POTA contest (``self.hunters`` is ``None`` there).
        The name is left empty; the UI fills it in from QRZ once per new call via
        :meth:`set_hunter_name`. Records are queued for broadcast rather than
        sent here, so logging stays synchronous and never waits on the network.
        """
        if self.hunters is None or qso.deleted:
            return
        record = self.hunters.worked(
            call=qso.call,
            station_id=self.engine.station_id,
            lamport=self.engine.clock.tick(),
            freq_hz=qso.freq_hz,
            band=qso.band_label,
            mode=qso.mode.value,
            when=qso.timestamp,
        )
        self.engine.apply_hunter(record)
        self._pending_hunters.append(record)

    def set_hunter_name(self, call: str, name: str) -> HunterRecord | None:
        """Attach the operator's name to a roster entry (from a QRZ lookup).

        Returns the updated record — broadcast it with :meth:`broadcast_hunters`
        — or ``None`` if there is nothing to change.
        """
        if self.hunters is None:
            return None
        record = self.hunters.set_name(
            call, self.engine.station_id, self.engine.clock.tick(), name
        )
        if record is None:
            return None
        self.engine.apply_hunter(record)
        self._pending_hunters.append(record)
        self._emit()
        return record

    def edit_hunter(self, old_call: str, new_call: str, name: str) -> list[HunterRecord]:
        """Correct a roster entry's callsign and/or operator name.

        Returns the records queued for broadcast (send them with
        :meth:`broadcast_hunters`); empty when nothing changed or the current log
        is not a POTA log. See :meth:`HunterStore.edit` for the merge semantics.
        """
        if self.hunters is None:
            return []
        changed = self.hunters.edit(
            old_call=old_call,
            new_call=new_call,
            name=name,
            station_id=self.engine.station_id,
            next_lamport=self.engine.clock.tick,
        )
        if not changed:
            return []
        if new_call.strip().upper() != old_call.strip().upper():
            self.engine.drop_hunter(old_call.strip().upper())
        for record in changed:
            self.engine.apply_hunter(record)
            self._pending_hunters.append(record)
        self._emit()
        return changed

    async def broadcast_hunters(self) -> None:
        """Send any roster records applied locally but not yet sent to peers."""
        if not self._pending_hunters:
            return
        pending, self._pending_hunters = self._pending_hunters, []
        for record in pending:
            await self.engine.broadcast_hunter(record)

    def _on_hunter(self, record: HunterRecord) -> None:
        """Persist a roster record that arrived from a peer."""
        if self.hunters is None:
            return
        if self.hunters.apply(record):
            self._emit()

    def hunter(self, call: str) -> Hunter | None:
        """The merged roster entry for ``call``, or ``None`` if never worked."""
        return self.hunters.get(call) if self.hunters is not None else None

    def hunters_by_worked(self) -> list[Hunter]:
        """The whole roster, most-worked first."""
        return self.hunters.all() if self.hunters is not None else []

    def hunter_needs_name(self, call: str) -> bool:
        """True if ``call`` is on the roster and nobody has a name for it yet."""
        return self.hunters is not None and self.hunters.needs_name(call)

    # ------------------------------------------------------------------ #
    # exchange parsing / validation
    # ------------------------------------------------------------------ #
    def parse_exchange(self, raw: str) -> dict[str, str]:
        return self.contest.parse_exchange(raw)

    def validate_exchange(self, parsed: dict[str, str]) -> list[str]:
        """Return a list of human-readable problems ([] means valid)."""
        errors: list[str] = []
        for fld in self.contest.exchange_fields():
            value = parsed.get(fld.name, "")
            if fld.required and not value:
                errors.append(f"{fld.label} is required")
            elif value and fld.validator and not fld.validator(value):
                errors.append(f"{fld.label} '{value}' is invalid")
        return errors

    # ------------------------------------------------------------------ #
    # dupe / partial check
    # ------------------------------------------------------------------ #
    def is_dupe(self, call: str, freq_hz: int, mode: Mode) -> bool:
        """True iff logging ``call`` now (on this band+mode) would duplicate a QSO.

        Uses the contest's :meth:`dupe_key`. For Field Day that is per band AND
        per mode-group (CW / Phone / Digital), so the same call is workable again
        on a different band, or on the same band in a different mode category.
        """
        if not call:
            return False
        probe = QSO(
            uuid="", station_id="", operator="", call=call.upper(), freq_hz=freq_hz, mode=mode
        )
        return self.contest.dupe_key(probe) in self._dupe_keys

    def dupe_label(self, call: str, freq_hz: int, mode: Mode) -> str:
        """``"DUPE"`` if working ``call`` now would duplicate a QSO already in the
        (network-wide) log for this slot, else ``""``."""
        return "DUPE" if self.is_dupe(call, freq_hz, mode) else ""

    def new_mults(
        self, call: str, freq_hz: int, mode: Mode, exchange: dict[str, str]
    ) -> set[tuple[str, str]]:
        """Multipliers a prospective QSO would newly add (empty if none/all worked)."""
        probe = QSO(
            uuid="",
            station_id="",
            operator="",
            call=call.upper(),
            freq_hz=freq_hz,
            mode=mode,
            exchange_rcvd={k: v for k, v in exchange.items() if v},
        )
        return {m for m in self.contest.multipliers(probe) if m not in self._mult_keys}

    def partial_matches(self, fragment: str, limit: int = 20) -> list[str]:
        """Worked calls beginning with ``fragment`` (a simple partial check)."""
        frag = fragment.upper()
        if not frag:
            return []
        calls = sorted({q.call for q in self.engine.log.qsos() if q.call.startswith(frag)})
        return calls[:limit]

    def worked_near(self, freq_hz: int, mode: Mode, *, tolerance_hz: int = 200) -> list[QSO]:
        """QSOs already worked at ~``freq_hz`` (within ``tolerance_hz``) on the same
        mode group, most-recent first.

        Drives the Search & Pounce dupe hint: when the operator tunes to a
        frequency, this surfaces a station already in the (network-wide) log on
        that frequency+mode, which is likely the same station still calling CQ —
        so the op can move on instead of working a dupe. Mode is matched by group
        (CW / Phone / Digital) to mirror the dupe rule. Returns ``[]`` for an
        out-of-band / unknown frequency.
        """
        if freq_hz <= 0:
            return []
        group = mode_group_for(mode)
        matches = [
            q
            for q in self.engine.log.qsos()  # already excludes tombstoned QSOs
            if q.call
            and abs(q.freq_hz - freq_hz) <= tolerance_hz
            and q.mode_group == group
        ]
        matches.sort(key=lambda q: q.timestamp, reverse=True)
        return matches

    # ------------------------------------------------------------------ #
    # views
    # ------------------------------------------------------------------ #
    def qsos(self) -> list[QSO]:
        return self.engine.log.qsos()

    def recent(self, n: int = 50) -> list[QSO]:
        return self.engine.log.qsos()[-n:]

    def score(self) -> ScoreSummary:
        return self.contest.score(self.engine.log.qsos(), self.config)

    @property
    def peers(self) -> dict[str, str]:
        return self.engine.peers

    def allowed_bands(self) -> set[str]:
        return self.contest.allowed_bands()

    def section_status(self) -> dict[str, set[tuple[str, str]]]:
        """Per section worked, the set of ``(band, mode_group)`` slots it was on."""
        status: dict[str, set[tuple[str, str]]] = {}
        for qso in self.engine.log.qsos():
            section = qso.exchange_rcvd.get("section", "").upper()
            if section:
                status.setdefault(section, set()).add((qso.band_label, qso.mode_group.value))
        return status

    def section_detail(self, section: str) -> list[dict]:
        """Who worked ``section``, for the map's detail panel.

        Returns one row per operator that logged a QSO whose received section
        matches, each ``{"operator", "calls", "bands", "modes", "count"}`` where
        ``calls``/``bands``/``modes`` are sorted lists of the distinct values and
        ``count`` is the number of QSOs. Pure and Qt-free, so it's unit-tested.
        Sorted by operator name. Empty list when the section is unworked.
        """
        target = section.upper()
        by_op: dict[str, dict[str, set[str] | int]] = {}
        for qso in self.engine.log.qsos():
            if qso.exchange_rcvd.get("section", "").upper() != target:
                continue
            row = by_op.setdefault(
                qso.operator, {"calls": set(), "bands": set(), "modes": set(), "count": 0}
            )
            row["calls"].add(qso.call)  # type: ignore[union-attr]
            row["bands"].add(qso.band_label)  # type: ignore[union-attr]
            row["modes"].add(qso.mode_group.value)  # type: ignore[union-attr]
            row["count"] += 1  # type: ignore[operator]
        return [
            {
                "operator": op,
                "calls": sorted(row["calls"]),  # type: ignore[arg-type]
                "bands": sorted(row["bands"]),  # type: ignore[arg-type]
                "modes": sorted(row["modes"]),  # type: ignore[arg-type]
                "count": row["count"],
            }
            for op, row in sorted(by_op.items())
        ]

    # ------------------------------------------------------------------ #
    # export
    # ------------------------------------------------------------------ #
    def export_adif(self, *, mine_only: bool = False) -> str:
        """Render the log as ADIF. ``mine_only`` keeps just this station's own QSOs
        (by station_id) — for a personal submission — vs the whole synced log."""
        qsos = self.engine.log.qsos()
        if mine_only:
            qsos = [q for q in qsos if q.station_id == self.engine.station_id]
        return write_adif(qsos, self.config, self.contest)

    def export_cabrillo(self) -> str:
        operators = {q.operator for q in self.engine.log.qsos()}
        return write_cabrillo(
            self.engine.log.qsos(), self.config, self.contest, self.score(), operators
        )

    def export_fieldday_summary(self) -> str:
        """Render the ARRL Field Day summary sheet (the figures the operator reads
        into the Field Day web app). Only meaningful for the Field Day contest."""
        operators = {q.operator for q in self.engine.log.qsos()}
        return write_fieldday_summary(
            self.engine.log.qsos(), self.config, self.contest, self.score(), operators
        )

    def fd_summary_info_entered(self) -> bool:
        """True once the operator has filled in the Field Day summary info (bonus
        points + participants) via the dialog. Drives the export prompt."""
        return BONUS_SELECTIONS_KEY in self.config.extra


def _assemble(
    contest: ContestDefinition,
    config: ContestConfig,
    operator: str | None,
    network: str | None,
    store: SqliteLog,
    station_id: str,
    hunters_db: str | Path | None = HUNTERS_DB,
) -> LogSession:
    if network:
        transport: NullTransport | MulticastTransport = MulticastTransport(network, station_id)
    else:
        transport = NullTransport("offline", station_id)
    engine = SyncEngine(transport, operator=operator or config.my_call, call=config.my_call)
    # The hunter roster is install-wide and POTA-only: open it only for a POTA
    # log so no other contest pays for it (LogSession also gates on contest.id).
    hunters = (
        HunterStore(hunters_db)
        if hunters_db is not None and contest.id == "pota"
        else None
    )
    return LogSession(
        contest=contest, config=config, engine=engine, store=store, hunters=hunters
    )


def _write_meta(
    store: SqliteLog,
    contest_id: str,
    config: ContestConfig,
    operator: str | None,
    network: str | None,
    station_id: str,
) -> None:
    store.set_meta("contest_id", contest_id)
    store.set_meta("my_call", config.my_call)
    store.set_meta("operator", operator or config.my_call)
    store.set_meta("network", network or "")
    store.set_meta("sent_exchange", json.dumps(config.sent_exchange))
    store.set_meta("extra", json.dumps(config.extra))
    # Stable per-log station identity, so this machine keeps the same sync id (and
    # its QSO stats) every time the log is reopened (see open_session).
    store.set_meta("station_id", station_id)


def build_session(
    *,
    contest_id: str,
    my_call: str,
    sent_exchange: dict[str, str],
    network: str | None,
    operator: str | None = None,
    power: str = "low_150w",
    bonus_points: int = 0,
    extra: dict[str, object] | None = None,
    db_path: str | Path = ":memory:",
    hunters_db: str | Path | None = HUNTERS_DB,
) -> LogSession:
    """Create a new log + session and persist its config into the log file.

    ``network`` blank/None => offline. ``extra`` (e.g. from a contest's
    ``config_fields``) is merged over the power/bonus defaults.
    """
    contest = get_contest(contest_id)
    merged_extra: dict[str, object] = {"power": power, "bonus_points": bonus_points}
    if extra:
        merged_extra.update(extra)
    config = ContestConfig(my_call=my_call, sent_exchange=sent_exchange, extra=merged_extra)
    store = SqliteLog(db_path)
    station_id = new_station_id()
    _write_meta(store, contest_id, config, operator, network, station_id)
    return _assemble(contest, config, operator, network, store, station_id, hunters_db)


def summarize_log(path: str | Path) -> dict | None:
    """Summary of one log file for the Open Log chooser, or None if unreadable.

    Includes the distinguishing detail — the event date (first QSO) and, for POTA,
    the park(s) — so logs of the same contest are uniquely identifiable (see
    :func:`log_detail`)."""
    path = Path(path)
    try:
        store = SqliteLog(path)
        meta = store.all_meta()
        rows = store.all()
        store.close()
    except Exception:  # noqa: BLE001 - unreadable/foreign/missing file
        return None
    contest_id = meta.get("contest_id", "")
    try:
        name = get_contest(contest_id).name
    except KeyError:
        name = contest_id or "?"
    # Event date = first QSO's UTC date; fall back to the file's date for an empty
    # log (≈ when it was created), so even unlogged events are still dated.
    if rows:
        start = rows[0].timestamp.date().isoformat()
    else:
        start = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).date().isoformat()
    try:
        extra = json.loads(meta.get("extra", "{}"))
    except (ValueError, TypeError):
        extra = {}
    return {
        "path": str(path),
        "contest": name,
        "contest_id": contest_id,
        "call": meta.get("my_call", ""),
        "qsos": len(rows),
        "mtime": path.stat().st_mtime,
        "start": start,
        "park": str(extra.get("park", "") or ""),
    }


def log_detail(summary: dict) -> str:
    """The distinguishing detail for a log summary: park(s) and/or event date, so
    two logs of the same contest are easy to tell apart. POTA shows the park(s);
    every log shows its event date."""
    parts: list[str] = []
    parks = [p.strip().upper() for p in summary.get("park", "").split(",") if p.strip()]
    if parks:
        parts.append(parks[0] if len(parks) == 1 else f"{parks[0]} +{len(parks) - 1}")
    if summary.get("start"):
        parts.append(summary["start"])
    return " · ".join(parts)


def list_logs(logs_dir: Path | None = None) -> list[dict]:
    """Summarize every saved log file (for the Open Log chooser), newest first."""
    from partyhams.app.state import LOGS_DIR

    logs_dir = logs_dir if logs_dir is not None else LOGS_DIR
    if not logs_dir.exists():
        return []
    out = [s for path in logs_dir.glob("*.sqlite") if (s := summarize_log(path))]
    out.sort(key=lambda d: d["mtime"], reverse=True)
    return out


def open_session(
    db_path: str | Path, hunters_db: str | Path | None = HUNTERS_DB
) -> LogSession:
    """Reopen an existing log file, restoring its contest + station config."""
    store = SqliteLog(db_path)
    meta = store.all_meta()
    if "contest_id" not in meta:
        raise ValueError(f"{db_path} is not a PartyHams log (no metadata)")
    contest = get_contest(meta["contest_id"])
    config = ContestConfig(
        my_call=meta.get("my_call", ""),
        sent_exchange=json.loads(meta.get("sent_exchange", "{}")),
        extra=json.loads(meta.get("extra", "{}")),
    )
    operator = meta.get("operator") or config.my_call
    network = meta.get("network") or None
    # Reuse the log's saved station id so stats carry across restarts. Logs created
    # before this field existed get one generated and persisted now (stable hereafter).
    station_id = meta.get("station_id")
    if not station_id:
        station_id = new_station_id()
        store.set_meta("station_id", station_id)
    return _assemble(contest, config, operator, network, store, station_id, hunters_db)
