"""F-key macros: variable expansion, persistence, and Field Day defaults."""

from __future__ import annotations

from datetime import datetime

from partyhams.app.macros import (
    DEFAULT_WPM,
    WPM_MAX,
    WPM_MIN,
    MacroSet,
    bank_key,
    esm_step,
    expand,
    greeting_for,
    load_macros,
    normalize_wpm_presets,
    save_macros,
)
from partyhams.contest import get


def test_normalize_wpm_presets_clamps_and_dedupes():
    # Out-of-range values clamp; duplicates drop; order is preserved.
    assert normalize_wpm_presets([24, 20]) == [24, 20]
    assert normalize_wpm_presets([1000, -5]) == [WPM_MAX, WPM_MIN]
    assert normalize_wpm_presets([24, 24, 20]) == [24, 20]
    assert normalize_wpm_presets([]) == []


def test_expand_substitutes_variables():
    ctx = {"MYCALL": "N0AW", "CALL": "K1ABC", "EXCH": "2A OR"}
    text, actions = expand("CQ FD {MYCALL} {MYCALL} FD", ctx)
    assert text == "CQ FD N0AW N0AW FD"
    assert actions == []

    text, actions = expand("{CALL} {EXCH}", ctx)
    assert text == "K1ABC 2A OR"


def test_expand_extracts_actions_and_collapses_space():
    ctx = {"MYCALL": "N0AW"}
    text, actions = expand("TU {MYCALL} FD {LOG}", ctx)
    assert text == "TU N0AW FD"  # the {LOG} marker is removed from the sent text
    assert actions == ["log"]

    text, actions = expand("  {WIPE}  ", {})
    assert text == ""
    assert actions == ["wipe"]


def test_expand_unknown_token_is_empty():
    text, _ = expand("{MYCALL} {NOPE}", {"MYCALL": "N0AW"})
    assert text == "N0AW"


def test_field_day_default_macros():
    macros = get("arrl-field-day").default_macros()
    assert set(macros) == {"CW.RUN", "CW.SP", "PHONE.RUN", "PHONE.SP"}
    run = {m.key: m.content for m in macros["CW.RUN"]}
    assert run[1] == "CQ FD {MYCALL} {MYCALL} FD"
    assert "{LOG}" in run[3]  # F3 TU logs the QSO in Run
    assert run[12] == "{WIPE}"
    sp = {m.key: m.content for m in macros["CW.SP"]}
    assert sp[3] == "TU {LOG}"  # S&P gets a briefer TU


def test_greeting_morning_from_midnight_to_noon():
    for hour, minute in ((0, 0), (0, 1), (6, 30), (11, 59)):
        assert greeting_for(datetime(2026, 9, 3, hour, minute)) == "GM"


def test_greeting_afternoon_from_noon_through_six():
    for hour, minute in ((12, 0), (12, 1), (15, 45), (18, 0)):
        assert greeting_for(datetime(2026, 9, 3, hour, minute)) == "GA"


def test_greeting_evening_from_one_past_six_to_midnight():
    for hour, minute in ((18, 1), (20, 0), (23, 59)):
        assert greeting_for(datetime(2026, 9, 3, hour, minute)) == "GE"


def test_greeting_boundaries_are_exact():
    """The two changeover minutes, which is where an off-by-one would hide."""
    assert greeting_for(datetime(2026, 9, 3, 11, 59)) == "GM"
    assert greeting_for(datetime(2026, 9, 3, 12, 0)) == "GA"
    assert greeting_for(datetime(2026, 9, 3, 18, 0)) == "GA"
    assert greeting_for(datetime(2026, 9, 3, 18, 1)) == "GE"


def test_greeting_ignores_seconds_within_a_boundary_minute():
    assert greeting_for(datetime(2026, 9, 3, 18, 0, 59)) == "GA"


def test_greeting_defaults_to_now():
    assert greeting_for() in {"GM", "GA", "GE"}


def test_greeting_expands_in_a_macro():
    text, _ = expand("{GMAE} {CALL}", {"GMAE": "GA", "CALL": "W1AW"})
    assert text == "GA W1AW"


def test_bank_key():
    assert bank_key("CW", run=True) == "CW.RUN"
    assert bank_key("PHONE", run=False) == "PHONE.SP"


def test_esm_step_run():
    assert esm_step(True, call_present=False, esm_sent=False, exch_complete=False).key == 1
    s = esm_step(True, call_present=True, esm_sent=False, exch_complete=False)
    assert s.key == 2 and s.set_sent and s.focus_exchange
    assert esm_step(True, call_present=True, esm_sent=True, exch_complete=False).key == 2
    done = esm_step(True, call_present=True, esm_sent=True, exch_complete=True)
    assert done.key == 3 and done.reset


