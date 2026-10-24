# PyInstaller spec for ActionCheese Sync on Windows.
#
# Build with packaging\build_app.ps1 rather than invoking this directly -- that
# script resolves the ffmpeg binaries and passes them in.
#
# NEVER RUN ON macOS: PyInstaller cannot cross-compile. A Windows executable
# has to be built on Windows. The macOS spec lives beside this one and the two
# deliberately share nothing but the entry point, because the differences are
# not conditionals -- there is no BUNDLE here, no .icns, no Info.plist.

import os
from pathlib import Path

block_cipher = None

binaries = []
if os.environ.get("ACSYNC_BUNDLE_FFMPEG"):
    for name in ("ffmpeg", "ffprobe"):
        source = os.environ.get(f"ACSYNC_{name.upper()}")
        if source and Path(source).is_file():
            binaries.append((source, "."))

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[],
    binaries=binaries,
    # The GPL requires the licence to travel with the binary.
    datas=[(os.path.join(SPECPATH, "..", "assets"), "assets"),
           (os.path.join(SPECPATH, "..", "LICENSE"), ".")],
    hiddenimports=["syncmyvids.gui"],
    hookspath=[],
    runtime_hooks=[],
    # Only exclude packages nothing imports. SciPy submodules are deliberately
    # *not* trimmed: scipy.signal pulls in stats, optimize and spatial
    # transitively, and excluding them builds an app that dies on launch.
    excludes=["matplotlib", "PIL", "pandas", "pytest", "IPython", "notebook",
              "tkinter.test", "test"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ActionCheese Sync",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # No console window: this is a GUI app, and `--cli` still works when it is
    # started from a terminal.
    console=False,
    disable_windowed_traceback=False,
    icon=os.path.join(SPECPATH, "icon.ico"),
)

coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, name="ActionCheese Sync",
)
