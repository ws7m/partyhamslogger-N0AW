# PyInstaller build spec for PartyHams Logger.
#
# Builds a self-contained app for the platform it runs on:
#   - Windows  -> dist/PartyHamsLogger/PartyHamsLogger.exe
#   - macOS    -> dist/PartyHamsLogger.app  (+ dist/PartyHamsLogger/)
#   - Linux    -> dist/PartyHamsLogger/PartyHamsLogger
#
# Run from the repo root:  pyinstaller --noconfirm --clean packaging/partyhams.spec
# (the Makefile `package*` targets wrap this). PyInstaller must run on the SAME
# OS/arch you're targeting — there is no cross-compilation.

import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))  # noqa: F821 (SPECPATH injected)
SRC = os.path.join(ROOT, "src")

APP_NAME = "PartyHamsLogger"
ICON_SVG = os.path.join(SRC, "partyhams", "ui", "assets", "icon.svg")

# The app-file icon for this platform (icon.icns on macOS, icon.ico on Windows),
# generated from icon.svg by scripts/make_icons.py. Linux desktop icons come from
# a .desktop file, not the binary, so PyInstaller's icon arg is unused there.
_icon_name = {"darwin": "icon.icns", "win32": "icon.ico"}.get(sys.platform)
icon_file = None
if _icon_name:
    candidate = os.path.join(SPECPATH, _icon_name)  # noqa: F821
    if os.path.exists(candidate):
        icon_file = candidate

# Bundle the app icon (loaded at runtime via Path(__file__).parent in ui/style.py)
# and make sure the self-registering radio/contest backends are pulled in.
datas = [(ICON_SVG, os.path.join("partyhams", "ui", "assets"))]

# Bundle the user-guide docs so the in-app Help viewer works from a packaged
# build (resolved at runtime via sys._MEIPASS in ui/help_window.py:find_docs_dir).
for _sub in ("guide", "screenshots"):
    _src = os.path.join(ROOT, "docs", _sub)
    if os.path.isdir(_src):
        datas.append((_src, os.path.join("docs", _sub)))
_wsjtx = os.path.join(ROOT, "docs", "WSJTX.md")
if os.path.isfile(_wsjtx):
    datas.append((_wsjtx, "docs"))
# Bundle certifi's CA bundle so HTTPS (QRZ, update check, POTA) verifies in the
# packaged app, which has no OS trust store. certifi.where() resolves to this at
# runtime — see partyhams/core/certs.py.
datas += collect_data_files("certifi")
hiddenimports = (
    collect_submodules("partyhams.radio")
    + collect_submodules("partyhams.contest")
    + ["PySide6.QtMultimedia", "certifi"]  # QtMultimedia: voice macros; certifi: HTTPS CA bundle
)

a = Analysis(
    [os.path.join(SRC, "partyhams", "__main__.py")],
    pathex=[SRC],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

# Target architecture: PyInstaller forbids --target-arch on the command line when
# a .spec is used, so we read it from the environment instead (set by the Makefile
# `package-mac-universal` target). None => build for the host architecture.
target_arch = os.environ.get("PYI_TARGET_ARCH") or None

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    console=False,  # GUI app — no console window on Windows
    target_arch=target_arch,
    icon=icon_file,
)
coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME)  # noqa: F821

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name=f"{APP_NAME}.app",
        icon=icon_file,
        bundle_identifier="com.jeremymturner.partyhams",
    )
