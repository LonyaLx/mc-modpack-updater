# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['simple_gui.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('simple_updater.py', '.'),
        ('avatar.png', '.'),
    ],
    hiddenimports=[
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        'PIL.ImageTk',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 这些库本程序用不到。Pillow 的可选加速会把 numpy 一起带进来，
        # 白白让 EXE 大十几 MB、启动也更慢，这里显式排除。
        'numpy',
        'scipy',
        'pandas',
        'matplotlib',
        'pyreadline3',
        'IPython',
        'jupyter',
    ],
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
    name='MC整合包更新器_单文件版',
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
