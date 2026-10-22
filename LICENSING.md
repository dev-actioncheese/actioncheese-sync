# Licensing

This repository holds two separately licensed works. Being in one repository
does not put a file under one licence: the scope below governs, and every
source file carries a matching `SPDX-License-Identifier` line.

| Path | Licence | Why |
|---|---|---|
| `src/`, `packaging/`, `tests/`, the macOS app | **GPL-3.0-or-later** | The distributed app bundles a Homebrew build of ffmpeg, which is GPL-bound |
| `swift/ActionCheeseSyncKit/` | **MIT** | No ffmpeg, no GPL code — original work over Apple frameworks only |

Copyright © 2026 Gabriel (Focalizat), sole copyright holder of all original
code here.

## Why the Swift package can ship on the App Store

GPLv3 and the App Store are incompatible: Apple's Licensed Application End User
Licence Agreement imposes distribution and device restrictions that GPLv3
forbids. An app that links a GPLv3 library therefore cannot be published there.

`swift/ActionCheeseSyncKit` is MIT, so that problem does not arise. Three facts
make the MIT grant sound rather than wishful:

1. **Nothing forces GPL on it.** The GPL obligation on this project comes from
   one place only — the Homebrew ffmpeg binary copied into the macOS app
   bundle. The Swift package does not use ffmpeg. It decodes with AVFoundation
   and computes with Accelerate, both Apple frameworks under Apple's own terms,
   and it has no third-party dependencies at all (see `Package.swift`).

2. **The author may license their own work twice.** `ActionCheeseSyncKit` is a
   port of the Python engine in `src/syncmyvids`. A translation of a work is
   normally a derivative of it — but both are the original work of the same
   copyright holder, who is free to release them under different terms. The
   Python engine is not itself derived from anyone else's GPL code: the
   envelope-correlation and GCC-PHAT techniques it uses are published methods,
   and methods are not copyrightable. No third-party source was copied into it.

3. **Nothing GPL-covered is shipped inside the package.** Its only generated
   artefact from the Python side is
   `Tests/ActionCheeseSyncKitTests/Golden/golden.json`, which is measurement
   data used to check parity. It is test-only and is not part of the library
   product, so it never reaches an app that depends on the package.

Consequently an App Store app may depend on `swift/ActionCheeseSyncKit` under
the MIT terms, and its obligations are the usual MIT ones: keep the copyright
notice and permission notice with the source or in the app's acknowledgements.

## Consuming just the Swift package

Depending on this repository's URL pulls in the GPL-licensed directories as
files on disk, even though they are not linked or distributed. To avoid any
argument on that point, `swift/ActionCheeseSyncKit` is self-contained and can
be lifted into a repository of its own, or vendored directly, with no change
to its sources.

## Not legal advice

This file records the intent of the copyright holder and the reasoning behind
it. It is not a legal opinion. If ActionCheese is going to be a commercial App
Store product, it is worth ten minutes of a lawyer's time to confirm.
