"""SQLite-backed POTA hunter roster.

One file for the whole install (``~/.partyhams/pota_hunters.sqlite``), not one
per log: the point of the roster is that it outlives any single activation, so
a station you worked at a park last spring is still recognized today.

The table is keyed by ``(call, station_id)`` — see
:mod:`partyhams.hunters.models` for why the tally is split per station rather
than stored as one row per call. :meth:`worked` and :meth:`set_name` write this
station's own row; :meth:`apply` merges a row arriving from a peer. Reads
(:meth:`get`, :meth:`all`) return the merged :class:`~partyhams.hunters.models.Hunter`
view and are what the UI should use.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from partyhams.app.state import APP_DIR
from partyhams.core.models import utcnow
from partyhams.hunters.models import Hunter, HunterRecord, merge

#: The install-wide roster file. Kept beside ``state.json`` and ``refdata/``.
HUNTERS_DB = APP_DIR / "pota_hunters.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pota_hunter (
    call         TEXT NOT NULL,
    station_id   TEXT NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    last_worked  TEXT NOT NULL,
    freq_hz      INTEGER NOT NULL DEFAULT 0,
    band         TEXT NOT NULL DEFAULT '',
    mode         TEXT NOT NULL DEFAULT '',
    worked_count INTEGER NOT NULL DEFAULT 0,
    lamport      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (call, station_id)
);
CREATE INDEX IF NOT EXISTS idx_hunter_call ON pota_hunter(call);
"""


class HunterStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- local writes -------------------------------------------------- #
    def worked(
        self,
        *,
        call: str,
        station_id: str,
        lamport: int,
        freq_hz: int = 0,
        band: str = "",
        mode: str = "",
        when: datetime | None = None,
        name: str = "",
    ) -> HunterRecord:
        """Record one QSO with ``call``, returning this station's updated record.

        A first contact inserts the row with ``worked_count = 1``; a later one
        refreshes the operating details (time, frequency, band, mode) and adds
        one to the count. An existing name is never cleared by a call that
        supplies none.
        """
        call = call.strip().upper()
        existing = self._record(call, station_id)
        record = HunterRecord(
            call=call,
            station_id=station_id,
            name=name.strip() or (existing.name if existing else ""),
            last_worked=when or utcnow(),
            freq_hz=freq_hz,
            band=band,
            mode=mode,
            worked_count=(existing.worked_count if existing else 0) + 1,
            lamport=lamport,
        )
        self._write(record)
        return record

    def set_name(
        self, call: str, station_id: str, lamport: int, name: str
    ) -> HunterRecord | None:
        """Fill in the operator's name on this station's row for ``call``.

        Returns the updated record to broadcast, or ``None`` if the row does not
        exist yet or the name is unchanged (so a no-op never floods the network).
        """
        call = call.strip().upper()
        name = name.strip()
        existing = self._record(call, station_id)
        if existing is None or not name or existing.name == name:
            return None
        existing.name = name
        existing.lamport = lamport
        self._write(existing)
        return existing

    def edit(
        self,
        *,
        old_call: str,
        new_call: str,
        name: str,
        station_id: str,
        next_lamport: Callable[[], int] | None = None,
    ) -> list[HunterRecord]:
        """Correct the callsign and/or operator name of a roster entry.

        Applies to **every** local row for ``old_call``, including rows owned by
        peer stations, so the correction is visible immediately in the merged
        view. Only rows this station owns get a fresh lamport and are returned
        for broadcast — a peer's row is ours to display, not to re-stamp, so its
        lamport is left alone and a genuine later update from that peer still
        wins. (The flip side: a peer that re-broadcasts after a rename can
        recreate the row under the old callsign. Single-operator rosters, where
        every row is ours, are unaffected.)

        Renaming onto a callsign already in the roster **merges** the two — per
        station the counts add and the newer contact supplies the operating
        details — which is what correcting a mistyped call should do. Returns the
        records to broadcast (empty if nothing changed).
        """
        old_call = old_call.strip().upper()
        new_call = new_call.strip().upper() or old_call
        name = name.strip()
        tick = next_lamport or (lambda: self.next_lamport(station_id))

        rows = self._rows_for(old_call)
        if not rows:
            return []
        renaming = new_call != old_call
        # Rows already filed under the new call, which the renamed ones merge into.
        target = {r.station_id: r for r in self._rows_for(new_call)} if renaming else {}

        changed: list[HunterRecord] = []
        for record in rows:
            updated = HunterRecord(
                call=new_call,
                station_id=record.station_id,
                name=name or record.name,
                last_worked=record.last_worked,
                freq_hz=record.freq_hz,
                band=record.band,
                mode=record.mode,
                worked_count=record.worked_count,
                lamport=record.lamport,
            )
            collision = target.pop(record.station_id, None)
            if collision is not None:
                updated = _combine(updated, collision)
            if record.station_id == station_id:
                updated.lamport = tick()
                changed.append(updated)
            if renaming:
                self._delete(old_call, record.station_id)
            self._write(updated)

        # Stations that had a row under the new call but none under the old one
        # still need the corrected name.
        for record in target.values():
            if not name or record.name == name:
                continue
            record.name = name
            if record.station_id == station_id:
                record.lamport = tick()
                changed.append(record)
            self._write(record)
        return changed

    def next_lamport(self, station_id: str) -> int:
        """One past the highest lamport this station has used in the roster.

        Only used when no clock is supplied (editing while no POTA log is open);
        a live session passes its engine's Lamport clock instead.
        """
        row = self._conn.execute(
            "SELECT MAX(lamport) AS high FROM pota_hunter WHERE station_id = ?",
            (station_id,),
        ).fetchone()
        return (row["high"] or 0) + 1

    # --- remote merge -------------------------------------------------- #
    def apply(self, record: HunterRecord) -> bool:
        """Merge a peer's record. Returns True if local state changed.

        A record is only ever written by the station that owns it, so ordering
        by ``lamport`` is enough; the ``worked_count`` tiebreak makes a replayed
        datagram harmless even if two writes somehow share a lamport.
        """
        existing = self._record(record.call, record.station_id)
        if existing is not None:
            if (record.lamport, record.worked_count) <= (
                existing.lamport,
                existing.worked_count,
            ):
                return False
        self._write(record)
        return True

    # --- reads --------------------------------------------------------- #
    def get(self, call: str) -> Hunter | None:
        """The merged roster entry for ``call``, or ``None`` if never worked."""
        rows = self._conn.execute(
            "SELECT * FROM pota_hunter WHERE call = ?", (call.strip().upper(),)
        )
        return merge(self._from_row(r) for r in rows)

    def all(self) -> list[Hunter]:
        """Every hunter, most-worked first (ties broken by callsign)."""
        grouped: dict[str, list[HunterRecord]] = defaultdict(list)
        for row in self._conn.execute("SELECT * FROM pota_hunter"):
            record = self._from_row(row)
            grouped[record.call].append(record)
        hunters = [h for records in grouped.values() if (h := merge(records))]
        hunters.sort(key=lambda h: (-h.worked_count, h.call))
        return hunters

    def records(self) -> list[HunterRecord]:
        """Every stored per-station record — what a peer gets on a full sync."""
        return [self._from_row(r) for r in self._conn.execute("SELECT * FROM pota_hunter")]

    def knows(self, call: str) -> bool:
        """True if ``call`` has been worked before (by any station)."""
        row = self._conn.execute(
            "SELECT 1 FROM pota_hunter WHERE call = ? LIMIT 1", (call.strip().upper(),)
        ).fetchone()
        return row is not None

    def needs_name(self, call: str) -> bool:
        """True if ``call`` is in the roster but no station has a name for it."""
        hunter = self.get(call)
        return hunter is not None and not hunter.name

    # --- mapping ------------------------------------------------------- #
    def _rows_for(self, call: str) -> list[HunterRecord]:
        return [
            self._from_row(r)
            for r in self._conn.execute("SELECT * FROM pota_hunter WHERE call = ?", (call,))
        ]

    def _delete(self, call: str, station_id: str) -> None:
        self._conn.execute(
            "DELETE FROM pota_hunter WHERE call = ? AND station_id = ?", (call, station_id)
        )
        self._conn.commit()

    def _record(self, call: str, station_id: str) -> HunterRecord | None:
        row = self._conn.execute(
            "SELECT * FROM pota_hunter WHERE call = ? AND station_id = ?",
            (call.strip().upper(), station_id),
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def _write(self, record: HunterRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO pota_hunter (call, station_id, name, last_worked, freq_hz,
                                     band, mode, worked_count, lamport)
            VALUES (:call, :station_id, :name, :last_worked, :freq_hz,
                    :band, :mode, :worked_count, :lamport)
            ON CONFLICT(call, station_id) DO UPDATE SET
                name=excluded.name, last_worked=excluded.last_worked,
                freq_hz=excluded.freq_hz, band=excluded.band, mode=excluded.mode,
                worked_count=excluded.worked_count, lamport=excluded.lamport
            """,
            {
                "call": record.call,
                "station_id": record.station_id,
                "name": record.name,
                "last_worked": record.last_worked.isoformat(),
                "freq_hz": record.freq_hz,
                "band": record.band,
                "mode": record.mode,
                "worked_count": record.worked_count,
                "lamport": record.lamport,
            },
        )
        self._conn.commit()

    @staticmethod
    def _from_row(row: sqlite3.Row) -> HunterRecord:
        return HunterRecord(
            call=row["call"],
            station_id=row["station_id"],
            name=row["name"],
            last_worked=datetime.fromisoformat(row["last_worked"]),
            freq_hz=row["freq_hz"],
            band=row["band"],
            mode=row["mode"],
            worked_count=row["worked_count"],
            lamport=row["lamport"],
        )


def _combine(primary: HunterRecord, other: HunterRecord) -> HunterRecord:
    """Fold two records for the same station into one (used when a rename lands
    on a callsign already in the roster). Counts add; the operating details come
    from whichever contact was more recent; ``primary``'s name takes precedence
    because it carries the operator's edit."""
    newest = primary if primary.last_worked >= other.last_worked else other
    return HunterRecord(
        call=primary.call,
        station_id=primary.station_id,
        name=primary.name or other.name,
        last_worked=newest.last_worked,
        freq_hz=newest.freq_hz,
        band=newest.band,
        mode=newest.mode,
        worked_count=primary.worked_count + other.worked_count,
        lamport=max(primary.lamport, other.lamport),
    )
