"""General logging mode: no contest rules, RST + comment entry, roster, export."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from factories import FREQ  # noqa: E402

from partyhams.app.session import build_session, open_session  # noqa: E402
from partyhams.contest import available, get  # noqa: E402
from partyhams.core.models import Mode  # noqa: E402
from partyhams.db.store import SqliteLog  # noqa: E402
from partyhams.export import write_adif  # noqa: E402


def general_session(tmp_path, name="log"):
    return build_session(
        contest_id="general",
        my_call="N0AW",
        sent_exchange={},
        network=None,
        db_path=tmp_path / f"{name}.sqlite",
        hunters_db=tmp_path / "pota_hunters.sqlite",
    )


def window(session):
    from PySide6.QtWidgets import QApplication

    from partyhams.ui.main_window import MainWindow

    QApplication.instance() or QApplication([])
    w = MainWindow(session)
    w.refresh()
    return w


# --------------------------------------------------------------------------- #
# the contest definition
# --------------------------------------------------------------------------- #
def test_general_is_offered_in_the_activity_picker():
    assert ("general", "General") in available()


def test_general_has_no_setup_or_exchange_fields():
    """This is what leaves the new-log dialog's lower section empty."""
    general = get("general")
    assert general.config_fields() == []
    assert general.exchange_fields() == []


def test_general_exchanges_rst():
    assert get("general").exchanges_rst is True


def test_general_allows_every_band_including_warc():
    bands = get("general").allowed_bands()
    assert {"160m", "20m", "6m", "2m"} <= bands
    assert {"30m", "17m", "12m"} <= bands  # WARC, excluded from real contests


def test_general_never_reports_a_dupe(tmp_path):
    session = general_session(tmp_path)
    for _ in range(3):
        session.record_qso(call="W1AW", freq_hz=FREQ["20m"], mode=Mode.CW, exchange={})
    # Same call, same band, same mode, same day — all three are real contacts.
    assert session.is_dupe("W1AW", FREQ["20m"], Mode.CW) is False
    assert session.dupe_label("W1AW", FREQ["20m"], Mode.CW) == ""
    assert session.score().qso_count == 3


def test_general_score_is_a_plain_qso_count(tmp_path):
    session = general_session(tmp_path)
    for call in ("W1AW", "K1ABC", "W1AW"):
        session.record_qso(call=call, freq_hz=FREQ["20m"], mode=Mode.CW, exchange={})
    summary = session.score()
    assert summary.qso_count == 3
    assert summary.total == 3


# --------------------------------------------------------------------------- #
# the comment column: schema, migration, persistence, sync
# --------------------------------------------------------------------------- #
def test_comment_round_trips_through_the_store(tmp_path):
    path = tmp_path / "log.sqlite"
    session = build_session(
        contest_id="general", my_call="N0AW", sent_exchange={}, network=None,
        db_path=path, hunters_db=tmp_path / "h.sqlite",
    )
    session.record_qso(
        call="W1AW", freq_hz=FREQ["20m"], mode=Mode.CW, exchange={},
        comment="Nice ragchew about antennas",
    )
    session.store.close()

    reopened = open_session(path, hunters_db=tmp_path / "h.sqlite")
    assert reopened.recent(1)[0].comment == "Nice ragchew about antennas"


