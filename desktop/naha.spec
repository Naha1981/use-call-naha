# PyInstaller build for the NahaLabs Windows desktop agent.
# Keep this as an onedir build: faster startup and no repeated unpacking of
# the large local Python runtime on every launch.

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

for package in ("piper", "faster_whisper", "ctranslate2"):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["desktop/naha_desktop.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Naha",
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
    a.datas,
    strip=False,
    upx=True,
    name="Naha",
)
