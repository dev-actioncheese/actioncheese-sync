# SPDX-License-Identifier: GPL-3.0-or-later
"""Entry point for the frozen app.

Kept separate from the package so PyInstaller has a plain script to analyse,
and so multiprocessing/threading start-up happens before anything else runs.
"""

import multiprocessing
import sys

if __name__ == "__main__":
    # Required before any thread or process is created in a frozen build.
    multiprocessing.freeze_support()

    # `--cli` runs the command-line interface from inside the bundle.  It exists
    # so a build can be verified end to end without driving the window, and it
    # gives anyone who wants to script the app a way in:
    #   "ActionCheese Sync.app/Contents/MacOS/ActionCheese Sync" \
    #       --cli sync in.xml -o out.xml
    if "--cli" in sys.argv[1:]:
        from syncmyvids.cli import main as cli_main
        sys.exit(cli_main([a for a in sys.argv[1:] if a != "--cli"]))

    from syncmyvids.gui import main
    sys.exit(main())
