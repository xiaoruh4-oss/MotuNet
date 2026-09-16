# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
project = Path(SPECPATH)
_a = Analysis([str(project / "main.py")], pathex=[str(project)], binaries=[], datas=[
    (str(project / "vendor"), "vendor"),
    (str(project / "docs"), "docs"),
    (str(project / "scenarios"), "scenarios"),
    (str(project / "README.md"), "."),
    (str(project / "THIRD_PARTY_NOTICES.md"), "."),
    (str(project / "vendor" / "licenses"), "vendor/licenses"),
    (str(project / "assets" / "motu-net-logo.png"), "assets"),
    (str(project / "assets" / "motu-net.ico"), "assets"),
], hiddenimports=["PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"], hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=["PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.QtMultimedia", "PySide6.QtNetworkAuth", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets"], noarchive=False)
_pyz = PYZ(_a.pure)
_exe = EXE(_pyz, _a.scripts, [], exclude_binaries=True, name="MotuNet", icon=str(project / "assets" / "motu-net.ico"), debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False)
_coll = COLLECT(_exe, _a.binaries, _a.datas, strip=False, upx=False, name="MotuNet")
