# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: onedir GUI build.

    pyinstaller --noconfirm dispatch.spec

Output: dist/Dispatch/Dispatch.exe. The CLI lives in the Python package (`dispatch`);
the frozen build is the desktop app.
"""
from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("dispatch", includes=["templates/**/*"])

a = Analysis(
    ["tools/frozen_entry.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "keyring.backends.Windows",
        "keyring.backends.macOS",
        "keyring.backends.SecretService",
        "keyring.backends.kwallet",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Dispatch",
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
    name="Dispatch",
)