def test_an_older_log_without_the_comment_column_migrates(tmp_path):
    """A log file created before this change must open and read cleanly."""
    path = Path(tmp_path) / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE qso (
            uuid TEXT PRIMARY KEY, station_id TEXT NOT NULL, operator TEXT NOT NULL,
            station_callsign TEXT NOT NULL DEFAULT '',
            lamport INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
            call TEXT NOT NULL, timestamp TEXT NOT NULL, freq_hz INTEGER NOT NULL,
            mode TEXT NOT NULL, rst_sent TEXT NOT NULL DEFAULT '',
            rst_rcvd TEXT NOT NULL DEFAULT '', serial_sent INTEGER,
            exchange_rcvd TEXT NOT NULL DEFAULT '{}',
            exchange_sent TEXT NOT NULL DEFAULT '{}');
        INSERT INTO qso (uuid, station_id, operator, lamport, deleted, call, timestamp,
                         freq_hz, mode)
        VALUES ('u1', 's1', 'N0AW', 1, 0, 'K1ABC', '2026-06-07T18:00:00+00:00',
                14040000, 'CW');
        """
    )
    conn.commit()
    conn.close()

    log = SqliteLog(path)
    rows = log.all()
    assert len(rows) == 1
    assert rows[0].comment == ""  # the new column defaults blank
    assert rows[0].call == "K1ABC"
    # ...and the migrated file accepts a comment from then on.
    from factories import make_qso

    log.upsert(make_qso("W1AW", uuid="u2"))
    log.close()


def test_comment_survives_the_wire():
    from factories import make_qso

    from partyhams.net.protocol import QsoMessage, decode, encode, qso_from_wire, qso_to_wire

    qso = make_qso("W1AW")
    qso.comment = "Ran 5 W to a wet string"
    assert qso_from_wire(qso_to_wire(qso)).comment == "Ran 5 W to a wet string"
    _, _, msg = decode(encode(QsoMessage(qso=qso), "net", "s1"))
    assert msg.qso.comment == "Ran 5 W to a wet string"


def test_a_peer_without_the_comment_field_still_decodes():
    """An older station's QSO datagram has no comment key — it must not blow up."""
    from factories import make_qso

    from partyhams.net.protocol import qso_from_wire, qso_to_wire

    wire = qso_to_wire(make_qso("W1AW"))
    del wire["comment"]
    assert qso_from_wire(wire).comment == ""


# --------------------------------------------------------------------------- #
# entry row: RST + comment
# --------------------------------------------------------------------------- #
def test_entry_row_has_rst_and_comment_in_general(tmp_path):
    w = window(general_session(tmp_path))
    assert w._rst_sent is not None
    assert w._rst_rcvd is not None
    assert w._comment is not None
    assert "Comment" in w._columns


def test_rst_boxes_default_to_the_modes_report(tmp_path):
    w = window(general_session(tmp_path))
    w._mode.setCurrentIndex(w._mode.findData(Mode.CW))
    assert w._rst_sent.text() == "599"
    w._mode.setCurrentIndex(w._mode.findData(Mode.USB))
    assert w._rst_sent.text() == "59"  # phone
    assert w._rst_rcvd.text() == "59"


def test_logging_uses_the_typed_rst_and_comment(tmp_path):
    w = window(general_session(tmp_path))
    w._call.setText("W1AW")
    w._rst_sent.setText("579")
    w._rst_rcvd.setText("339")
    w._comment.setText("QRN heavy, 5 W")
    w._try_log()

    qso = w.session.recent(1)[0]
    assert qso.call == "W1AW"
    assert qso.rst_sent == "579"
    assert qso.rst_rcvd == "339"
    assert qso.comment == "QRN heavy, 5 W"


def test_entry_fields_are_cleared_and_rst_reset_after_logging(tmp_path):
    w = window(general_session(tmp_path))
    w._call.setText("W1AW")
    w._rst_rcvd.setText("339")
    w._comment.setText("first")
    w._try_log()

    assert w._call.text() == ""
    assert w._comment.text() == ""
    assert w._rst_rcvd.text() == "599"  # back to the mode default, not blank


def test_an_emptied_rst_box_logs_the_default_not_a_blank(tmp_path):
    w = window(general_session(tmp_path))
    w._call.setText("W1AW")
    w._rst_sent.setText("")
    w._rst_rcvd.setText("")
    w._try_log()

    qso = w.session.recent(1)[0]
    assert qso.rst_sent == "599"
    assert qso.rst_rcvd == "599"


def test_tab_order_walks_call_then_rst_then_comment(tmp_path):
    w = window(general_session(tmp_path))
    assert w._entry_fields[0] is w._call
    assert w._entry_fields[-1] is w._comment
    assert w._rst_sent in w._entry_fields
    assert w._rst_rcvd in w._entry_fields


def test_comment_is_not_upper_cased(tmp_path):
    """It's prose, unlike every other entry field."""
    w = window(general_session(tmp_path))
    w._call.setText("W1AW")
    w._comment.setText("Worked from the cabin")
    w._try_log()
    assert w.session.recent(1)[0].comment == "Worked from the cabin"


