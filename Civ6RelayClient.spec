# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

pydivert_data = collect_data_files("pydivert", includes=["windivert_dll/*"])
pydivert_data += copy_metadata("pydivert", recursive=True)
pydivert_data += [("THIRD_PARTY_NOTICES.md", "."), ("README.md", ".")]

a = Analysis(
    ["run_client.py"],
    pathex=[],
    binaries=[],
    datas=pydivert_data,
    hiddenimports=["psutil", "pydivert"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="x86_64",
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,
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
