"""POTA hunter-roster data model.

A *hunter* is a station worked during a Parks on the Air activity. The roster is
a long-lived, **app-level** record of who we work regularly — deliberately
separate from the per-log ``qso`` table, which starts empty with every new log.

Two types live here:

* :class:`HunterRecord` — one *station's own* tally for one callsign. This is the
  unit that is stored and synced, and it is owned exclusively by the station
  whose ``station_id`` it carries: no other station ever writes it.
* :class:`Hunter` — the merged, read-only view of a callsign across every
  station, produced by :func:`merge`. This is what the app displays.

**Why the split.** ``worked_count`` is a counter, and counters do not survive
naive last-writer-wins. If two stations each work W1AW once, both would set
``worked_count = 2``; LWW keeps one of them and a contact is silently lost.
Splitting the row per station makes the roster a grow-only counter: a station
only ever increments its own tally and the total is the sum, so any arrival
order converges to the same number. The "last worked" observation (time, freq,
band, mode) *is* a genuine maximum, so it takes the newest record across
stations instead of being summed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from partyhams.core.models import Mode, utcnow


@dataclass
class HunterRecord:
    """One station's tally for one hunter callsign (the stored + synced unit).

    ``lamport`` orders this station's own successive writes so a re-delivered or
    out-of-order datagram can never move the record backwards. Because only the
    owning station writes it, there is no cross-station tiebreak to make.
    """

    call: str
    station_id: str
    name: str = ""  # operator's name from QRZ ("" => unknown)
    last_worked: datetime = field(default_factory=utcnow)
    freq_hz: int = 0
    band: str = ""
    mode: str = Mode.CW.value
    worked_count: int = 0
    lamport: int = 0


@dataclass
class Hunter:
    """The merged view of one callsign across every station in the network."""

    call: str
    name: str = ""
    last_worked: datetime = field(default_factory=utcnow)
    freq_hz: int = 0
    band: str = ""
    mode: str = Mode.CW.value
    worked_count: int = 0
    #: How many stations have worked this call (1 for a single-op roster).
    stations: int = 0


def merge(records: Iterable[HunterRecord]) -> Hunter | None:
    """Collapse one callsign's per-station records into a single :class:`Hunter`.

    ``worked_count`` sums; the operating details come from the most recently
    worked record; the name comes from the newest record that carries one (so a
    station that has looked the call up on QRZ supplies it to stations that
    haven't). Returns ``None`` for an empty iterable.
    """
    rows = list(records)
    if not rows:
        return None
    # Newest first, with station_id as a stable tiebreak for identical stamps.
    rows.sort(key=lambda r: (r.last_worked, r.station_id), reverse=True)
    newest = rows[0]
    name = next((r.name for r in rows if r.name), "")
    return Hunter(
        call=newest.call,
        name=name,
        last_worked=newest.last_worked,
        freq_hz=newest.freq_hz,
        band=newest.band,
        mode=newest.mode,
        worked_count=sum(r.worked_count for r in rows),
        stations=len(rows),
    )
