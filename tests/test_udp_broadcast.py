"""One-way UDP log broadcast: datagram layout, payload, settings, and delivery."""

from __future__ import annotations

import os
import socket
import struct

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from partyhams.app.session import build_session  # noqa: E402
from partyhams.app.state import (  # noqa: E402
    AppState,
    clamp_port,
    is_valid_host,
    load_state,
    save_state,
)
from partyhams.wsjtx.broadcast import (  # noqa: E402
    ADIF_VERSION,
    BROADCAST_ID,
    PROGRAM_ID,
    LogBroadcaster,
    build_datagram,
    build_payload,
)
from partyhams.wsjtx.protocol import (  # noqa: E402
    MAGIC,
    TYPE_LOGGED_ADIF,
    encode_logged_adif,
)

# The exact 27 bytes the reference (Xojo) implementation writes, up to and
# including the first payload byte. Everything but the payload length is fixed.
REFERENCE_HEADER = bytes(
    [173, 188, 203, 218, 0, 0, 0, 2, 0, 0, 0, 12, 0, 0, 0, 6,
     87, 83, 74, 84, 45, 88]
)


def general_session(tmp_path):
    return build_session(
        contest_id="general", my_call="N0AW", sent_exchange={}, network=None,
        db_path=tmp_path / "g.sqlite", hunters_db=tmp_path / "h.sqlite",
    )


# --------------------------------------------------------------------------- #
# datagram layout
# --------------------------------------------------------------------------- #
def test_header_matches_the_reference_implementation_byte_for_byte():
    data = build_datagram("<CALL:4>W1AW<EOR>")
    assert data[:22] == REFERENCE_HEADER


def test_header_fields_decode_as_expected():
    data = build_datagram("<CALL:4>W1AW<EOR>")
    magic, schema, msg_type = struct.unpack(">III", data[:12])
    assert magic == MAGIC
    assert schema == 2
    assert msg_type == TYPE_LOGGED_ADIF == 12
    id_len = struct.unpack(">I", data[12:16])[0]
    assert id_len == 6
    assert data[16:22] == b"WSJT-X"


def test_payload_length_is_computed_not_fixed():
    """A hardcoded length only holds for one record; ours must track the payload."""
    short = build_datagram("<CALL:4>W1AW<EOR>")
    long = build_datagram("<CALL:5>K1ABC" + "<COMMENT:40>" + "x" * 40 + "<EOR>")
    short_len = struct.unpack(">I", short[22:26])[0]
    long_len = struct.unpack(">I", long[22:26])[0]
    assert short_len != long_len
    # ...and each declared length is exactly the bytes that follow it.
    assert short_len == len(short) - 26
    assert long_len == len(long) - 26


def test_payload_starts_with_a_newline_like_the_reference():
    data = build_datagram("<CALL:4>W1AW<EOR>")
    assert data[26:27] == b"\n"


def test_a_receiver_can_reconstruct_the_payload():
    record = "<CALL:4>W1AW<MODE:2>CW<EOR>"
    data = build_datagram(record)
    length = struct.unpack(">I", data[22:26])[0]
    payload = data[26 : 26 + length].decode("utf-8")
    assert payload == build_payload(record)
    assert payload.endswith(record + "\n")


def test_utf8_payload_length_counts_bytes_not_characters():
    data = build_datagram("<NAME:6>Renée<EOR>")  # é is two UTF-8 bytes
    length = struct.unpack(">I", data[22:26])[0]
    assert length == len(data) - 26


# --------------------------------------------------------------------------- #
# the ADIF payload
# --------------------------------------------------------------------------- #
def test_payload_carries_the_adif_header_and_the_record():
    payload = build_payload("<CALL:4>W1AW<EOR>")
    assert f"<adif_ver:{len(ADIF_VERSION)}>{ADIF_VERSION}" in payload
    assert f"<programid:{len(PROGRAM_ID)}>{PROGRAM_ID}" in payload
    assert "<EOH>" in payload
    assert payload.index("<EOH>") < payload.index("<CALL:4>W1AW")


