# -*- mode: python ; coding: utf-8 -*-
import pathlib

from PyInstaller.utils.hooks import collect_all

# VERSION is the single source of truth: it is bundled so the running app can
# report itself, and stamped into the plist so Finder's Get Info agrees.
VERSION = pathlib.Path('VERSION').read_text(encoding='utf-8').strip() or 'dev'

datas = [('templates', 'templates'), ('static', 'static'), ('VERSION', '.')]
binaries = []
hiddenimports = ['webview', 'webview.platforms.cocoa', 'flask', 'flask_limiter', 'dateutil', 'db_sqlite', 'database', 'investment_import', 'openpyxl', 'ai_tools', 'ai_chat', 'ai_backends']
# The routes live in a package now. PyInstaller does follow the static imports in
# routes/__init__.py, but a miss here only shows up in the packaged app, so the
# modules are named outright.
hiddenimports += ['core', 'routes'] + [
    'routes.' + m for m in (
        'bank_import', 'categories', 'csv_import', 'dashboard', 'merchant_rules',
        'chat', 'net_worth', 'notes', 'subscriptions', 'system', 'transactions',
    )
]
tmp_ret = collect_all('webview')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['psycopg', 'psycopg_pool', 'authlib'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Balance',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['static/icon.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Balance',
)
app = BUNDLE(
    coll,
    name='Balance.app',
    icon='static/icon.icns',
    bundle_identifier='com.juliuspaulin.balance',
    version=VERSION,
    info_plist={
        'CFBundleShortVersionString': VERSION,
        'CFBundleVersion': VERSION,
        'NSHighResolutionCapable': True,
    },
)
