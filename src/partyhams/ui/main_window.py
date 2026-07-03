"""The main logging window: score bar, keyboard-first entry row, and live log.

Binds to a :class:`~partyhams.app.session.LogSession`. The entry row is built
*from the contest's exchange schema*, so this same window serves any contest —
Field Day today, CQ WW tomorrow — with no UI changes.

Keyboard flow (N1MM-style): type the call, Enter advances to the next empty
field, Enter on the last field logs the QSO, clears, and refocuses the call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QActionGroup, QCloseEvent, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from partyhams.app.banter import BANTER_COOLDOWN_MIN, StationSnapshot, choose_message
from partyhams.app.macros import (
    CW_SPEED_LABELS,
    CW_SPEED_MODES,
    CW_SPEED_RESTORE,
    CW_SPEED_SYNC,
    WPM_MAX,
    WPM_MIN,
    ESMStep,
    bank_key,
    clamp_wpm,
    cw_duration_seconds,
    esm_step,
    expand,
    load_macros,
    normalize_cw_speed_mode,
    normalize_wpm_presets,
    save_macros,
)
from partyhams.app.radio import RadioPoller
from partyhams.app.session import LogSession, default_rst
from partyhams.app.update import (
    apply_update,
    check_for_update,
    clamp_interval_hours,
    download_asset,
    extract_bundle,
    is_asset_url,
    is_frozen,
)
from partyhams.contest.sections import is_valid_section
from partyhams.core.models import (
    QSO,
    Band,
    Mode,
    band_by_label,
    band_for_freq,
    mode_group_for,
    utcnow,
)
from partyhams.export import park_adif_name, timestamped_adif_name
from partyhams.qrz import QrzClient, format_record
from partyhams.radio.base import Capability, RadioState
from partyhams.refdata import RefData
from partyhams.ui import shortcuts as sc
from partyhams.ui import style
from partyhams.ui.about_dialog import AboutDialog
from partyhams.ui.cluster_window import ClusterWindow
from partyhams.ui.help_window import HelpWindow
from partyhams.ui.macros_dialog import MacrosDialog
from partyhams.ui.network_panel import NetworkPanel
from partyhams.ui.sections_window import SectionsWindow
from partyhams.ui.shortcuts import ShortcutsDialog
from partyhams.ui.widgets import make_upper
from partyhams.ui.wsjtx_panel import WsjtxPanel
from partyhams.wsjtx.callers import CallerTracker
from partyhams.wsjtx.convert import (
    map_mode,
    parse_tx_power,
    qso_logged_to_record,
    tx_even_from_epoch,
)
from partyhams.wsjtx.listener import WsjtxListener
from partyhams.wsjtx.protocol import Decode, QSOLogged, Status

# Modes offered in the entry row.
_ENTRY_MODES = [Mode.CW, Mode.USB, Mode.LSB, Mode.FM, Mode.RTTY, Mode.FT8, Mode.FT4]

# Default hint shown on the empty call field (replaced by live match/QSL hints).
_CALL_TOOLTIP = (
    "Type the worked station's callsign, then press Enter to advance. "
    "Hints (dupe, super-check-partial, QRZ) appear here as you type."
)


def _format_tx_status(word: str, key: int, label: str, text: str) -> str:
    """Build the transmit indicator shown on the left of the status bar.

    ``word`` is ``TRANSMITTING`` while sending, then ``SENT`` once done.
    """
    label_part = f" — {label}" if label else ""
    # key <= 0 marks a non-F-key send (e.g. a partial call) — omit the "Fn" tag.
    key_part = f" — F{key}" if key > 0 else ""
    return f"{word}{key_part}{label_part} — {text}"


#: Allowed Auto-CQ repeat intervals (seconds) and the clamp bounds.
AUTOCQ_INTERVALS = (5, 8, 10, 15, 20, 30)
AUTOCQ_MIN = 5
AUTOCQ_MAX = 30


def clamp_autocq_interval(seconds: int) -> int:
    """Clamp an Auto-CQ interval into the supported 5..30 second range."""
    return max(AUTOCQ_MIN, min(AUTOCQ_MAX, int(seconds)))


def should_autocq(run: bool, enabled: bool, call_text: str) -> bool:
    """Whether the Auto-CQ timer should fire F1 right now.

    Only in Run mode, only while enabled, and never while the operator has
    started entering a callsign (we don't keep CQ-ing while working someone).
    """
    return bool(run and enabled and not call_text.strip())


def _humanize_ago(delta: timedelta) -> str:
    """A compact "time since" label for the S&P dupe hint (e.g. ``"8 min ago"``)."""
    secs = max(0, int(delta.total_seconds()))
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins} min ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


#: Allowed periodic ADIF auto-export interval bounds (minutes).
AUTOEXPORT_MIN = 5
AUTOEXPORT_MAX = 60


def clamp_export_minutes(minutes: int) -> int:
    """Clamp an auto-export interval into the supported 5..60 minute range."""
    return max(AUTOEXPORT_MIN, min(AUTOEXPORT_MAX, int(minutes)))


def _fs_supports_at_sign(directory: Path | None = None) -> bool:
    """Best-effort: can the target filesystem hold an ``@`` in a filename?

    Probes by creating and deleting a tiny file with an ``@`` in its name in
    ``directory`` (falling back to the system temp dir). Any failure — ``@``
    rejected, dir missing, or not writable — returns ``False`` so we use the
    always-safe ``_`` separator instead.
    """
    import os
    import tempfile

    probe_dir = str(directory) if directory is not None else tempfile.gettempdir()
    probe = os.path.join(probe_dir, f".partyhams_at_probe@{os.getpid()}")
    try:
        with open(probe, "w"):
            pass
        os.unlink(probe)
        return True
    except OSError:
        return False


def should_autoexport(
    enabled: bool, only_if_new: bool, current_count: int, last_count: int
) -> bool:
    """Whether a periodic auto-export should write now (timer/log checks aside).

    Disabled never exports. When "only if new" is set, export only if the QSO
    count has increased since the last successful export; otherwise always.
    """
    if not enabled:
        return False
    if only_if_new and current_count <= last_count:
        return False
    return True


class MainWindow(QMainWindow):
    def __init__(
        self,
        session: LogSession,
        on_close: Callable[[], None] | None = None,
        radio_poller: RadioPoller | None = None,
    ) -> None:
        super().__init__()
        self.session = session
        self._on_close = on_close
        self._macros = load_macros(session.contest)  # this event's F-key macros
        self._macros_dialog = None
        self._sound = None  # keep a ref to the playing voice clip
        self._run = True  # Run vs Search & Pounce (picks the macro bank)
        self._esm = False  # ESM: Enter sends the next message
        self._esm_sent = False  # have we sent our exchange/call this QSO?
        # Partial-call policy (Run ESM): when False (default), a "?" in the call
        # field makes Enter send the partial verbatim and hold; when True, ESM
        # advances anyway. Set from app state; persisted via on_change_esm_send_on_query.
        self._esm_send_on_query = False
        self.on_change_esm_send_on_query: Callable[[bool], None] | None = None
        self._esm_send_on_query_action = None
        #: When set, the log table is filtered to this callsign (every QSO the
        #: network has logged with it) — driven by a dupe on the call field.
        self._call_filter = ""
        self._autocq = False  # Auto-CQ: repeat F1 on a timer while in Run mode
        self._autocq_interval = 10  # seconds; set from app state via set_autocq_interval
        # CW keyer-speed ownership (Radio menu). Set from app state via
        # set_cw_speed_mode; the app persists changes via on_change_cw_speed_mode.
        self._cw_speed_mode = CW_SPEED_SYNC
        self.on_change_cw_speed_mode: Callable[[str], None] | None = None
        self._cw_speed_actions: dict[str, object] = {}
        # Quick CW WPM presets shown on the CW speed bar. Set from app state via
        # set_cw_wpm_presets; the app persists changes via on_change_cw_wpm_presets.
        self._cw_wpm_presets: list[int] = [24, 20]
        self._cw_presets_enabled = True
        self.on_change_cw_wpm_presets: Callable[[list[int], bool], None] | None = None
        # "Restore after sending" bookkeeping: the radio's own speed to restore to,
        # and the pending restore task (cancelled/rescheduled across rapid sends).
        self._cw_restore_wpm: int | None = None
        self._cw_restore_task: asyncio.Task | None = None
        # Last keyer speed we commanded the rig (macro or keyboard send / push), so
        # Sync mode doesn't re-adopt our own echo as an operator knob change.
        self._last_commanded_wpm: int | None = None
        #: Set by the app: on_autocq_interval(secs) persists the chosen interval.
        self.on_autocq_interval: Callable[[int], None] | None = None
        # Periodic ADIF auto-export settings (driven from app state).
        self._autoexport_enabled = True
        self._autoexport_minutes = 5  # clamped to 5..60 when applied
        self._autoexport_only_if_new = True
        self._autoexport_last_count = 0  # QSO count at the last successful export
        #: Set by the app: on_change_autoexport(enabled, minutes, only_if_new).
        self.on_change_autoexport: Callable[[bool, int, bool], None] | None = None
        self._sections_window: SectionsWindow | None = None
        self._cluster_window: ClusterWindow | None = None
        # Reference data (super-check-partial, city.dat, LoTW/eQSL/QRZ user lists).
        # Imported via Tools menu; loaded from disk on launch (missing => empty).
        self._refdata = RefData()
        self._refdata.load()
        # QRZ.com lookup: credentials come from app state; lookups are debounced
        # and run in the background, surfacing results in the status bar.
        self._qrz = QrzClient()
        self._qrz_last_call = ""  # debounce: don't re-look-up the same call
        #: Set by the app: on_change_qrz(username, password) persists credentials.
        self.on_change_qrz: Callable[[str, str], None] | None = None
        self._qrz_dialog = None  # the QRZ login dialog while open
        #: Set by the app to no-arg callbacks that switch radio / log.
        self.on_change_radio: Callable[[], None] | None = None
        self.on_new_log: Callable[[], None] | None = None
        self.on_open_log: Callable[[], None] | None = None
        #: Open a specific log by path (a Recent Logs entry).
        self.on_open_log_path: Callable[[str], None] | None = None
        #: Returns recent logs as (path, label) pairs, most-recent first.
        self.recent_logs_provider: Callable[[], list[tuple[str, str]]] | None = None
        #: Set by the app: on_change_theme(name) applies + persists a theme.
        self.on_change_theme: Callable[[str], None] | None = None
        #: Set by the app: on_change_font(family, size) applies + persists a font.
        self.on_change_font: Callable[[str | None, int], None] | None = None
        self._radio_dialog = None  # app keeps the open radio dialog alive here
        self._log_dialog = None  # app keeps the open new/open-log dialog alive here
        self._shortcuts_dialog = None  # the Keyboard Shortcuts dialog while open
        self._about_dialog = None  # the About dialog while open
        self._help_window = None  # the User Guide window while open
        self._autoexport_dialog = None  # the Auto-export settings dialog while open
        # WSJT-X UDP integration (digital modes). The listener is created on
        # demand by set_wsjtx; _wsjtx_active flips the F-key bar -> info panel.
        self._wsjtx_listener: WsjtxListener | None = None
        self._wsjtx_enabled = False
        self._wsjtx_port = 2237
        self._wsjtx_host = ""  # "" = all interfaces; a multicast group is joined
        self._wsjtx_active = False
        # The exact data sub-mode WSJT-X reports (FT8 vs FT4). A CAT rig only knows
        # "data/USB" (read back as FT8), so while WSJT-X is active its mode wins.
        self._wsjtx_mode: Mode | None = None
        self._wsjtx_id = ""  # the reporting WSJT-X instance id (for replies)
        self._wsjtx_highlighted: set[str] = set()  # calls we've already colored
        # Live "callers" panel: who is calling us in FT8/FT4. Field Day tracks the
        # section they send; POTA tints park activators green. Buttons expire after
        # the tracker's TTL (~5 min); a timer prunes them even without new decodes.
        self._callers = CallerTracker(
            is_section=(is_valid_section if session.contest.id == "arrl-field-day" else None)
        )
        self._callers_timer = QTimer(self)
        self._callers_timer.setInterval(20_000)
        self._callers_timer.timeout.connect(self._refresh_callers)
        #: Set by the app: on_change_wsjtx(enabled, port, host) persists the choice.
        self.on_change_wsjtx: Callable[[bool, int, str], None] | None = None
        try:
            self._loop = asyncio.get_event_loop()
        except RuntimeError:
            self._loop = None  # no loop (e.g. tests) -> log locally, skip broadcast

        # CAT state (managed via set_poller). When a poller is present, band/mode/
        # frequency follow the rig and the manual combos become read-only mirrors.
        self._poller: RadioPoller | None = None
        self._cat = False
        self._radio_freq: int | None = None
        self._radio_mode: Mode | None = None
        self._radio_connected = False
        # Search & Pounce dupe hint: the last frequency we offered a "worked here"
        # suggestion for, so we prompt once per QSY rather than on every poll.
        self._sp_probe_freq: int | None = None

        self.setWindowTitle(f"PartyHams Logger — {session.config.my_call} — {session.contest.name}")
        self.resize(1060, 580)

        # Log columns adapt to the contest (Field Day has no RST exchange).
        self._columns = ["UTC", "Call", "Band", "Mode"]
        if session.contest.exchanges_rst:
            self._columns += ["RST S", "RST R"]
        self._columns += ["Exchange", "Op"]

        # Frequency readout (live from CAT when a radio is connected). Lives in the
        # status bar; created here because building the entry row triggers an early
        # _update_freq_readout (the band combo's default-index change).
        self._freq = QLabel()
        self._freq.setStyleSheet(f"color: {style.ACCENT}; font-weight: 600;")
        # WSJT-X transmit period (EVEN/ODD), shown just right of the FT8/FT4 mode.
        self._tx_period = QLabel()
        self._tx_period.setStyleSheet(f"color: {style.AMBER}; font-weight: 600;")

        self._build_menu()
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.addWidget(self._build_station_bar())
        layout.addWidget(self._build_entry_row())
        layout.addWidget(self._build_log_table(), stretch=1)
        self._fkey_bar = self._build_fkey_bar()
        layout.addWidget(self._fkey_bar)
        self._cw_bar = self._build_cw_bar()
        layout.addWidget(self._cw_bar)
        self._wsjtx_panel = WsjtxPanel()
        self._wsjtx_panel.on_call = self._reply_to_caller
        self._wsjtx_panel.setVisible(False)
        layout.addWidget(self._wsjtx_panel)
        self.setCentralWidget(root)
        self._setup_fkey_shortcuts()

        session.add_listener(self.refresh)
        # Permanent radio indicator on the right of the status bar. Give it room and
        # center the text vertically so the rig description isn't cramped.
        self._radio_status_label = QLabel()
        self._radio_status_label.setAlignment(
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight
        )
        self._radio_status_label.setMinimumWidth(240)
        # Auto-update state (configured by the app from AppState via set_auto_update).
        self._auto_update_enabled = True
        self._auto_update_interval_hours = 1
        self.on_change_auto_update: Callable[[bool, int], None] | None = None
        self._update_info = None  # the available UpdateInfo, once found
        self._update_downloading = False
        # A green ⬇ appears here when a newer release is available (click to install).
        self._update_btn = QPushButton("⬇")
        self._update_btn.setFlat(True)
        self._update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._update_btn.setVisible(False)
        self._update_btn.clicked.connect(self._start_update)
        self._style_update_btn()
        # Download progress, shown in the status bar only while a download runs.
        self._download_bar = QProgressBar()
        self._download_bar.setMaximumWidth(180)
        self._download_bar.setVisible(False)
        # Left-to-right in the right-aligned permanent area: download bar, update
        # indicator, frequency/band/mode, then the WSJT-X period, then the radio.
        self.statusBar().addPermanentWidget(self._download_bar)
        self.statusBar().addPermanentWidget(self._update_btn)
        self.statusBar().addPermanentWidget(self._freq)
        self.statusBar().addPermanentWidget(self._tx_period)
        self.statusBar().addPermanentWidget(self._radio_status_label)
        self.statusBar().setSizeGripEnabled(False)

        self._build_network_panel()
        self.set_poller(radio_poller)
        self._setup_auto_export()
        self._setup_autocq()
        self._setup_qrz()
        self._setup_update_check()
        self._call.setFocus()
        self.refresh()

    def _setup_auto_export(self) -> None:
        """Periodically snapshot the log to a timestamped ADIF backup."""
        self._auto_export_timer = QTimer(self)
        self._auto_export_timer.timeout.connect(self._auto_export_adif)
        self._apply_autoexport_timer()

    def _apply_autoexport_timer(self) -> None:
        """(Re)arm or stop the timer from the current auto-export settings."""
        self._auto_export_timer.stop()
        if self._autoexport_enabled:
            minutes = clamp_export_minutes(self._autoexport_minutes)
            self._auto_export_timer.start(minutes * 60 * 1000)

    def set_autoexport(self, enabled: bool, minutes: int, only_if_new: bool) -> None:
        """Apply saved/edited auto-export settings and re-arm the timer."""
        self._autoexport_enabled = enabled
        self._autoexport_minutes = clamp_export_minutes(minutes)
        self._autoexport_only_if_new = only_if_new
        if hasattr(self, "_auto_export_timer"):
            self._apply_autoexport_timer()

    def _setup_autocq(self) -> None:
        """Create (stopped) the timer that repeats F1 while Auto-CQ is on."""
        self._autocq_timer = QTimer(self)
        self._autocq_timer.timeout.connect(self._autocq_tick)

    def set_autocq_interval(self, seconds: int) -> None:
        """Apply a saved/preset interval (clamped). Restarts the timer if live."""
        self._autocq_interval = clamp_autocq_interval(seconds)
        if self._autocq:
            self._start_autocq()  # re-arm at the new interval

    def _set_autocq(self, on: bool) -> None:
        if on:
            self._start_autocq()
        else:
            self._stop_autocq()

    def _start_autocq(self) -> None:
        self._autocq = True
        if hasattr(self, "_autocq_action"):
            self._autocq_action.setChecked(True)
        self._autocq_timer.start(self._autocq_interval * 1000)
        self.statusBar().showMessage(f"Auto-CQ on ({self._autocq_interval}s)", 2000)
        self._fire_macro(1)  # send the first CQ immediately

    def _stop_autocq(self) -> None:
        was_on = self._autocq
        self._autocq = False
        self._autocq_timer.stop()
        if hasattr(self, "_autocq_action"):
            self._autocq_action.setChecked(False)
        if was_on:
            self.statusBar().showMessage("Auto-CQ stopped", 2000)

    def _on_call_typed(self) -> None:
        """Pause Auto-CQ the moment a callsign is being entered."""
        if self._autocq and self._call.text().strip():
            self._stop_autocq()

    def _autocq_tick(self) -> None:
        """Fire F1 if conditions still hold; otherwise stop the repeat."""
        if should_autocq(self._run, self._autocq, self._call.text()):
            self._fire_macro(1)
        else:
            self._stop_autocq()

    # ------------------------------------------------------------------ #
    # QRZ.com callsign lookup (debounced, background)
    # ------------------------------------------------------------------ #
    def _setup_qrz(self) -> None:
        """Create the (stopped) debounce timer that fires a QRZ lookup."""
        self._qrz_timer = QTimer(self)
        self._qrz_timer.setSingleShot(True)
        self._qrz_timer.setInterval(600)  # ms pause before looking up
        self._qrz_timer.timeout.connect(self._qrz_lookup_now)

    def set_qrz_credentials(self, username: str, password: str) -> None:
        """Apply saved/edited QRZ credentials; clears any cached session key."""
        self._qrz.username = username
        self._qrz.password = password
        self._qrz.key = None
        self._qrz_last_call = ""

    def _qrz_enabled(self) -> bool:
        return bool(self._qrz.username and self._qrz.password)

    def _on_call_qrz(self) -> None:
        """Debounce a QRZ lookup on a short pause after the call changes."""
        if not self._qrz_enabled():
            return
        call = self._call.text().strip().upper()
        if not call or call == self._qrz_last_call:
            return
        self._qrz_timer.start()  # (re)start the debounce; fires after the pause

    def _qrz_lookup_now(self) -> None:
        """Kick off a background QRZ lookup for the current callsign."""
        call = self._call.text().strip().upper()
        if not call or not self._qrz_enabled() or call == self._qrz_last_call:
            return
        self._qrz_last_call = call
        if self._loop is None or not self._loop.is_running():
            return  # no loop (tests) -> skip the network call
        self._loop.create_task(self._do_qrz_lookup(call))

    async def _do_qrz_lookup(self, call: str) -> None:
        """Run the (blocking) QRZ lookup off the UI thread and show the result."""
        record = await asyncio.get_event_loop().run_in_executor(None, self._qrz.lookup, call)
        if call != self._call.text().strip().upper():
            return  # the operator moved on; don't clobber a newer entry
        if record is not None:
            self.statusBar().showMessage(format_record(record), 8000)
        elif self._qrz.last_error:
            self.statusBar().showMessage(self._qrz.last_error, 4000)

    # ------------------------------------------------------------------ #
    # update check (periodic, background) + in-app download/install
    # ------------------------------------------------------------------ #
    def _setup_update_check(self) -> None:
        """Start the periodic release check and run an initial check after launch."""
        self._update_timer = QTimer(self)
        self._update_timer.timeout.connect(self._check_for_update)
        self._apply_update_schedule()
        QTimer.singleShot(5000, self._check_for_update)  # initial check ~5s in

    def set_auto_update(self, enabled: bool, interval_hours: int) -> None:
        """Apply the saved auto-update preference (enable + interval, 1h..7d)."""
        self._auto_update_enabled = enabled
        self._auto_update_interval_hours = clamp_interval_hours(interval_hours)
        self._apply_update_schedule()

    def _apply_update_schedule(self) -> None:
        if not hasattr(self, "_update_timer"):
            return
        if self._auto_update_enabled:
            self._update_timer.start(self._auto_update_interval_hours * 60 * 60 * 1000)
        else:
            self._update_timer.stop()

    def _edit_update_settings(self) -> None:
        from partyhams.ui.update_dialog import UpdateSettingsDialog

        dialog = UpdateSettingsDialog(
            self._auto_update_enabled,
            self._auto_update_interval_hours,
            on_check_now=lambda: self._check_for_update(force=True),
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            enabled, hours = dialog.settings()
            self.set_auto_update(enabled, hours)
            if self.on_change_auto_update is not None:
                self.on_change_auto_update(enabled, hours)  # app persists it

    def _style_update_btn(self) -> None:
        self._update_btn.setStyleSheet(
            f"QPushButton {{ color: {style.MULT}; font-weight: 800; border: none; }}"
        )

    def _check_for_update(self, *, force: bool = False) -> None:
        """Kick off a background check. ``force`` (the manual button) runs even when
        auto-update is disabled and reports 'up to date'."""
        if not force and not self._auto_update_enabled:
            return
        if self._loop is None or not self._loop.is_running():
            return  # no loop (tests) -> skip the network call
        self._loop.create_task(self._do_check_for_update(force=force))

    async def _do_check_for_update(self, *, force: bool = False) -> None:
        """Look up the latest release off the UI thread; show the ⬇ if it's newer."""
        from partyhams import __version__

        if force:
            self.statusBar().showMessage("Checking for updates…", 2000)
        try:
            info = await asyncio.get_event_loop().run_in_executor(
                None, check_for_update, __version__
            )
        except Exception as exc:  # noqa: BLE001 - a failed check is non-fatal
            if force:
                self.statusBar().showMessage(f"Update check failed: {exc}", 4000)
            return
        if info is not None:
            self._show_update_available(info)
        elif force:
            self.statusBar().showMessage(f"You're up to date (v{__version__}).", 4000)

    def _show_update_available(self, info) -> None:
        self._update_info = info
        self._update_btn.setToolTip(f"Version {info.version} is available — click to install")
        self._update_btn.setVisible(True)

    # --- download + install ---
    def _start_update(self) -> None:
        """⬇ clicked: confirm, then download + install the new version in-app."""
        info = self._update_info
        if info is None or self._update_downloading:
            return
        from PySide6.QtWidgets import QMessageBox

        confirm = QMessageBox.question(
            self,
            "Download update",
            f"Download and install version {info.version}?\n\n"
            "The app will restart into the new version when it's ready.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        if not is_asset_url(info.url):
            # No build for this platform — fall back to the release page in a browser.
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl(info.url))
            return
        if self._loop is None or not self._loop.is_running():
            return
        self._update_downloading = True
        self._update_btn.setVisible(False)
        self._download_bar.setRange(0, 100)
        self._download_bar.setValue(0)
        self._download_bar.setVisible(True)
        self.statusBar().showMessage(f"Downloading version {info.version}…", 0)
        self._loop.create_task(self._do_download_and_install(info))

    def _on_download_progress(self, received: int, total: int) -> None:
        if total > 0:
            self._download_bar.setRange(0, 100)
            self._download_bar.setValue(int(received * 100 / total))
        else:
            self._download_bar.setRange(0, 0)  # indeterminate (no Content-Length)

    async def _do_download_and_install(self, info) -> None:
        import tempfile

        from PySide6.QtCore import QObject, Signal

        class _Signaller(QObject):
            progress = Signal(int, int)

        signaller = _Signaller()
        signaller.progress.connect(self._on_download_progress)
        loop = asyncio.get_event_loop()
        tmp = Path(tempfile.mkdtemp(prefix="partyhams-update-"))
        try:
            archive = await loop.run_in_executor(
                None,
                lambda: download_asset(
                    info.url, tmp, progress=lambda r, t: signaller.progress.emit(r, t)
                ),
            )
            bundle = await loop.run_in_executor(None, lambda: extract_bundle(archive, tmp))
        except Exception as exc:  # noqa: BLE001 - surface the failure, let them retry
            self._download_bar.setVisible(False)
            self._update_downloading = False
            self._update_btn.setVisible(True)
            self.statusBar().showMessage(f"Update download failed: {exc}", 6000)
            return
        self._download_bar.setVisible(False)
        self.statusBar().clearMessage()
        self._finish_update(info, bundle)

    def _finish_update(self, info, bundle: Path) -> None:
        from PySide6.QtWidgets import QMessageBox

        if not is_frozen():
            # A source checkout can't replace itself — leave the build for the user.
            QMessageBox.information(
                self,
                "Update downloaded",
                f"Version {info.version} was downloaded to:\n{bundle}\n\n"
                "Self-install only works in a packaged build; from a source checkout, "
                "run it from there.",
            )
            self._update_downloading = False
            return
        restart = QMessageBox.question(
            self,
            "Install update",
            f"Version {info.version} is ready. Restart now to finish installing?",
        )
        if restart != QMessageBox.StandardButton.Yes:
            self._update_downloading = False
            self.statusBar().showMessage("Update will install on the next restart.", 5000)
            return
        try:
            apply_update(bundle)  # spawns a detached helper that swaps + relaunches
        except Exception as exc:  # noqa: BLE001
            self._update_downloading = False
            self.statusBar().showMessage(f"Install failed: {exc}", 6000)
            return
        from PySide6.QtWidgets import QApplication

        QApplication.quit()  # let the helper take over

    def _auto_export_adif(self) -> None:
        path = getattr(self.session.store, "path", ":memory:")
        qsos = self.session.qsos()
        if path == ":memory:" or not qsos:
            return  # nothing worth backing up (transient or empty log)
        if not should_autoexport(
            self._autoexport_enabled,
            self._autoexport_only_if_new,
            len(qsos),
            self._autoexport_last_count,
        ):
            return  # disabled, or "only if new" and no QSOs added since last export
        try:
            out_dir = Path(path).resolve().parent / "adif-backups"
            out_dir.mkdir(parents=True, exist_ok=True)
            name = timestamped_adif_name(
                self.session.config.my_call, self.session.contest.id, utcnow()
            )
            target = out_dir / name
            target.write_text(self.session.export_adif())
            self._autoexport_last_count = len(qsos)
            self.statusBar().showMessage(f"Auto-exported ADIF → {target.name}", 3000)
        except OSError as exc:  # noqa: BLE001 - a backup failure must never disrupt logging
            self.statusBar().showMessage(f"Auto-export failed: {exc}", 4000)

    def _build_network_panel(self) -> None:
        """Dockable side panel: station roster + chat (toggle via the View menu)."""
        self._panel = NetworkPanel(self.session)
        self._panel.on_send_chat = self._send_chat
        self._panel.on_request_sync = self._request_full_log
        dock = QDockWidget("Network", self)
        dock.setObjectName("networkDock")
        self._network_dock = dock
        dock.setWidget(self._panel)
        dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        toggle = dock.toggleViewAction()
        toggle.setShortcut(QKeySequence(sc.TOGGLE_NETWORK))
        self._view_menu.addAction(toggle)

        # Backfill persisted/synced history (in send order) before live updates.
        for entry in self.session.chat_messages():
            self._panel.append_chat(entry)
        self.session.add_chat_listener(self._panel.append_chat)
        self.session.add_roster_listener(self._panel.refresh_roster)
        # Rates change with the clock, so refresh the roster on a timer too.
        self._roster_timer = QTimer(self)
        self._roster_timer.setInterval(2000)
        self._roster_timer.timeout.connect(self._panel.refresh_roster)
        self._roster_timer.start()

        # ContestBot: drop a fun automated message into the chat, but no more than
        # once every ~20 minutes so it stays un-spammy. The timer ticks each minute
        # (to track rate deltas and catch the Field Day :50 WWV window); posting is
        # gated by the cooldown. Seed the cooldown to "now" for a quiet startup.
        self._banter_prev: list[StationSnapshot] | None = None
        self._banter_counter = 0
        self._banter_last = utcnow()
        self._banter_timer = QTimer(self)
        self._banter_timer.setInterval(60_000)  # once a minute
        self._banter_timer.timeout.connect(self._banter_tick)
        self._banter_timer.start()

    def _send_chat(self, to_op: str, text: str) -> None:
        self.session.post_chat(to_op, text)  # local echo via the chat listener
        if self._loop is not None and self._loop.is_running():
            self._loop.create_task(self.session.broadcast_chat(to_op, text))

    def _banter_snapshot(self) -> list[StationSnapshot]:
        """Build a plain-data activity snapshot for the banter engine."""
        now = utcnow()
        snaps = []
        for row in self.session.roster():
            stats = self.session.station_stats(row["station_id"])
            last = stats["last"]
            age = (now - last).total_seconds() / 60.0 if last else None
            snaps.append(
                StationSnapshot(
                    operator=row["operator"] or row["call"],
                    rate_15=row["rates"][15],
                    total=row["total"],
                    last_qso_age_min=age,
                )
            )
        return snaps

    def _banter_tick(self) -> None:
        """Once a minute: track activity, and at most every ~20 minutes post a
        ContestBot message visible to everyone."""
        now = utcnow()
        snapshot = self._banter_snapshot()
        prev = self._banter_prev
        self._banter_prev = snapshot  # always advance, so rate deltas stay fresh
        self._banter_counter += 1
        if (now - self._banter_last).total_seconds() < BANTER_COOLDOWN_MIN * 60:
            return  # still cooling down — keep quiet
        field_day = self.session.contest.id == "arrl-field-day"
        message = choose_message(
            snapshot,
            prev,
            self._banter_counter,
            minute_of_hour=now.minute,
            field_day=field_day,
        )
        if message:
            self._send_chat("*", message)  # local echo + broadcast to all peers
            self._banter_last = now

    def _request_full_log(self) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.create_task(self.session.request_full_log())
            self.statusBar().showMessage("Requested full logs from all stations…", 4000)
        else:
            self.statusBar().showMessage("Not networked — no peers to sync with", 4000)

    # ------------------------------------------------------------------ #
    # F-key macros
    # ------------------------------------------------------------------ #
    def _build_fkey_bar(self) -> QWidget:
        bar = QWidget()
        hbox = QHBoxLayout(bar)
        hbox.setContentsMargins(2, 2, 2, 2)
        hbox.setSpacing(3)

        self._runsp_btn = QPushButton()
        self._runsp_btn.setObjectName("fkey")
        self._runsp_btn.setMinimumHeight(46)
        self._runsp_btn.setFixedWidth(64)
        self._runsp_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._runsp_btn.clicked.connect(lambda: self._set_run(not self._run))
        hbox.addWidget(self._runsp_btn)

        self._fkey_buttons: list[QPushButton] = []
        for key in range(1, 13):
            btn = QPushButton()
            btn.setObjectName("fkey")
            btn.setMinimumHeight(46)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keep focus in the call field
            btn.clicked.connect(lambda _checked=False, k=key: self._fire_macro(k))
            self._fkey_buttons.append(btn)
            hbox.addWidget(btn)
        return bar

    def _set_run(self, run: bool) -> None:
        if run == self._run:
            return
        self._run = run
        if not run:
            self._stop_autocq()  # Auto-CQ only makes sense in Run mode
        self._update_fkey_bar()

    def _set_esm(self, on: bool) -> None:
        self._esm = on
        self._esm_sent = False
        self._esm_badge.setVisible(on)
        self._update_fkey_bar()
        self.statusBar().showMessage(f"ESM {'on' if on else 'off'}", 2000)

    def set_esm_send_on_query(self, on: bool) -> None:
        """Apply the persisted partial-call policy (no save callback fired)."""
        self._esm_send_on_query = bool(on)
        if self._esm_send_on_query_action is not None:
            self._esm_send_on_query_action.setChecked(self._esm_send_on_query)

    def _set_esm_send_on_query(self, on: bool) -> None:
        self._esm_send_on_query = bool(on)
        if self.on_change_esm_send_on_query is not None:
            self.on_change_esm_send_on_query(self._esm_send_on_query)

    def _setup_fkey_shortcuts(self) -> None:
        for key in range(1, 13):
            seq = QKeySequence(getattr(Qt.Key, f"Key_F{key}"))
            shortcut = QShortcut(seq, self)
            shortcut.activated.connect(lambda k=key: self._fire_macro(k))
        # Escape = emergency stop transmitting.
        stop = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        stop.activated.connect(self._stop_tx)

    def _stop_tx(self) -> None:
        self._stop_autocq()  # ESC always halts the Auto-CQ repeat
        radio = self._poller.radio if self._poller is not None else None
        if radio is None or self._loop is None or not self._loop.is_running():
            return
        self._loop.create_task(self._do_stop_tx(radio))

    async def _do_stop_tx(self, radio) -> None:
        try:
            await radio.stop_tx()
            self.statusBar().showMessage("TX stopped", 1500)
        except Exception as exc:  # noqa: BLE001 - emergency stop must never crash
            self.statusBar().showMessage(f"Stop TX failed: {exc}", 3000)

    def _current_group(self) -> str:
        return mode_group_for(self._current_mode()).value

    def _current_bank(self) -> str:
        return bank_key(self._current_group(), self._run)

    def _update_fkey_bar(self) -> None:
        bank = self._current_bank()
        self._runsp_btn.setText("RUN" if self._run else "S&&P")
        # RUN stands out in amber; S&P uses the on-accent color for contrast.
        runsp_color = style.AMBER if self._run else style.ON_ACCENT
        self._runsp_btn.setStyleSheet(
            f"QPushButton#fkey {{ color: {runsp_color}; font-weight: 700; }}"
        )
        next_key = self._esm_next_key()
        for key, btn in enumerate(self._fkey_buttons, start=1):
            macro = self._macros.get(bank, key)
            label = macro.label if macro else ""
            btn.setText(f"F{key}\n{label}" if label else f"F{key}")
            btn.setEnabled(bool(macro and macro.content.strip()))
            # Highlight the key Enter would send next under ESM.
            if key == next_key:
                btn.setStyleSheet(f"QPushButton#fkey {{ border: 2px solid {style.MULT}; }}")
            else:
                btn.setStyleSheet("")

    # ------------------------------------------------------------------ #
    # CW speed bar (issue #4): quick WPM presets + a live keyboard sender
    # ------------------------------------------------------------------ #
    def _build_cw_bar(self) -> QWidget:
        """Below the F-key bar: a CW-speed box and a live keyboard sender. The speed
        is a spin box showing the current WPM with up/down stepper arrows; the
        keyboard Up/Down arrows nudge it too (from the entry or keyboard fields, or
        the box itself). The keyboard field sends each character as it's typed and
        Enter clears it. Only shown in CW mode (see _update_bottom_bars)."""
        bar = QWidget()
        hbox = QHBoxLayout(bar)
        hbox.setContentsMargins(2, 2, 2, 2)
        hbox.setSpacing(3)

        hbox.addWidget(QLabel("CW"))
        self._wpm_spin = QSpinBox()
        self._wpm_spin.setRange(WPM_MIN, WPM_MAX)
        self._wpm_spin.setValue(self._macros.cw_wpm)
        self._wpm_spin.setSuffix(" WPM")
        self._wpm_spin.setAccelerated(True)  # hold an arrow to ramp the value
        self._wpm_spin.setMinimumHeight(32)
        self._wpm_spin.setFixedWidth(92)
        # ClickFocus so Tab keeps cycling the QSO entry fields, but a click lets the
        # operator type a value or use the box's own arrows.
        self._wpm_spin.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._wpm_spin.setToolTip("CW speed for macros / F-keys")
        self._wpm_spin.valueChanged.connect(self._on_wpm_spin_changed)
        hbox.addWidget(self._wpm_spin)

        # Quick WPM presets: click a button to set the macro speed; right-click to
        # edit/delete; the + adds one. The whole strip hides when presets are off.
        self._presets_bar = QWidget()
        self._presets_box = QHBoxLayout(self._presets_bar)
        self._presets_box.setContentsMargins(0, 0, 0, 0)
        self._presets_box.setSpacing(3)
        hbox.addWidget(self._presets_bar)
        self._rebuild_cw_presets()

        # Live CW keyboard: sends as you type, Enter clears. Kept out of
        # _entry_fields so Space/Tab/Enter behave as text + clear (not field nav).
        self._cw_kbd_sent = ""  # text already streamed to the keyer this line
        self._cw_keyboard = QLineEdit()
        self._cw_keyboard.setPlaceholderText("Type to send CW…  (Enter clears)")
        self._cw_keyboard.textEdited.connect(self._on_cw_keyboard_edited)
        self._cw_keyboard.returnPressed.connect(self._clear_cw_keyboard)
        self._cw_keyboard.installEventFilter(self)  # Up/Down -> change keyboard WPM
        hbox.addWidget(self._cw_keyboard, stretch=1)

        # A separate speed used only for the live keyboard sender above, so you can
        # type freehand slower than your macros run.
        hbox.addWidget(QLabel("Kbd"))
        self._kbd_wpm_spin = QSpinBox()
        self._kbd_wpm_spin.setRange(WPM_MIN, WPM_MAX)
        self._kbd_wpm_spin.setValue(self._macros.cw_kbd_wpm)
        self._kbd_wpm_spin.setSuffix(" WPM")
        self._kbd_wpm_spin.setAccelerated(True)
        self._kbd_wpm_spin.setMinimumHeight(32)
        self._kbd_wpm_spin.setFixedWidth(92)
        self._kbd_wpm_spin.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._kbd_wpm_spin.setToolTip("CW speed for the live keyboard sender")
        self._kbd_wpm_spin.valueChanged.connect(self._on_kbd_wpm_spin_changed)
        hbox.addWidget(self._kbd_wpm_spin)
        return bar

    def _set_wpm(self, wpm: int, *, source: str = "user") -> None:
        """Set the CW keyer speed and persist it with the event's macros.

        ``source="user"`` means the operator changed it here (a button/arrow): in
        Sync mode that change is pushed straight to the radio. ``source="radio"``
        means we're adopting a change the operator made on the rig (Sync mode), so
        we must *not* echo it back."""
        wpm = clamp_wpm(wpm)
        if wpm == self._macros.cw_wpm:
            self._update_cw_bar()
            # Still push to radio on an explicit user action (e.g. clicking a preset
            # button that matches the current value) — the radio may be out of sync.
            if source == "user" and self._cw_speed_mode != CW_SPEED_RESTORE:
                self._push_wpm_to_radio(wpm)
            return
        self._macros.cw_wpm = wpm
        save_macros(self.session.contest.id, self._macros)
        self._update_cw_bar()
        self.statusBar().showMessage(f"CW speed {wpm} WPM", 1500)
        if source == "user" and self._cw_speed_mode != CW_SPEED_RESTORE:
            self._push_wpm_to_radio(wpm)

    def _bump_wpm(self, delta: int) -> None:
        self._set_wpm(self._macros.cw_wpm + delta)

    def _on_wpm_spin_changed(self, value: int) -> None:
        """The macro WPM spin box (typed value or its up/down arrows) changed."""
        self._set_wpm(value)

    def _set_kbd_wpm(self, wpm: int) -> None:
        """Set the live-keyboard CW speed (separate from the macro speed) and persist
        it. Applied only to characters typed in the keyboard sender."""
        wpm = clamp_wpm(wpm)
        if wpm == self._macros.cw_kbd_wpm:
            self._update_cw_bar()
            return
        self._macros.cw_kbd_wpm = wpm
        save_macros(self.session.contest.id, self._macros)
        self._update_cw_bar()
        self.statusBar().showMessage(f"Keyboard CW speed {wpm} WPM", 1500)
        if self._cw_speed_mode != CW_SPEED_RESTORE:
            self._push_wpm_to_radio(wpm)

    def _bump_kbd_wpm(self, delta: int) -> None:
        self._set_kbd_wpm(self._macros.cw_kbd_wpm + delta)

    def _on_kbd_wpm_spin_changed(self, value: int) -> None:
        """The keyboard WPM spin box changed."""
        self._set_kbd_wpm(value)

    # --- CW WPM presets (quick-speed buttons on the CW bar) --- #
    def set_cw_wpm_presets(self, presets: list[int], enabled: bool) -> None:
        """Set the preset list + feature switch from app state (no persistence)."""
        self._cw_wpm_presets = normalize_wpm_presets(presets)
        self._cw_presets_enabled = bool(enabled)
        if hasattr(self, "_presets_bar"):
            self._rebuild_cw_presets()

    def _save_cw_presets(self) -> None:
        """Persist the current preset list + switch via the app callback."""
        if self.on_change_cw_wpm_presets is not None:
            self.on_change_cw_wpm_presets(list(self._cw_wpm_presets), self._cw_presets_enabled)

    def _rebuild_cw_presets(self) -> None:
        """Repopulate the preset strip from ``self._cw_wpm_presets`` (+ add button)."""
        while self._presets_box.count():
            item = self._presets_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for wpm in self._cw_wpm_presets:
            btn = QPushButton(str(wpm))
            btn.setMinimumHeight(32)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keep Tab on the QSO fields
            btn.setToolTip(f"Set CW speed to {wpm} WPM (right-click to edit/delete)")
            btn.clicked.connect(lambda _checked=False, w=wpm: self._set_wpm(w))
            btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                lambda _pos, w=wpm, b=btn: self._show_preset_menu(w, b)
            )
            self._presets_box.addWidget(btn)
        add = QPushButton("+")
        add.setMinimumHeight(32)
        add.setFixedWidth(32)
        add.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        add.setToolTip("Add a CW WPM preset")
        add.clicked.connect(self._add_cw_preset)
        self._presets_box.addWidget(add)
        self._presets_bar.setVisible(self._cw_presets_enabled)

    def _show_preset_menu(self, wpm: int, anchor: QPushButton) -> None:
        menu = QMenu(self)
        menu.addAction("Change…", lambda: self._edit_cw_preset(wpm))
        menu.addAction("Delete", lambda: self._delete_cw_preset(wpm))
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def _add_cw_preset(self) -> None:
        value, ok = QInputDialog.getInt(
            self, "Add CW WPM Preset", "Speed (WPM):", 20, WPM_MIN, WPM_MAX
        )
        if not ok:
            return
        wpm = clamp_wpm(value)
        if wpm not in self._cw_wpm_presets:
            self._cw_wpm_presets.append(wpm)
            self._save_cw_presets()
            self._rebuild_cw_presets()

    def _edit_cw_preset(self, wpm: int) -> None:
        value, ok = QInputDialog.getInt(
            self, "Change CW WPM Preset", "Speed (WPM):", wpm, WPM_MIN, WPM_MAX
        )
        if not ok:
            return
        new = clamp_wpm(value)
        self._cw_wpm_presets = normalize_wpm_presets(
            [new if p == wpm else p for p in self._cw_wpm_presets]
        )
        self._save_cw_presets()
        self._rebuild_cw_presets()

    def _delete_cw_preset(self, wpm: int) -> None:
        self._cw_wpm_presets = [p for p in self._cw_wpm_presets if p != wpm]
        self._save_cw_presets()
        self._rebuild_cw_presets()

    def _edit_cw_presets(self) -> None:
        """Radio menu → manage presets and toggle the whole feature on/off."""
        from partyhams.ui.cw_presets_dialog import CwPresetsDialog

        dialog = CwPresetsDialog(self._cw_wpm_presets, self._cw_presets_enabled, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._cw_wpm_presets, self._cw_presets_enabled = dialog.settings()
            self._save_cw_presets()
            self._rebuild_cw_presets()

    # --- CW keyer-speed ownership modes (Radio menu) --- #
    def _build_cw_speed_menu(self, radio_menu) -> None:
        """Submenu picking who owns the keyer speed: restore / always / sync."""
        menu = radio_menu.addMenu("CW Speed Control")
        group = QActionGroup(self)
        group.setExclusive(True)
        for mode in CW_SPEED_MODES:
            action = menu.addAction(CW_SPEED_LABELS[mode])
            action.setCheckable(True)
            action.setActionGroup(group)
            action.triggered.connect(lambda _checked=False, m=mode: self._choose_cw_speed_mode(m))
            self._cw_speed_actions[mode] = action
        self._sync_cw_speed_menu()

    def _sync_cw_speed_menu(self) -> None:
        action = self._cw_speed_actions.get(self._cw_speed_mode)
        if action is not None:
            action.setChecked(True)

    def set_cw_speed_mode(self, mode: str) -> None:
        """Apply a CW-speed mode (from app state); does not persist."""
        self._cw_speed_mode = normalize_cw_speed_mode(mode)
        self._sync_cw_speed_menu()

    def _choose_cw_speed_mode(self, mode: str) -> None:
        """Operator picked a mode from the menu: apply, persist, and (Sync) align
        the rig to the logger's current speed right away."""
        self.set_cw_speed_mode(mode)
        if self.on_change_cw_speed_mode is not None:
            self.on_change_cw_speed_mode(self._cw_speed_mode)
        self.statusBar().showMessage(f"CW speed: {CW_SPEED_LABELS[self._cw_speed_mode]}", 2500)
        if self._cw_speed_mode == CW_SPEED_SYNC:
            self._push_wpm_to_radio(self._macros.cw_wpm)

    def _keyer_radio(self):  # noqa: ANN202 - a Radio or None
        """The connected radio iff it can set keyer speed, else None."""
        radio = self._poller.radio if self._poller is not None else None
        if radio is None or not radio.supports(Capability.KEYER_SPEED):
            return None
        return radio

    def _push_wpm_to_radio(self, wpm: int) -> None:
        """Fire-and-forget: set the rig's keyer speed to ``wpm`` (Sync mode)."""
        radio = self._keyer_radio()
        if radio is None or self._loop is None or not self._loop.is_running():
            return
        self._loop.create_task(self._do_set_wpm(radio, wpm))

    async def _do_set_wpm(self, radio, wpm: int) -> None:
        try:
            await radio.set_wpm(wpm)
            self._last_commanded_wpm = wpm  # so Sync doesn't re-adopt our own change
        except Exception as exc:  # noqa: BLE001 - never crash on a keyer-speed push
            self.statusBar().showMessage(f"Set radio WPM failed: {exc}", 3000)

    def _update_cw_bar(self) -> None:
        # Reflect both speeds in their boxes without re-triggering valueChanged.
        for spin, value in (
            (self._wpm_spin, self._macros.cw_wpm),
            (self._kbd_wpm_spin, self._macros.cw_kbd_wpm),
        ):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)

    def _on_cw_keyboard_edited(self, text: str) -> None:
        """Send only the characters newly appended since the last edit, so live
        typing streams to the keyer one chunk at a time. A deletion or mid-line
        edit can't be un-sent — it just resyncs the tracker, no send. Sent at the
        separate keyboard speed (``cw_kbd_wpm``), not the macro speed."""
        if text.startswith(self._cw_kbd_sent) and len(text) > len(self._cw_kbd_sent):
            self._send_cw(text[len(self._cw_kbd_sent) :].upper(), wpm=self._macros.cw_kbd_wpm)
        self._cw_kbd_sent = text

    def _clear_cw_keyboard(self) -> None:
        self._cw_keyboard.clear()
        self._cw_kbd_sent = ""

    def _macro_context(self) -> dict[str, str]:
        sent = self.session.config.sent_exchange
        ctx = {
            "MYCALL": self.session.config.my_call,
            "CALL": self._call.text().strip().upper(),
            "EXCH": " ".join(v for v in sent.values() if v),
            "OP": self.session.engine.operator,
            "RST": default_rst(self._current_mode()),
        }
        for name, value in sent.items():
            ctx[name.upper()] = value
        return ctx

    def _fire_macro(self, key: int) -> None:
        if key == 1:
            self._set_run(True)  # CQ implies Run mode
        group = self._current_group()
        macro = self._macros.get(self._current_bank(), key)
        if macro is None or not macro.content.strip():
            return
        if group == "DIGITAL":
            self.statusBar().showMessage("Digital macros not supported yet", 3000)
            return
        if group == "PHONE":
            self._play_wav(macro.content, tx_desc=(key, macro.label, Path(macro.content).name))
            return
        # CW / text
        text, actions = expand(macro.content, self._macro_context())
        if text:
            self._send_cw(text, tx_desc=(key, macro.label, text))
        for action in actions:
            if action == "log":
                self._try_log()
            elif action == "wipe":
                self._wipe_entry()

    # --- ESM (Enter sends messages) ---
    def _exchange_complete(self) -> bool:
        if not self._call.text().strip():
            return False
        parsed = {n: e.text().strip().upper() for n, e in self._exchange_edits.items()}
        return not self.session.validate_exchange(parsed)

    def _esm_step(self) -> ESMStep:
        call_text = self._call.text()
        return esm_step(
            self._run,
            bool(call_text.strip()),
            self._esm_sent,
            self._exchange_complete(),
            call_uncertain="?" in call_text,
            send_on_query=self._esm_send_on_query,
        )

    def _esm_next_key(self) -> int | None:
        if not self._esm:
            return None
        return self._esm_step().key

    def _on_enter(self) -> None:
        if self._esm:
            self._esm_advance()
        else:
            self._advance_or_log()

    def _esm_advance(self) -> None:
        step = self._esm_step()
        if step.query:
            self._send_partial_call()
            return
        if step.key is None:
            self._call.setFocus()
            return
        if step.set_sent:
            self._esm_sent = True
        self._fire_macro(step.key)
        if step.log:
            self._try_log()
        if step.focus_exchange:
            self._focus_first_empty_exchange()
        if step.reset:
            self._esm_sent = False
        self._update_fkey_bar()

    def _send_partial_call(self) -> None:
        """Run ESM, partial call: send the call field verbatim (e.g. ``N0?W``) and
        hold the QSO. The first ``?`` is left selected so the operator can type the
        fill-in character(s) to overwrite it as more of the call is copied."""
        call = self._call.text().strip()
        if call:
            self._send_cw(call, tx_desc=(0, "Partial", call))
        # Select the first "?" so a typed fill-in replaces it (N1MM-style).
        self._call.setFocus()
        idx = self._call.text().find("?")
        if idx >= 0:
            self._call.setSelection(idx, 1)

    def _focus_first_empty_exchange(self) -> None:
        for field in self.session.contest.exchange_fields():
            edit = self._exchange_edits[field.name]
            if not edit.text().strip():
                edit.setFocus()
                return
        self._call.setFocus()

    def eventFilter(self, obj: object, event: QEvent) -> bool:
        """Space/Tab walk forward through the QSO entry fields, Shift+Tab back.
        The key is consumed (no space is typed); installed on the entry fields in
        _build_entry_row."""
        if event.type() == QEvent.Type.KeyPress and obj in self._entry_fields:
            key = event.key()
            shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            if key == Qt.Key.Key_Backtab or (key == Qt.Key.Key_Tab and shift):
                self._advance_entry_field(obj, -1)
                return True
            if key in (Qt.Key.Key_Space, Qt.Key.Key_Tab):
                self._advance_entry_field(obj, +1)
                return True
        # Up/Down nudge a CW speed (single-line edits don't use vertical arrows, so
        # nothing is shadowed). In the entry fields it's the macro speed; in the live
        # CW keyboard it's that field's own keyboard speed.
        if event.type() == QEvent.Type.KeyPress:
            delta = (
                +1 if event.key() == Qt.Key.Key_Up else -1 if event.key() == Qt.Key.Key_Down else 0
            )
            if delta and obj in self._entry_fields:
                self._bump_wpm(delta)
                return True
            if delta and obj is self._cw_keyboard:
                self._bump_kbd_wpm(delta)
                return True
        return super().eventFilter(obj, event)

    def _advance_entry_field(self, current: object, delta: int) -> None:
        """Move focus to the next/previous QSO entry field (no wrap). The target's
        text is selected so it can be overtyped."""
        i = self._entry_fields.index(current) + delta
        if 0 <= i < len(self._entry_fields):
            target = self._entry_fields[i]
            target.setFocus()
            target.selectAll()

    def _send_cw(
        self, text: str, tx_desc: tuple[int, str, str] | None = None, *, wpm: int | None = None
    ) -> None:
        radio = self._poller.radio if self._poller is not None else None
        if radio is None or not radio.supports(Capability.SEND_CW):
            self.statusBar().showMessage("No CW keyer — configure a radio", 3000)
            return
        wpm = self._macros.cw_wpm if wpm is None else wpm
        if tx_desc is not None:
            self._show_tx_status("TRANSMITTING", tx_desc, timeout=0)
        if self._loop is not None and self._loop.is_running():
            self._loop.create_task(self._do_send_cw(radio, text, tx_desc, wpm))

    async def _do_send_cw(
        self, radio, text: str, tx_desc: tuple[int, str, str] | None = None, wpm: int | None = None
    ) -> None:
        wpm = self._macros.cw_wpm if wpm is None else wpm
        try:
            restore = self._cw_speed_mode == CW_SPEED_RESTORE and radio.supports(
                Capability.KEYER_SPEED
            )
            if restore:
                await self._send_cw_then_restore(radio, text, wpm)
            elif self._cw_speed_mode == CW_SPEED_SYNC:
                # Sync: the radio's speed was already pushed when the spinner changed.
                # Don't re-assert the macro speed here — that would override a
                # keyboard-speed change or preset the user just made.
                await radio.send_cw(text)
                self._last_commanded_wpm = wpm
            else:
                # Always: explicitly assert the requested speed before each send.
                await radio.send_cw(text, wpm=wpm)
                self._last_commanded_wpm = wpm
            if tx_desc is not None:
                self._show_tx_status("SENT", tx_desc, timeout=5000)
            else:
                self.statusBar().showMessage(f"CW: {text}", 2500)
        except Exception as exc:  # noqa: BLE001 - surface keyer errors, don't crash
            self.statusBar().showMessage(f"CW failed: {exc}", 4000)

    async def _send_cw_then_restore(self, radio, text: str, wpm: int) -> None:
        """Restore mode: remember the rig's own speed, key at ``wpm``, then schedule
        a restore to the rig's speed once keying should be done."""
        if self._cw_restore_wpm is None:  # first send of a burst: capture the knob
            try:
                self._cw_restore_wpm = await radio.read_wpm()
            except Exception:  # noqa: BLE001 - if we can't read it, we can't restore
                self._cw_restore_wpm = None
        await radio.send_cw(text, wpm=wpm)
        self._schedule_cw_restore(radio, text, wpm)

    def _schedule_cw_restore(self, radio, text: str, wpm: int) -> None:
        if self._cw_restore_wpm is None or self._loop is None or not self._loop.is_running():
            return
        if self._cw_restore_task is not None:
            self._cw_restore_task.cancel()  # a newer send supersedes the pending restore
        # Pad the estimate: restoring late is harmless, early would change speed mid-CW.
        delay = cw_duration_seconds(text, wpm) * 1.2 + 0.5
        self._cw_restore_task = self._loop.create_task(self._restore_cw_after(radio, delay))

    async def _restore_cw_after(self, radio, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return  # superseded by a newer send — keep the captured speed for later
        prev = self._cw_restore_wpm
        self._cw_restore_wpm = None
        self._cw_restore_task = None
        if prev is not None:
            try:
                await radio.set_wpm(prev)
            except Exception as exc:  # noqa: BLE001 - best-effort restore
                self.statusBar().showMessage(f"Restore radio WPM failed: {exc}", 3000)

    def _show_tx_status(self, word: str, tx_desc: tuple[int, str, str], timeout: int) -> None:
        """Left-of-status indicator: ``TRANSMITTING — F1 — CQ — CQ FD W7ABC``."""
        self.statusBar().showMessage(_format_tx_status(word, *tx_desc), timeout)

    def _play_wav(self, path: str, tx_desc: tuple[int, str, str] | None = None) -> None:
        if not path:
            self.statusBar().showMessage("No audio assigned to that key", 2500)
            return
        if not Path(path).exists():
            self.statusBar().showMessage(f"Audio file not found: {path}", 4000)
            return
        try:
            from PySide6.QtCore import QUrl
            from PySide6.QtMultimedia import QSoundEffect
        except ImportError:
            self.statusBar().showMessage("Audio unavailable (QtMultimedia missing)", 4000)
            return
        self._sound = QSoundEffect(self)
        self._sound.setSource(QUrl.fromLocalFile(path))
        if tx_desc is not None:
            self._show_tx_status("TRANSMITTING", tx_desc, timeout=0)
            # Flip to SENT once playback stops.
            self._sound.playingChanged.connect(lambda: self._on_wav_playing_changed(tx_desc))
        self._sound.play()

    def _on_wav_playing_changed(self, tx_desc: tuple[int, str, str]) -> None:
        if self._sound is not None and not self._sound.isPlaying():
            self._show_tx_status("SENT", tx_desc, timeout=5000)

    def _wipe_entry(self) -> None:
        self._call.clear()
        for edit in self._exchange_edits.values():
            edit.clear()
        self._call.setFocus()

    def _open_log_folder(self) -> None:
        """Reveal the folder holding the current log in the OS file browser."""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from partyhams.app.state import LOGS_DIR

        path = self.session.store.path
        folder = Path(path).resolve().parent if path and path != ":memory:" else LOGS_DIR
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _wipe_log(self) -> None:
        """Delete every QSO from the current log (keeps the contest setup)."""
        from PySide6.QtWidgets import QMessageBox

        count = len(self.session.qsos())
        confirm = QMessageBox.question(
            self,
            "Wipe Current Log",
            f"Delete all {count} QSO(s) from this log? The contest setup is kept, "
            "but the contacts can't be recovered.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.session.wipe_log()
        self.statusBar().showMessage("Log wiped", 3000)

    def _delete_app_data(self) -> None:
        """Delete ~/.partyhams (every log + all settings) and quit the app."""
        import shutil

        from PySide6.QtWidgets import QMessageBox

        from partyhams.app.state import APP_DIR

        confirm = QMessageBox.warning(
            self,
            "Delete All App Data",
            f"Permanently delete {APP_DIR} — every log and all settings — and quit?\n"
            "This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.session.store.close()  # release the SQLite handle before removing it
        shutil.rmtree(APP_DIR, ignore_errors=True)
        self.close()  # triggers the graceful shutdown path -> app quits

    def _edit_macros(self) -> None:
        dialog = MacrosDialog(self._macros, self.session.contest, parent=self)
        self._macros_dialog = dialog  # keep alive while open
        dialog.finished.connect(lambda result: self._on_macros_done(dialog, result))
        dialog.open()

    def _on_macros_done(self, dialog: MacrosDialog, result: int) -> None:
        self._macros_dialog = None
        if result == QDialog.DialogCode.Accepted.value:
            self._macros = dialog.result_macroset()
            save_macros(self.session.contest.id, self._macros)
            self._update_fkey_bar()
            self._update_cw_bar()  # the dialog can change the CW speed too

    def _build_menu(self) -> None:
        logs_menu = self.menuBar().addMenu("Logs")
        new = logs_menu.addAction("New Log…", lambda: self.on_new_log and self.on_new_log())
        new.setShortcut(QKeySequence(sc.NEW_LOG))
        open_log = logs_menu.addAction("Open Log…", lambda: self.on_open_log and self.on_open_log())
        open_log.setShortcut(QKeySequence(sc.OPEN_LOG))
        self._recent_menu = logs_menu.addMenu("Open Recent")
        self._recent_menu.aboutToShow.connect(self._rebuild_recent_menu)
        logs_menu.addSeparator()
        edit_log = logs_menu.addAction("Edit Log…", self._edit_log)
        edit_log.setStatusTip("Edit this log's station setup — call, operator, exchange, park")
        set_op = logs_menu.addAction("Set Operator…", self._choose_operator)
        set_op.setShortcut(QKeySequence(sc.SET_OPERATOR))
        set_op.setStatusTip("Set who is at the key — stamps new QSOs and colors the log")
        logs_menu.addSeparator()
        adif_menu = logs_menu.addMenu("Export ADIF")
        adif_all = adif_menu.addAction(
            "All stations…", lambda: self._export_adif(mine_only=False)
        )
        adif_all.setShortcut(QKeySequence(sc.EXPORT_ADIF))
        adif_all.setStatusTip("Every QSO in this activity (the whole synced log)")
        adif_mine = adif_menu.addAction(
            "My QSOs only…", lambda: self._export_adif(mine_only=True)
        )
        adif_mine.setStatusTip("Only the QSOs this station logged — for a personal submission")
        cabrillo = logs_menu.addAction("Export Cabrillo…", self._export_cabrillo)
        cabrillo.setShortcut(QKeySequence(sc.EXPORT_CABRILLO))
        if self.session.contest.id == "arrl-field-day":
            fd_info = logs_menu.addAction("Field Day Summary Info…", self._edit_fd_summary)
            fd_info.setStatusTip("Enter participants, club, and bonus points for the summary sheet")
            fd_summary = logs_menu.addAction("Export Field Day Summary…", self._export_fd_summary)
            fd_summary.setStatusTip("Summary sheet of figures to enter in the Field Day web app")
        logs_menu.addAction("Auto-export…", self._edit_autoexport)
        logs_menu.addAction("Open Log Folder", self._open_log_folder)
        logs_menu.addSeparator()
        logs_menu.addAction("QRZ Login…", self._edit_qrz)
        logs_menu.addSeparator()
        wipe = logs_menu.addAction("Wipe Current Log…", self._wipe_log)
        wipe.setStatusTip("Delete every QSO from this log (keeps the contest setup)")
        nuke = logs_menu.addAction("Delete All App Data && Quit…", self._delete_app_data)
        nuke.setStatusTip("Remove ~/.partyhams (all logs and settings) and quit")

        radio_menu = self.menuBar().addMenu("Radio")
        select_radio = radio_menu.addAction("Select Radio…", self._radio_menu_clicked)
        select_radio.setShortcut(QKeySequence(sc.SELECT_RADIO))
        select_radio.setStatusTip(
            "Choose how to read the rig (Hamlib, FlexRadio, Icom CI-V/LAN) or stay manual"
        )
        self._build_cw_speed_menu(radio_menu)
        radio_menu.addAction("CW WPM Presets…", self._edit_cw_presets).setStatusTip(
            "Add, change, or remove the quick CW speed buttons — or turn them off entirely"
        )

        self._build_wsjtx_menu()

        macros_menu = self.menuBar().addMenu("Macros")
        edit_macros = macros_menu.addAction("Edit Macros…", self._edit_macros)
        edit_macros.setShortcut(QKeySequence(sc.EDIT_MACROS))
        esm_action = macros_menu.addAction("ESM — Enter sends messages")
        esm_action.setCheckable(True)
        esm_action.setShortcut(QKeySequence(sc.TOGGLE_ESM))
        esm_action.toggled.connect(self._set_esm)
        send_on_query = macros_menu.addAction("ESM — Send exchange on partial call (?)")
        send_on_query.setCheckable(True)
        send_on_query.setChecked(self._esm_send_on_query)
        send_on_query.setStatusTip(
            "When off (default), a '?' in the call field sends the partial verbatim and "
            "holds; when on, Enter runs the exchange anyway (Run mode)"
        )
        send_on_query.toggled.connect(self._set_esm_send_on_query)
        self._esm_send_on_query_action = send_on_query
        self._build_autocq_menu(macros_menu)

        # The dock toggle is added to this menu later by _build_network_panel.
        self._view_menu = self.menuBar().addMenu("View")
        sections = self._view_menu.addAction("Sections Worked…", self._open_sections)
        sections.setShortcut(QKeySequence(sc.SECTIONS))
        sections.setStatusTip("Live multiplier grid and schematic section map")
        cluster = self._view_menu.addAction("DX Cluster…", self._open_cluster)
        cluster.setStatusTip("Connect to a DX cluster and QSY the rig to spots")
        self._build_theme_menu(self._view_menu)
        font = self._view_menu.addAction("Font…", self._choose_font)
        font.setStatusTip("Set the app-wide base font family and size")

        tools_menu = self.menuBar().addMenu("Tools")
        ref_menu = tools_menu.addMenu("Reference Data")
        ref_menu.addAction("Import Super Check Partial…", self._import_scp)
        ref_menu.addAction("Import city.dat…", self._import_city)
        ref_menu.addAction("Import Call History…", self._import_call_history)
        ref_menu.addSeparator()
        ref_menu.addAction("Import LoTW users…", self._import_lotw)
        ref_menu.addAction("Import eQSL users…", self._import_eqsl)
        ref_menu.addAction("Import QRZ users…", self._import_qrz)
        tools_menu.addSeparator()
        tools_menu.addAction(
            "Check for Updates…", lambda: self._check_for_update(force=True)
        ).setStatusTip("Check GitHub for a newer release now")
        tools_menu.addAction("Update Settings…", self._edit_update_settings).setStatusTip(
            "Turn the auto-update check on/off and set how often it runs"
        )

        help_menu = self.menuBar().addMenu("Help")
        guide = help_menu.addAction("User Guide…", self._show_help)
        guide.setStatusTip("Open the illustrated user guide for every screen")
        shortcuts = help_menu.addAction("Keyboard Shortcuts…", self._show_shortcuts)
        shortcuts.setShortcut(QKeySequence(sc.SHORTCUTS))
        shortcuts.setStatusTip("Show the full keyboard-shortcut reference")
        help_menu.addSeparator()
        about = help_menu.addAction("About PartyHams Logger…", self._show_about)
        about.setStatusTip("Version, credits, and the project link")

    def _build_autocq_menu(self, macros_menu) -> None:
        macros_menu.addSeparator()
        self._autocq_action = macros_menu.addAction("Auto-CQ (repeat F1)")
        self._autocq_action.setCheckable(True)
        self._autocq_action.toggled.connect(self._set_autocq)
        interval_menu = macros_menu.addMenu("Auto-CQ Interval")
        self._autocq_group = QActionGroup(self)
        self._autocq_group.setExclusive(True)
        for secs in AUTOCQ_INTERVALS:
            action = interval_menu.addAction(f"{secs}s")
            action.setCheckable(True)
            action.setChecked(secs == self._autocq_interval)
            self._autocq_group.addAction(action)
            action.triggered.connect(lambda _checked=False, s=secs: self._choose_autocq_interval(s))

    def _choose_autocq_interval(self, seconds: int) -> None:
        self.set_autocq_interval(seconds)
        if self.on_autocq_interval is not None:
            self.on_autocq_interval(self._autocq_interval)  # app persists it
        self.statusBar().showMessage(f"Auto-CQ interval {self._autocq_interval}s", 2000)

    def _edit_log(self) -> None:
        """Edit this log's station setup (call, operator, sent exchange, and
        contest fields like the POTA park/entity/location) after creation. The
        activity type and sync network are fixed at creation and shown locked."""
        from partyhams.ui.log_dialog import LogDialog

        existing = {
            "contest_id": self.session.contest.id,
            "my_call": self.session.config.my_call,
            "operator": self.session.engine.operator,
            "network": self.session.store.get_meta("network", "") or "",
            "sent_exchange": self.session.config.sent_exchange,
            "extra": self.session.config.extra,
        }
        dialog = LogDialog(existing=existing, parent=self)
        self._log_edit_dialog = dialog  # keep alive while open
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        s = dialog.settings()
        self.session.update_config(
            my_call=s["my_call"],
            operator=s["operator"],
            sent_exchange=s["sent_exchange"],
            extra=s["extra"],
        )
        self.refresh()
        self._update_radio_label()
        self.statusBar().showMessage("Log settings updated", 3000)

    def _choose_operator(self) -> None:
        """Prompt for who's now at the key. New QSOs are stamped with this
        operator and the log re-colors (white = this op, blue = others)."""
        from PySide6.QtWidgets import QInputDialog

        current = self.session.engine.operator
        op, ok = QInputDialog.getText(
            self, "Set Operator", "Operator callsign:", text=current
        )
        if not ok:
            return
        op = op.strip().upper()
        if not op:
            return
        self.session.set_operator(op)
        self.statusBar().showMessage(f"Operator set to {op}", 2500)

    def _build_wsjtx_menu(self) -> None:
        """The WSJT-X menu: toggle the UDP listener and set its server + port."""
        menu = self.menuBar().addMenu("WSJT-X")
        self._wsjtx_action = menu.addAction("Enable WSJT-X (UDP)")
        self._wsjtx_action.setCheckable(True)
        self._wsjtx_action.toggled.connect(self._toggle_wsjtx)
        menu.addAction("Set UDP Server…", self._choose_wsjtx_host)
        menu.addAction("Set UDP Port…", self._choose_wsjtx_port)

    def _toggle_wsjtx(self, enabled: bool) -> None:
        """Enable/disable the listener (menu handler) and persist the choice."""
        self.set_wsjtx(enabled, self._wsjtx_port, self._wsjtx_host)
        self._persist_wsjtx()

    def _persist_wsjtx(self) -> None:
        if self.on_change_wsjtx is not None:
            self.on_change_wsjtx(self._wsjtx_enabled, self._wsjtx_port, self._wsjtx_host)

    def _choose_wsjtx_port(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        port, ok = QInputDialog.getInt(self, "WSJT-X UDP Port", "Port:", self._wsjtx_port, 1, 65535)
        if not ok:
            return
        self.set_wsjtx(self._wsjtx_enabled, port, self._wsjtx_host)
        self._persist_wsjtx()

    def _choose_wsjtx_host(self) -> None:
        """Set the address WSJT-X's UDP Server sends to. Blank = all interfaces;
        a multicast group (e.g. 224.0.0.1) is joined so multicast reaches us."""
        from PySide6.QtWidgets import QInputDialog

        host, ok = QInputDialog.getText(
            self,
            "WSJT-X UDP Server",
            "Server address — blank for all interfaces, or a multicast\n"
            "group (224.0.0.1–239.255.255.255) to join it:",
            text=self._wsjtx_host,
        )
        if not ok:
            return
        self.set_wsjtx(self._wsjtx_enabled, self._wsjtx_port, host.strip())
        self._persist_wsjtx()

    def set_wsjtx(self, enabled: bool, port: int, host: str = "") -> None:
        """Apply WSJT-X settings: (re)start or stop the UDP listener.

        ``host`` is the address WSJT-X's UDP Server targets: "" binds all
        interfaces (unicast); a multicast group is joined so multicast reaches us.
        Idempotent and safe without a running loop (tests): when there's no
        asyncio loop the settings are stored but no socket is opened.
        """
        self._wsjtx_port = int(port)
        self._wsjtx_host = host or ""
        self._wsjtx_enabled = bool(enabled)
        if hasattr(self, "_wsjtx_action"):
            self._wsjtx_action.setChecked(self._wsjtx_enabled)
        if self._loop is None or not self._loop.is_running():
            return  # headless/tests — nothing to bind
        self._loop.create_task(self._restart_wsjtx())

    async def stop_wsjtx(self) -> None:
        """Stop the UDP listener (called during window teardown)."""
        if self._wsjtx_listener is not None:
            await self._wsjtx_listener.stop()
            self._wsjtx_listener = None

    async def _restart_wsjtx(self) -> None:
        """Tear down any existing listener and start a fresh one if enabled."""
        if self._wsjtx_listener is not None:
            await self._wsjtx_listener.stop()
            self._wsjtx_listener = None
        if not self._wsjtx_enabled:
            self._set_wsjtx_active(False)
            return
        listener = WsjtxListener(
            port=self._wsjtx_port,
            host=self._wsjtx_host,
            on_qso_logged=self._on_wsjtx_qso,
            on_status=self._on_wsjtx_status,
            on_decode=self._on_wsjtx_decode,
        )
        try:
            await listener.start()
        except OSError as exc:
            self.statusBar().showMessage(f"WSJT-X listen failed: {exc}", 5000)
            return
        self._wsjtx_listener = listener
        where = f"{self._wsjtx_host}:" if self._wsjtx_host else ":"
        self.statusBar().showMessage(f"WSJT-X UDP listening on {where}{self._wsjtx_port}", 3000)

    # --- WSJT-X message handlers (called from the asyncio thread) ---
    def _on_wsjtx_qso(self, msg: QSOLogged) -> None:
        """Log a WSJT-X-reported QSO into our log. The record carries a content-
        derived uuid, so a duplicated UDP delivery (WSJT-X sends one copy per
        outgoing interface, and multicast can re-deliver) is deduped here rather
        than stacking up as repeated entries."""
        kwargs = qso_logged_to_record(msg, self.session.contest)
        if not kwargs["call"]:
            return
        existing = self.session.engine.log.get(str(kwargs["uuid"]))
        if existing is not None and not existing.deleted:
            return  # already logged this exact contact — a duplicate packet
        try:
            qso = self.session.record_qso(**kwargs)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - never let a peer packet crash us
            self.statusBar().showMessage(f"WSJT-X log error: {exc}", 4000)
            return
        self._broadcast(qso)
        # We've worked them — drop their caller button.
        if self._callers.remove(msg.dx_call):
            self._refresh_callers()
        # If WSJT-X reported the transmit power, share it so peers see our power.
        power_w = parse_tx_power(msg.tx_power)
        if power_w is not None:
            self.session.set_local_status(qso.freq_hz, qso.mode, power_w=power_w)
        self.statusBar().showMessage(f"WSJT-X logged {kwargs['call']}", 3000)

    def _on_wsjtx_status(self, status: Status) -> None:
        """Track WSJT-X transmit state; flip to the info panel for data modes."""
        self._wsjtx_id = status.id
        mapped = self._map_status_mode(status.mode)
        group = mode_group_for(mapped)
        active = group.value == "DIGITAL"
        self._set_wsjtx_active(active)
        # WSJT-X's sub-mode (FT8/FT4) overrides the rig's coarse data mode while active.
        self._wsjtx_mode = mapped if active else None
        if not active:
            self._update_freq_readout()  # drop the FT8/FT4 label from the status bar
            return
        # Reflect WSJT-X's actual data mode (FT8 vs FT4) in the entry mode field.
        mode_idx = self._mode.findData(mapped)
        if mode_idx >= 0:
            self._mode.setCurrentIndex(mode_idx)
        self._update_freq_readout()  # status bar mode follows WSJT-X under CAT
        sending = status.tx_mode or status.mode
        if status.dx_call:
            sending = f"{status.dx_call} ({sending})"
        self._wsjtx_panel.set_status(
            mode=status.mode,
            dial_freq=status.dial_freq,
            tx_enabled=status.tx_enabled,
            transmitting=status.transmitting,
            tx_period_odd=status.tx_period_odd,
            sending=sending if status.transmitting else "",
        )
        # Broadcast which FT8/FT4 sequence we transmit on so peers see odd/even.
        # Prefer WSJT-X's explicit tx_period_odd; otherwise derive from the clock.
        if status.tx_period_odd is not None:
            ft_tx_even = 0 if status.tx_period_odd else 1
        else:
            ft_tx_even = tx_even_from_epoch(utcnow().timestamp(), status.mode)
        # Show the period (EVEN/ODD) in the status bar, just right of the FT8/FT4 mode.
        self._tx_period.setText({1: "EVEN", 0: "ODD"}.get(ft_tx_even, ""))
        self.session.set_local_status(
            status.dial_freq, self._map_status_mode(status.mode), ft_tx_even=ft_tx_even
        )

    def _on_wsjtx_decode(self, decode: Decode) -> None:
        """Track stations calling us (for the caller buttons) and highlight CQ
        candidates whose section we still need."""
        self._callers.ingest(decode, my_call=self.session.config.my_call, now=utcnow().timestamp())
        if self._wsjtx_active:
            self._refresh_callers()
        self._maybe_highlight(decode)

    def _refresh_callers(self) -> None:
        """Repaint the caller buttons from the tracker (also expires stale ones)."""
        now = utcnow().timestamp()
        self._callers.prune(now)
        self._wsjtx_panel.set_callers(
            [(c.call, c.section, c.pota) for c in self._callers.active(now)]
        )

    def _reply_to_caller(self, call: str) -> None:
        """A caller button was clicked: ask WSJT-X to answer that station."""
        decode = self._callers.decode_for(call)
        listener = self._wsjtx_listener
        if decode is None or listener is None:
            self.statusBar().showMessage(f"Can't answer {call} — WSJT-X not connected", 3000)
            return
        if listener.send_reply(self._wsjtx_id, decode):
            self.statusBar().showMessage(f"Answering {call} via WSJT-X…", 2500)
        else:
            self.statusBar().showMessage(f"Couldn't send reply to {call}", 3000)

    @staticmethod
    def _map_status_mode(mode: str) -> Mode:
        return map_mode(mode)

    def _set_wsjtx_active(self, active: bool) -> None:
        """Swap the F-key bar for the WSJT-X panel (or back) when state changes."""
        if active == self._wsjtx_active:
            return
        self._wsjtx_active = active
        self._update_bottom_bars()
        if active:
            self._refresh_callers()
            self._callers_timer.start()
        else:
            self._callers_timer.stop()
            self._callers.clear()
            self._wsjtx_panel.clear_callers()
            self._tx_period.setText("")  # no EVEN/ODD when WSJT-X isn't driving

    def _update_bottom_bars(self) -> None:
        """Decide what fills the slot below the log. The F-key macro bar shows only
        for CW/SSB (where macros apply); the WSJT-X panel shows while WSJT-X drives a
        data mode; other modes (RTTY, FM/AM, FT8/FT4 without WSJT-X) show neither."""
        self._wsjtx_panel.setVisible(self._wsjtx_active)
        macro_mode = self._current_mode() in (Mode.CW, Mode.USB, Mode.LSB)
        self._fkey_bar.setVisible(macro_mode and not self._wsjtx_active)
        # CW speed + keyboard sender only apply to CW.
        self._cw_bar.setVisible(self._current_mode() == Mode.CW and not self._wsjtx_active)
        self._update_cw_bar()

    def _maybe_highlight(self, decode: Decode) -> None:
        """Best-effort: tell WSJT-X to color CQ candidates whose section we need.

        Parses the calling station from a ``CQ ...`` decode and, if its section
        is still unworked on this band/mode, sends a HighlightCallsign reply.
        Sections aren't carried in FT8 decodes, so this colors *every* fresh CQ
        candidate while we still have unworked sections — a prompt to call them.
        """
        listener = self._wsjtx_listener
        if listener is None or not decode.message.upper().startswith("CQ"):
            return
        call = self._cq_call(decode.message)
        if not call or call in self._wsjtx_highlighted:
            return
        if not self._have_unworked_sections():
            return
        listener.send_highlight(
            self._wsjtx_id,
            call,
            background=(40, 90, 40, 255),  # green wash = "go work this one"
            foreground=(255, 255, 255, 255),
        )
        self._wsjtx_highlighted.add(call)

    @staticmethod
    def _cq_call(message: str) -> str:
        """Extract the calling station from a ``CQ [DX/dir] CALL [GRID]`` decode."""
        tokens = message.split()
        if not tokens or tokens[0].upper() != "CQ":
            return ""
        # Skip optional CQ qualifiers (e.g. "CQ DX", "CQ NA", "CQ TEST").
        idx = 1
        if idx < len(tokens) and len(tokens[idx]) <= 3 and tokens[idx].isalpha():
            idx += 1
        return tokens[idx].upper() if idx < len(tokens) else ""

    def _have_unworked_sections(self) -> bool:
        """True if at least one section's slot is still unworked (best-effort)."""
        worked = self.session.section_status()
        from partyhams.contest.sections import ARRL_SECTIONS

        return len(worked) < len(ARRL_SECTIONS)

    def _build_theme_menu(self, view_menu) -> None:
        view_menu.addSeparator()
        theme_menu = view_menu.addMenu("Theme")
        group = QActionGroup(self)
        group.setExclusive(True)
        last_dark = None
        for name, dark in style.theme_names():
            if dark != last_dark:
                # Non-selectable header dividing the dark themes from the light.
                if last_dark is not None:
                    theme_menu.addSeparator()
                header = theme_menu.addAction("Dark Themes" if dark else "Light Themes")
                header.setEnabled(False)
                last_dark = dark
            action = theme_menu.addAction(name)
            action.setCheckable(True)
            action.setStatusTip(f"Apply the {name} color theme (applies instantly)")
            action.setChecked(name == style.active_name())
            group.addAction(action)
            action.triggered.connect(lambda _checked=False, n=name: self._change_theme(n))

    def _change_theme(self, name: str) -> None:
        if self.on_change_theme is not None:
            self.on_change_theme(name)  # app applies, persists, and restyles
        else:
            from PySide6.QtWidgets import QApplication

            style.apply_theme(QApplication.instance(), name)
            self.restyle()

    def _choose_font(self) -> None:
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QFontDialog

        family, size = style.active_font()
        current = QFont(family, size) if family else QFont()
        current.setPointSize(size)
        font, ok = QFontDialog.getFont(current, self, "Choose Font")
        if not ok:
            return
        if self.on_change_font is not None:
            self.on_change_font(font.family(), font.pointSize())  # app applies + persists
        else:
            from PySide6.QtWidgets import QApplication

            style.apply_font(QApplication.instance(), font.family(), font.pointSize())
            self.restyle()

    def restyle(self) -> None:
        """Re-apply palette-derived inline styles after a live theme change."""
        # The shared dupe/mult badge is re-styled (palette-aware) by refresh() below.
        self._freq.setStyleSheet(f"color: {style.ACCENT}; font-weight: 600;")
        self._style_update_btn()
        self._update_fkey_bar()
        self._update_radio_label()
        self._panel.restyle()
        self.refresh()  # rebuilds the score bar and CAT-aware indicators

    def _rebuild_recent_menu(self) -> None:
        self._recent_menu.clear()
        entries = self.recent_logs_provider() if self.recent_logs_provider else []
        if not entries:
            self._recent_menu.addAction("(no recent logs)").setEnabled(False)
            return
        for path, label in entries:
            self._recent_menu.addAction(
                label,
                lambda checked=False, p=path: self.on_open_log_path and self.on_open_log_path(p),
            )

    def _show_shortcuts(self) -> None:
        dialog = ShortcutsDialog(parent=self)
        self._shortcuts_dialog = dialog  # keep alive while open
        dialog.finished.connect(lambda _result: setattr(self, "_shortcuts_dialog", None))
        dialog.open()

    def _show_about(self) -> None:
        dialog = AboutDialog(parent=self)
        self._about_dialog = dialog  # keep alive while open
        dialog.finished.connect(lambda _result: setattr(self, "_about_dialog", None))
        dialog.open()

    def _show_help(self) -> None:
        if self._help_window is None:
            self._help_window = HelpWindow()  # keep the ref alive like the others
        self._help_window.show()
        self._help_window.raise_()
        self._help_window.activateWindow()

    def _open_sections(self) -> None:
        if self._sections_window is None:
            self._sections_window = SectionsWindow(self.session)
        self._sections_window.show()
        self._sections_window.raise_()
        self._sections_window.activateWindow()

    def _open_cluster(self) -> None:
        if self._cluster_window is None:
            self._cluster_window = ClusterWindow(
                poller=self._poller,
                login_call=self.session.config.my_call,
                loop=self._loop,
            )
        self._cluster_window.set_poller(self._poller)
        self._cluster_window.show()
        self._cluster_window.raise_()
        self._cluster_window.activateWindow()

    # ------------------------------------------------------------------ #
    # reference data imports (Tools → Reference Data)
    # ------------------------------------------------------------------ #
    def _pick_refdata_file(self, title: str) -> str | None:
        """Prompt for a reference file and return its text, or None if cancelled."""
        path, _ = QFileDialog.getOpenFileName(
            self, title, "", "Reference data (*.txt *.dat *.scp *.csv);;All files (*)"
        )
        if not path:
            return None
        try:
            return Path(path).read_text(errors="ignore")
        except OSError as exc:
            self.statusBar().showMessage(f"Could not read {Path(path).name}: {exc}", 4000)
            return None

    def _import_scp(self) -> None:
        text = self._pick_refdata_file("Import Super Check Partial")
        if text is not None:
            count = self._refdata.import_scp(text)
            self.statusBar().showMessage(f"Loaded {count} super-check-partial calls", 4000)

    def _import_city(self) -> None:
        text = self._pick_refdata_file("Import city.dat")
        if text is not None:
            count = self._refdata.import_city_dat(text)
            self.statusBar().showMessage(f"Loaded {count} city.dat records", 4000)

    def _import_call_history(self) -> None:
        text = self._pick_refdata_file("Import Call History")
        if text is None:
            return
        fields = [f.name for f in self.session.contest.exchange_fields()]
        count = self._refdata.import_call_history(text, fields)
        # Report which exchange fields the file actually populated, so a column-name
        # mismatch (e.g. an N1MM file whose columns we don't recognize) is obvious
        # instead of looking like a silent success — see issue #19.
        filled = {k for rec in self._refdata.history.values() for k in rec}
        ordered = [f.name for f in self.session.contest.exchange_fields() if f.name in filled]
        if count and ordered:
            self.statusBar().showMessage(
                f"Loaded {count} call-history entries · filled {', '.join(ordered)}", 6000
            )
        else:
            from PySide6.QtWidgets import QMessageBox

            expected = ", ".join(f.name for f in self.session.contest.exchange_fields())
            QMessageBox.warning(
                self,
                "Call History",
                f"Imported the file but recognized no usable exchange data "
                f"({count} entries).\n\n"
                f"Columns are matched to this contest's exchange fields ({expected}). "
                f"N1MM files use 'Sect' for the section and 'Exch1' for the class; "
                f"make sure your file's header names those columns and that you've "
                f"selected the right contest before importing.",
            )
        self._autofill_from_history()  # apply to the call already in the box

    def _import_lotw(self) -> None:
        text = self._pick_refdata_file("Import LoTW users")
        if text is not None:
            count = self._refdata.import_lotw(text)
            self.statusBar().showMessage(f"Loaded {count} LoTW users", 4000)

    def _import_eqsl(self) -> None:
        text = self._pick_refdata_file("Import eQSL users")
        if text is not None:
            count = self._refdata.import_eqsl(text)
            self.statusBar().showMessage(f"Loaded {count} eQSL users", 4000)

    def _import_qrz(self) -> None:
        text = self._pick_refdata_file("Import QRZ users")
        if text is not None:
            count = self._refdata.import_qrz(text)
            self.statusBar().showMessage(f"Loaded {count} QRZ users", 4000)

    def _radio_menu_clicked(self) -> None:
        if self.on_change_radio is not None:
            self.on_change_radio()

    def set_poller(self, poller: RadioPoller | None) -> None:
        """Attach (or detach) a radio poller and rewire CAT state live."""
        self._poller = poller
        self._cat = poller is not None
        self._radio_freq = None
        self._radio_mode = None
        self._sp_probe_freq = None
        self._radio_connected = poller.connected if poller is not None else False
        self._update_band_mode_boxes()
        if poller is not None:
            poller.on_state = self._on_radio_state
            poller.on_status = self._on_radio_status
            if poller.state is not None:
                self._apply_radio_state(poller.state)
        if self._cluster_window is not None:
            self._cluster_window.set_poller(poller)
        self._refresh_indicators()
        self._update_radio_label()

    def _update_band_mode_boxes(self) -> None:
        """Show the manual Band/Mode pickers only when no radio supplies them.
        With CAT the rig reports band+mode automatically, so the boxes are hidden
        and that data appears in the status bar's frequency readout instead."""
        show = not self._cat
        for w in (self._band_label, self._band, self._mode_label, self._mode):
            w.setVisible(show)

    def _update_radio_label(self) -> None:
        if self._poller is None:
            self._radio_status_label.setText("📻 No radio (manual)")
            self._radio_status_label.setStyleSheet(f"color: {style.TEXT_DIM};")
            return
        desc = self._poller.radio.description()
        if self._radio_connected:
            self._radio_status_label.setText(f"📻 {desc}")
            self._radio_status_label.setStyleSheet(f"color: {style.MULT};")
        else:
            self._radio_status_label.setText(f"📻 {desc} · disconnected")
            self._radio_status_label.setStyleSheet(f"color: {style.AMBER};")

    def closeEvent(self, event: QCloseEvent) -> None:
        # Hand control back to the app loop for graceful async shutdown.
        if self._on_close is not None:
            self._on_close()
        event.accept()

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #
    def _build_station_bar(self) -> QLabel:
        self._station_label = QLabel()
        self._station_label.setObjectName("scoreBar")  # share the score bar's theming
        self._station_label.setTextFormat(Qt.TextFormat.RichText)
        return self._station_label

    def _update_station_bar(self) -> None:
        """The single top line: Station/Operator (merged when the same call), the
        POTA park context (one park = id + name + location; several = an
        "N-fer in <location>"), and the running QSO count."""
        dim = style.TEXT_DIM
        call = self.session.config.my_call
        operator = self.session.engine.operator
        if operator and operator != call:
            who = (
                f"<span style='color:{dim}'>Station</span> "
                f"<b style='color:{style.ACCENT}'>{call}</b> "
                f"<span style='color:{dim}'>· Op</span> <b>{operator}</b>"
            )
        else:
            who = (
                f"<span style='color:{dim}'>Station/Operator</span> "
                f"<b style='color:{style.ACCENT}'>{call}</b>"
            )
        parts = [who]
        pota = self._pota_context()
        if pota:
            parts.append(pota)
        qsos = self.session.score().qso_count
        parts.append(
            f"<span style='color:{dim}'>QSOs</span> <b style='color:{style.TEXT}'>{qsos}</b>"
        )
        self._station_label.setText(" &nbsp;|&nbsp; ".join(parts))

    def _pota_context(self) -> str:
        """The POTA park segment for the station line, or '' when not applicable."""
        if self.session.contest.id != "pota":
            return ""
        extra = self.session.config.extra
        raw = str(extra.get("park", "") or "")
        parks = [p.strip().upper() for p in raw.split(",") if p.strip()]
        if not parks:
            return ""
        location = str(extra.get("location", "") or "").strip()
        loc_txt = f" <span style='color:{style.TEXT_DIM}'>·</span> {location}" if location else ""
        if len(parks) == 1:
            ref = parks[0]
            names = extra.get("park_names") if isinstance(extra.get("park_names"), dict) else {}
            name = str(names.get(ref, "")).strip()
            name_txt = f" {name}" if name else ""
            return f"<b style='color:{style.MULT}'>{ref}</b>{name_txt}{loc_txt}"
        # Multiple parks at one site: an "N-fer".
        in_txt = f" in {location}" if location else ""
        return f"<b style='color:{style.MULT}'>{len(parks)}-fer</b>{in_txt}"

    def _build_entry_row(self) -> QWidget:
        row = QWidget()
        hbox = QHBoxLayout(row)

        self._call = QLineEdit()
        self._call.setPlaceholderText("Call")
        self._call.setToolTip(_CALL_TOOLTIP)
        self._call.setMinimumWidth(110)
        self._call.setMaximumWidth(160)
        make_upper(self._call)
        self._call.textChanged.connect(lambda *_: self._autofill_from_history())
        self._call.textChanged.connect(lambda *_: self._refresh_indicators())
        self._call.textChanged.connect(self._on_call_typed)
        self._call.textChanged.connect(lambda *_: self._on_call_qrz())
        self._call.returnPressed.connect(self._on_enter)
        hbox.addWidget(QLabel("Call"))
        hbox.addWidget(self._call)

        # Exchange fields, generated from the contest definition.
        self._exchange_edits: dict[str, QLineEdit] = {}
        for field in self.session.contest.exchange_fields():
            edit = QLineEdit()
            edit.setMinimumWidth(72)
            edit.setMaximumWidth(100)
            edit.setPlaceholderText(field.label)
            make_upper(edit)
            edit.returnPressed.connect(self._on_enter)
            edit.textChanged.connect(lambda *_: self._refresh_indicators())
            self._exchange_edits[field.name] = edit
            hbox.addWidget(QLabel(field.label))
            hbox.addWidget(edit)

        # Keyboard-first entry: Space/Tab walk forward through call -> exchange
        # fields, Shift+Tab back. Handled in eventFilter (which consumes the key so
        # no space is inserted) — see _advance_entry_field.
        self._entry_fields: list[QLineEdit] = [self._call, *self._exchange_edits.values()]
        for f in self._entry_fields:
            f.installEventFilter(self)

        # Manual Band + Mode pickers. These are shown only when no CAT radio is
        # feeding band/mode; with a radio they're hidden and that data moves to
        # the status bar (see _update_band_mode_boxes / _update_freq_readout).
        self._band = QComboBox()
        for band in self._sorted_bands():
            self._band.addItem(band.label, band)
        default_band = self._band.findText("20m")  # busiest FD band
        if default_band >= 0:
            self._band.setCurrentIndex(default_band)
        self._band.currentIndexChanged.connect(lambda *_: self._refresh_indicators())
        self._band_label = QLabel("Band")
        hbox.addWidget(self._band_label)
        hbox.addWidget(self._band)

        self._mode = QComboBox()
        for mode in _ENTRY_MODES:
            self._mode.addItem(mode.value, mode)
        self._mode.currentIndexChanged.connect(lambda *_: self._refresh_indicators())
        self._mode_label = QLabel("Mode")
        hbox.addWidget(self._mode_label)
        hbox.addWidget(self._mode)

        log_btn = QPushButton("Log")
        log_btn.clicked.connect(self._try_log)
        hbox.addWidget(log_btn)

        # ESM indicator — visible only while ESM (Enter sends messages) is on.
        self._esm_badge = QLabel("ESM")
        self._esm_badge.setObjectName("esmBadge")
        self._esm_badge.setVisible(self._esm)
        hbox.addWidget(self._esm_badge)

        hbox.addStretch(1)
        return row

    def _build_log_table(self) -> QTableWidget:
        self._table = QTableWidget(0, len(self._columns))
        self._table.setHorizontalHeaderLabels(self._columns)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        # Share width across all columns instead of dumping the slack into the last
        # one (which made "Op" enormous). Stretch divides space evenly.
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        # Right-click to delete, double-click to edit. _row_qsos maps rows -> QSO.
        self._row_qsos: list[QSO] = []
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_qso_menu)
        self._table.cellDoubleClicked.connect(lambda row, _col: self._edit_qso(row))
        return self._table

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _sorted_bands(self) -> list[Band]:
        bands = [band_by_label(lbl) for lbl in self.session.allowed_bands()]
        bands = [b for b in bands if b is not None]
        bands.sort(key=lambda b: b.low_hz)
        return bands

    def _current_band(self) -> Band:
        return self._band.currentData()

    def _current_freq(self) -> int:
        if self._cat and self._radio_freq is not None:
            return self._radio_freq
        band = self._current_band()
        return (band.low_hz + band.high_hz) // 2

    def _current_mode(self) -> Mode:
        # WSJT-X knows the exact sub-mode (FT8 vs FT4); a CAT rig only reports a
        # coarse data mode, so WSJT-X wins whenever it's driving a data mode.
        if self._wsjtx_active and self._wsjtx_mode is not None:
            return self._wsjtx_mode
        if self._cat and self._radio_mode is not None:
            return self._radio_mode
        return self._mode.currentData()

    # ------------------------------------------------------------------ #
    # CAT (radio) integration
    # ------------------------------------------------------------------ #
    def _on_radio_state(self, state: RadioState) -> None:
        self._apply_radio_state(state)

    def _on_radio_status(self, connected: bool, error: str | None) -> None:
        self._radio_connected = connected
        if not connected:
            self.statusBar().showMessage(
                f"Radio disconnected{f' ({error})' if error else ''}", 3000
            )
        self._update_freq_readout()
        self._update_radio_label()

    def _apply_radio_state(self, state: RadioState) -> None:
        self._radio_freq = state.freq_hz
        self._radio_mode = state.mode
        # Mirror onto the (disabled) combos for a familiar display.
        band = band_for_freq(state.freq_hz)
        if band is not None:
            idx = self._band.findText(band.label)
            if idx >= 0:
                self._band.setCurrentIndex(idx)
        mode_idx = self._mode.findData(state.mode)
        if mode_idx >= 0:
            self._mode.setCurrentIndex(mode_idx)
        self._maybe_follow_radio_wpm(state)
        self._maybe_warn_worked_here(state)
        self._refresh_indicators()  # updates dupe/mult badges + the freq readout
        self._update_radio_label()  # model/nickname may have just arrived

    def _maybe_follow_radio_wpm(self, state: RadioState) -> None:
        """Sync mode: adopt a keyer-speed change made on the radio into the logger
        (without echoing it back). Ignore a reading that just reflects a speed we
        ourselves commanded (e.g. a keyboard send at the separate keyboard speed),
        so our own sends don't get re-adopted as the macro speed."""
        if self._cw_speed_mode != CW_SPEED_SYNC or state.wpm is None:
            return
        if state.wpm == self._last_commanded_wpm:
            return  # our own echo, not an operator knob change
        if state.wpm != self._macros.cw_wpm:
            self._set_wpm(state.wpm, source="radio")

    def _maybe_warn_worked_here(self, state: RadioState) -> None:
        """Search & Pounce convenience (issue #1): on a QSY to a new frequency with
        no call entered yet, pre-fill the call of a station already worked here and
        flag it as a likely dupe in the status bar — so the op can move on instead
        of working it again. Opt-outs: Run mode, or a call already being entered.

        We act only on a real QSY (frequency moved beyond the match tolerance from
        the last suggestion), not on every poll while parked on one frequency, so a
        call the op cleared isn't immediately re-filled.
        """
        if self._run:  # Run mode = calling CQ, not tuning around
            return
        if self._call.text().strip():  # don't clobber a call in progress
            return
        freq = state.freq_hz
        tol = 200
        if freq <= 0:
            return
        if self._sp_probe_freq is not None and abs(freq - self._sp_probe_freq) <= tol:
            return  # still on (essentially) the same spot we already checked
        self._sp_probe_freq = freq
        matches = self.session.worked_near(freq, state.mode, tolerance_hz=tol)
        if not matches:
            return
        worked = matches[0]
        self._call.setText(worked.call)  # reddens the field as a dupe via _refresh_indicators
        ago = _humanize_ago(utcnow() - worked.timestamp)
        self.statusBar().showMessage(
            f"Possible dupe: {worked.call} worked here {ago} "
            f"({worked.band_label} {worked.mode_group.value})",
            8000,
        )

    def _update_freq_readout(self) -> None:
        freq = self._current_freq()
        mhz, khz, hz = freq // 1_000_000, (freq // 1000) % 1000, (freq % 1000) // 10
        band = band_for_freq(freq)
        text = f"{mhz}.{khz:03d}.{hz:02d}  {band.label if band else '?'}"
        if self._cat:
            if self._radio_connected:
                # Frequency, band, and mode — the Band/Mode boxes are hidden under CAT.
                self._freq.setText(f"📻 {text}  {self._current_mode().value}")
                self._freq.setStyleSheet(f"color: {style.ACCENT}; font-weight: 600;")
            else:
                self._freq.setText("📻 no radio")
                self._freq.setStyleSheet(f"color: {style.AMBER}; font-weight: 600;")
        else:
            self._freq.setText(text)
            self._freq.setStyleSheet(f"color: {style.TEXT_DIM};")

    # ------------------------------------------------------------------ #
    # entry behavior
    # ------------------------------------------------------------------ #
    def _refresh_indicators(self) -> None:
        """Validation lives in the input boxes (no text badge): a dupe reds the call
        field, an invalid exchange entry reds that field, and a new multiplier
        (e.g. a new section) greens it."""
        call = self._call.text().strip().upper()
        if not call:
            self._esm_sent = False  # new QSO starts unsent
        freq, mode = self._current_freq(), self._current_mode()
        is_dupe = self.session.is_dupe(call, freq, mode) if call else False

        # On a dupe, filter the log to that call so you can see every QSO the whole
        # network has made with it; clear the filter when the call isn't a dupe
        # (including when the call field is emptied). Reload only on a change.
        wanted_filter = call if is_dupe else ""
        if wanted_filter != self._call_filter:
            self._call_filter = wanted_filter
            self._reload_table()

        exchange = {name: e.text().strip().upper() for name, e in self._exchange_edits.items()}
        new = self.session.new_mults(call, freq, mode, exchange) if call and not is_dupe else set()
        new_types = {mtype for mtype, _ in new}

        # A duplicate callsign reds the call field.
        self._call.setStyleSheet(f"border: 1px solid {style.DUPE};" if is_dupe else "")
        # Box each exchange field: red if its value is invalid, green if it carries a
        # new multiplier (e.g. a new section), nothing when valid and not new.
        for field in self.session.contest.exchange_fields():
            edit = self._exchange_edits[field.name]
            value = edit.text().strip().upper()
            if value and field.validator is not None and not field.validator(value):
                edit.setStyleSheet(f"border: 1px solid {style.DUPE};")
            elif value and field.name in new_types:
                edit.setStyleSheet(
                    f"border: 1px solid {style.MULT}; background-color: {style.MULT_BG};"
                )
            else:
                edit.setStyleSheet("")

        self._update_call_hint(call)
        self._update_freq_readout()
        self._update_fkey_bar()  # F-key labels follow the mode (CW vs phone)
        self._update_bottom_bars()  # show the F-key bar only for CW/SSB modes
        # Let peers see what band/mode we're on (broadcast by the presence loop).
        self.session.set_local_status(freq, mode)

    def _update_call_hint(self, call: str) -> None:
        """Tooltip on the call field: SCP partial matches + known-user flags.

        Non-intrusive — only shown when reference data is loaded and matches. SCP
        suggestions also draw from the operator's already-worked calls.
        """
        if not call:
            self._call.setToolTip(_CALL_TOOLTIP)
            return
        lines: list[str] = []
        worked = self.session.partial_matches(call)
        scp = self._refdata.is_scp_match(call)
        suggestions = sorted({*worked, *scp})[:12]
        if suggestions:
            lines.append("Matches: " + "  ".join(suggestions))
        flags = []
        if self._refdata.uses_lotw(call):
            flags.append("LoTW")
        if self._refdata.uses_eqsl(call):
            flags.append("eQSL")
        if self._refdata.qrz_known(call):
            flags.append("QRZ")
        if flags:
            lines.append("Known to: " + ", ".join(flags))
        qth = self._refdata.city_lookup(call)
        if qth:
            parts = [qth.get("name"), qth.get("state"), qth.get("section")]
            label = ", ".join(p for p in parts if p)
            if label:
                lines.append("QTH: " + label)
        self._call.setToolTip("\n".join(lines))

    def _autofill_from_history(self) -> None:
        """Pre-fill exchange fields from the imported call-history file (issue #3).

        Non-destructive: only fills fields the operator has left blank, so anything
        typed (or a correction) always wins. Runs on call changes, so clearing a
        field by hand won't be re-stomped until the callsign changes."""
        call = self._call.text().strip().upper()
        if not call:
            return
        known = self._refdata.history_lookup(call)
        if not known:
            return
        for field in self.session.contest.exchange_fields():
            edit = self._exchange_edits[field.name]
            value = known.get(field.name)
            if value and not edit.text().strip():
                edit.setText(value)

    def _advance_or_log(self) -> None:
        if not self._call.text().strip():
            self._call.setFocus()
            return
        for field in self.session.contest.exchange_fields():
            edit = self._exchange_edits[field.name]
            if field.required and not edit.text().strip():
                edit.setFocus()
                return
        self._try_log()

    def _try_log(self) -> None:
        call = self._call.text().strip().upper()
        if not call:
            self._flash(self._call)
            self._call.setFocus()
            self.statusBar().showMessage("Enter a callsign to log", 3000)
            return
        parsed = {name: e.text().strip().upper() for name, e in self._exchange_edits.items()}
        errors = self.session.validate_exchange(parsed)
        if errors:
            # Make the failure obvious: flash the first bad field and focus it.
            self._highlight_invalid(parsed)
            self.statusBar().showMessage("Not logged — " + " • ".join(errors), 5000)
            return

        # Record locally and synchronously so the log updates instantly, then
        # broadcast to peers as a best-effort side effect (offline = no-op).
        qso = self.session.record_qso(
            call=call, freq_hz=self._current_freq(), mode=self._current_mode(), exchange=parsed
        )
        self._broadcast(qso)

        self._call.clear()
        for edit in self._exchange_edits.values():
            edit.clear()
        self._call.setFocus()
        self.statusBar().showMessage(f"Logged {call}", 2500)

    def _broadcast(self, qso) -> None:
        """Fire-and-forget network broadcast; the QSO is already logged locally."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return  # no running loop (offline/tests) -> local log is enough
        try:
            loop.create_task(self.session.broadcast(qso))
        except Exception as exc:  # noqa: BLE001 - never block logging on the network
            self.statusBar().showMessage(f"Logged (broadcast deferred: {exc})", 3000)

    def _highlight_invalid(self, parsed: dict[str, str]) -> None:
        focused = False
        for field in self.session.contest.exchange_fields():
            value = parsed.get(field.name, "")
            ok = bool(value) and (field.validator is None or field.validator(value))
            if (field.required and not value) or not ok:
                edit = self._exchange_edits[field.name]
                self._flash(edit)
                if not focused:
                    edit.setFocus()
                    focused = True

    def _flash(self, widget: QLineEdit) -> None:
        """Briefly outline a field in red to signal a problem."""
        widget.setStyleSheet(f"border: 1px solid {style.DUPE}; background-color: #3a2326;")
        QTimer.singleShot(900, lambda: widget.setStyleSheet(""))

    # ------------------------------------------------------------------ #
    # refresh (fired by the session on any log change)
    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        self._update_station_bar()
        self._refresh_indicators()
        self._reload_table()

    def _reload_table(self) -> None:
        if self._call_filter:
            # Every QSO the network has logged with this call (newest first).
            qsos = [q for q in self.session.qsos() if q.call.upper() == self._call_filter]
            qsos.reverse()
        else:
            qsos = list(reversed(self.session.recent(200)))  # newest first
        self._row_qsos = qsos  # row index -> QSO (for edit/delete)
        self._table.setRowCount(len(qsos))
        current_op = self.session.engine.operator.upper()
        for row, q in enumerate(qsos):
            exchange = " ".join(
                q.exchange_rcvd.get(f.name, "") for f in self.session.contest.exchange_fields()
            )
            values = [q.timestamp.strftime("%H:%M:%S"), q.call, q.band_label, q.mode.value]
            if self.session.contest.exchanges_rst:
                values += [q.rst_sent, q.rst_rcvd]
            values += [exchange.strip(), q.operator]
            # White = this operator's own QSOs; blue = another operator's. Keyed on
            # the (persisted) operator call so it stays correct after a reopen.
            is_peer = q.operator.upper() != current_op
            for col, val in enumerate(values):
                item = QTableWidgetItem(val)
                if is_peer:
                    item.setForeground(QColor(style.PEER))
                self._table.setItem(row, col, item)

    def _qso_at(self, row: int) -> QSO | None:
        return self._row_qsos[row] if 0 <= row < len(self._row_qsos) else None

    def _show_qso_menu(self, pos) -> None:
        """Right-click menu on a log row: edit or delete the QSO."""
        from PySide6.QtWidgets import QMenu

        qso = self._qso_at(self._table.rowAt(pos.y()))
        if qso is None:
            return
        menu = QMenu(self)
        edit = menu.addAction("Edit QSO…")
        delete = menu.addAction("Delete QSO")
        chosen = menu.exec(self._table.viewport().mapToGlobal(pos))
        if chosen is edit:
            self._edit_qso(self._table.rowAt(pos.y()))
        elif chosen is delete:
            self._delete_qso(self._table.rowAt(pos.y()))

    def _delete_qso(self, row: int) -> None:
        from PySide6.QtWidgets import QMessageBox

        qso = self._qso_at(row)
        if qso is None:
            return
        confirm = QMessageBox.question(
            self,
            "Delete QSO",
            f"Delete the QSO with {qso.call} on {qso.band_label} {qso.mode.value}?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        tombstone = self.session.delete_qso(qso)
        self._broadcast(tombstone)
        self.statusBar().showMessage(f"Deleted {qso.call}", 2500)

    def _edit_qso(self, row: int) -> None:
        from partyhams.ui.qso_dialog import QsoEditDialog

        qso = self._qso_at(row)
        if qso is None:
            return
        dialog = QsoEditDialog(self.session, qso, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        if not values["call"]:
            self.statusBar().showMessage("Not saved — a callsign is required", 3000)
            return
        amended = self.session.update_qso(qso, **values)  # type: ignore[arg-type]
        self._broadcast(amended)
        self.statusBar().showMessage(f"Updated {amended.call}", 2500)

    # ------------------------------------------------------------------ #
    # export
    # ------------------------------------------------------------------ #
    def _export_adif(self, *, mine_only: bool) -> None:
        label = "my QSOs" if mine_only else "all stations"
        suggested = self._default_adif_path(mine_only=mine_only)
        path, _ = QFileDialog.getSaveFileName(
            self, f"Export ADIF — {label}", suggested, "ADIF (*.adi *.adif)"
        )
        if not path:
            return
        Path(path).write_text(self.session.export_adif(mine_only=mine_only))
        n = self._exported_qso_count(mine_only)
        self.statusBar().showMessage(f"Exported {n} QSOs ({label}) to {path}", 4000)

    def _exported_qso_count(self, mine_only: bool) -> int:
        qsos = self.session.qsos()
        if mine_only:
            sid = self.session.engine.station_id
            return sum(1 for q in qsos if q.station_id == sid)
        return len(qsos)

    def _default_adif_path(self, *, mine_only: bool = True) -> str:
        """Suggested export path: ``CALL@PARK_YYYYMMDD.adif`` in the log's folder
        (``@`` swapped for ``_`` on a filesystem that can't store it). The
        all-stations export gets a ``-all`` suffix so it doesn't overwrite the
        personal one."""
        log_path = getattr(self.session.store, "path", "")
        out_dir = Path(log_path).resolve().parent if log_path and log_path != ":memory:" else None
        call = self.session.config.my_call
        # For a multi-park (n-fer) log, use the first park in the filename — commas
        # don't belong in filenames and the full list is inside the ADIF anyway.
        park = str(self.session.config.extra.get("park", "") or "").split(",")[0].strip()
        name = park_adif_name(call, park, utcnow(), at_sign=_fs_supports_at_sign(out_dir))
        if not mine_only and name.endswith(".adif"):
            name = f"{name[:-len('.adif')]}-all.adif"
        return str(out_dir / name) if out_dir is not None else name

    def _export_cabrillo(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Cabrillo", "partyhams.cbr", "Cabrillo (*.cbr *.log)"
        )
        if path:
            Path(path).write_text(self.session.export_cabrillo())
            self.statusBar().showMessage(f"Exported Cabrillo to {path}", 4000)

    def _edit_fd_summary(self) -> bool:
        """Open the Field Day summary-info dialog (participants, club, bonuses) and
        persist the result. Returns True if the operator accepted it."""
        from partyhams.ui.fd_summary_dialog import FieldDaySummaryDialog

        dialog = FieldDaySummaryDialog(self.session.config.extra, parent=self)
        self._fd_summary_dialog = dialog  # keep alive while open
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        self.session.update_config(
            my_call=self.session.config.my_call,
            operator=self.session.engine.operator,
            sent_exchange=self.session.config.sent_exchange,
            extra=dialog.settings(),
        )
        self.refresh()
        self.statusBar().showMessage("Field Day summary info saved", 3000)
        return True

    def _export_fd_summary(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        # Prompt for the supplementary info if it's never been filled in, so the
        # exported sheet is complete (bonus points, participants, …).
        if not self.session.fd_summary_info_entered():
            choice = QMessageBox.question(
                self,
                "Field Day Summary",
                "Summary info (bonus points, participants) hasn't been entered yet.\n"
                "Fill it in now so the summary sheet is complete?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if choice == QMessageBox.StandardButton.Yes and not self._edit_fd_summary():
                return  # operator cancelled the info dialog — abort the export
        suggested = f"{self.session.config.my_call.upper() or 'fieldday'}-summary.txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Field Day Summary", suggested, "Text (*.txt)"
        )
        if path:
            Path(path).write_text(self.session.export_fieldday_summary())
            self.statusBar().showMessage(f"Exported Field Day summary to {path}", 4000)

    def _edit_qrz(self) -> None:
        from partyhams.ui.qrz_dialog import QrzDialog

        dialog = QrzDialog(self._qrz.username, self._qrz.password, parent=self)
        self._qrz_dialog = dialog  # keep alive while open
        dialog.on_test = lambda username, password: self._qrz_test(dialog, username, password)
        dialog.finished.connect(lambda result: self._qrz_done(dialog, result))
        dialog.open()

    def _qrz_test(self, dialog, username: str, password: str) -> None:
        """Run a background login + W1AW lookup to verify the entered creds."""
        if self._loop is None or not self._loop.is_running():
            dialog.show_test_result(False, "Cannot test now (no event loop running).")
            return
        self._loop.create_task(self._do_qrz_test(dialog, username, password))

    async def _do_qrz_test(self, dialog, username: str, password: str) -> None:
        """Verify QRZ credentials off the UI thread, then report into the dialog."""
        probe = QrzClient(username, password)
        ok, message = await asyncio.get_event_loop().run_in_executor(None, probe.verify)
        if self._qrz_dialog is not dialog:
            return  # dialog was closed before the test returned
        dialog.show_test_result(ok, message)

    def _qrz_done(self, dialog, result: int) -> None:
        self._qrz_dialog = None
        if result != QDialog.DialogCode.Accepted.value:
            return
        username, password = dialog.settings()
        self.set_qrz_credentials(username, password)
        if self.on_change_qrz is not None:
            self.on_change_qrz(username, password)  # app persists it
        if self._qrz_enabled():
            self.statusBar().showMessage(f"QRZ login set for {username}", 3000)
            self._qrz_lookup_now()  # look up the current call right away
        else:
            self.statusBar().showMessage("QRZ lookups disabled", 3000)

    def _edit_autoexport(self) -> None:
        from partyhams.ui.autoexport_dialog import AutoExportDialog

        dialog = AutoExportDialog(
            self._autoexport_enabled,
            self._autoexport_minutes,
            self._autoexport_only_if_new,
            parent=self,
        )
        self._autoexport_dialog = dialog  # keep alive while open
        dialog.finished.connect(lambda result: self._autoexport_done(dialog, result))
        dialog.open()

    def _autoexport_done(self, dialog, result: int) -> None:
        self._autoexport_dialog = None
        if result != QDialog.DialogCode.Accepted.value:
            return
        enabled, minutes, only_if_new = dialog.settings()
        self.set_autoexport(enabled, minutes, only_if_new)
        if self.on_change_autoexport is not None:
            self.on_change_autoexport(enabled, self._autoexport_minutes, only_if_new)
        state = "on" if enabled else "off"
        self.statusBar().showMessage(f"Auto-export {state} ({self._autoexport_minutes} min)", 3000)
