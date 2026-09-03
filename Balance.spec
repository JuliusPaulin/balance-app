# -*- mode: python ; coding: utf-8 -*-
import pathlib

from PyInstaller.utils.hooks import collect_all

# VERSION is the single source of truth: it is bundled so the running app can
# report itself, and stamped into the plist so Finder's Get Info agrees.
VERSION = pathlib.Path('VERSION').read_text(encoding='utf-8').strip() or 'dev'

datas = [('templates', 'templates'), ('static', 'static'), ('VERSION', '.'),
         # llama.cpp's server travels in the bundle; the weights do not and
         # are fetched on first use. scripts/fetch_runtime.sh puts it here.
         ('vendor/llama', 'vendor/llama'),
         # Apache 2.0 asks that its terms and the attribution travel with
         # the model, so they ship and are copied beside the weights.
         ('licences', 'licences')]
binaries = []
hiddenimports = ['webview', 'webview.platforms.cocoa', 'flask', 'flask_limiter',
                 'dateutil', 'openpyxl',
                 # main.py reaches into AppKit for the frameless window: the
                 # drag strip, the window buttons' actions, the appearance.
                 # All three are imported inside functions, and a miss shows
                 # up only in the packaged app.
                 'AppKit', 'PyObjCTools', 'PyObjCTools.AppHelper', 'objc']
# The app's own code lives in four packages. PyInstaller does follow the static
# imports in each __init__.py, but a miss here only shows up in the packaged
# app — never in the tests, never in a run from source — so every module is
# named outright. A new module in any of these lists belongs here too.
hiddenimports += ['core', 'config', 'ai', 'data', 'services', 'routes'] + [
    'routes.' + m for m in (
        'bank_import', 'categories', 'csv_import', 'dashboard', 'merchant_rules',
        'chat', 'net_worth', 'notes', 'subscriptions', 'system', 'transactions',
    )
] + [
    'ai.' + m for m in ('backends', 'chat', 'tools', 'runtime')
] + [
    'data.' + m for m in ('db', 'sqlite', 'schema')
] + [
    'services.' + m for m in (
        'networth', 'recurring', 'investment_import', 'enable_banking',
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
