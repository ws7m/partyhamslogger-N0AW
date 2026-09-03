"""Settings dialog for the one-way UDP log broadcast (Logs → UDP Broadcast…).

Collects the three things the broadcast needs — on/off, where to send, and which
port — and validates the address before letting the dialog close, so a typo is
caught here rather than silently failing on every QSO. The address and port are
disabled while the feature is off.

Qt-only: it gathers values; the caller applies and persists them.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from partyhams.app.state import PORT_MAX, PORT_MIN, clamp_port, is_valid_host
from partyhams.ui import style
from partyhams.wsjtx.broadcast import DEFAULT_HOST, DEFAULT_PORT

_HELP = (
    "Announces every QSO you log as a WSJT-X-format ADIF datagram, so other "
    "software on your network (GridTracker, Log4OM, N3FJP, …) can pick it up. "
    "One-way and separate from multi-station log sync.\n\n"
    "Use 127.0.0.1 for a program on this computer, a machine's IP address to "
    "target it directly, or a broadcast address such as 192.168.1.255 to reach "
    "everything on the subnet."
)


class UdpLogDialog(QDialog):
    def __init__(
        self,
        enabled: bool,
        host: str,
        port: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("UDP Log Broadcast")
        self.setMinimumWidth(420)

        outer = QVBoxLayout(self)
        form = QFormLayout()

        self._enabled = QCheckBox("Broadcast each logged QSO over UDP")
        self._enabled.setChecked(enabled)
        form.addRow(self._enabled)

        self._host = QLineEdit(host or DEFAULT_HOST)
        self._host.setPlaceholderText(DEFAULT_HOST)
        form.addRow("Address:", self._host)

        self._port = QSpinBox()
        self._port.setRange(PORT_MIN, PORT_MAX)
        self._port.setValue(clamp_port(port, DEFAULT_PORT))
        form.addRow("Port:", self._port)
        outer.addLayout(form)

        help_label = QLabel(_HELP)
        help_label.setWordWrap(True)
        help_label.setStyleSheet(f"color: {style.TEXT_DIM};")
        outer.addWidget(help_label)

        self._error = QLabel()
        self._error.setStyleSheet(f"color: {style.DUPE}; font-weight: bold;")
        self._error.setWordWrap(True)
        self._error.hide()
        outer.addWidget(self._error)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self._enabled.toggled.connect(self._sync_enabled)
        self._sync_enabled(self._enabled.isChecked())

    def _sync_enabled(self, on: bool) -> None:
        self._host.setEnabled(on)
        self._port.setEnabled(on)

    def accept(self) -> None:
        """Validate the address before closing; an invalid one keeps us open.

        Only checked when the feature is on — someone turning it off shouldn't be
        blocked by an address they no longer care about.
        """
        if self._enabled.isChecked() and not is_valid_host(self._host.text()):
            self._error.setText(
                "Enter an address to send to — an IP like 192.168.1.50, a "
                "broadcast address like 192.168.1.255, or a hostname."
            )
            self._error.show()
            self._host.setFocus()
            return
        super().accept()

    def settings(self) -> tuple[bool, str, int]:
        """The chosen ``(enabled, host, port)``."""
        return (
            self._enabled.isChecked(),
            self._host.text().strip() or DEFAULT_HOST,
            self._port.value(),
        )
