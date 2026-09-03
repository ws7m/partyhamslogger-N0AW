"""POTA hunter roster: store round-trip, merge rule, session hook, and P2P sync."""

from __future__ import annotations

from datetime import UTC, datetime

from factories import FREQ

from partyhams.app.session import build_session, open_session
from partyhams.core.clock import new_station_id
from partyhams.core.models import Mode
from partyhams.hunters import Hunter, HunterRecord, HunterStore, merge
from partyhams.net.engine import SyncEngine
from partyhams.net.loopback import LoopbackBus, LoopbackTransport
from partyhams.net.protocol import HunterMessage, HunterSyncResponse, decode, encode

NETWORK = "test-net"
WHEN = datetime(2026, 6, 27, 18, 0, 0, tzinfo=UTC)


def make_record(call="W1AW", station_id="s1", **kw) -> HunterRecord:
    base = dict(
        name="Hiram",
        last_worked=WHEN,
        freq_hz=FREQ["20m"],
        band="20m",
        mode=Mode.CW.value,
        worked_count=1,
        lamport=1,
    )
    base.update(kw)
    return HunterRecord(call=call, station_id=station_id, **base)


async def make_engine(bus: LoopbackBus, call: str) -> SyncEngine:
    transport = LoopbackTransport(bus, NETWORK, station_id=new_station_id())
    engine = SyncEngine(transport, operator=call, call=call)
    await engine.join()
    return engine


async def converge(*engines: SyncEngine, max_rounds: int = 200) -> None:
    for _ in range(max_rounds):
        progressed = False
        for engine in engines:
            if await engine.pump_once():
                progressed = True
        if not progressed:
            return
    raise AssertionError("engines did not reach a quiescent state")


# --------------------------------------------------------------------------- #
# store: insert, increment, persistence
# --------------------------------------------------------------------------- #
def test_first_contact_inserts_with_count_one():
    store = HunterStore()
    store.worked(
        call="w1aw", station_id="s1", lamport=1,
        freq_hz=FREQ["20m"], band="20m", mode=Mode.CW.value, when=WHEN, name="Hiram",
    )
    hunter = store.get("W1AW")
    assert hunter is not None
    assert hunter.call == "W1AW"  # normalized to upper case
    assert hunter.name == "Hiram"
    assert hunter.band == "20m"
    assert hunter.mode == Mode.CW.value
    assert hunter.freq_hz == FREQ["20m"]
    assert hunter.last_worked == WHEN
    assert hunter.worked_count == 1
    store.close()


def test_second_contact_increments_and_refreshes_details():
    store = HunterStore()
    store.worked(call="W1AW", station_id="s1", lamport=1, freq_hz=FREQ["20m"],
                 band="20m", mode=Mode.CW.value, when=WHEN, name="Hiram")
    later = datetime(2026, 8, 1, 12, 30, tzinfo=UTC)
    store.worked(call="W1AW", station_id="s1", lamport=2, freq_hz=FREQ["40m"],
                 band="40m", mode=Mode.USB.value, when=later)

    hunter = store.get("W1AW")
    assert hunter.worked_count == 2
    assert hunter.last_worked == later  # date/time refreshed
    assert hunter.freq_hz == FREQ["40m"]  # frequency refreshed
    assert hunter.band == "40m"
    assert hunter.mode == Mode.USB.value  # mode refreshed
    assert hunter.name == "Hiram"  # a nameless re-work never clears the name
    store.close()


def test_roster_persists_across_reopen(tmp_path):
    path = tmp_path / "pota_hunters.sqlite"
    store = HunterStore(path)
    store.worked(call="W1AW", station_id="s1", lamport=1, band="20m",
                 mode=Mode.CW.value, when=WHEN, name="Hiram")
    store.close()

    reopened = HunterStore(path)
    hunter = reopened.get("W1AW")
    assert hunter is not None
    assert hunter.name == "Hiram"
    assert hunter.worked_count == 1
    reopened.close()


