"""Radio-selection screen — shown after a log is set up if no radio is configured.

Separate from log creation on purpose: the radio is a per-station hardware choice,
not part of the log/event. "None" is a valid, remembered answer (manual band/mode).

For FlexRadio the dialog auto-discovers radios on the LAN (populating a dropdown),
lets you type an IP directly, and offers a Verify button to test connectivity.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

# IC-705 / IC-7610: Icom proprietary UDP LAN protocol (requires username + password).
_UDP_LAN_KINDS = ("icom705-lan", "icom7610-lan")
# IC-7300 MK2: plain TCP CI-V connection (no username/password).
_TCP_CIV_LAN_KINDS = ("icom7300mk2-lan",)
# Combined set used where IP-address entry and a port field are needed.
_LAN_KINDS = _UDP_LAN_KINDS + _TCP_CIV_LAN_KINDS
# Radio kinds that use an Icom CI-V serial port.
_CIV_SERIAL_KINDS = ("icom705", "icom7610", "icom7300mk2")
_CIV_BAUD_RATES = (4800, 9600, 19200, 38400, 57600, 115200)
_CIV_DEFAULT_BAUD = 19200
_ICOM_LAN_DEFAULT_PORT = 50001


class _DiscoverWorker(QThread):
    """Runs a blocking Flex LAN discovery off the UI thread."""

    found = Signal(list)

    def run(self) -> None:
        from partyhams.radio.flex import discover_sync

        try:
            self.found.emit(discover_sync(timeout=2.0))
        except Exception:  # noqa: BLE001 - discovery never crashes the dialog
            self.found.emit([])


class _VerifyWorker(QThread):
    """Tests connectivity to a radio's control port off the UI thread."""

    done = Signal(bool, str)

    def __init__(self, host: str, kind: str, port: int, parent=None) -> None:
        super().__init__(parent)
        self._host = host
        self._kind = kind
        self._port = port

    def run(self) -> None:
        try:
            if self._kind in _UDP_LAN_KINDS:
                from partyhams.radio.icom_net import verify_connectivity
                ok = verify_connectivity(self._host, self._port, timeout=2.0)
            elif self._kind in _TCP_CIV_LAN_KINDS:
                from partyhams.radio.icom_tcp import verify_connectivity
                ok = verify_connectivity(self._host, self._port, timeout=2.0)
            else:
                from partyhams.radio.flex import verify_connectivity
                ok = verify_connectivity(self._host, timeout=2.0)
            self.done.emit(ok, self._host)
        except Exception:  # noqa: BLE001 - discovery never crashes the dialog
            self.done.emit(False, self._host)


