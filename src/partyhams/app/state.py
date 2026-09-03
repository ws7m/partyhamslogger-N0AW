"""Persistent application state — which log is current, and the radio choice.

Stored as a small JSON file under ``~/.partyhams/``. This is what lets the app
reopen the last log on launch and skip the radio prompt once it's been answered.
Logs themselves live in ``~/.partyhams/logs/`` and are self-describing (their
config is stored inside the SQLite file's ``meta`` table — see ``db/store.py``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

APP_DIR = Path.home() / ".partyhams"
LOGS_DIR = APP_DIR / "logs"
STATE_FILE = APP_DIR / "state.json"


#: How many recently-used logs to remember for the Recent Logs menu.
MAX_RECENT_LOGS = 8


@dataclass
class AppState:
    #: Absolute path to the log to reopen on launch (None => show log creation).
    current_log: str | None = None
    #: Saved radio choice, e.g. {"kind": "hamlib"|"flex"|"none", "conn": "..."}.
    #: Native-LAN Icom kinds also carry "user"/"password". None means the radio
    #: prompt hasn't been answered yet.
    radio: dict | None = None
    #: Recently-used log paths, most-recent first (for the Recent Logs menu).
    recent_logs: list[str] = field(default_factory=list)
    #: Selected UI theme name (None => follow the OS light/dark setting).
    theme: str | None = None
    #: Auto-CQ repeat interval in seconds (clamped to 5..30 when used).
    autocq_interval: int = 10
    #: Base UI font family (None => Qt default) and point size (clamped 8..28).
    font_family: str | None = None
    font_size: int = 13
    #: Whether the periodic ADIF auto-export is enabled.
    autoexport_enabled: bool = True
    #: Auto-export interval in minutes (clamped to 5..60 when used).
    autoexport_minutes: int = 5
    #: Only auto-export when there are new QSOs since the last auto-export.
    autoexport_only_if_new: bool = True
    #: Whether to listen for WSJT-X UDP messages (digital-mode integration).
    wsjtx_enabled: bool = False
    #: UDP port WSJT-X reports to (WSJT-X default is 2237).
    wsjtx_port: int = 2237
    #: Address WSJT-X's "UDP Server" sends to. "" = bind all interfaces (unicast);
    #: a multicast group (224.0.0.0–239.255.255.255, e.g. 224.0.0.1) is joined.
    wsjtx_host: str = ""
    #: QRZ.com XML-API credentials for callsign lookups (empty => disabled).
    qrz_username: str = ""
    qrz_password: str = ""
    #: Whether to periodically check GitHub for a newer release (privacy opt-out).
    auto_update_enabled: bool = True
    #: How often to check for updates, in hours (clamped to 1 hour .. 7 days).
    auto_update_interval_hours: int = 1
    #: Who owns the CW keyer speed: "restore" / "always" / "sync" (see app.macros).
    cw_speed_mode: str = "sync"
    #: Quick CW WPM preset buttons on the CW speed bar — click one to set the macro
    #: speed. Right-click a button to edit/delete; the + button adds one. Configurable
    #: in Radio → CW WPM Presets…. Seeded with 24 and 20.
    cw_wpm_presets: list[int] = field(default_factory=lambda: [24, 20])
    #: Master switch for the CW WPM preset feature. When False, no preset buttons or
    #: the + add button are shown on the CW speed bar at all.
    cw_presets_enabled: bool = True
    #: Run-ESM partial-call policy. False (default): a "?" in the call field sends
    #: the partial verbatim and holds the QSO. True: ESM runs the exchange anyway.
    esm_send_on_query: bool = False
    #: One-way UDP log broadcast (Logs → UDP Broadcast…). Announces each logged
    #: QSO as a WSJT-X-format ADIF datagram so other software on the LAN can pick
    #: it up. Unrelated to the peer-to-peer sync network, which is per-log.
    udp_log_enabled: bool = False
    #: Address to send to: a host, a unicast IP, or a broadcast address.
    udp_log_host: str = "127.0.0.1"
    udp_log_port: int = 2237


#: Valid TCP/UDP port range for the log-broadcast target.
PORT_MIN = 1
PORT_MAX = 65535


def clamp_port(value: object, default: int = 2237) -> int:
    """Coerce a persisted or typed port into 1..65535, falling back to ``default``.

    A hand-edited settings file (or a blank field) must not leave the app trying
    to send to port 0 or a string.
    """
    try:
        port = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(PORT_MIN, min(PORT_MAX, port))


def is_valid_host(value: str) -> bool:
    """True for something usable as a UDP destination.

    Deliberately permissive: an IPv4 address (the common case, including a
    broadcast address like 192.168.1.255) or a hostname. Rejects only what is
    obviously unusable — blank, whitespace, or an IPv4-shaped string with an
    octet out of range, which is almost always a typo rather than a hostname.
    """
    host = value.strip()
    if not host or any(c.isspace() for c in host):
        return False
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return all(0 <= int(p) <= 255 for p in parts)
    return True


def load_state(path: Path = STATE_FILE) -> AppState:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return AppState()
    return AppState(
        current_log=data.get("current_log"),
        radio=data.get("radio"),
        recent_logs=data.get("recent_logs") or [],
        theme=data.get("theme"),
        autocq_interval=data.get("autocq_interval", 10),
        font_family=data.get("font_family"),
        font_size=data.get("font_size", 13),
        autoexport_enabled=data.get("autoexport_enabled", True),
        autoexport_minutes=data.get("autoexport_minutes", 5),
        autoexport_only_if_new=data.get("autoexport_only_if_new", True),
        wsjtx_enabled=data.get("wsjtx_enabled", False),
        wsjtx_port=data.get("wsjtx_port", 2237),
        wsjtx_host=data.get("wsjtx_host", ""),
        qrz_username=data.get("qrz_username", ""),
        qrz_password=data.get("qrz_password", ""),
        auto_update_enabled=data.get("auto_update_enabled", True),
        auto_update_interval_hours=data.get("auto_update_interval_hours", 1),
        cw_speed_mode=data.get("cw_speed_mode", "sync"),
        cw_wpm_presets=[int(v) for v in data.get("cw_wpm_presets", [24, 20])],
        cw_presets_enabled=data.get("cw_presets_enabled", True),
        esm_send_on_query=data.get("esm_send_on_query", False),
        udp_log_enabled=data.get("udp_log_enabled", False),
        udp_log_host=data.get("udp_log_host", "127.0.0.1"),
        udp_log_port=clamp_port(data.get("udp_log_port", 2237)),
    )


def save_state(state: AppState, path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "current_log": state.current_log,
        "radio": state.radio,
        "recent_logs": state.recent_logs,
        "theme": state.theme,
        "autocq_interval": state.autocq_interval,
        "font_family": state.font_family,
        "font_size": state.font_size,
        "autoexport_enabled": state.autoexport_enabled,
        "autoexport_minutes": state.autoexport_minutes,
        "autoexport_only_if_new": state.autoexport_only_if_new,
        "wsjtx_enabled": state.wsjtx_enabled,
        "wsjtx_port": state.wsjtx_port,
        "wsjtx_host": state.wsjtx_host,
        "qrz_username": state.qrz_username,
        "qrz_password": state.qrz_password,
        "auto_update_enabled": state.auto_update_enabled,
        "auto_update_interval_hours": state.auto_update_interval_hours,
        "cw_speed_mode": state.cw_speed_mode,
        "cw_wpm_presets": state.cw_wpm_presets,
        "cw_presets_enabled": state.cw_presets_enabled,
        "esm_send_on_query": state.esm_send_on_query,
        "udp_log_enabled": state.udp_log_enabled,
        "udp_log_host": state.udp_log_host,
        "udp_log_port": state.udp_log_port,
    }
    path.write_text(json.dumps(payload, indent=2))


def push_recent(state: AppState, path: str) -> None:
    """Record ``path`` as the most-recently-used log (dedup, capped)."""
    recent = [p for p in state.recent_logs if p != path]
    recent.insert(0, path)
    state.recent_logs = recent[:MAX_RECENT_LOGS]


def new_log_path(
    contest_id: str, call: str, logs_dir: Path = LOGS_DIR, when: date | None = None
) -> str:
    """A unique default path for a new event: ``<contest>-<call>-<YYYYMMDD>.sqlite``.

    The date keeps recurring events distinct (e.g. Field Day 2025 vs 2026), and a
    ``-N`` suffix is appended if a log with that name already exists, so creating a
    second log the same day never overwrites the first."""
    safe = re.sub(r"[^A-Za-z0-9]+", "_", call).strip("_") or "station"
    logs_dir.mkdir(parents=True, exist_ok=True)
    day = (when or date.today()).strftime("%Y%m%d")
    base = f"{contest_id}-{safe}-{day}"
    candidate = logs_dir / f"{base}.sqlite"
    n = 2
    while candidate.exists():
        candidate = logs_dir / f"{base}-{n}.sqlite"
        n += 1
    return str(candidate)