def test_payload_is_a_parsable_mini_adif_document():
    payload = build_payload("<CALL:4>W1AW<EOR>")
    header, _, body = payload.partition("<EOH>")
    assert "adif_ver" in header and "programid" in header
    assert body.strip() == "<CALL:4>W1AW<EOR>"


def test_broadcast_id_is_wsjtx_so_receivers_recognize_it():
    assert BROADCAST_ID == "WSJT-X"
    assert encode_logged_adif(BROADCAST_ID, "x")[16:22] == b"WSJT-X"


# --------------------------------------------------------------------------- #
# the broadcaster
# --------------------------------------------------------------------------- #
def test_disabled_broadcaster_sends_nothing():
    caster = LogBroadcaster(enabled=False, host="127.0.0.1", port=1)
    assert caster.send("<CALL:4>W1AW<EOR>") is False
    assert caster.last_error is None  # not an error — just off


def test_a_real_datagram_arrives_on_a_loopback_socket():
    """End-to-end over a real UDP socket, not a mock."""
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(3.0)
    port = receiver.getsockname()[1]

    caster = LogBroadcaster(enabled=True, host="127.0.0.1", port=port)
    record = "<CALL:4>W1AW<MODE:2>CW<EOR>"
    assert caster.send(record) is True
    assert caster.last_error is None

    data, _ = receiver.recvfrom(65535)
    receiver.close()
    assert data[:22] == REFERENCE_HEADER
    length = struct.unpack(">I", data[22:26])[0]
    assert data[26 : 26 + length].decode("utf-8").endswith(record + "\n")


def test_configure_updates_the_target():
    caster = LogBroadcaster()
    caster.configure(True, "192.168.1.255", 2333)
    assert (caster.enabled, caster.host, caster.port) == (True, "192.168.1.255", 2333)


def test_a_blank_host_falls_back_to_the_default():
    caster = LogBroadcaster()
    caster.configure(True, "   ", 2237)
    assert caster.host == "127.0.0.1"


def test_an_unreachable_target_reports_instead_of_raising():
    caster = LogBroadcaster(enabled=True, host="203.0.113.0", port=1)
    caster.host = "not a hostname at all"
    assert caster.send("<CALL:4>W1AW<EOR>") is False
    assert caster.last_error and "failed" in caster.last_error


def test_an_oversized_record_is_refused_not_fragmented():
    caster = LogBroadcaster(enabled=True, host="127.0.0.1", port=9)
    assert caster.send("<X:70000>" + "y" * 70_000 + "<EOR>") is False
    assert "too large" in caster.last_error


# --------------------------------------------------------------------------- #
# settings persistence + validation
# --------------------------------------------------------------------------- #
def test_settings_default_to_off():
    state = AppState()
    assert state.udp_log_enabled is False
    assert state.udp_log_host == "127.0.0.1"
    assert state.udp_log_port == 2237


def test_settings_round_trip_globally(tmp_path):
    path = tmp_path / "state.json"
    save_state(
        AppState(udp_log_enabled=True, udp_log_host="192.168.1.255", udp_log_port=2333),
        path,
    )
    loaded = load_state(path)
    assert loaded.udp_log_enabled is True
    assert loaded.udp_log_host == "192.168.1.255"
    assert loaded.udp_log_port == 2333


def test_a_hand_edited_bad_port_is_clamped(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"udp_log_port": 999999}')
    assert load_state(path).udp_log_port == 65535
    path.write_text('{"udp_log_port": "wat"}')
    assert load_state(path).udp_log_port == 2237


def test_clamp_port_bounds():
    assert clamp_port(0) == 1
    assert clamp_port(70000) == 65535
    assert clamp_port(2237) == 2237
    assert clamp_port(None) == 2237


def test_host_validation():
    assert is_valid_host("127.0.0.1")
    assert is_valid_host("192.168.1.255")  # a broadcast address
    assert is_valid_host("gridtracker.local")
    assert not is_valid_host("")
    assert not is_valid_host("   ")
    assert not is_valid_host("192.168.1.999")  # out-of-range octet: a typo
    assert is_valid_host("  192.168.1.5  ")  # surrounding space is trimmed
    assert not is_valid_host("192.168.1.5 60")  # embedded space: not one host


