# PyInstaller spec for the ActionCheese Sync desktop app.
#
# Build with packaging/build_app.sh rather than invoking this directly -- that
# script resolves the ffmpeg binaries and passes them in.
#
# ffmpeg/ffprobe are copied into Contents/Resources when ACSYNC_BUNDLE_FFMPEG
# is set, so the app runs on a machine with nothing installed.  See the licence
# note in build_app.sh before sharing such a build.

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
    console=False,
    disable_windowed_traceback=False,
    # argv_emulation MUST stay off. It builds a Cocoa menu during start-up and
    # trips an NSMenuItem assertion in an unsigned bundle, aborting the process
    # with SIGABRT -- an intermittent "app does not launch" that is miserable to
    # diagnose. Opened documents are picked up through Tk's own
    # ::tk::mac::OpenDocument handler instead (see gui.py).
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, name="ActionCheese Sync",
)

app = BUNDLE(
    coll,
    name="ActionCheese Sync.app",
    icon=os.path.join(SPECPATH, "icon.icns"),
    bundle_identifier="com.gabrielban.actioncheesesync",
    info_plist={
        "CFBundleName": "ActionCheese Sync",
        "CFBundleDisplayName": "ActionCheese Sync",
        "CFBundleShortVersionString": os.environ.get("ACSYNC_VERSION", "0.1.0"),
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        "CFBundleDocumentTypes": [{
            "CFBundleTypeName": "Final Cut Pro XML",
            "CFBundleTypeExtensions": ["xml"],
            "CFBundleTypeRole": "Editor",
        }],
    },
)
