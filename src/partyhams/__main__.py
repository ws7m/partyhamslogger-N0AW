"""``python -m partyhams`` / the ``partyhams`` console script entry point."""

from __future__ import annotations

import logging
import os
import sys
import traceback
from pathlib import Path

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    stream=sys.stderr,
)

# Always write a rolling radio debug log so CI-V frame traces are available
# without needing to re-run from a terminal.  The file resets on each launch
# (mode="w") so it never grows unboundedly.
_radio_log_path = Path.home() / ".partyhams" / "radio-debug.log"
try:
    _radio_log_path.parent.mkdir(parents=True, exist_ok=True)
    _fh = logging.FileHandler(_radio_log_path, mode="w", encoding="utf-8")
    _fh.setLevel(logging.DEBUG)
    _fh.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s"))
    logging.getLogger("partyhams.radio.icom_tcp").addHandler(_fh)
    logging.getLogger("partyhams.radio.icom_tcp").setLevel(logging.DEBUG)
except OSError:
    pass  # non-fatal if the log file can't be opened

# PARTYHAMS_RADIO_DEBUG=1 also mirrors CI-V frames to stderr.
if os.environ.get("PARTYHAMS_RADIO_DEBUG"):
    logging.getLogger("partyhams.radio.icom_tcp").setLevel(logging.DEBUG)


def main() -> int:
    # Imported lazily so the headless core stays importable without PySide6.
    try:
        from partyhams.ui.app import run
    except ImportError as exc:  # pragma: no cover
        print(
            "PartyHams needs PySide6 to launch the UI. Install dev/runtime deps:\n"
            "  uv pip install -e .\n"
            f"(import error: {exc})",
            file=sys.stderr,
        )
        return 1

    try:
        return run()
    except Exception:  # noqa: BLE001 - top-level crash reporter
        report = traceback.format_exc()
        log_path = Path.cwd() / "partyhams-error.log"
        try:
            log_path.write_text(report)
        except OSError:
            log_path = None
        print("\n" + "=" * 70, file=sys.stderr)
        print("PartyHams hit an error and had to stop. Please share this:", file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        print(report, file=sys.stderr)
        if log_path is not None:
            print(f"(also saved to {log_path})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
