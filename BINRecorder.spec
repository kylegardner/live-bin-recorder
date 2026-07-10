# -*- mode: python ; coding: utf-8 -*-
import sys
block_cipher = None

a = Analysis(
    ['bin_downloader.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'pymavlink',
        'pymavlink.dialects.v20.ardupilotmega',
        'pymavlink.dialects.v10.ardupilotmega',
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'scipy'],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='BIN Block Receiver',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='BIN Block Receiver',
)

# macOS .app bundle only
if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='BIN Block Receiver.app',
        bundle_identifier='au.com.remoteaerospace.binrecorder',
        version='1.0.0',
        info_plist={
            'NSHighResolutionCapable': True,
            'NSRequiresAquaSystemAppearance': False,
            'CFBundleShortVersionString': '1.0.0',
            'CFBundleDisplayName': 'BIN Block Receiver',
        },
    )
