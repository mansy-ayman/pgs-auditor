# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files

a = Analysis(
    ['Kobo_Bridge_Esri_Manual_Tree_Wait.pyw'],
    pathex=[],
    binaries=[],
    datas=collect_data_files('tzdata') + collect_data_files('certifi'),
    hiddenimports=['updater', 'pgs_icon', 'pgs_store', 'pgs_key', 'tzdata'],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PGS_Tool',
    icon=['PGS_Tool.ico'],
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
)