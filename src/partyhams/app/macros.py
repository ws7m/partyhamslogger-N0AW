"""F-key macros: variable substitution and per-contest persistence.

A macro's CW/digital ``content`` is text with ``{VAR}`` substitutions and inline
``{ACTION}`` markers; phone content is a ``.wav`` path. Substitution is N1MM-style
but uses our ``{NAME}`` syntax (e.g. ``{MYCALL}``, ``{CALL}``, ``{EXCH}``).
The UI supplies the values; a token with no value expands to empty, which is
how ``{HUNTERNAME}`` disappears for a station that isn't on the POTA roster.

Each *event* (contest) keeps its own macro set, persisted under
``~/.partyhams/macros/<contest_id>.json``; missing files fall back to the
contest's :meth:`~partyhams.contest.base.ContestDefinition.default_macros`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from partyhams.app.state import APP_DIR
from partyhams.contest.base import ContestDefinition, Macro

MACROS_DIR = APP_DIR / "macros"
DEFAULT_WPM = 28

# CW keyer speed bounds (WPM).
WPM_MIN = 5
WPM_MAX = 60

# Who owns the CW keyer speed when a radio is connected (see the Radio menu):
#   RESTORE — the logger sets its speed while keying, then restores the radio's
#             own speed when it stops sending (the rig's knob "wins" between sends).
#   ALWAYS  — the logger asserts its speed on every transmission.
#   SYNC    — the logger follows speed changes made on the radio, and pushes its
#             own changes to the radio immediately (both stay in agreement).
CW_SPEED_RESTORE = "restore"
CW_SPEED_ALWAYS = "always"
CW_SPEED_SYNC = "sync"
CW_SPEED_MODES: tuple[str, ...] = (CW_SPEED_RESTORE, CW_SPEED_ALWAYS, CW_SPEED_SYNC)
CW_SPEED_DEFAULT = CW_SPEED_SYNC

# Human-readable menu labels, in display order.
CW_SPEED_LABELS: dict[str, str] = {
    CW_SPEED_RESTORE: "Restore radio speed after sending",
    CW_SPEED_ALWAYS: "Logger always sets speed",
    CW_SPEED_SYNC: "Sync with radio",
}


def clamp_wpm(wpm: int) -> int:
    """Clamp a CW keyer speed into the supported WPM range."""
    return max(WPM_MIN, min(WPM_MAX, int(wpm)))


def normalize_wpm_presets(values: list[int]) -> list[int]:
    """Clamp each CW WPM preset into range and drop duplicates, preserving order."""
    out: list[int] = []
    for value in values:
        try:
            wpm = clamp_wpm(value)
        except (TypeError, ValueError):
            continue
        if wpm not in out:
            out.append(wpm)
    return out


def normalize_cw_speed_mode(mode: str | None) -> str:
    """Coerce a persisted/selected CW-speed mode to a known value (else default)."""
    return mode if mode in CW_SPEED_MODES else CW_SPEED_DEFAULT


def cw_duration_seconds(text: str, wpm: int) -> float:
    """Best-effort estimate of how long ``text`` takes to key at ``wpm``.

    Uses the PARIS standard (one dot-unit = 1.2/wpm seconds) and a rough average
    of ten dot-units per character (including inter-character/word spacing). Used
    only to schedule the "restore radio speed after sending" so the restore lands
    *after* keying finishes; an overestimate is harmless, an underestimate would
    change speed mid-CW, so callers pad it.
    """
    wpm = max(1, int(wpm))
    dot = 1.2 / wpm
    units = max(1, len(text)) * 10
    return units * dot

# Time-of-day greeting boundaries, in **local** minutes past midnight. Everything
# else in the logger runs on UTC, but a greeting has to match the clock on the
# operator's wall, so this one deliberately uses local time.
_AFTERNOON_FROM = 12 * 60  # 12:00 PM
_EVENING_FROM = 18 * 60 + 1  # 6:01 PM (6:00 PM itself is still afternoon)


def greeting_for(when: datetime | None = None) -> str:
    """The CW greeting for a local wall-clock time: ``GM`` / ``GA`` / ``GE``.

    Morning runs from midnight through 11:59 AM, afternoon from 12:00 PM through
    6:00 PM, and evening from 6:01 PM through 11:59 PM. ``when`` defaults to the
    current local time; pass a naive local ``datetime`` to test a boundary.
    """
    when = when or datetime.now()
    minutes = when.hour * 60 + when.minute
    if minutes < _AFTERNOON_FROM:
        return "GM"
    if minutes < _EVENING_FROM:
        return "GA"
    return "GE"


# Inline markers that trigger an action instead of being sent as text.
_ACTIONS = {"LOG", "WIPE"}
_TOKEN_RE = re.compile(r"\{([A-Za-z0-9_&?]+)\}")


def expand(template: str, context: dict[str, str]) -> tuple[str, list[str]]:
    """Expand ``{VAR}`` tokens and pull out ``{ACTION}`` markers.

    Returns ``(text_to_send, actions)`` where actions is a lowercase list like
    ``["log"]``. Unknown ``{tokens}`` expand to empty. Whitespace is collapsed.
    """
    actions: list[str] = []

    def replace(match: re.Match) -> str:
        name = match.group(1).upper()
        if name in _ACTIONS:
            actions.append(name.lower())
            return ""
        return str(context.get(name, ""))

    text = _TOKEN_RE.sub(replace, template)
    text = re.sub(r"\s+", " ", text).strip()
    return text, actions


def bank_key(mode_group: str, run: bool) -> str:
    """Macro bank id for a mode group + Run/S&P, e.g. ``"CW.RUN"`` / ``"PHONE.SP"``."""
    return f"{mode_group}.{'RUN' if run else 'SP'}"


@dataclass
class ESMStep:
    """What the Enter key should do next under ESM (Enter Sends Messages).

    ``key`` is the F-key to fire (None = just focus the call field); the flags
    drive the surrounding UI actions.
    """

    key: int | None
    set_sent: bool = False  # mark "we've sent our exchange/call" for this QSO
    reset: bool = False  # QSO finished -> clear the sent flag
    log: bool = False  # log the QSO after firing the key
    focus_exchange: bool = False  # move to the first empty exchange field
    query: bool = False  # partial call ("?"): send the call verbatim, don't advance


def esm_step(
    run: bool,
    call_present: bool,
    esm_sent: bool,
    exch_complete: bool,
    call_uncertain: bool = False,
    send_on_query: bool = False,
) -> ESMStep:
    """Map the current entry state to the next ESM action (N1MM-style).

    Run:  CQ (F1) → send exchange (F2) → TU + log (F3).
    S&P:  send my call (F4) → send exchange + log (F2).

    ``call_uncertain`` is true when the call field holds a partial call (a typed
    ``?``, e.g. ``N0?W``). In Run, unless ``send_on_query`` is set, Enter sends the
    partial back verbatim and the QSO does *not* advance (returns ``query=True``).
    S&P is unaffected.
    """
    if run:
        if not call_present:
            return ESMStep(1)  # CQ
        if call_uncertain and not send_on_query:
            # Partial call: send it (e.g. "N0?W") and hold — don't run the exchange.
            return ESMStep(None, query=True)
        if not esm_sent:
            return ESMStep(2, set_sent=True, focus_exchange=True)  # send exchange
        if not exch_complete:
            return ESMStep(2)  # repeat exchange while we wait
        return ESMStep(3, reset=True)  # TU (F3 logs via {LOG})
    # Search & Pounce
    if not call_present:
        return ESMStep(None)
    if not esm_sent:
        return ESMStep(4, set_sent=True, focus_exchange=True)  # send my call
    if not exch_complete:
        return ESMStep(4)  # resend my call
    return ESMStep(2, log=True, reset=True)  # send exchange, then log


@dataclass
class MacroSet:
    cw_wpm: int = DEFAULT_WPM  # macro / F-key sending speed
    cw_kbd_wpm: int = DEFAULT_WPM  # speed for the live keyboard CW sender
    groups: dict[str, list[Macro]] = field(default_factory=dict)

    def get(self, group: str, key: int) -> Macro | None:
        for macro in self.groups.get(group, []):
            if macro.key == key:
                return macro
        return None

    @classmethod
    def from_defaults(cls, contest: ContestDefinition) -> MacroSet:
        return cls(
            cw_wpm=DEFAULT_WPM, cw_kbd_wpm=DEFAULT_WPM, groups=contest.default_macros()
        )


def _macros_path(contest_id: str, macros_dir: Path = MACROS_DIR) -> Path:
    return macros_dir / f"{contest_id}.json"


def load_macros(contest: ContestDefinition, macros_dir: Path = MACROS_DIR) -> MacroSet:
    """Load this event's macros, or the contest defaults if none are saved."""
    path = _macros_path(contest.id, macros_dir)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return MacroSet.from_defaults(contest)
    groups = {
        group: [Macro(m["key"], m["label"], m["content"]) for m in macros]
        for group, macros in data.get("groups", {}).items()
    }
    if not groups:
        groups = contest.default_macros()
    cw_wpm = int(data.get("cw_wpm", DEFAULT_WPM))
    # Older logs have no keyboard speed — default it to the macro speed.
    cw_kbd_wpm = int(data.get("cw_kbd_wpm", cw_wpm))
    return MacroSet(cw_wpm=cw_wpm, cw_kbd_wpm=cw_kbd_wpm, groups=groups)


def save_macros(contest_id: str, macro_set: MacroSet, macros_dir: Path = MACROS_DIR) -> None:
    macros_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "cw_wpm": macro_set.cw_wpm,
        "cw_kbd_wpm": macro_set.cw_kbd_wpm,
        "groups": {
            group: [{"key": m.key, "label": m.label, "content": m.content} for m in macros]
            for group, macros in macro_set.groups.items()
        },
    }
    _macros_path(contest_id, macros_dir).write_text(json.dumps(payload, indent=2))