def test_esm_step_sp():
    assert esm_step(False, call_present=False, esm_sent=False, exch_complete=False).key is None
    s = esm_step(False, call_present=True, esm_sent=False, exch_complete=False)
    assert s.key == 4 and s.set_sent and s.focus_exchange
    final = esm_step(False, call_present=True, esm_sent=True, exch_complete=True)
    assert final.key == 2 and final.log and final.reset


def test_esm_step_partial_call_run_holds():
    # A "?" in the call field (partial call) holds the QSO in Run: query step, no advance.
    s = esm_step(
        True, call_present=True, esm_sent=False, exch_complete=False, call_uncertain=True
    )
    assert s.query and s.key is None and not s.set_sent and not s.log and not s.reset
    # The hold persists even after we've sent the exchange, until the "?" is gone.
    s2 = esm_step(
        True, call_present=True, esm_sent=True, exch_complete=True, call_uncertain=True
    )
    assert s2.query and s2.key is None


def test_esm_step_partial_call_send_anyway():
    # With the opt-in policy, a "?" no longer holds — ESM runs the exchange as usual.
    s = esm_step(
        True,
        call_present=True,
        esm_sent=False,
        exch_complete=False,
        call_uncertain=True,
        send_on_query=True,
    )
    assert not s.query and s.key == 2 and s.set_sent


def test_esm_step_partial_call_sp_unaffected():
    # S&P ignores the partial-call hold entirely (Run-only feature).
    s = esm_step(
        False, call_present=True, esm_sent=False, exch_complete=False, call_uncertain=True
    )
    assert not s.query and s.key == 4 and s.set_sent


def test_esm_step_empty_call_still_cqs():
    # An empty field still CQs even if flagged uncertain (no call to send back).
    s = esm_step(
        True, call_present=False, esm_sent=False, exch_complete=False, call_uncertain=True
    )
    assert not s.query and s.key == 1


def test_load_defaults_and_round_trip(tmp_path):
    contest = get("arrl-field-day")
    # No file yet -> contest defaults.
    ms = load_macros(contest, macros_dir=tmp_path)
    assert ms.cw_wpm == DEFAULT_WPM
    assert ms.cw_kbd_wpm == DEFAULT_WPM
    assert ms.get("CW.RUN", 1).content == "CQ FD {MYCALL} {MYCALL} FD"

    # Customize + save, then reload from the per-contest file.
    ms.cw_wpm = 32
    ms.cw_kbd_wpm = 18  # the separate keyboard speed round-trips too
    ms.get("CW.RUN", 1).content = "CQ TEST {MYCALL}"
    save_macros(contest.id, ms, macros_dir=tmp_path)
    assert (tmp_path / "arrl-field-day.json").exists()

    reloaded = load_macros(contest, macros_dir=tmp_path)
    assert reloaded.cw_wpm == 32
    assert reloaded.cw_kbd_wpm == 18
    assert reloaded.get("CW.RUN", 1).content == "CQ TEST {MYCALL}"


def test_macroset_get_missing():
    ms = MacroSet()
    assert ms.get("CW", 1) is None


def test_clamp_wpm_bounds():
    from partyhams.app.macros import WPM_MAX, WPM_MIN, clamp_wpm

    assert clamp_wpm(24) == 24
    assert clamp_wpm(WPM_MIN - 5) == WPM_MIN
    assert clamp_wpm(WPM_MAX + 99) == WPM_MAX
    assert clamp_wpm(WPM_MIN) == WPM_MIN
    assert clamp_wpm(WPM_MAX) == WPM_MAX


def test_normalize_cw_speed_mode():
    from partyhams.app.macros import (
        CW_SPEED_DEFAULT,
        CW_SPEED_RESTORE,
        normalize_cw_speed_mode,
    )

    assert normalize_cw_speed_mode("restore") == CW_SPEED_RESTORE
    assert normalize_cw_speed_mode("sync") == "sync"
    assert normalize_cw_speed_mode("bogus") == CW_SPEED_DEFAULT
    assert normalize_cw_speed_mode(None) == CW_SPEED_DEFAULT


def test_cw_duration_seconds_scales_with_length_and_speed():
    from partyhams.app.macros import cw_duration_seconds

    # Longer text takes longer; faster speed takes less time.
    assert cw_duration_seconds("CQ TEST", 25) > cw_duration_seconds("CQ", 25)
    assert cw_duration_seconds("CQ TEST", 40) < cw_duration_seconds("CQ TEST", 20)
    # Always a positive, finite estimate (even for empty text / silly speeds).
    assert cw_duration_seconds("", 25) > 0
    assert cw_duration_seconds("X", 0) > 0  # wpm floored to 1, no ZeroDivision
