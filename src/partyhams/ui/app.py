"""Qt application bootstrap and startup flow.

On launch:
  1. If a *current log* is remembered, reopen it straight into the logging window.
  2. Otherwise show the log-creation screen (activity type + station setup).
  3. Once a log exists, if no radio has been configured, show the radio screen.
The current-log pointer and radio choice persist (``app/state.py``), so a normal
restart resumes silently. The asyncio loop is bridged to Qt via qasync.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import QApplication, QDialog

from partyhams.app.locks import (
    acquire_log_lock,
    is_log_in_use,
    refresh_log_lock,
    release_log_lock,
)
from partyhams.app.radio import RadioPoller
from partyhams.app.session import (
    LogSession,
    build_session,
    log_detail,
    open_session,
    summarize_log,
)
from partyhams.app.state import AppState, load_state, new_log_path, push_recent, save_state
from partyhams.radio.civ_protocol import (
    CIV_ADDR_IC705,
    CIV_ADDR_IC7300MK2,
    CIV_ADDR_IC7610,
)
from partyhams.radio.flex import FlexRadio
from partyhams.radio.hamlib import HamlibRadio
from partyhams.radio.icom_civ import IcomCIV
from partyhams.radio.icom_net import IcomNet
from partyhams.radio.icom_tcp import IcomTCP
from partyhams.ui.log_dialog import LogDialog
from partyhams.ui.main_window import MainWindow
from partyhams.ui.open_log_dialog import OpenLogDialog
from partyhams.ui.radio_dialog import RadioDialog
from partyhams.ui.style import app_icon, apply_font, apply_theme

APP_NAME = "PartyHams Logger"


class _GracefulQuitFilter(QObject):
    """Route an app-level Quit (macOS ⌘Q, the app menu's Quit) through the same
    graceful shutdown as closing the window.

    Without this, ⌘Q stops the qasync event loop while ``amain()`` is still
    awaiting the window-close event — raising "Event loop stopped before Future
    completed" and leaking a pending task. We instead run ``on_quit`` (which
    unblocks ``amain``'s teardown; it calls ``app.quit()`` itself once done) and
    consume the event so Qt doesn't tear the loop down underneath us.
    """

    def __init__(self, on_quit) -> None:
        super().__init__()
        self._on_quit = on_quit

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Quit:
            self._on_quit()
            return True  # consume: the graceful path stops the loop when ready
        return super().eventFilter(obj, event)


def _set_macos_app_name(name: str) -> None:
    """Set the macOS application menu title (the bold first menu).

    Qt reads it from the running bundle's ``CFBundleName``; for an unbundled
    Python process that's "Python". We override it via the Objective-C runtime
    (no extra dependency) and it must happen *before* QApplication is created.
    Best-effort: silently does nothing off macOS or if the runtime call fails.
    """
    if sys.platform != "darwin":
        return
    try:
        from ctypes import c_char_p, c_void_p, cdll, util

        objc = cdll.LoadLibrary(util.find_library("objc"))
        objc.objc_getClass.restype = c_void_p
        objc.objc_getClass.argtypes = [c_char_p]
        objc.sel_registerName.restype = c_void_p
        objc.sel_registerName.argtypes = [c_char_p]

        def send(receiver, selector, *args, argtypes=()):
            objc.objc_msgSend.restype = c_void_p
            objc.objc_msgSend.argtypes = [c_void_p, c_void_p, *argtypes]
            return objc.objc_msgSend(receiver, objc.sel_registerName(selector), *args)

        ns_string = objc.objc_getClass(b"NSString")

        def nsstr(text: str):
            return send(
                ns_string, b"stringWithUTF8String:", text.encode("utf-8"), argtypes=[c_char_p]
            )

        bundle = send(objc.objc_getClass(b"NSBundle"), b"mainBundle")
        info = send(bundle, b"infoDictionary")
        send(
            info,
            b"setObject:forKey:",
            nsstr(name),
            nsstr("CFBundleName"),
            argtypes=[c_void_p, c_void_p],
        )
    except Exception:  # noqa: BLE001 - cosmetic; never block startup
        pass


def _poller_from_radio(radio: dict | None) -> RadioPoller | None:
    """Build a RadioPoller from a saved radio choice, or None for manual."""
    if not radio:
        return None
    kind = radio.get("kind", "none")
    conn = radio.get("conn", "").strip()
    _civ_serial_addrs = {
        "icom705": CIV_ADDR_IC705,
        "icom7610": CIV_ADDR_IC7610,
        "icom7300mk2": CIV_ADDR_IC7300MK2,
    }
    _civ_udp_lan_addrs = {
        "icom705-lan": CIV_ADDR_IC705,
        "icom7610-lan": CIV_ADDR_IC7610,
    }
    if kind in _civ_serial_addrs:  # conn is a serial port path
        baud = int(radio.get("baud", 19200))
        return RadioPoller(IcomCIV(conn, baud=baud, civ_address=_civ_serial_addrs[kind]))
    if kind == "icom7300mk2-lan":  # conn is the radio's IP/hostname; TCP CI-V
        lan_port = int(radio.get("port", 50001))
        return RadioPoller(IcomTCP(conn, port=lan_port, civ_address=CIV_ADDR_IC7300MK2))
    if kind in _civ_udp_lan_addrs:  # conn is the radio's IP/hostname; Icom UDP native
        lan_port = int(radio.get("port", 50001))
        return RadioPoller(
            IcomNet(
                conn,
                radio.get("user", ""),
                radio.get("password", ""),
                civ_address=_civ_udp_lan_addrs[kind],
                control_port=lan_port,
            )
        )
    host, _, port_str = conn.partition(":")
    host = host.strip() or None
    port = int(port_str) if port_str.strip().isdigit() else None
    if kind == "hamlib":
        return RadioPoller(HamlibRadio(host or "127.0.0.1", port or 4532))
    if kind == "flex":
        return RadioPoller(FlexRadio(host, port or 4992))  # host=None -> discover
    return None


def _session_from_log_dialog(cfg: dict) -> tuple[LogSession, str]:
    db_path = new_log_path(cfg["contest_id"], cfg["my_call"])
    session = build_session(
        contest_id=cfg["contest_id"],
        my_call=cfg["my_call"],
        operator=cfg["operator"],
        sent_exchange=cfg["sent_exchange"],
        network=cfg["network"] or None,
        extra=cfg["extra"],
        db_path=db_path,
    )
    return session, db_path


def _remember_log(state: AppState, path: str) -> None:
    """Mark ``path`` as the current log and bump it to the top of Recent Logs."""
    state.current_log = path
    push_recent(state, path)
    save_state(state)


def _open_or_create_log(state: AppState) -> tuple[LogSession | None, bool]:
    """Reopen the remembered log, or run the creation screen.

    Returns ``(session, forced_setup)``. ``forced_setup`` is True when we deliberately
    sent the user through setup because the remembered log is already open in another
    instance on this machine — so this launch picks a *different* log (its own
    station identity) and re-verifies the radio. ``session`` is None if cancelled.
    """
    remembered = state.current_log
    in_use = bool(remembered) and is_log_in_use(remembered, now=time.time())
    if remembered and Path(remembered).exists() and not in_use:
        with contextlib.suppress(Exception):
            session = open_session(remembered)  # fall through if corrupt/old
            _remember_log(state, remembered)
            return session, False

    dialog = LogDialog()
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None, in_use
    session, db_path = _session_from_log_dialog(dialog.settings())
    _remember_log(state, db_path)
    return session, in_use


async def _start_poller(poller: RadioPoller | None, window: MainWindow) -> RadioPoller | None:
    """Start a poller, falling back to manual entry if the radio isn't reachable."""
    if poller is None:
        return None
    try:
        await poller.start()
    except Exception as exc:  # noqa: BLE001
        window.statusBar().showMessage(f"Radio not reachable, manual mode: {exc}", 5000)
        return None
    return poller


def run() -> int:
    """Launch the application. Returns the process exit code."""
    import qasync

    _set_macos_app_name(APP_NAME)  # must precede QApplication construction
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(app_icon())  # window/taskbar; also the macOS dock tile
    app.setQuitOnLastWindowClosed(False)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    state = load_state()
    apply_theme(app, state.theme)  # saved theme, or the OS-matching default
    apply_font(app, state.font_family, state.font_size)  # saved base font
    session, forced_setup = _open_or_create_log(state)
    if session is None:
        return 0

    # Prompt for a radio if none is set, or always re-verify it for a second
    # instance forced through setup (it may be on a different rig/band).
    if state.radio is None or forced_setup:
        rdlg = RadioDialog(current=state.radio)
        if rdlg.exec() == QDialog.DialogCode.Accepted:
            state.radio = rdlg.settings()
            save_state(state)

    close_event = asyncio.Event()
    # ctx carries the live state across the window loop: the session in use, the
    # next one to switch to (set by New/Open Log), and the shared radio poller.
    ctx: dict = {"session": session, "next": None, "poller": None}

    def _graceful_quit() -> None:
        ctx["next"] = None  # quit, don't reopen another window
        if not close_event.is_set():
            close_event.set()  # let amain() finish teardown, then it calls app.quit()

    quit_filter = _GracefulQuitFilter(_graceful_quit)
    app.installEventFilter(quit_filter)

    # Quit cleanly on Ctrl-C / kill from a terminal. Without this, SIGINT raises
    # KeyboardInterrupt inside a Qt virtual override (e.g. eventFilter), where Qt
    # swallows it — so the app keeps running and spams a traceback on every ^C.
    # Routing the signal through the asyncio loop runs the same graceful teardown
    # as ⌘Q / closing the window. Falls back to immediate exit where the loop
    # can't install signal handlers (e.g. Windows); state is persisted on change.
    import signal

    for _sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(_sig, _graceful_quit)
        except (NotImplementedError, RuntimeError, ValueError):
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            break

    def _active_log_path() -> str:
        s = ctx["session"]
        return getattr(s.store, "path", "") if s is not None else ""

    # Heartbeat the active log's lock so other instances can tell we're alive
    # (and reclaim the lock if we crash). See app/locks.py.
    lock_timer = QTimer()
    lock_timer.setInterval(60_000)
    lock_timer.timeout.connect(lambda: refresh_log_lock(_active_log_path(), now=time.time()))
    lock_timer.start()

    async def amain() -> None:
        while ctx["session"] is not None:
            active = ctx["session"]
            await active.start()
            acquire_log_lock(_active_log_path(), now=time.time())
            window = MainWindow(active, on_close=close_event.set)
            window.on_change_radio = lambda w=window: _request_radio_change(w)
            window.on_new_log = lambda w=window: _new_log_flow(w)
            window.on_open_log = lambda w=window: _open_log_flow(w)
            window.on_open_log_path = _open_recent
            window.recent_logs_provider = _recent_entries
            window.on_change_theme = _change_theme
            window.on_change_font = _change_font
            window.on_autocq_interval = _change_autocq_interval
            window.set_autocq_interval(state.autocq_interval)
            window.on_change_autoexport = _change_autoexport
            window.set_autoexport(
                state.autoexport_enabled,
                state.autoexport_minutes,
                state.autoexport_only_if_new,
            )
            window.on_change_wsjtx = _change_wsjtx
            window.set_wsjtx(state.wsjtx_enabled, state.wsjtx_port, state.wsjtx_host)
            window.on_change_udp_log = _change_udp_log
            window.set_udp_log(
                state.udp_log_enabled, state.udp_log_host, state.udp_log_port
            )
            window.on_change_qrz = _change_qrz
            window.set_qrz_credentials(state.qrz_username, state.qrz_password)
            window.on_change_auto_update = _change_auto_update
            window.set_auto_update(state.auto_update_enabled, state.auto_update_interval_hours)
            window.on_change_cw_speed_mode = _change_cw_speed_mode
            window.set_cw_speed_mode(state.cw_speed_mode)
            window.on_change_cw_wpm_presets = _change_cw_wpm_presets
            window.set_cw_wpm_presets(state.cw_wpm_presets, state.cw_presets_enabled)
            window.on_change_esm_send_on_query = _change_esm_send_on_query
            window.set_esm_send_on_query(state.esm_send_on_query)
            window.show()
            ctx["poller"] = await _start_poller(_poller_from_radio(state.radio), window)
            window.set_poller(ctx["poller"])

            await close_event.wait()  # window closed, or a log switch was requested
            close_event.clear()

            if ctx["poller"] is not None:
                await ctx["poller"].stop()
                ctx["poller"] = None
            await window.stop_wsjtx()
            release_log_lock(_active_log_path())  # let other instances reuse this log
            await active.stop()
            window._on_close = None  # don't re-fire close_event during teardown
            if window._sections_window is not None:
                window._sections_window.close()
            window.close()
            window.deleteLater()

            ctx["session"], ctx["next"] = ctx["next"], None
        app.quit()

    # --- log switching (New / Open) ---
    def _new_log_flow(window: MainWindow) -> None:
        dialog = LogDialog(parent=window)
        window._log_dialog = dialog
        dialog.finished.connect(lambda result: _new_log_done(dialog, result))
        dialog.open()

    def _new_log_done(dialog: LogDialog, result: int) -> None:
        if result == QDialog.DialogCode.Accepted.value:
            new_session, db_path = _session_from_log_dialog(dialog.settings())
            _remember_log(state, db_path)
            _switch_to(new_session)

    def _open_log_flow(window: MainWindow) -> None:
        dialog = OpenLogDialog(parent=window)
        window._log_dialog = dialog
        dialog.finished.connect(lambda result: _open_log_done(dialog, result))
        dialog.open()

    def _open_log_done(dialog: OpenLogDialog, result: int) -> None:
        path = dialog.selected_path()
        if result == QDialog.DialogCode.Accepted.value and path:
            _open_recent(path)

    def _open_recent(path: str) -> None:
        if path == state.current_log:
            return  # already open
        try:
            new_session = open_session(path)
        except Exception:  # noqa: BLE001 - unreadable/foreign/missing file; ignore
            return
        _remember_log(state, path)
        _switch_to(new_session)

    def _recent_entries() -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        for path in state.recent_logs:
            if path == state.current_log:
                continue  # the active log isn't a switch target
            summary = summarize_log(path)
            if summary is None:
                continue  # skip deleted/unreadable logs
            label = summary["contest"]
            if summary["call"]:
                label = f"{label} — {summary['call']}"
            detail = log_detail(summary)
            if detail:
                label = f"{label} · {detail}"
            entries.append((path, label))
        return entries

    def _switch_to(new_session: LogSession) -> None:
        ctx["next"] = new_session
        close_event.set()  # ends the current window's wait; the loop rebuilds

    # --- radio change (live) ---
    def _request_radio_change(window: MainWindow) -> None:
        # Non-blocking (open() + finished) so we never spin a nested event loop
        # inside a running task — that re-enters the asyncio scheduler.
        dialog = RadioDialog(current=state.radio, parent=window)
        window._radio_dialog = dialog
        dialog.finished.connect(lambda result: _on_radio_dialog_done(window, dialog, result))
        dialog.open()

    def _on_radio_dialog_done(window: MainWindow, dialog: RadioDialog, result: int) -> None:
        window._radio_dialog = None
        if result == QDialog.DialogCode.Accepted.value:
            state.radio = dialog.settings()
            save_state(state)
            loop.create_task(_apply_radio(window))

    async def _apply_radio(window: MainWindow) -> None:
        if ctx["poller"] is not None:
            await ctx["poller"].stop()
        ctx["poller"] = await _start_poller(_poller_from_radio(state.radio), window)
        window.set_poller(ctx["poller"])

    # --- Auto-CQ interval (live + persisted) ---
    def _change_autocq_interval(seconds: int) -> None:
        state.autocq_interval = seconds
        save_state(state)

    # --- ADIF auto-export settings (live + persisted) ---
    def _change_autoexport(enabled: bool, minutes: int, only_if_new: bool) -> None:
        state.autoexport_enabled = enabled
        state.autoexport_minutes = minutes
        state.autoexport_only_if_new = only_if_new
        save_state(state)

    # --- WSJT-X UDP settings (live + persisted) ---
    def _change_wsjtx(enabled: bool, port: int, host: str = "") -> None:
        state.wsjtx_enabled = enabled
        state.wsjtx_port = port
        state.wsjtx_host = host
        save_state(state)

    # --- UDP log broadcast (persisted) ---
    def _change_udp_log(enabled: bool, host: str, port: int) -> None:
        state.udp_log_enabled = enabled
        state.udp_log_host = host
        state.udp_log_port = port
        save_state(state)

    # --- QRZ credentials (persisted) ---
    def _change_qrz(username: str, password: str) -> None:
        state.qrz_username = username
        state.qrz_password = password
        save_state(state)

    # --- auto-update preference (persisted) ---
    def _change_auto_update(enabled: bool, interval_hours: int) -> None:
        state.auto_update_enabled = enabled
        state.auto_update_interval_hours = interval_hours
        save_state(state)

    def _change_cw_speed_mode(mode: str) -> None:
        state.cw_speed_mode = mode
        save_state(state)

    def _change_cw_wpm_presets(presets: list[int], enabled: bool) -> None:
        state.cw_wpm_presets = presets
        state.cw_presets_enabled = enabled
        save_state(state)

    def _change_esm_send_on_query(on: bool) -> None:
        state.esm_send_on_query = on
        save_state(state)

    # --- theme change (live) ---
    def _change_theme(name: str) -> None:
        state.theme = apply_theme(app, name)
        save_state(state)
        for win in app.topLevelWidgets():
            if isinstance(win, MainWindow):
                win.restyle()

    # --- font change (live + persisted) ---
    def _change_font(family: str | None, size: int) -> None:
        state.font_family, state.font_size = apply_font(app, family, size)
        save_state(state)
        for win in app.topLevelWidgets():
            if isinstance(win, MainWindow):
                win.restyle()

    with loop:
        try:
            loop.run_until_complete(amain())
        except RuntimeError as exc:
            # Belt-and-suspenders: if a quit path still stops the loop before
            # amain() finishes (the window is already gone by then), treat this
            # specific qasync error as a normal exit instead of crashing.
            if "Event loop stopped before Future completed" not in str(exc):
                raise
    return 0
