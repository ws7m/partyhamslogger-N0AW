"""Editor for the POTA hunter roster (Tools → Edit POTA Hunters…).

Lists everyone worked during a Parks on the Air activity and lets the operator
correct the two fields a lookup can get wrong: the callsign (a busted copy) and
the name (QRZ has the wrong one, or none at all). The worked count, band, mode,
and last-worked time are shown read-only — they are the log's account of what
happened, not something to hand-edit.

Qt-only by design, matching the other dialogs here: it collects edits and hands
each one to the ``apply_edit`` callback supplied by the caller, which owns the
store, the Lamport clock, and any peer broadcast.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from partyhams.hunters import Hunter
from partyhams.ui import style

_COLUMNS = ["Call", "Name", "Worked", "Band", "Mode", "Last worked (UTC)"]
_CALL_COL = 0
_NAME_COL = 1
_EDITABLE = (_CALL_COL, _NAME_COL)

_HELP = (
    "Edit a callsign or name, then Save. Renaming a call onto one already in the "
    "roster merges the two entries and adds their counts."
)


class HuntersDialog(QDialog):
    def __init__(
        self,
        load: Callable[[], list[Hunter]],
        apply_edit: Callable[[str, str, str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._load = load
        self._apply_edit = apply_edit
        #: Callsign as loaded, per row — the key an edit is applied against.
        self._original: list[tuple[str, str]] = []

        self.setWindowTitle("PartyHams Logger — POTA Hunters")
        self.setMinimumSize(620, 420)

        outer = QVBoxLayout(self)

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(_NAME_COL, QHeaderView.ResizeMode.Stretch)
        outer.addWidget(self._table)

        self._status = QLabel(_HELP)
        self._status.setWordWrap(True)
        outer.addWidget(self._status)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).clicked.connect(self._save)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.close)
        outer.addWidget(buttons)

        self.refresh()

    # ------------------------------------------------------------------ #
    # table
    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        """(Re)load the roster from the store, discarding any uncommitted edits."""
        hunters = self._load()
        self._original = [(h.call, h.name) for h in hunters]
        self._table.setRowCount(len(hunters))
        for row, hunter in enumerate(hunters):
            values = [
                hunter.call,
                hunter.name,
                str(hunter.worked_count),
                hunter.band,
                hunter.mode,
                hunter.last_worked.strftime("%Y-%m-%d %H:%M"),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col not in _EDITABLE:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row, col, item)
        for col in range(len(_COLUMNS)):
            if col != _NAME_COL:
                self._table.resizeColumnToContents(col)
        if not hunters:
            self._show(
                "No hunters yet — the roster fills in as you log POTA contacts.", error=False
            )

    def _text(self, row: int, col: int) -> str:
        item = self._table.item(row, col)
        return item.text().strip() if item is not None else ""

    def _pending(self) -> list[tuple[str, str, str]]:
        """Rows whose call or name was changed, as ``(old_call, call, name)``."""
        edits = []
        for row, (old_call, old_name) in enumerate(self._original):
            call = self._text(row, _CALL_COL).upper()
            name = self._text(row, _NAME_COL)
            if call != old_call or name != old_name:
                edits.append((old_call, call, name))
        return edits

    # ------------------------------------------------------------------ #
    # save / close
    # ------------------------------------------------------------------ #
    def _save(self) -> None:
        blank = [
            self._original[row][0]
            for row in range(len(self._original))
            if not self._text(row, _CALL_COL)
        ]
        if blank:
            self._show(
                f"A callsign can't be blank — restore it for {', '.join(blank)}.", error=True
            )
            return
        edits = self._pending()
        if not edits:
            self._show("Nothing to save.", error=False)
            return
        for old_call, call, name in edits:
            self._apply_edit(old_call, call, name)
        self.refresh()
        self._show(f"Saved {len(edits)} change{'s' if len(edits) != 1 else ''}.", error=False)

    def close(self) -> bool:
        """Close, confirming first if there are edits that were never saved."""
        if self._pending() and not self._confirm_discard():
            return False
        return super().close()

    def _confirm_discard(self) -> bool:
        answer = QMessageBox.question(
            self,
            "Discard changes?",
            "You have unsaved edits to the hunter roster. Close without saving?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Discard

    def _show(self, message: str, *, error: bool) -> None:
        self._status.setText(message)
        self._status.setStyleSheet(f"color: {style.DUPE}; font-weight: bold;" if error else "")