def test_wipe_clears_the_comment_and_restores_rst(tmp_path):
    w = window(general_session(tmp_path))
    w._comment.setText("scratch")
    w._rst_rcvd.setText("111")
    w._wipe_entry()
    assert w._comment.text() == ""
    assert w._rst_rcvd.text() == "599"


# --------------------------------------------------------------------------- #
# the other contests are unchanged
# --------------------------------------------------------------------------- #
def test_pota_gains_rst_boxes_but_no_comment(tmp_path):
    session = build_session(
        contest_id="pota", my_call="N0AW", sent_exchange={}, network=None,
        extra={"park": "US-1234"}, db_path=tmp_path / "p.sqlite",
        hunters_db=tmp_path / "h.sqlite",
    )
    w = window(session)
    assert w._rst_sent is not None  # POTA exchanges a real report
    assert w._comment is None  # ...but no comment box
    assert "Comment" not in w._columns


def test_field_day_entry_row_is_untouched(tmp_path):
    session = build_session(
        contest_id="arrl-field-day", my_call="N0AW",
        sent_exchange={"class": "1D", "section": "CO"}, network=None,
        db_path=tmp_path / "fd.sqlite", hunters_db=tmp_path / "h.sqlite",
    )
    w = window(session)
    assert w._rst_sent is None  # Field Day exchanges no RST
    assert w._comment is None
    assert "Comment" not in w._columns
    assert "RST S" not in w._columns


# --------------------------------------------------------------------------- #
# hunter roster + ADIF
# --------------------------------------------------------------------------- #
def test_general_qsos_populate_the_hunter_roster(tmp_path):
    session = general_session(tmp_path)
    session.record_qso(call="W1AW", freq_hz=FREQ["20m"], mode=Mode.CW, exchange={})
    hunter = session.hunter("W1AW")
    assert hunter is not None
    assert hunter.worked_count == 1
    assert hunter.band == "20m"


def test_the_roster_is_shared_between_general_and_pota(tmp_path):
    """Someone met at a park and later worked casually is one roster entry."""
    hunters_db = tmp_path / "pota_hunters.sqlite"
    pota = build_session(
        contest_id="pota", my_call="N0AW", sent_exchange={}, network=None,
        extra={"park": "US-1234"}, db_path=tmp_path / "p.sqlite", hunters_db=hunters_db,
    )
    pota.record_qso(call="W1AW", freq_hz=FREQ["20m"], mode=Mode.CW, exchange={})
    pota.set_hunter_name("W1AW", "Tim")

    casual = build_session(
        contest_id="general", my_call="N0AW", sent_exchange={}, network=None,
        db_path=tmp_path / "g.sqlite", hunters_db=hunters_db,
    )
    assert casual.hunter("W1AW").name == "Tim"
    casual.record_qso(call="W1AW", freq_hz=FREQ["40m"], mode=Mode.USB, exchange={})
    assert casual.hunter("W1AW").worked_count == 2


def test_huntername_macro_works_in_general(tmp_path):
    from partyhams.app.macros import expand

    w = window(general_session(tmp_path))
    w.session.record_qso(call="W1AW", freq_hz=FREQ["20m"], mode=Mode.CW, exchange={})
    w.session.set_hunter_name("W1AW", "Tim")
    w._call.setText("W1AW")
    text, _ = expand("TU {HUNTERNAME}", w._macro_context())
    assert text == "TU TIM"


def test_comment_is_exported_as_adif_comment(tmp_path):
    session = general_session(tmp_path)
    session.record_qso(
        call="W1AW", freq_hz=FREQ["20m"], mode=Mode.CW, exchange={},
        comment="First QSO with the new vertical",
    )
    out = write_adif(session.recent(10), session.config, session.contest)
    assert "<COMMENT:31>First QSO with the new vertical" in out
    assert "<RST_SENT:3>599" in out


def test_a_blank_comment_is_omitted_from_adif(tmp_path):
    session = general_session(tmp_path)
    session.record_qso(call="W1AW", freq_hz=FREQ["20m"], mode=Mode.CW, exchange={})
    out = write_adif(session.recent(10), session.config, session.contest)
    assert "COMMENT" not in out