def test_set_name_fills_a_blank_and_is_a_no_op_when_unchanged():
    store = HunterStore()
    assert store.set_name("W1AW", "s1", 1, "Hiram") is None  # not on the roster yet

    store.worked(call="W1AW", station_id="s1", lamport=1, when=WHEN)
    assert store.needs_name("W1AW") is True

    record = store.set_name("W1AW", "s1", 2, "Hiram")
    assert record is not None and record.name == "Hiram"
    assert store.get("W1AW").name == "Hiram"
    assert store.needs_name("W1AW") is False
    # Re-setting the same name changes nothing (so it is never re-broadcast).
    assert store.set_name("W1AW", "s1", 3, "Hiram") is None
    store.close()


def test_knows_and_all_ordering():
    store = HunterStore()
    for _ in range(3):
        store.worked(call="K1ABC", station_id="s1", lamport=1, when=WHEN)
    store.worked(call="W1AW", station_id="s1", lamport=2, when=WHEN)

    assert store.knows("k1abc") is True
    assert store.knows("N0AW") is False
    assert [h.call for h in store.all()] == ["K1ABC", "W1AW"]  # most-worked first
    store.close()


# --------------------------------------------------------------------------- #
# merge rule: a counter must not be last-writer-wins
# --------------------------------------------------------------------------- #
def test_merge_sums_counts_across_stations():
    """Two stations each working a call three times means six contacts, not three."""
    merged = merge([
        make_record(station_id="s1", worked_count=3),
        make_record(station_id="s2", worked_count=3),
    ])
    assert merged.worked_count == 6
    assert merged.stations == 2


def test_merge_takes_the_newest_observation():
    older = make_record(station_id="s1", last_worked=WHEN, band="20m", mode=Mode.CW.value)
    newer = make_record(
        station_id="s2",
        last_worked=datetime(2026, 8, 1, tzinfo=UTC),
        band="40m",
        mode=Mode.USB.value,
        freq_hz=FREQ["40m"],
    )
    for records in ([older, newer], [newer, older]):  # order must not matter
        merged = merge(records)
        assert merged.band == "40m"
        assert merged.mode == Mode.USB.value
        assert merged.freq_hz == FREQ["40m"]
        assert merged.last_worked == newer.last_worked


def test_merge_borrows_a_name_from_whichever_station_has_one():
    merged = merge([
        make_record(station_id="s1", name=""),
        make_record(station_id="s2", name="Hiram"),
    ])
    assert merged.name == "Hiram"


def test_merge_of_nothing_is_none():
    assert merge([]) is None


def test_apply_ignores_a_stale_or_replayed_record():
    store = HunterStore()
    assert store.apply(make_record(worked_count=5, lamport=9)) is True
    assert store.apply(make_record(worked_count=5, lamport=9)) is False  # replay
    assert store.apply(make_record(worked_count=2, lamport=4)) is False  # stale
    assert store.get("W1AW").worked_count == 5
    store.close()


def test_two_stations_are_separate_rows_not_a_conflict():
    store = HunterStore()
    store.worked(call="W1AW", station_id="s1", lamport=1, when=WHEN)
    store.apply(make_record(station_id="s2", worked_count=1, lamport=1))
    assert store.get("W1AW").worked_count == 2
    assert len(store.records()) == 2
    store.close()


# --------------------------------------------------------------------------- #
# wire protocol
# --------------------------------------------------------------------------- #
def test_hunter_message_round_trips_the_wire():
    record = make_record(worked_count=4, lamport=7)
    _, _, decoded = decode(encode(HunterMessage(hunter=record), NETWORK, "s1"))
    assert isinstance(decoded, HunterMessage)
    assert decoded.hunter == record


def test_hunter_sync_response_round_trips_the_wire():
    records = [make_record(call="W1AW"), make_record(call="K1ABC", station_id="s2")]
    _, _, decoded = decode(encode(HunterSyncResponse(hunters=records), NETWORK, "s1"))
    assert isinstance(decoded, HunterSyncResponse)
    assert decoded.hunters == records


