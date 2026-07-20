# -*- mode: python ; coding: utf-8 -*-

import re

# ============================================================
# 从 version.py 动态读取版本 & 应用信息（单一来源）
# ============================================================
with open("version.py", encoding="utf-8") as _f:
    _src = _f.read()

_app_name = re.search(r'APP_NAME\s*=\s*"(.*?)"', _src).group(1)
_app_version = re.search(r'APP_VERSION\s*=\s*"(.*?)"', _src).group(1)
_app_desc = re.search(r'APP_DESCRIPTION\s*=\s*"(.*?)"', _src).group(1)

# 解析 MAJOR.MINOR.PATCH[.BUILD] 用于 VSVersionInfo
_ver_list = [int(x) for x in _app_version.replace("-", ".").split(".")]
while len(_ver_list) < 4:
    _ver_list.append(0)
_ver_tuple = tuple(_ver_list)

# ============================================================
# Analysis
# ============================================================
a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("ui/dark_theme.qss", "ui"),
        ("config/default_config.json", "config"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# ============================================================
# Windows EXE 版本信息（右键 → 属性 → 详细信息可见）
# ============================================================
try:
    from pyinstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    _version_info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=_ver_tuple, prodvers=_ver_tuple),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "040904B0",
                        [
                            StringStruct("FileDescription", _app_desc),
                            StringStruct("FileVersion", _app_version),
                            StringStruct("ProductName", _app_name),
                            StringStruct("ProductVersion", _app_version),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )
except ImportError:
    # 非 Windows 或 PyInstaller 版本不支持 VSVersionInfo 时静默跳过
    _version_info = None

# ============================================================
# EXE
# ============================================================
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=_app_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=_version_info,
)