# --------------------------------------------------------------------------- #
# the window: menu, dialog, and firing on a logged QSO
# --------------------------------------------------------------------------- #
def window(session):
    from PySide6.QtWidgets import QApplication

    from partyhams.ui.main_window import MainWindow

    QApplication.instance() or QApplication([])
    w = MainWindow(session)
    w.refresh()
    return w


def test_logs_menu_offers_the_setting(tmp_path):
    w = window(general_session(tmp_path))
    logs = next(m for m in w.menuBar().actions() if m.text() == "Logs").menu()
    assert "UDP Broadcast…" in [a.text() for a in logs.actions()]


def test_logging_a_qso_sends_a_datagram(tmp_path):
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(3.0)
    port = receiver.getsockname()[1]

    w = window(general_session(tmp_path))
    w.set_udp_log(True, "127.0.0.1", port)
    w._call.setText("W1AW")
    w._comment.setText("via UDP")
    w._try_log()

    data, _ = receiver.recvfrom(65535)
    receiver.close()
    payload = data[26:].decode("utf-8")
    assert data[:22] == REFERENCE_HEADER
    assert "<CALL:4>W1AW" in payload
    assert "<COMMENT:7>via UDP" in payload
    assert payload.rstrip().endswith("<EOR>")


def test_nothing_is_sent_while_disabled(tmp_path):
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(("127.0.0.1", 0))
    receiver.settimeout(0.4)
    port = receiver.getsockname()[1]

    w = window(general_session(tmp_path))
    w.set_udp_log(False, "127.0.0.1", port)
    w._call.setText("W1AW")
    w._try_log()

    try:
        data, _ = receiver.recvfrom(65535)
        raise AssertionError(f"expected no datagram, got {len(data)} bytes")
    except TimeoutError:
        pass
    finally:
        receiver.close()
    assert w.session.recent(1)[0].call == "W1AW"  # the QSO still logged


def test_a_failing_broadcast_never_blocks_the_log(tmp_path):
    w = window(general_session(tmp_path))
    w.set_udp_log(True, "no such host anywhere", 2237)
    w._call.setText("W1AW")
    w._try_log()
    assert w.session.recent(1)[0].call == "W1AW"  # logged regardless


def test_the_dialog_collects_and_validates(tmp_path):
    from PySide6.QtWidgets import QApplication

    from partyhams.ui.udp_log_dialog import UdpLogDialog

    QApplication.instance() or QApplication([])
    dialog = UdpLogDialog(True, "192.168.1.255", 2333)
    assert dialog.settings() == (True, "192.168.1.255", 2333)

    # A blank address while enabled must keep the dialog open.
    dialog._host.setText("")
    dialog.accept()
    assert dialog.result() != int(dialog.DialogCode.Accepted)
    # isHidden(), not isVisible(): the dialog itself was never shown, so no
    # child reports visible — isHidden() reflects the explicit show/hide.
    assert not dialog._error.isHidden()

    # ...but turning it off lets you close without fixing the address.
    dialog._enabled.setChecked(False)
    dialog.accept()
    assert dialog.result() == int(dialog.DialogCode.Accepted)


def test_the_dialog_greys_out_the_target_when_disabled():
    from PySide6.QtWidgets import QApplication

    from partyhams.ui.udp_log_dialog import UdpLogDialog

    QApplication.instance() or QApplication([])
    dialog = UdpLogDialog(False, "127.0.0.1", 2237)
    assert not dialog._host.isEnabled()
    assert not dialog._port.isEnabled()
    dialog._enabled.setChecked(True)
    assert dialog._host.isEnabled()
    assert dialog._port.isEnabled()


def test_broadcast_and_file_export_declare_the_same_format_and_program():
    """A datagram and an exported file must not advertise different versions."""
    from partyhams.export.adif import ADIF_VERSION as FILE_ADIF_VERSION

    assert ADIF_VERSION == FILE_ADIF_VERSION == "3.1.4"
    assert PROGRAM_ID == "PartyHams"

    payload = build_payload("<CALL:4>W1AW<EOR>")
    assert f"<adif_ver:5>{FILE_ADIF_VERSION}" in payload
    assert "<programid:9>PartyHams" in payload