# --------------------------------------------------------------------------- #
# session integration
# --------------------------------------------------------------------------- #
def pota_session(tmp_path, name="log"):
    return build_session(
        contest_id="pota",
        my_call="N0AW",
        sent_exchange={},
        network=None,
        extra={"park": "US-1234"},
        db_path=tmp_path / f"{name}.sqlite",
        hunters_db=tmp_path / "pota_hunters.sqlite",
    )


def test_logging_a_pota_qso_rosters_the_hunter(tmp_path):
    session = pota_session(tmp_path)
    session.record_qso(call="W1AW", freq_hz=FREQ["20m"], mode=Mode.CW, exchange={})

    hunter = session.hunter("W1AW")
    assert hunter is not None
    assert hunter.worked_count == 1
    assert hunter.band == "20m"  # derived from the QSO's frequency
    assert hunter.mode == Mode.CW.value
    assert hunter.name == ""  # nothing known until QRZ supplies one


def test_reworking_the_same_hunter_increments(tmp_path):
    session = pota_session(tmp_path)
    for _ in range(3):
        session.record_qso(call="W1AW", freq_hz=FREQ["20m"], mode=Mode.CW, exchange={})
    assert session.hunter("W1AW").worked_count == 3


def test_session_name_fill_and_lookup(tmp_path):
    session = pota_session(tmp_path)
    session.record_qso(call="W1AW", freq_hz=FREQ["20m"], mode=Mode.CW, exchange={})
    assert session.hunter_needs_name("W1AW") is True

    assert session.set_hunter_name("W1AW", "Hiram") is not None
    assert session.hunter("W1AW").name == "Hiram"
    assert session.hunter_needs_name("W1AW") is False


def test_roster_survives_into_a_second_log(tmp_path):
    """The whole point of an app-level roster: a new log still knows the regulars."""
    first = pota_session(tmp_path, name="june")
    first.record_qso(call="W1AW", freq_hz=FREQ["20m"], mode=Mode.CW, exchange={})
    first.set_hunter_name("W1AW", "Hiram")

    second = pota_session(tmp_path, name="august")
    hunter = second.hunter("W1AW")
    assert hunter is not None
    assert hunter.name == "Hiram"
    second.record_qso(call="W1AW", freq_hz=FREQ["40m"], mode=Mode.USB, exchange={})
    assert second.hunter("W1AW").worked_count == 2  # carried over and incremented


def test_reopened_log_keeps_the_roster(tmp_path):
    path = tmp_path / "log.sqlite"
    hunters_db = tmp_path / "pota_hunters.sqlite"
    session = build_session(
        contest_id="pota", my_call="N0AW", sent_exchange={}, network=None,
        extra={"park": "US-1234"}, db_path=path, hunters_db=hunters_db,
    )
    session.record_qso(call="W1AW", freq_hz=FREQ["20m"], mode=Mode.CW, exchange={})
    session.store.close()

    reopened = open_session(path, hunters_db=hunters_db)
    assert reopened.hunter("W1AW").worked_count == 1


def test_field_day_does_not_touch_the_roster(tmp_path):
    """The roster is POTA-only — a Field Day log must not populate it."""
    session = build_session(
        contest_id="arrl-field-day", my_call="N0AW",
        sent_exchange={"class": "1D", "section": "CO"}, network=None,
        db_path=tmp_path / "fd.sqlite", hunters_db=tmp_path / "pota_hunters.sqlite",
    )
    session.record_qso(
        call="W1AW", freq_hz=FREQ["20m"], mode=Mode.CW,
        exchange={"class": "2A", "section": "EPA"},
    )
    assert session.hunters is None
    assert session.hunter("W1AW") is None
    assert session.hunters_by_worked() == []
    assert not (tmp_path / "pota_hunters.sqlite").exists()


