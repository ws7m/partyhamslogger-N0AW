"""Edit dialog for a single logged QSO (Logs table → double-click).

Gathers the editable details of a contact — call, band, mode, the contest's
exchange fields, optional RST, and the UTC time — pre-filled from the QSO. It is
contest-aware (exchange fields and whether RST applies come from the session's
contest) and Qt-only: it collects values; the caller applies them via
``LogSession.update_qso``. The frequency is preserved exactly unless the band is
changed, in which case the band's midpoint is used (matching manual entry).
"""

from __future__ import annotations

from datetime import UTC, datetime

from PySide6.QtCore import QDateTime, QTimeZone
from PySide6.QtWidgets import (
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from partyhams.app.session import LogSession
from partyhams.core.models import QSO, Mode
from partyhams.ui import style
from partyhams.ui.widgets import make_upper

# Modes offered when editing — a superset covering anything we might have logged.
_EDIT_MODES = [
    Mode.CW,
    Mode.USB,
    Mode.LSB,
    Mode.FM,
    Mode.AM,
    Mode.RTTY,
    Mode.PSK31,
    Mode.FT8,
    Mode.FT4,
]


class QsoEditDialog(QDialog):
    def __init__(self, session: LogSession, qso: QSO, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session = session
        self._qso = qso
        self.setWindowTitle(f"Edit QSO — {qso.call}")
        self.setMinimumWidth(360)

        self._call = QLineEdit(qso.call)
        make_upper(self._call)

        self._band = QComboBox()
        bands = session.allowed_bands()
        for label in bands:
            self._band.addItem(label, label)
        idx = self._band.findData(qso.band_label)
        if idx >= 0:
            self._band.setCurrentIndex(idx)
        self._orig_band = qso.band_label

        self._mode = QComboBox()
        for mode in _EDIT_MODES:
            self._mode.addItem(mode.value, mode)
        mode_idx = self._mode.findData(qso.mode)
        if mode_idx >= 0:
            self._mode.setCurrentIndex(mode_idx)

        self._time = QDateTimeEdit()
        self._time.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self._time.setTimeZone(QTimeZone.utc())
        self._time.setDateTime(
            QDateTime.fromSecsSinceEpoch(int(qso.timestamp.timestamp()), QTimeZone.utc())
        )

        form = QFormLayout()
        form.addRow("Call", self._call)
        form.addRow("Band", self._band)
        form.addRow("Mode", self._mode)

        self._rst_sent = self._rst_rcvd = None
        if session.contest.exchanges_rst:
            self._rst_sent = QLineEdit(qso.rst_sent)
            self._rst_rcvd = QLineEdit(qso.rst_rcvd)
            form.addRow("RST Sent", self._rst_sent)
            form.addRow("RST Rcvd", self._rst_rcvd)

        self._exchange_edits: dict[str, QLineEdit] = {}
        for field in session.contest.exchange_fields():
            edit = QLineEdit(qso.exchange_rcvd.get(field.name, ""))
            make_upper(edit)
            self._exchange_edits[field.name] = edit
            form.addRow(field.label, edit)

        form.addRow("Time (UTC)", self._time)

        # Free-text note, for the contests that collect one (General logging).
        self._comment: QLineEdit | None = None
        if session.contest.id == "general":
            self._comment = QLineEdit(qso.comment)
            form.addRow("Comment", self._comment)

        # Validation problems (invalid section/class, missing call) shown on Save.
        self._error = QLabel()
        self._error.setStyleSheet(f"color: {style.DUPE}; font-weight: bold;")
        self._error.setWordWrap(True)
        self._error.hide()

        outer = QVBoxLayout(self)
        outer.addLayout(form)
        outer.addWidget(self._error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _errors(self) -> list[str]:
        """Validation problems with the current fields ([] when valid): a required
        call and per-field required/validator checks (e.g. an invalid section)."""
        problems: list[str] = []
        if not self._call.text().strip():
            problems.append("Callsign is required")
        for field in self._session.contest.exchange_fields():
            value = self._exchange_edits[field.name].text().strip().upper()
            if field.required and not value:
                problems.append(f"{field.label} is required")
            elif value and field.validator and not field.validator(value):
                problems.append(f"{field.label} '{value}' is invalid")
        return problems

    def accept(self) -> None:
        """Validate before closing; on failure show the problems and stay open."""
        problems = self._errors()
        if problems:
            self._error.setText("  •  ".join(problems))
            self._error.show()
            return
        super().accept()

    def _freq_hz(self) -> int:
        """Keep the QSO's exact frequency unless the band changed; then use the
        new band's midpoint (the same convention as manual entry)."""
        band_label = self._band.currentData()
        if band_label == self._orig_band:
            return self._qso.freq_hz
        from partyhams.core.models import band_by_label

        band = band_by_label(band_label)
        return (band.low_hz + band.high_hz) // 2 if band else self._qso.freq_hz

    def values(self) -> dict[str, object]:
        """The edited fields as ``LogSession.update_qso`` keyword arguments."""
        # The editor is UTC-spec (set in __init__), so the value is already UTC.
        when = datetime.fromtimestamp(self._time.dateTime().toSecsSinceEpoch(), tz=UTC)
        exchange = {
            name: e.text().strip().upper() for name, e in self._exchange_edits.items() if e.text()
        }
        out: dict[str, object] = {
            "call": self._call.text().strip().upper(),
            "freq_hz": self._freq_hz(),
            "mode": self._mode.currentData(),
            "exchange": exchange,
            "timestamp": when,
        }
        if self._rst_sent is not None and self._rst_rcvd is not None:
            out["rst_sent"] = self._rst_sent.text().strip()
            out["rst_rcvd"] = self._rst_rcvd.text().strip()
        if self._comment is not None:
            out["comment"] = self._comment.text().strip()
        return out
