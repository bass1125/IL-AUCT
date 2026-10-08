# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = []
hiddenimports += collect_submodules('campusnet')


a = Analysis(
    ['uninstall.py'],
    # 两个路径都要：app.py 在同目录（外壳靠它复用），campusnet 是隔壁仓库
    pathex=[
        'D:/i_Lian/CampusNetAssistant',
        'D:/i_Lian/campusnet-autologin/campusnet',
    ],
    binaries=[],
    datas=[('web', 'web')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='卸载 IL AUCT',
    icon='D:/i_Lian/CampusNetAssistant/app.ico',
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
)