# --------------------------------------------------------------------------- #
# peer-to-peer sync
# --------------------------------------------------------------------------- #
async def test_a_hunter_record_reaches_a_peer():
    bus = LoopbackBus()
    a = await make_engine(bus, "N0AW")
    b = await make_engine(bus, "W0CPH")

    record = make_record(station_id=a.station_id, worked_count=1, lamport=1)
    a.apply_hunter(record)
    await a.broadcast_hunter(record)
    await converge(a, b)

    assert b.hunters[(record.call, a.station_id)] == record


async def test_concurrent_work_sums_instead_of_overwriting():
    """Both stations work W1AW twice; every station must end up seeing four."""
    bus = LoopbackBus()
    a = await make_engine(bus, "N0AW")
    b = await make_engine(bus, "W0CPH")
    stores = {a.station_id: HunterStore(), b.station_id: HunterStore()}
    a.on_hunter = lambda r: stores[a.station_id].apply(r)
    b.on_hunter = lambda r: stores[b.station_id].apply(r)

    for engine in (a, b):
        for _ in (1, 2):
            record = stores[engine.station_id].worked(
                call="W1AW", station_id=engine.station_id,
                lamport=engine.clock.tick(), band="20m", mode=Mode.CW.value,
            )
            engine.apply_hunter(record)
            await engine.broadcast_hunter(record)
    await converge(a, b)

    for store in stores.values():
        assert store.get("W1AW").worked_count == 4
    for store in stores.values():
        store.close()


async def test_a_late_joiner_gets_the_whole_roster():
    bus = LoopbackBus()
    a = await make_engine(bus, "N0AW")
    record = make_record(station_id=a.station_id, worked_count=3, lamport=1)
    a.apply_hunter(record)

    b = await make_engine(bus, "W0CPH")
    await b.request_full_log()
    await converge(a, b)

    assert b.hunters[(record.call, a.station_id)].worked_count == 3


async def test_a_name_learned_by_one_station_reaches_the_others():
    bus = LoopbackBus()
    a = await make_engine(bus, "N0AW")
    b = await make_engine(bus, "W0CPH")
    store_b = HunterStore()
    b.on_hunter = lambda r: store_b.apply(r)

    # A works the call with no name, then QRZ supplies one.
    unnamed = make_record(station_id=a.station_id, name="", lamport=1)
    a.apply_hunter(unnamed)
    await a.broadcast_hunter(unnamed)
    named = make_record(station_id=a.station_id, name="Hiram", lamport=2)
    a.apply_hunter(named)
    await a.broadcast_hunter(named)
    await converge(a, b)

    assert store_b.get("W1AW").name == "Hiram"
    assert store_b.get("W1AW").worked_count == 1  # a rename is not a new contact
    store_b.close()


def test_hunter_dataclass_defaults_are_sane():
    hunter = Hunter(call="W1AW")
    assert hunter.worked_count == 0
    assert hunter.name == ""


# --------------------------------------------------------------------------- #
# editing: rename a call, correct a name
# --------------------------------------------------------------------------- #
def test_edit_corrects_a_name():
    store = HunterStore()
    store.worked(call="W1AW", station_id="s1", lamport=1, when=WHEN, name="Tim")
    changed = store.edit(
        old_call="W1AW", new_call="W1AW", name="Timothy", station_id="s1"
    )
    assert [r.name for r in changed] == ["Timothy"]
    assert store.get("W1AW").name == "Timothy"
    assert store.get("W1AW").worked_count == 1  # a correction is not a contact
    store.close()


def test_edit_renames_a_busted_call_and_keeps_the_history():
    store = HunterStore()
    for lamport in (1, 2, 3):
        store.worked(call="W1AV", station_id="s1", lamport=lamport, band="20m",
                     mode=Mode.CW.value, when=WHEN, name="Hiram")
    store.edit(old_call="W1AV", new_call="W1AW", name="Hiram", station_id="s1")

    assert store.get("W1AV") is None  # the old spelling is gone
    renamed = store.get("W1AW")
    assert renamed.worked_count == 3  # the count follows the rename
    assert renamed.name == "Hiram"
    assert renamed.band == "20m"
    store.close()


