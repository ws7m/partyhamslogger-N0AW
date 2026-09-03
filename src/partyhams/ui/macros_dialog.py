"""Editor for an event's F-key macros (per mode group), saved per contest."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from partyhams.app.macros import MacroSet
from partyhams.contest.base import ContestDefinition, Macro

_HELP = (
    "CW/digital: text with {MYCALL} {CALL} {EXCH} {RST} {GMAE} (GM/GA/GE by local "
    "time), plus {LOG} (log the QSO) and {WIPE} (clear entry). In POTA, "
    "{HUNTERNAME} is the hunter's first name from the roster (empty if they "
    "aren't on it). Phone: a .wav file path."
)
_BANK_LABELS = {
    "CW.RUN": "CW · Run",
    "CW.SP": "CW · S&P",
    "PHONE.RUN": "Phone · Run",
    "PHONE.SP": "Phone · S&P",
}


class MacrosDialog(QDialog):
    def __init__(
        self, macro_set: MacroSet, contest: ContestDefinition, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"PartyHams Logger — {contest.name} Macros")
        self.setMinimumWidth(560)

        # Work on an isolated copy until the user accepts.
        self._working: dict[str, list[Macro]] = {
            group: [Macro(m.key, m.label, m.content) for m in macros]
            for group, macros in macro_set.groups.items()
        }
        self._current_group = next(iter(self._working), "CW")

        outer = QVBoxLayout(self)
        top = QFormLayout()
        self._group = QComboBox()
        for group in self._working:
            self._group.addItem(_BANK_LABELS.get(group, group), group)
        self._wpm = QSpinBox()
        self._wpm.setRange(5, 60)
        self._wpm.setValue(macro_set.cw_wpm)
        top.addRow("Mode", self._group)
        top.addRow("CW speed (WPM)", self._wpm)
        outer.addLayout(top)

        self._table = QTableWidget(12, 3)
        self._table.setHorizontalHeaderLabels(["Key", "Label", "Content"])
        self._table.verticalHeader().setVisible(False)
        self._table.setColumnWidth(0, 44)
        self._table.setColumnWidth(1, 110)
        self._table.horizontalHeader().setStretchLastSection(True)
        outer.addWidget(self._table)

        help_label = QLabel(_HELP)
        help_label.setWordWrap(True)
        outer.addWidget(help_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self._group.currentIndexChanged.connect(self._on_group_changed)
        self._load_group(self._current_group)

    def _on_group_changed(self) -> None:
        self._flush_table_to(self._current_group)
        self._current_group = self._group.currentData()
        self._load_group(self._current_group)

    def _load_group(self, group: str) -> None:
        macros = {m.key: m for m in self._working.get(group, [])}
        for row in range(12):
            key = row + 1
            macro = macros.get(key, Macro(key, "", ""))
            key_item = QTableWidgetItem(f"F{key}")
            key_item.setFlags(key_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, 0, key_item)
            self._table.setItem(row, 1, QTableWidgetItem(macro.label))
            self._table.setItem(row, 2, QTableWidgetItem(macro.content))

    def _flush_table_to(self, group: str) -> None:
        macros: list[Macro] = []
        for row in range(12):
            label = self._item_text(row, 1)
            content = self._item_text(row, 2)
            macros.append(Macro(row + 1, label, content))
        self._working[group] = macros

    def _item_text(self, row: int, col: int) -> str:
        item = self._table.item(row, col)
        return item.text().strip() if item is not None else ""

    def result_macroset(self) -> MacroSet:
        self._flush_table_to(self._current_group)
        return MacroSet(cw_wpm=self._wpm.value(), groups=self._working)
