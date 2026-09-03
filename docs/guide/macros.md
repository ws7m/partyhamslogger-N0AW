# Macros & ESM

![Macros editor](../screenshots/macros.png)

Open the editor from **Macros → Edit Macros…**. Macros are the twelve F-key
messages shown on the bar at the bottom of the main window. They are saved
per contest and split into banks.

## Banks

There is a bank per mode group and Run/S&P state:

- **CW · Run**, **CW · S&P**
- **Phone · Run**, **Phone · S&P**

The active bank follows your current mode and the Run/S&P toggle (Tab on the
main window), so F2 sends the right thing in each context.

## Macro content

- **CW / digital** — plain text with substitutions: `{MYCALL}`, `{CALL}`,
  `{EXCH}`, `{RST}`, `{GMAE}`, plus the actions `{LOG}` (log the QSO) and
  `{WIPE}` (clear the entry row). A **CW speed (WPM)** setting controls sending
  speed.
- **Phone** — a path to a `.wav` file that is played as your voice message.

### `{GMAE}` — greeting the time of day

`{GMAE}` expands to `GM`, `GA`, or `GE` according to your **local** clock — the
one exception to the logger's usual all-UTC rule, since a greeting has to match
the time where you are, not the time in the log:

| Local time | Sends |
| --- | --- |
| 12:00 AM – 11:59 AM | `GM` |
| 12:00 PM – 6:00 PM | `GA` |
| 6:01 PM – 11:59 PM | `GE` |

So `{GMAE} {CALL} {EXCH}` keys as `GA W1AW 5NN` on a mid-afternoon activation
and `GE W1AW 5NN` after supper. It works in every contest, not just POTA.

### `{HUNTERNAME}` — greeting a POTA regular by name

In a POTA log, `{HUNTERNAME}` expands to the first name of the station in the
call field, looked up in the [hunter roster](pota.md) as you type. A station who
isn't on the roster — or any non-POTA log — expands it to nothing, and the
leftover space is collapsed, so one macro covers both cases:

| Macro content | Worked before (Tim) | First contact |
| --- | --- | --- |
| `TU {HUNTERNAME} 5NN {LOG}` | `TU TIM 5NN` | `TU 5NN` |
| `{CALL} GM {HUNTERNAME} {EXCH}` | `W1AW GM TIM 5NN` | `W1AW GM 5NN` |

The name comes from the QRZ lookup done the first time you work someone, and is
stored per callsign — see **Tools → Edit POTA Hunters…** to correct one.

## CW WPM presets

In CW mode the speed bar shows quick **WPM preset** buttons (seeded with 24 and
20) between the CW speed box and the live keyboard sender. Click one to jump the
macro speed to that value. **Right-click** a preset to change or delete it, and
use the **+** button to add another. Manage the full list — or turn the whole
feature off — from **Radio → CW WPM Presets…**; with presets disabled the
buttons and the **+** are hidden entirely.

## ESM (Enter Sends Messages)

Toggle **Macros → ESM**. With ESM on, Enter advances through the natural
calling sequence (your call, exchange, TU) instead of just moving between
fields — fewer keystrokes during a run.

## Auto-CQ

**Macros → Auto-CQ** repeats F1 (your CQ) on a timer while you're in Run mode
and haven't started typing a callsign. Set the cadence under **Auto-CQ
Interval** (5–30 s).

## Limitations

- Phone macros need a readable `.wav` file and Qt Multimedia (bundled in
  packaged builds).
- Auto-CQ only fires in Run mode and pauses the moment you start entering a
  call, so it never CQs over a contact in progress.
- Substitution tokens are fixed to the set above.
