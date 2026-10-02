# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path

block_cipher = None

project_dir = Path.cwd()

added_files = [
    ('web_ui_prototype/static', 'web_ui_prototype/static'),
    ('chrome_extension', 'chrome_extension'),
    ('config_workers.json', '.'),
    ('app_logo.png', '.'),
    ('app_icon.ico', '.'),
]

hidden_imports = [
    'openpyxl',
    'openpyxl.cell',
    'openpyxl.workbook',
    'openpyxl.worksheet',
    'openpyxl.styles',
    'openpyxl.formatting',
    'openpyxl.utils',
    'et_xmlfile',
    'websockets',
    'websockets.legacy',
    'websockets.legacy.server',
    'tkinter',
    'tkinter.filedialog',
    'tkinter.messagebox',
    'psutil',
    'curl_cffi',
    'requests',
    'zain_checker',
    'zain_checker.chrome_manager',
    'zain_checker.clean_sheet_builder',
    'zain_checker.config',
    'zain_checker.console',
    'zain_checker.domain_models',
    'zain_checker.extension_data',
    'zain_checker.money',
    'zain_checker.pipeline',
    'zain_checker.reconciliation',
    'zain_checker.security',
    'zain_checker.telegram_controller',
    'zain_checker.simulation_suite',
    'zain_checker.telemetry',
    'zain_checker.web_bridge',
    'zain_checker.workbook',
    'zain_checker.zain',
    'main',
]

a = Analysis(
    ['main.py'],
    pathex=[str(project_dir), str(project_dir / 'zain_checker')],
    binaries=[],
    datas=added_files,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(
    a.pure,
    a.zipped_data,
    cipher=block_cipher,
)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ZainChecker',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='app_icon.ico',
)

