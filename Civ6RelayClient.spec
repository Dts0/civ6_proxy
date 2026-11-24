# -*- mode: python ; coding: utf-8 -*-

import sys

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

windows = sys.platform == "win32"
data = [("THIRD_PARTY_NOTICES.md", "."), ("README.md", ".")]
hiddenimports = ["psutil"]
if windows:
    data += collect_data_files("pydivert", includes=["windivert_dll/*"])
    data += copy_metadata("pydivert", recursive=True)
    hiddenimports.append("pydivert")

a = Analysis(
    ["run_client.py"],
    pathex=[],
    binaries=[],
    datas=data,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[] if windows else ["pydivert"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Civ6RelayClient",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=not windows,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="x86_64",
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=windows,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Civ6RelayClient",
)
