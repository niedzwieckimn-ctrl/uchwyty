# -*- mode: python ; coding: utf-8 -*-
"""Portable, windowed Windows build. User data stays in LOCALAPPDATA."""

from pathlib import Path

root = Path(SPECPATH).resolve()

a = Analysis(
    [str(root / "run_app.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=[(
        str(root / "src" / "annual_inventory" / "persistence" / "migrations"),
        "annual_inventory/persistence/migrations",
    )],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# The build host also has Poppler in its runtime search path. PyInstaller may
# collect Poppler's versioned ICU 78 DLLs under the generic names below.
# Qt needs the unversioned Windows ICU API, so those foreign DLLs must not
# shadow the system libraries. This is verified by the isolated EXE smoke test.
a.binaries = [
    entry for entry in a.binaries
    if Path(entry[0]).name.casefold() not in {"icuuc.dll", "icudt78.dll"}
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RoczneRozliczenie",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="RoczneRozliczenie",
)
