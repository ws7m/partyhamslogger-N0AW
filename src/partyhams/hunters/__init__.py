"""POTA hunter roster — who we work regularly, across every log.

Populated automatically while a Parks on the Air log is open: each logged QSO
adds or increments the worked station's entry, and the operator's name is filled
in from QRZ. Unlike the QSO log (one SQLite file per event), the roster is a
single install-wide file so it accumulates over time.

The roster syncs peer-to-peer alongside the log, using the same transport and
Lamport clock — see :mod:`partyhams.hunters.models` for the merge rule.
"""

from partyhams.hunters.models import Hunter, HunterRecord, merge
from partyhams.hunters.store import HUNTERS_DB, HunterStore

__all__ = ["HUNTERS_DB", "Hunter", "HunterRecord", "HunterStore", "merge"]
