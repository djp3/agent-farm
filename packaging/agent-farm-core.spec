# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Python side of agent-farm ("agent-farm-core").
#
# Built by scripts/build_app.sh into dist/python/agent-farm-core/ (onedir). The Swift
# app copies that folder into agent-farm.app/Contents/Resources and runs
# agent-farm-core inside its SwiftTerm view. Nothing is signed here: release.sh
# signs the whole bundle inside-out afterwards.
import os
from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas, binaries, hiddenimports = [], [], []
for pkg in ("textual", "rich"):          # both ship data files / lazily imported modules
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["watchdog", "tkinter", "unittest", "pydoc", "doctest", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="agent-farm-core",
    debug=False,
    strip=False,
    upx=False,
    console=True,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="agent-farm-core")