def test_renaming_onto_an_existing_call_merges_the_two():
    """Fixing a typo onto a call already in the roster must add the counts."""
    store = HunterStore()
    for lamport in (1, 2):
        store.worked(call="W1AV", station_id="s1", lamport=lamport, when=WHEN)
    later = datetime(2026, 8, 1, tzinfo=UTC)
    store.worked(call="W1AW", station_id="s1", lamport=3, band="40m",
                 mode=Mode.USB.value, when=later, name="Hiram")

    store.edit(old_call="W1AV", new_call="W1AW", name="", station_id="s1")

    merged = store.get("W1AW")
    assert merged.worked_count == 3  # 2 + 1, nothing lost
    assert merged.band == "40m"  # details from the more recent contact
    assert merged.mode == Mode.USB.value
    assert merged.last_worked == later
    assert merged.name == "Hiram"
    assert store.get("W1AV") is None
    assert len(store.records()) == 1  # the old row is really gone
    store.close()


def test_edit_applies_to_peer_rows_but_only_returns_our_own():
    """A correction shows up immediately, but we never re-stamp a peer's record."""
    store = HunterStore()
    store.worked(call="W1AV", station_id="s1", lamport=1, when=WHEN)
    store.apply(make_record(call="W1AV", station_id="s2", worked_count=4, lamport=9))

    changed = store.edit(
        old_call="W1AV", new_call="W1AW", name="Hiram", station_id="s1"
    )
    assert [r.station_id for r in changed] == ["s1"]  # only ours is broadcast
    renamed = store.get("W1AW")
    assert renamed.worked_count == 5  # both rows followed the rename locally
    assert renamed.name == "Hiram"
    assert {r.station_id for r in store.records()} == {"s1", "s2"}
    # The peer's row keeps its own lamport, so a genuine later update still wins.
    peer = next(r for r in store.records() if r.station_id == "s2")
    assert peer.lamport == 9
    store.close()


def test_edit_of_an_unknown_call_changes_nothing():
    store = HunterStore()
    assert store.edit(old_call="N0ONE", new_call="W1AW", name="X", station_id="s1") == []
    assert store.all() == []
    store.close()


def test_next_lamport_advances_past_this_stations_highest():
    store = HunterStore()
    store.worked(call="W1AW", station_id="s1", lamport=7, when=WHEN)
    store.apply(make_record(call="K1ABC", station_id="s2", lamport=99))
    assert store.next_lamport("s1") == 8  # ignores the peer's clock
    assert store.next_lamport("s3") == 1  # a station with no rows starts at 1
    store.close()


def test_session_edit_queues_the_change_for_broadcast(tmp_path):
    session = pota_session(tmp_path)
    session.record_qso(call="W1AV", freq_hz=FREQ["20m"], mode=Mode.CW, exchange={})

    changed = session.edit_hunter("W1AV", "W1AW", "Hiram")
    assert len(changed) == 1
    assert session.hunter("W1AW").name == "Hiram"
    assert session.hunter("W1AV") is None
    # The superseded call must not linger in the roster the engine serves peers.
    assert not [k for k in session.engine.hunters if k[0] == "W1AV"]
    assert [(k[0]) for k in session.engine.hunters] == ["W1AW"]


def test_session_edit_is_a_no_op_off_pota(tmp_path):
    session = build_session(
        contest_id="arrl-field-day", my_call="N0AW",
        sent_exchange={"class": "1D", "section": "CO"}, network=None,
        db_path=tmp_path / "fd.sqlite", hunters_db=tmp_path / "pota_hunters.sqlite",
    )
    assert session.edit_hunter("W1AV", "W1AW", "Hiram") == []


# --------------------------------------------------------------------------- #
# the Tools -> Edit POTA Hunters... dialog
# --------------------------------------------------------------------------- #
def hunters_dialog(store, station_id="s1"):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from partyhams.ui.hunters_dialog import HuntersDialog

    QApplication.instance() or QApplication([])
    return HuntersDialog(
        store.all,
        lambda old, call, name: store.edit(
            old_call=old, new_call=call, name=name, station_id=station_id
        ),
    )


