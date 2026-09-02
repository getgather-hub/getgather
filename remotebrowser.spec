# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import copy_metadata

datas = [
    ('getgather/webui', 'getgather/webui'),
]
hiddenimports = []

for pkg in [
    'uvicorn',
    'fastapi',
    'pydantic',
    'starlette',
    'httpx',
    'anyio',
    'httptools',
    'websockets',
    'pydantic_core',
    'logfire',
    'sentry_sdk',
]:
    try:
        datas += copy_metadata(pkg)
    except Exception:
        pass

a = Analysis(
    ['remotebrowser.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=['hooks'],
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
    name='remotebrowser',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
