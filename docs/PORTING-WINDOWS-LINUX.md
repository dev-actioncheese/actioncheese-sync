# Porting ActionCheese Sync to Windows and Linux

A handoff for continuing this on a Windows machine. Written on macOS, where
the portable half could be tested and the rest could not.

## Where it stands

**Done and tested on macOS** — the code is already cross-platform:

- `<pathurl>` translation handles Windows drive letters and UNC shares in both
  directions. This was the one real blocker: a Windows export reads as
  `/C:/Footage/x`, and leaving that leading slash on gives a *relative* path
  with no drive, so every clip reports as missing and nothing syncs. Writing
  was equally broken — backslashes came out percent-encoded as `%5C`, giving
  an XML that imports without complaint and relinks nothing. Six tests in
  `tests/test_fcp7.py::TestWindowsPathurls` cover both directions and both
  path flavours.
- `audio/ffmpeg.py` looks for `ffmpeg.exe` / `ffprobe.exe` on Windows.
- Preferences go to `%APPDATA%` on Windows, `$XDG_CONFIG_HOME` on Linux.
- Fonts resolve per platform (Segoe UI / Consolas on Windows, DejaVu on
  Linux) instead of asking for macOS's Helvetica and Menlo.
- The ttk theme is `clam`, which ships with Tk everywhere and honours colours,
  so the dark theme should look the same on all three.

**Written but never executed** — these need Windows to run at all:

- `packaging/actioncheese-sync-windows.spec`
- `packaging/build_app.ps1`
- `packaging/icon.ico` (generated; `make_icon.py` writes it alongside `.icns`)

**Not started**: Linux packaging (AppImage is the pragmatic choice).

## Do this first, before any packaging

Verify the path handling against reality rather than against my inference.
I do not have a Windows-exported timeline, so the exact shape Premiere writes
is assumed, not observed.

1. In Premiere on Windows, make a sequence with two or three clips from a
   couple of cameras. File > Export > Final Cut Pro XML.
2. Open the XML in a text editor and find a `<pathurl>`. Compare it with what
   `TestWindowsPathurls` expects:
   `file://localhost/C:/Footage/Cam%20A/A001.MP4`
3. If it differs — a different host, no host, encoded differently — fix
   `pathurl_to_path` / `path_to_pathurl` in `src/syncmyvids/formats/fcp7.py`
   and update those tests to match what Premiere actually wrote.
4. `actioncheese-sync probe <that file>` should list every clip **without**
   `[media missing]`. If anything says missing, the path translation is still
   wrong and nothing else matters yet.
5. Sync it, import the result back into Premiere, and confirm the clips relink
   and land where expected.

## Then build

```
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q          # expect 288 passing
winget install Gyan.FFmpeg                 # or pass -NoFfmpeg
packaging\build_app.ps1
```

Expect to fix things here. PyInstaller on Windows differs from macOS in ways
that only appear when you run it — missing DLLs, tkinter not collected,
SciPy's extension modules. The macOS spec's comments about *not* trimming
SciPy submodules apply identically: `scipy.signal` pulls in `stats`,
`optimize` and `spatial`, and excluding them builds an app that dies on
launch.

Then run the built exe and actually use it: add a folder, add an XML, sync,
check the timeline strips draw and the waveforms appear.

## Signing: deliberately not done

The build is unsigned and SmartScreen will warn on first run — "More info"
then "Run anyway". That is a decision, not an oversight: a certificate is a
recurring cost that a free app does not justify, and reputation with
SmartScreen accrues slowly even once you have one. Say so plainly on the
download page; people mind the surprise more than the click.

## What is macOS-only and should stay that way

`build_app.sh`, `sign_and_notarize.sh`, `entitlements.plist`, `icon.icns`, and
the `BUNDLE`/`info_plist` section of `actioncheese-sync.spec`. Do not try to
make one spec serve both platforms — the differences are structural, not
conditional.

`swift/ActionCheeseSyncKit` is Apple-only by design (AVFoundation and
Accelerate) and is for the iOS app, not for this port.

## Linux, when you get to it

Everything above applies except signing. AppImage keeps it to one file that
runs anywhere; expect the system ffmpeg rather than bundling one. The audience
is DaVinci Resolve users, since Premiere does not exist on Linux, so weigh it
accordingly.

## Context worth carrying over

- Accuracy is validated against a hand-synced four-camera shoot: every clip
  within 0.60 s of where it was placed by hand, zero same-device overlaps,
  zero shooting-order inversions. If a change makes that worse, it is wrong.
- `tests/fixtures/make_fixtures.py` generates synthetic media with known
  offsets, so the end-to-end tests need no real footage.
- `packaging/make_demo_shoot.py` builds a whole fake multicam shoot for
  screenshots — useful for checking the GUI on a machine with no footage.