def test_dialog_lists_the_roster_most_worked_first():
    store = HunterStore()
    store.worked(call="W1AW", station_id="s1", lamport=1, band="20m",
                 mode=Mode.CW.value, when=WHEN, name="Hiram")
    for lamport in (2, 3):
        store.worked(call="K1ABC", station_id="s1", lamport=lamport, band="40m",
                     mode=Mode.USB.value, when=WHEN)

    dialog = hunters_dialog(store)
    assert dialog._table.rowCount() == 2
    assert dialog._table.item(0, 0).text() == "K1ABC"  # worked twice
    assert dialog._table.item(0, 2).text() == "2"
    assert dialog._table.item(1, 0).text() == "W1AW"
    assert dialog._table.item(1, 1).text() == "Hiram"
    store.close()


def test_dialog_only_lets_call_and_name_be_edited():
    from PySide6.QtCore import Qt

    store = HunterStore()
    store.worked(call="W1AW", station_id="s1", lamport=1, when=WHEN)
    dialog = hunters_dialog(store)
    editable = [
        col
        for col in range(dialog._table.columnCount())
        if dialog._table.item(0, col).flags() & Qt.ItemFlag.ItemIsEditable
    ]
    assert editable == [0, 1]  # Call and Name; the tallies are read-only
    store.close()


def test_dialog_saves_a_name_edit_to_the_database():
    store = HunterStore()
    store.worked(call="W1AW", station_id="s1", lamport=1, when=WHEN)
    dialog = hunters_dialog(store)
    dialog._table.item(0, 1).setText("Hiram")
    dialog._save()

    assert store.get("W1AW").name == "Hiram"
    assert dialog._pending() == []  # the table reloaded from the store
    store.close()


def test_dialog_saves_a_call_edit_and_reloads():
    store = HunterStore()
    store.worked(call="W1AV", station_id="s1", lamport=1, when=WHEN)
    dialog = hunters_dialog(store)
    dialog._table.item(0, 0).setText("W1AW")
    dialog._save()

    assert store.get("W1AW") is not None
    assert store.get("W1AV") is None
    assert dialog._table.item(0, 0).text() == "W1AW"
    store.close()


def test_dialog_refuses_to_save_a_blank_call():
    store = HunterStore()
    store.worked(call="W1AW", station_id="s1", lamport=1, when=WHEN)
    dialog = hunters_dialog(store)
    dialog._table.item(0, 0).setText("")
    dialog._save()

    assert store.get("W1AW") is not None  # untouched
    assert "can't be blank" in dialog._status.text()
    store.close()


def test_dialog_handles_an_empty_roster():
    store = HunterStore()
    dialog = hunters_dialog(store)
    assert dialog._table.rowCount() == 0
    assert "No hunters yet" in dialog._status.text()
    dialog._save()
    assert "Nothing to save" in dialog._status.text()
    store.close()


def test_dialog_uppercases_a_lowercased_call_edit():
    store = HunterStore()
    store.worked(call="W1AV", station_id="s1", lamport=1, when=WHEN)
    dialog = hunters_dialog(store)
    dialog._table.item(0, 0).setText("w1aw")
    dialog._save()
    assert store.get("W1AW") is not None
    store.close()


# --------------------------------------------------------------------------- #
# {HUNTERNAME} macro substitution
# --------------------------------------------------------------------------- #
def pota_window(tmp_path):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from partyhams.ui.main_window import MainWindow

    QApplication.instance() or QApplication([])
    window = MainWindow(pota_session(tmp_path))
    window.refresh()
    return window