class RadioDialog(QDialog):
    def __init__(self, current: dict | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("PartyHams Logger — Select Radio")
        self.setMinimumWidth(420)
        self._disc_worker: _DiscoverWorker | None = None
        self._verify_worker: _VerifyWorker | None = None

        self._radio = QComboBox()
        self._radio.addItem("None — manual band/mode", "none")
        self._radio.addItem("Hamlib (rigctld)", "hamlib")
        self._radio.addItem("FlexRadio (native)", "flex")
        self._radio.addItem("Icom IC-705 (CI-V serial)", "icom705")
        self._radio.addItem("Icom IC-7610 (CI-V serial)", "icom7610")
        self._radio.addItem("Icom IC-7300 MK2 (CI-V serial)", "icom7300mk2")
        self._radio.addItem("Icom IC-705 (LAN / Ethernet)", "icom705-lan")
        self._radio.addItem("Icom IC-7610 (LAN / Ethernet)", "icom7610-lan")
        self._radio.addItem("Icom IC-7300 MK2 (LAN / Ethernet)", "icom7300mk2-lan")
        self._conn = QLineEdit()
        self._conn.setEnabled(False)

        # CI-V serial baud rate (shown for icom*05 / icom*610 / icom*300mk2 serial kinds).
        self._baud = QComboBox()
        for rate in _CIV_BAUD_RATES:
            self._baud.addItem(str(rate), rate)
        self._baud.setCurrentIndex(_CIV_BAUD_RATES.index(_CIV_DEFAULT_BAUD))

        # Icom LAN control port (shown for *-lan kinds; Icom default is 50001).
        self._lan_port = QSpinBox()
        self._lan_port.setRange(1, 65535)
        self._lan_port.setValue(_ICOM_LAN_DEFAULT_PORT)

        self._user = QLineEdit()
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)

        # FlexRadio discovery row: a dropdown of found radios + a rescan button.
        self._flex_combo = QComboBox()
        self._flex_combo.addItem("(scan to find radios)", "")
        self._flex_combo.currentIndexChanged.connect(lambda _i: self._on_flex_pick())
        self._discover_btn = QPushButton("Rescan")
        self._discover_btn.clicked.connect(self._start_discovery)
        flex_row = QWidget()
        fr = QHBoxLayout(flex_row)
        fr.setContentsMargins(0, 0, 0, 0)
        fr.addWidget(self._flex_combo, stretch=1)
        fr.addWidget(self._discover_btn)
        self._flex_row = flex_row

        # Verify-connectivity row (IP-based connections).
        self._verify_btn = QPushButton("Verify")
        self._verify_btn.clicked.connect(self._verify)
        self._verify_status = QLabel("")
        verify_row = QWidget()
        vr = QHBoxLayout(verify_row)
        vr.setContentsMargins(0, 0, 0, 0)
        vr.addWidget(self._verify_btn)
        vr.addWidget(self._verify_status, stretch=1)
        self._verify_row = verify_row

        if current:
            idx = self._radio.findData(current.get("kind", "none"))
            if idx >= 0:
                self._radio.setCurrentIndex(idx)
            self._conn.setText(current.get("conn", ""))
            saved_baud = current.get("baud", _CIV_DEFAULT_BAUD)
            baud_idx = self._baud.findData(saved_baud)
            if baud_idx >= 0:
                self._baud.setCurrentIndex(baud_idx)
            self._lan_port.setValue(current.get("port", _ICOM_LAN_DEFAULT_PORT))
            self._user.setText(current.get("user", ""))
            self._password.setText(current.get("password", ""))

        info = QLabel(
            "Choose how to read frequency/mode from your rig. You can change this "
            "later, and 'None' is fine — you'll just pick band/mode manually."
        )
        info.setWordWrap(True)

        outer = QVBoxLayout(self)
        outer.addWidget(info)
        self._form = QFormLayout()
        self._form.addRow("Radio", self._radio)
        self._form.addRow("Discovered", self._flex_row)
        self._form.addRow("Connection", self._conn)
        self._form.addRow("Baud rate", self._baud)
        self._form.addRow("Port", self._lan_port)
        self._form.addRow("", self._verify_row)
        self._form.addRow("Username", self._user)
        self._form.addRow("Password", self._password)
        outer.addLayout(self._form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        # Connect only after the form exists — restoring `current` above can fire it.
        self._radio.currentIndexChanged.connect(lambda _i: self._on_radio_changed())
        self.finished.connect(lambda _r: self._stop_workers())
        self._on_radio_changed()

    def _stop_workers(self) -> None:
        # Let in-flight discovery/verify threads finish so they aren't destroyed
        # mid-run (Qt aborts on that). Both are bounded to a couple of seconds.
        for worker in (self._disc_worker, self._verify_worker):
            if worker is not None and worker.isRunning():
                worker.wait(3000)

    # ------------------------------------------------------------------ #
    # per-kind visibility + placeholders
    # ------------------------------------------------------------------ #
    def _on_radio_changed(self) -> None:
        kind = self._radio.currentData()
        is_udp_lan = kind in _UDP_LAN_KINDS
        is_tcp_civ_lan = kind in _TCP_CIV_LAN_KINDS
        is_civ_serial = kind in _CIV_SERIAL_KINDS
        is_flex = kind == "flex"
        has_lan_port = is_udp_lan or is_tcp_civ_lan
        ip_based = is_flex or has_lan_port or kind == "hamlib"
        self._conn.setEnabled(kind != "none")
        self._form.setRowVisible(self._flex_row, is_flex)
        self._form.setRowVisible(self._baud, is_civ_serial)
        self._form.setRowVisible(self._lan_port, has_lan_port)
        self._form.setRowVisible(self._verify_row, ip_based)
        self._form.setRowVisible(self._user, is_udp_lan)
        self._form.setRowVisible(self._password, is_udp_lan)
        self._verify_status.setText("")
        if kind == "hamlib":
            self._conn.setPlaceholderText("rigctld host:port (default 127.0.0.1:4532)")
        elif is_flex:
            self._conn.setPlaceholderText("radio IP (blank = auto-discover)")
        elif is_civ_serial:
            self._conn.setPlaceholderText("serial port (e.g. /dev/cu.usbmodem…)")
        elif is_tcp_civ_lan:
            self._conn.setPlaceholderText("radio IP or hostname")
        elif is_udp_lan:
            self._conn.setPlaceholderText("radio IP or hostname (Network function must be On)")
        else:
            self._conn.setPlaceholderText("")
        if is_flex and self._flex_combo.count() <= 1:
            self._start_discovery()  # auto-discover on first switch to Flex

    # ------------------------------------------------------------------ #
    # Flex discovery + verify
    # ------------------------------------------------------------------ #
    def _start_discovery(self) -> None:
        if self._disc_worker is not None and self._disc_worker.isRunning():
            return
        self._discover_btn.setEnabled(False)
        self._verify_status.setText("Scanning…")
        self._disc_worker = _DiscoverWorker(self)
        self._disc_worker.found.connect(self._on_discovered)
        self._disc_worker.start()

    def _on_discovered(self, radios: list) -> None:
        self._discover_btn.setEnabled(True)
        keep = self._conn.text().strip()
        self._flex_combo.blockSignals(True)
        self._flex_combo.clear()
        if radios:
            self._flex_combo.addItem("— pick a discovered radio —", "")
            for r in radios:
                self._flex_combo.addItem(r.label(), r.ip)
            self._verify_status.setText(f"Found {len(radios)} radio(s)")
        else:
            self._flex_combo.addItem("(none found — enter an IP)", "")
            self._verify_status.setText("No radios found")
        self._flex_combo.blockSignals(False)
        self._conn.setText(keep)  # don't clobber a manually typed IP

    def _on_flex_pick(self) -> None:
        ip = self._flex_combo.currentData()
        if ip:
            self._conn.setText(ip)

    def _verify(self) -> None:
        host, _, _ = self._conn.text().strip().partition(":")
        if not host:
            self._verify_status.setText("Enter an IP to verify")
            return
        if self._verify_worker is not None and self._verify_worker.isRunning():
            return
        kind = self._radio.currentData()
        port = self._lan_port.value() if kind in _LAN_KINDS else 0
        self._verify_btn.setEnabled(False)
        self._verify_status.setText("Checking…")
        self._verify_worker = _VerifyWorker(host, kind, port, self)
        self._verify_worker.done.connect(self._on_verified)
        self._verify_worker.start()

    def _on_verified(self, ok: bool, host: str) -> None:
        self._verify_btn.setEnabled(True)
        self._verify_status.setText(f"✓ reachable: {host}" if ok else f"✗ no response: {host}")

    def settings(self) -> dict:
        return {
            "kind": self._radio.currentData(),
            "conn": self._conn.text().strip(),
            "baud": self._baud.currentData(),
            "port": self._lan_port.value(),
            "user": self._user.text().strip(),
            "password": self._password.text(),
        }
