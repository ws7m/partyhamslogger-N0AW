#!/usr/bin/env python
"""Generate the platform app-file icons from the source SVG.

Rasterizes ``src/partyhams/ui/assets/icon.svg`` (via Qt's SVG renderer — already a
dependency) into the icon files PyInstaller bakes into the built app *file* itself:

    packaging/icon.icns   # macOS .app bundle icon (built with iconutil)
    packaging/icon.ico    # Windows .exe icon (multi-size, PNG-compressed)

These are committed so the release workflow picks them up (see packaging/partyhams.spec).
Re-run this whenever icon.svg changes:  python scripts/make_icons.py

The .icns step needs macOS (`iconutil`); the .ico step is cross-platform.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO = Path(__file__).resolve().parent.parent
SVG = REPO / "src" / "partyhams" / "ui" / "assets" / "icon.svg"
OUT_DIR = REPO / "packaging"

from PySide6.QtCore import QBuffer, QByteArray, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

# macOS .iconset members: (filename, pixel size). iconutil turns these into .icns.
_ICNS_SIZES = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]
# Sizes Windows wants in a .ico (256 is stored as PNG; the rest too).
_ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]


def _render(renderer: QSvgRenderer, size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    renderer.render(painter)
    painter.end()
    return img


def _png_bytes(img: QImage) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


def make_ico(renderer: QSvgRenderer, path: Path) -> None:
    """Write a multi-size .ico whose images are PNG-compressed (Vista+ format)."""
    images = [(size, _png_bytes(_render(renderer, size))) for size in _ICO_SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))  # reserved, type=1 (icon), count
    entries = b""
    data = b""
    offset = 6 + len(images) * 16  # header + one 16-byte dir entry per image
    for size, png in images:
        dim = 0 if size >= 256 else size  # 0 in the byte means 256
        entries += struct.pack(
            "<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset
        )  # w,h,colors,reserved,planes,bpp,bytes,offset
        data += png
        offset += len(png)
    path.write_bytes(header + entries + data)


def make_icns(renderer: QSvgRenderer, path: Path) -> None:
    """Write a macOS .icns via iconutil from a generated .iconset."""
    if sys.platform != "darwin":
        print("skip icon.icns (needs macOS / iconutil)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        for name, size in _ICNS_SIZES:
            (iconset / name).write_bytes(_png_bytes(_render(renderer, size)))
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(path)], check=True
        )


def main() -> int:
    if not SVG.exists():
        print(f"missing source SVG: {SVG}")
        return 1
    QGuiApplication.instance() or QGuiApplication([])
    renderer = QSvgRenderer(str(SVG))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    make_icns(renderer, OUT_DIR / "icon.icns")
    make_ico(renderer, OUT_DIR / "icon.ico")
    for f in ("icon.icns", "icon.ico"):
        p = OUT_DIR / f
        if p.exists():
            print(f"wrote {p.relative_to(REPO)} ({p.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
