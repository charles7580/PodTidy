# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_data_files

# Collect third-party package data
datas = []
binaries = []
hiddenimports = [
    'tkinterdnd2',
    'customtkinter',
    'mutagen',
    'mutagen.mp3',
    'mutagen.id3',
    'mutagen.easyid3',
    'mutagen.mp4',
    'PIL',
    'PIL.Image',
    'imageio_ffmpeg',
    'podcast_engine',
]

for lib in ['customtkinter', 'tkinterdnd2', 'mutagen']:
    tmp = collect_all(lib)
    datas += tmp[0]
    binaries += tmp[1]
    hiddenimports += tmp[2]

# Collect imageio-ffmpeg binary
try:
    ffmpeg_datas, ffmpeg_bins, ffmpeg_hidden = collect_all('imageio_ffmpeg')
    datas += ffmpeg_datas
    binaries += ffmpeg_bins
    hiddenimports += ffmpeg_hidden
except Exception:
    pass

a = Analysis(
    ['main_gui.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
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
    exclude_binaries=True,
    name='PodTidy',
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
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PodTidy',
)