def test_huntername_is_the_rostered_first_name(tmp_path):
    from partyhams.app.macros import expand

    window = pota_window(tmp_path)
    session = window.session
    session.record_qso(call="W1AW", freq_hz=FREQ["20m"], mode=Mode.CW, exchange={})
    session.set_hunter_name("W1AW", "Tim")

    window._call.setText("W1AW")
    # Upper-cased for the keyer, though the roster stores "Tim" for the editor.
    assert window._macro_context()["HUNTERNAME"] == "TIM"
    assert session.hunter("W1AW").name == "Tim"
    text, _ = expand("TU {HUNTERNAME} 5NN", window._macro_context())
    assert text == "TU TIM 5NN"


def test_huntername_is_empty_for_a_station_not_on_the_roster(tmp_path):
    from partyhams.app.macros import expand

    window = pota_window(tmp_path)
    window._call.setText("N0ONE")
    assert window._macro_context()["HUNTERNAME"] == ""
    # The gap left behind must collapse, so one macro serves both cases.
    text, _ = expand("TU {HUNTERNAME} 5NN", window._macro_context())
    assert text == "TU 5NN"


def test_huntername_is_empty_when_a_hunter_has_no_name_yet(tmp_path):
    window = pota_window(tmp_path)
    window.session.record_qso(call="W1AW", freq_hz=FREQ["20m"], mode=Mode.CW, exchange={})
    window._call.setText("W1AW")  # on the roster, but QRZ gave us no name
    assert window._macro_context()["HUNTERNAME"] == ""


def test_huntername_is_empty_with_no_call_entered(tmp_path):
    window = pota_window(tmp_path)
    window._call.setText("")
    assert window._macro_context()["HUNTERNAME"] == ""


def test_huntername_tracks_the_call_field(tmp_path):
    window = pota_window(tmp_path)
    session = window.session
    for call, name in (("W1AW", "Tim"), ("K1ABC", "Ann")):
        session.record_qso(call=call, freq_hz=FREQ["20m"], mode=Mode.CW, exchange={})
        session.set_hunter_name(call, name)

    window._call.setText("k1abc")  # lower case in the field still matches
    assert window._macro_context()["HUNTERNAME"] == "ANN"
    window._call.setText("W1AW")
    assert window._macro_context()["HUNTERNAME"] == "TIM"


def test_huntername_is_empty_off_pota(tmp_path):
    """A Field Day log has no roster, so the token must resolve to nothing."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from partyhams.ui.main_window import MainWindow

    QApplication.instance() or QApplication([])
    session = build_session(
        contest_id="arrl-field-day", my_call="N0AW",
        sent_exchange={"class": "1D", "section": "CO"}, network=None,
        db_path=tmp_path / "fd.sqlite", hunters_db=tmp_path / "pota_hunters.sqlite",
    )
    window = MainWindow(session)
    window.refresh()
    window._call.setText("W1AW")
    assert window._macro_context()["HUNTERNAME"] == ""


def test_gmae_is_in_the_macro_context(tmp_path):
    """The window supplies {GMAE} from the local clock, alongside {HUNTERNAME}."""
    window = pota_window(tmp_path)
    assert window._macro_context()["GMAE"] in {"GM", "GA", "GE"}


def test_gmae_and_huntername_combine_in_one_greeting(tmp_path):
    from unittest.mock import patch

    from partyhams.app.macros import expand

    window = pota_window(tmp_path)
    session = window.session
    session.record_qso(call="W1AW", freq_hz=FREQ["20m"], mode=Mode.CW, exchange={})
    session.set_hunter_name("W1AW", "Tim")
    window._call.setText("W1AW")

    with patch("partyhams.ui.main_window.greeting_for", return_value="GA"):
        text, _ = expand("{GMAE} {HUNTERNAME} {CALL} 5NN", window._macro_context())
    assert text == "GA TIM W1AW 5NN"

    # ...and the same macro still reads correctly for an unknown station.
    window._call.setText("N0ONE")
    with patch("partyhams.ui.main_window.greeting_for", return_value="GE"):
        text, _ = expand("{GMAE} {HUNTERNAME} {CALL} 5NN", window._macro_context())
    assert text == "GE N0ONE 5NN"
