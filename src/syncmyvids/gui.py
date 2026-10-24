# SPDX-License-Identifier: GPL-3.0-or-later
"""A small Tk desktop front end.

Deliberately built on tkinter: it ships with Python, so the packaged app needs
no toolkit beyond the interpreter itself, and there is nothing here an editor
has to learn -- pick a timeline, press Sync.

The engine runs on a worker thread and reports back through a queue.  Tk is not
thread-safe, so *no* widget is touched from that thread; the UI polls the queue
on the main loop instead.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
import json
import os
import subprocess
import tkinter as tk
import traceback
import urllib.parse
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Optional

from . import __version__, about
from .audio import ffmpeg
from .audio.features import FeatureCache
from .engine import SyncOptions, load_inputs, sync_timeline
from .errors import SyncMyVidsError
from .formats import fcp7
from .timelineview import (
    COLOUR_BG, COLOUR_GRID, COLOUR_MARKER, COLOUR_ROW, COLOUR_ROW_ALT,
    COLOUR_TEXT, COLOUR_UNSYNCED, COLOUR_VIDEO, COLOUR_WAVE,
    MONO_FONT, UI_FONT,
    TimelineCanvas, load_peaks, snapshot,
)

#: The companion panel's width, and the width text wraps to inside it.
SIDEBAR_WIDTH = 248
SIDEBAR_TEXT = SIDEBAR_WIDTH - 28

#: The window, in the same colours as the icon and the timeline strips.
#: Cheese is the accent, not the background: a whole window of it would be
#: unreadable, and the strips need to be the brightest thing on screen.
CHEESE = COLOUR_VIDEO
CHEESE_DEEP = "#E9A10C"
CHEESE_DARK = COLOUR_WAVE
PANEL = "#1E222C"
PANEL_RAISED = "#262B36"
WINDOW = "#191C24"
INK = "#EDE7D8"
INK_DIM = COLOUR_TEXT

log = logging.getLogger(__name__)

LABEL_COLOURS = [
    "Rose", "Mango", "Iris", "Violet", "Caribbean", "Lavender", "Cerulean",
    "Forest", "Purple", "Blue", "Teal", "Magenta", "Tan", "Green", "Brown",
    "Yellow", "White", "(none)",
]


#: Where the window remembers the little it needs to. Deliberately tiny and
#: deliberately not in the project: it is a property of this Mac's copy of the
#: app, not of any timeline.
def _prefs_path() -> Path:
    """Where this machine keeps the window's preferences.

    Each platform has its own convention and ignoring them puts a stray
    Library folder in a Windows home directory.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "ActionCheese Sync" / "window.json"


def _load_prefs() -> dict:
    try:
        return json.loads(_prefs_path().read_text())
    except Exception:  # noqa: BLE001 - no preferences is not a problem
        return {}


def _save_prefs(values: dict) -> None:
    try:
        path = _prefs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(values, indent=2))
    except Exception as exc:  # noqa: BLE001 - never lose a run over a preference
        log.debug("could not save preferences: %s", exc)


def _asset(name: str) -> Optional[Path]:
    """Find a bundled asset in a checkout or inside a frozen app."""
    roots = [Path(__file__).resolve().parent.parent.parent / "assets"]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.insert(0, Path(meipass) / "assets")
    for root in roots:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


class SyncApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.messages: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.worker: Optional[threading.Thread] = None

        root.title(f"{about.APP_NAME} {__version__}")
        root.minsize(900, 720)
        self.root = root

        #: Inputs, in the order they were added: .xml timelines and folders,
        #: mixed freely. An entry is a single path, or a list of folders
        #: standing for one camera that filled several cards. The list is the
        #: model; the listbox only shows it.
        self.inputs: list = []
        self.output_path = tk.StringVar()
        self.trust_synced = tk.BooleanVar(value=False)
        self._prefs = _load_prefs()
        self.sidebar_open = bool(self._prefs.get("sidebar_open", True))
        self.close_gaps = tk.BooleanVar(value=True)
        self.correct_drift = tk.BooleanVar(value=True)
        self.placement = tk.StringVar(value="inline")
        self.label_colour = tk.StringVar(value="Rose")
        self.reference = tk.StringVar(value="")
        self.confidence = tk.StringVar(value="0.45")
        self.suffix = tk.StringVar(value="_synced")
        self.unsynced_audio = tk.StringVar(value="keep")
        self.unsynced_video = tk.StringVar(value="keep")
        self.status = tk.StringVar(value="Ready")
        self._started = 0.0

        self._set_window_icon(root)
        self._apply_theme(root)
        self._build(root)
        self._build_menu(root)
        self._check_ffmpeg()
        self._register_open_handler(root)
        root.after(100, self._drain)

    # -------------------------------------------------------------- appearance
    def _apply_theme(self, root: tk.Tk) -> None:
        """Dress the window to match the icon.

        This means leaving macOS's native ttk theme, which ignores almost every
        colour asked of it -- an aqua button stays an aqua button. "clam" is
        the one bundled theme that honours them, so it is the price of the app
        looking like itself rather than like a stock dialog.

        Everything here is cosmetic: if any of it fails on some Tk build, the
        app keeps working in default dress.
        """
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:  # pragma: no cover - depends on the Tk build
            return

        root.configure(background=WINDOW)
        style.configure(".", background=WINDOW, foreground=INK,
                        fieldbackground=PANEL, bordercolor=COLOUR_GRID,
                        lightcolor=PANEL_RAISED, darkcolor=WINDOW,
                        focuscolor=CHEESE, troughcolor=PANEL)
        style.configure("TFrame", background=WINDOW)
        style.configure("TLabel", background=WINDOW, foreground=INK)
        style.configure("TLabelframe", background=WINDOW, bordercolor=COLOUR_GRID,
                        relief="solid", borderwidth=1)
        # The group headings are the one place the cheese reads as a label
        # rather than as decoration.
        style.configure("TLabelframe.Label", background=WINDOW, foreground=CHEESE)

        style.configure("TButton", background=PANEL_RAISED, foreground=INK,
                        bordercolor=COLOUR_GRID, focusthickness=1,
                        focuscolor=CHEESE, padding=(10, 5), relief="flat")
        style.map("TButton",
                  background=[("pressed", CHEESE_DEEP), ("active", "#333A47"),
                              ("disabled", PANEL)])

        # The one button that does the work gets the full cheese.
        style.configure("Cheese.TButton", background=CHEESE, foreground=CHEESE_DARK,
                        padding=(18, 6), relief="flat", bordercolor=CHEESE_DEEP)
        style.map("Cheese.TButton",
                  background=[("pressed", CHEESE_DEEP), ("active", "#FFD75C"),
                              ("disabled", PANEL_RAISED)])

        # clam's indicator is its own element with its own colour options --
        # the generic "indicatorcolor" every other theme uses does nothing
        # here, which leaves a white box with a black tick in a dark window.
        style.configure("TCheckbutton", background=WINDOW, foreground=INK,
                        focuscolor=WINDOW, indicatormargin=(0, 0, 7, 0),
                        indicatorbackground=PANEL, indicatorforeground=CHEESE_DARK,
                        upperbordercolor=COLOUR_GRID, lowerbordercolor=COLOUR_GRID)
        style.map("TCheckbutton",
                  indicatorbackground=[("selected", CHEESE),
                                       ("active", PANEL_RAISED),
                                       ("disabled", PANEL)],
                  indicatorforeground=[("selected", CHEESE_DARK)],
                  upperbordercolor=[("selected", CHEESE_DEEP)],
                  lowerbordercolor=[("selected", CHEESE_DEEP)],
                  foreground=[("disabled", "#6B6F79")])

        style.configure("TEntry", fieldbackground=PANEL, foreground=INK,
                        insertcolor=CHEESE, bordercolor=COLOUR_GRID, padding=4)
        style.configure("TCombobox", fieldbackground=PANEL, background=PANEL_RAISED,
                        foreground=INK, arrowcolor=CHEESE, bordercolor=COLOUR_GRID,
                        padding=3)
        # The dropdown list is a plain Tk widget, reachable only this way.
        root.option_add("*TCombobox*Listbox.background", PANEL)
        root.option_add("*TCombobox*Listbox.foreground", INK)
        root.option_add("*TCombobox*Listbox.selectBackground", CHEESE)
        root.option_add("*TCombobox*Listbox.selectForeground", CHEESE_DARK)

        style.configure("TNotebook", background=WINDOW, bordercolor=COLOUR_GRID)
        style.configure("TNotebook.Tab", background=PANEL, foreground=INK_DIM,
                        padding=(14, 6), bordercolor=COLOUR_GRID)


        style.configure("TProgressbar", background=CHEESE, troughcolor=PANEL,
                        bordercolor=COLOUR_GRID, lightcolor=CHEESE,
                        darkcolor=CHEESE_DEEP)
        style.configure("Vertical.TScrollbar", background=PANEL_RAISED,
                        troughcolor=PANEL, bordercolor=WINDOW, arrowcolor=INK_DIM)
        style.configure("Horizontal.TScrollbar", background=PANEL_RAISED,
                        troughcolor=PANEL, bordercolor=WINDOW, arrowcolor=INK_DIM)

        # --- states we never asked about ---------------------------------
        #
        # configure() sets the resting colours; the *other* states keep
        # whatever clam shipped, and clam is a light theme. So a combobox went
        # near-white (#eeebe7) under the pointer while its text stayed
        # near-white, a read-only entry went #dcdad5, and the focus ring was
        # clam's blue. There is no "and everything else" in ttk: every state
        # has to be named, or it keeps the light default.
        # clam maps "active" to a near-white on the *root* style, so every
        # widget that does not override it inherits a white background under
        # the pointer -- labels and group headings included. Killing it here
        # covers the ones nobody thinks to name.
        style.map(".",
                  background=[("active", PANEL_RAISED), ("disabled", WINDOW)],
                  foreground=[("active", INK), ("disabled", "#6B6F79")])
        style.map("TLabel",
                  background=[("active", WINDOW)], foreground=[("active", INK)])
        style.map("TLabelframe.Label",
                  background=[("active", WINDOW)], foreground=[("active", CHEESE)])
        style.map("TFrame", background=[("active", WINDOW)])

        style.map("TButton",
                  foreground=[("pressed", CHEESE_DARK), ("active", INK),
                              ("disabled", "#6B6F79")],
                  lightcolor=[("pressed", CHEESE_DEEP), ("active", PANEL_RAISED)],
                  darkcolor=[("pressed", CHEESE_DEEP), ("active", WINDOW)])
        style.map("Cheese.TButton",
                  foreground=[("pressed", CHEESE_DARK), ("active", CHEESE_DARK),
                              ("disabled", "#6B6F79")],
                  lightcolor=[("pressed", CHEESE_DEEP), ("active", "#FFD75C")],
                  darkcolor=[("pressed", CHEESE_DEEP), ("active", CHEESE_DEEP)])
        style.map("TCheckbutton",
                  background=[("active", WINDOW), ("pressed", WINDOW)],
                  foreground=[("active", CHEESE), ("disabled", "#6B6F79")])
        style.map("TEntry",
                  background=[("readonly", PANEL), ("disabled", PANEL)],
                  fieldbackground=[("readonly", PANEL), ("disabled", PANEL)],
                  foreground=[("disabled", "#6B6F79")],
                  bordercolor=[("focus", CHEESE)],
                  lightcolor=[("focus", PANEL_RAISED)],
                  darkcolor=[("focus", PANEL_RAISED)])
        style.map("TCombobox",
                  background=[("active", PANEL_RAISED), ("pressed", PANEL_RAISED),
                              ("readonly", PANEL_RAISED), ("disabled", PANEL)],
                  fieldbackground=[("readonly", PANEL), ("active", PANEL),
                                   ("focus", PANEL), ("disabled", PANEL)],
                  foreground=[("active", INK), ("pressed", INK),
                              ("readonly", INK), ("disabled", "#6B6F79")],
                  arrowcolor=[("active", CHEESE), ("pressed", CHEESE_DEEP),
                              ("disabled", "#6B6F79")],
                  bordercolor=[("focus", CHEESE), ("hover", CHEESE_DEEP)],
                  lightcolor=[("focus", PANEL_RAISED)],
                  darkcolor=[("focus", PANEL_RAISED)])
        style.map("TNotebook.Tab",
                  background=[("selected", CHEESE), ("active", PANEL_RAISED)],
                  foreground=[("selected", CHEESE_DARK), ("active", INK)],
                  lightcolor=[("selected", CHEESE), ("active", PANEL_RAISED)],
                  darkcolor=[("selected", CHEESE_DEEP), ("active", WINDOW)])

    def _set_window_icon(self, root: tk.Tk) -> None:
        icon = _asset("logo-256.png")
        if icon is None:
            return
        try:
            self._icon = tk.PhotoImage(file=str(icon))
            root.iconphoto(True, self._icon)
        except tk.TclError:
            pass  # An icon is a nicety; never let it stop the app opening.

    # ------------------------------------------------------------------- menus
    def _build_menu(self, root: tk.Tk) -> None:
        menubar = tk.Menu(root)

        helpmenu = tk.Menu(menubar, name="help", tearoff=0)
        helpmenu.add_command(label=f"{about.APP_NAME} Help", command=self.show_help)
        helpmenu.add_separator()
        helpmenu.add_command(label="Report a Bug…", command=self._report_bug)
        helpmenu.add_command(label=f"About {about.APP_NAME}", command=self.show_about)
        menubar.add_cascade(label="Help", menu=helpmenu)
        root.configure(menu=menubar)

        # macOS puts About under the application menu; wire that up too.
        try:
            root.createcommand("tkAboutDialog", self.show_about)
        except tk.TclError:
            pass

    def show_about(self) -> None:
        self._text_window(f"About {about.APP_NAME}", about.about_text(), width=64, height=18)

    def show_help(self) -> None:
        self._text_window(f"{about.APP_NAME} Help", about.HELP_TEXT, width=82, height=32)

    def _report_bug(self) -> None:
        subject = urllib.parse.quote(f"{about.APP_NAME} {__version__} - bug report")
        body = urllib.parse.quote(
            "What you did:\n\nWhat happened:\n\nWhat you expected:\n\n"
            "Please attach the timeline XML and the log text if you can.\n"
        )
        webbrowser.open(f"mailto:{about.BUG_EMAIL}?subject={subject}&body={body}")

    def _text_window(self, title: str, body: str, *, width: int, height: int) -> None:
        window = tk.Toplevel(self.root)
        window.title(title)
        window.transient(self.root)

        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        text = tk.Text(frame, wrap="word", width=width, height=height,
                       font=(MONO_FONT, 11), padx=10, pady=8)
        text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)
        text.insert("1.0", body)
        text.configure(state="disabled")

        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text=f"Email {about.BUG_EMAIL}",
                   command=self._report_bug).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Close", command=window.destroy).pack(side="left")
        window.bind("<Escape>", lambda _event: window.destroy())

    def _register_open_handler(self, root: tk.Tk) -> None:
        """Accept files opened from Finder ("Open With", or dropped on the icon).

        Tk delivers these itself on macOS.  PyInstaller's ``argv_emulation`` does
        the same job by building a Cocoa menu during start-up, which trips an
        NSMenuItem assertion in an unsigned bundle and aborts the process, so
        this is the only mechanism the app relies on.
        """
        try:
            root.createcommand("::tk::mac::OpenDocument", self._open_documents)
        except tk.TclError:
            pass  # Not macOS; the file picker is the only route in.

    def _open_documents(self, *paths: str) -> None:
        """Files and folders dropped on the app, or opened from Finder."""
        wanted = [Path(c) for c in paths
                  if c.lower().endswith(".xml") or Path(c).is_dir()]
        if wanted:
            self._add_inputs(wanted)

    # ------------------------------------------------------------------ layout
    def _build(self, root: tk.Tk) -> None:
        shell = ttk.Frame(root)
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(0, weight=1)

        outer = ttk.Frame(shell, padding=12)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(0, weight=1)

        self._build_sidebar(shell)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(1, weight=1)
        badge = _asset("logo-64.png")
        if badge is not None:
            try:
                self._badge = tk.PhotoImage(file=str(badge)).subsample(2, 2)
                ttk.Label(header, image=self._badge).grid(row=0, column=0,
                                                          rowspan=2, padx=(0, 10))
            except tk.TclError:  # pragma: no cover - depends on the Tk build
                pass
        ttk.Label(header, text=about.APP_NAME, foreground=CHEESE,
                  font=(UI_FONT, 17, "bold")).grid(row=0, column=1, sticky="sw")
        ttk.Label(header, text=about.TAGLINE, foreground=INK_DIM
                  ).grid(row=1, column=1, sticky="nw")

        files = ttk.LabelFrame(outer, text="Inputs", padding=10)
        files.grid(row=1, column=0, sticky="ew")
        files.columnconfigure(0, weight=1)

        listbox_frame = ttk.Frame(files)
        listbox_frame.grid(row=0, column=0, sticky="ew")
        listbox_frame.columnconfigure(0, weight=1)
        self.input_list = tk.Listbox(
            listbox_frame, height=4, activestyle="none", selectmode="extended",
            exportselection=False, background=PANEL, foreground=INK,
            selectbackground=CHEESE, selectforeground=CHEESE_DARK,
            highlightthickness=1, highlightbackground=COLOUR_GRID,
            highlightcolor=CHEESE, borderwidth=0, font=(MONO_FONT, 11))
        self.input_list.grid(row=0, column=0, sticky="ew")
        input_scroll = ttk.Scrollbar(listbox_frame, orient="vertical",
                                     command=self.input_list.yview)
        input_scroll.grid(row=0, column=1, sticky="ns")
        self.input_list.configure(yscrollcommand=input_scroll.set)
        self.input_list.bind("<<ListboxSelect>>",
                             lambda _e: self._update_merge_button())

        buttons = ttk.Frame(files)
        buttons.grid(row=0, column=1, sticky="n", padx=(8, 0))
        ttk.Button(buttons, text="Add XML…", command=self._choose_input, width=12
                   ).grid(row=0, column=0, sticky="ew")
        ttk.Button(buttons, text="Add folder…", command=self._choose_folder, width=12
                   ).grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.merge_button = ttk.Button(buttons, text="Merge",
                                       command=self._merge_inputs, width=12)
        self.merge_button.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        self.merge_button.state(["disabled"])
        ttk.Button(buttons, text="Remove", command=self._remove_inputs, width=12
                   ).grid(row=3, column=0, sticky="ew", pady=(4, 0))

        ttk.Label(files,
                  text="One folder per camera or recorder. Pick several at once. "
                       "An ActionCheese shoot folder brings its cameras in already "
                       "in sync. Select two folders and press Merge if one camera "
                       "filled both cards.",
                  foreground="#94A3B8", wraplength=620, justify="left"
                  ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Label(files, text="Output XML").grid(row=2, column=0, sticky="w", pady=(8, 0), padx=(0, 8))
        output_row = ttk.Frame(files)
        output_row.grid(row=3, column=0, columnspan=2, sticky="ew")
        output_row.columnconfigure(0, weight=1)
        ttk.Entry(output_row, textvariable=self.output_path).grid(row=0, column=0, sticky="ew")
        ttk.Button(output_row, text="Choose…", command=self._choose_output
                   ).grid(row=0, column=1, padx=(8, 0))

        options = ttk.LabelFrame(outer, text="Options", padding=10)
        options.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        for column in (1, 3):
            options.columnconfigure(column, weight=1)

        ttk.Checkbutton(options, text="Close empty gaps", variable=self.close_gaps
                        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(options, text="Correct recorder drift", variable=self.correct_drift
                        ).grid(row=0, column=2, columnspan=2, sticky="w")
        ttk.Checkbutton(options, text="Trust already-synced clips",
                        variable=self.trust_synced
                        ).grid(row=5, column=0, columnspan=4, sticky="w", pady=(8, 0))

        ttk.Label(options, text="Unmatched clips").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(options, textvariable=self.placement, state="readonly", width=10,
                     values=["inline", "append"]).grid(row=1, column=1, sticky="w", pady=(8, 0))

        ttk.Label(options, text="Mark with").grid(row=1, column=2, sticky="w", pady=(8, 0), padx=(16, 0))
        ttk.Combobox(options, textvariable=self.label_colour, state="readonly", width=12,
                     values=LABEL_COLOURS).grid(row=1, column=3, sticky="w", pady=(8, 0))

        ttk.Label(options, text="Reference track").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(options, textvariable=self.reference, width=12).grid(row=2, column=1, sticky="w", pady=(8, 0))

        ttk.Label(options, text="Min confidence").grid(row=2, column=2, sticky="w", pady=(8, 0), padx=(16, 0))
        ttk.Entry(options, textvariable=self.confidence, width=12).grid(row=2, column=3, sticky="w", pady=(8, 0))

        ttk.Label(options, text="Name suffix").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(options, textvariable=self.suffix, width=12).grid(row=3, column=1, sticky="w", pady=(8, 0))

        ttk.Label(options, text="Unmatched audio").grid(row=3, column=2, sticky="w", pady=(8, 0), padx=(16, 0))
        ttk.Combobox(options, textvariable=self.unsynced_audio, state="readonly", width=12,
                     values=["keep", "split"]).grid(row=3, column=3, sticky="w", pady=(8, 0))

        ttk.Label(options, text="Unmatched picture").grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(options, textvariable=self.unsynced_video, state="readonly", width=10,
                     values=["keep", "split"]).grid(row=4, column=1, sticky="w", pady=(8, 0))

        actions = ttk.Frame(outer)
        actions.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        actions.columnconfigure(1, weight=1)
        self.run_button = ttk.Button(actions, text="Sync", command=self._start,
                                     style="Cheese.TButton")
        self.run_button.grid(row=0, column=0)
        self.progress = ttk.Progressbar(actions, mode="determinate", maximum=1000)
        self.progress.grid(row=0, column=1, sticky="ew", padx=12)
        ttk.Button(actions, text="Help", command=self.show_help).grid(row=0, column=2)
        ttk.Button(actions, text="About", command=self.show_about).grid(row=0, column=3, padx=(8, 0))

        self.tabs = ttk.Notebook(outer)
        self.tabs.grid(row=4, column=0, sticky="nsew", pady=(12, 0))
        outer.rowconfigure(4, weight=1)

        timeline_tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(timeline_tab, text="Timeline")
        timeline_tab.columnconfigure(0, weight=1)

        # Scrollable, because a project with many tracks outgrows the window.
        strip = tk.Canvas(timeline_tab, highlightthickness=0, background=COLOUR_BG)
        strip.grid(row=0, column=0, sticky="nsew")
        timeline_tab.rowconfigure(0, weight=1)
        strip_scroll = ttk.Scrollbar(timeline_tab, orient="vertical", command=strip.yview)
        strip_scroll.grid(row=0, column=1, sticky="ns")
        strip.configure(yscrollcommand=strip_scroll.set)

        holder = ttk.Frame(strip)
        strip.create_window((0, 0), window=holder, anchor="nw", tags="holder")
        holder.columnconfigure(0, weight=1)
        holder.bind("<Configure>",
                    lambda _e: strip.configure(scrollregion=strip.bbox("all")))
        strip.bind("<Configure>",
                   lambda e: strip.itemconfigure("holder", width=e.width))

        self.before_view = TimelineCanvas(holder, "Imported")
        self.before_view.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.after_view = TimelineCanvas(holder, "Exported")
        self.after_view.grid(row=1, column=0, sticky="ew")
        legend = ttk.Frame(holder)
        legend.grid(row=2, column=0, sticky="w", pady=(8, 0))
        for column, (swatch, text) in enumerate((
                (COLOUR_VIDEO, "picture"), (CHEESE_DEEP, "sound"),
                (COLOUR_UNSYNCED, "unmatched"), (COLOUR_MARKER, "marker"))):
            chip = tk.Canvas(legend, width=11, height=11, highlightthickness=0,
                             background=WINDOW)
            chip.create_oval(1, 1, 10, 10, fill=swatch, outline="")
            chip.grid(row=0, column=column * 2, padx=(0 if column == 0 else 14, 5))
            ttk.Label(legend, text=text, foreground=INK_DIM).grid(
                row=0, column=column * 2 + 1)

        log_tab = ttk.Frame(self.tabs, padding=6)
        self.tabs.add(log_tab, text="Log")
        log_tab.columnconfigure(0, weight=1)
        log_tab.rowconfigure(0, weight=1)

        self.log = tk.Text(log_tab, height=16, wrap="none", state="disabled",
                           font=(MONO_FONT, 11), background=COLOUR_BG,
                           foreground=INK, insertbackground=CHEESE,
                           selectbackground=CHEESE, selectforeground=CHEESE_DARK,
                           highlightthickness=0, borderwidth=0, padx=8, pady=6)
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_tab, orient="vertical", command=self.log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)

        ttk.Label(outer, textvariable=self.status, anchor="w",
                  foreground=INK_DIM).grid(row=5, column=0, sticky="ew", pady=(8, 0))

    # ------------------------------------------------------------------ sidebar
    def _build_sidebar(self, shell: ttk.Frame) -> None:
        """A narrow panel about the companion app, on the right.

        It is advertising, so it is built to be ignorable: it never appears
        over anything, never interrupts a run, makes no network call of its
        own, and folds away to a strip the width of one arrow. Whichever state
        it is left in is the state it comes back in.
        """
        self.sidebar = ttk.Frame(shell, padding=(12, 12, 12, 12))
        self.sidebar.grid(row=0, column=1, sticky="ns")
        self.sidebar.columnconfigure(0, weight=1)

        self.sidebar_strip = ttk.Frame(shell, padding=(2, 12, 2, 12))
        self.sidebar_strip.grid(row=0, column=1, sticky="ns")

        # Collapsed: one button, pointing the way back.
        ttk.Button(self.sidebar_strip, text="\u2039", width=2,
                   command=self._toggle_sidebar).grid(row=0, column=0, sticky="n")

        head = ttk.Frame(self.sidebar)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        ttk.Label(head, text="ALSO FROM FOCALIZAT", foreground=INK_DIM,
                  font=(UI_FONT, 9)).grid(row=0, column=0, sticky="w")
        ttk.Button(head, text="\u203a", width=2, command=self._toggle_sidebar
                   ).grid(row=0, column=1, sticky="e")

        icon = _asset(about.COMPANION_ICON)
        if icon is not None:
            try:
                # 512 px art, and PhotoImage only divides by whole numbers.
                self._companion_icon = tk.PhotoImage(file=str(icon)).subsample(8, 8)
                ttk.Label(self.sidebar, image=self._companion_icon).grid(
                    row=1, column=0, pady=(10, 8))
            except tk.TclError:  # pragma: no cover - depends on the Tk build
                pass

        ttk.Label(self.sidebar, text=about.COMPANION_NAME, foreground=CHEESE,
                  font=(UI_FONT, 12, "bold"), wraplength=SIDEBAR_TEXT,
                  justify="left").grid(row=2, column=0, sticky="w")
        ttk.Label(self.sidebar, text=about.COMPANION_PITCH, foreground=INK,
                  wraplength=SIDEBAR_TEXT, justify="left").grid(
                      row=3, column=0, sticky="w", pady=(6, 10))

        points = ttk.Frame(self.sidebar)
        points.grid(row=4, column=0, sticky="ew")
        points.columnconfigure(1, weight=1)
        for index, point in enumerate(about.COMPANION_POINTS):
            ttk.Label(points, text="\u2022", foreground=CHEESE).grid(
                row=index, column=0, sticky="nw", padx=(0, 6), pady=(0, 6))
            ttk.Label(points, text=point, foreground=INK, wraplength=SIDEBAR_TEXT - 16,
                      justify="left").grid(row=index, column=1, sticky="w", pady=(0, 6))

        ttk.Label(self.sidebar, text=about.COMPANION_PRICING, foreground=INK_DIM,
                  wraplength=SIDEBAR_TEXT, justify="left").grid(
                      row=5, column=0, sticky="w", pady=(6, 10))
        ttk.Button(self.sidebar, text=about.COMPANION_BUTTON,
                   style="Cheese.TButton", command=self._open_companion).grid(
                       row=6, column=0, sticky="ew")
        ttk.Label(self.sidebar, text=about.COMPANION_STORE_NOTE, foreground=INK_DIM,
                  font=(UI_FONT, 9)).grid(row=7, column=0, sticky="w", pady=(6, 0))

        self._apply_sidebar_state()

    def _apply_sidebar_state(self) -> None:
        showing = self.sidebar if self.sidebar_open else self.sidebar_strip
        hiding = self.sidebar_strip if self.sidebar_open else self.sidebar
        hiding.grid_remove()
        showing.grid()
        # Keep room for the work when the panel is out, give it back when it is
        # folded away.
        width = 900 + (SIDEBAR_WIDTH if self.sidebar_open else 26)
        try:
            self.root.minsize(width, 720)
        except tk.TclError:  # pragma: no cover - depends on the Tk build
            pass

    def _toggle_sidebar(self) -> None:
        self.sidebar_open = not self.sidebar_open
        self._apply_sidebar_state()
        self._prefs["sidebar_open"] = self.sidebar_open
        _save_prefs(self._prefs)

    def _open_companion(self) -> None:
        webbrowser.open(about.COMPANION_URL)

    # ------------------------------------------------------------------ helpers
    def _write(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _check_ffmpeg(self) -> None:
        try:
            ffmpeg_bin, _ = ffmpeg.ensure_available()
            self._write(f"Using ffmpeg: {ffmpeg_bin}")
        except SyncMyVidsError as exc:
            self._write(f"WARNING: {exc}")
            self.status.set("ffmpeg not found - sync will not run")

    def _choose_input(self) -> None:
        chosen = filedialog.askopenfilenames(
            title="Choose Final Cut Pro XML timelines",
            filetypes=[("Final Cut Pro XML", "*.xml"), ("All files", "*.*")],
            initialdir=self._last_dir() or "",
        )
        picked = [Path(c) for c in chosen]
        if picked:
            self._remember_dir(picked[0])
        self._add_inputs(picked)

    def _last_dir(self) -> Optional[str]:
        """Where the last input came from, if that place still exists."""
        remembered = self._prefs.get("last_dir")
        if remembered and Path(remembered).is_dir():
            return remembered
        return None

    def _remember_dir(self, path: Path) -> None:
        folder = path if path.is_dir() else path.parent
        # The *parent* of a camera folder, so the next dialog opens on the
        # shoot and its other cameras are one click away rather than one
        # navigation. Picking cam1 then cam2 then cam3 out of the same shoot
        # is the common case and it should not mean re-walking the tree.
        parent = folder.parent if folder.parent != folder else folder
        self._prefs["last_dir"] = str(parent)
        _save_prefs(self._prefs)

    def _choose_folders_macos(self) -> list[Path]:
        """Several folders in one go, via the system panel.

        Tk's own directory chooser takes one folder at a time on every
        platform, and on a shoot with four cameras that is four trips through
        the same dialog. AppleScript's chooser does support a multiple
        selection, so on macOS we ask it instead and fall back to Tk if it is
        refused or unavailable.
        """
        location = self._last_dir()
        default = f'default location (POSIX file "{location}")' if location else ""
        script = (
            'set picked to choose folder with prompt '
            '"Choose camera folders, or an ActionCheese shoot folder" '
            f'{default} with multiple selections allowed\n'
            'set out to ""\n'
            'repeat with item_ in picked\n'
            '  set out to out & POSIX path of item_ & linefeed\n'
            'end repeat\n'
            'return out'
        )
        try:
            done = subprocess.run(["osascript", "-e", script],
                                  capture_output=True, text=True, timeout=600)
        except Exception:  # noqa: BLE001 - fall back to Tk on any failure
            return []
        if done.returncode != 0:
            return []        # cancelled, or no osascript
        return [Path(line) for line in done.stdout.splitlines() if line.strip()]

    def _choose_folder(self) -> None:
        chosen: list[Path] = []
        if sys.platform == "darwin":
            chosen = self._choose_folders_macos()
        if not chosen:
            one = filedialog.askdirectory(
                title="Choose a camera folder, or an ActionCheese shoot folder",
                initialdir=self._last_dir() or "")
            chosen = [Path(one)] if one else []
        if chosen:
            self._remember_dir(chosen[0])
            self._add_inputs(chosen)

    @staticmethod
    def _members(entry) -> list[Path]:
        """An entry's paths, whether it is one or a merged group."""
        return list(entry) if isinstance(entry, list) else [entry]

    def _claimed(self) -> set[Path]:
        return {p.resolve() for entry in self.inputs for p in self._members(entry)}

    def _add_inputs(self, paths: list[Path]) -> None:
        added = False
        claimed = self._claimed()
        for path in paths:
            if path.resolve() in claimed:
                continue
            claimed.add(path.resolve())
            self.inputs.append(path)
            added = True
        if not added:
            return
        self._refresh_inputs()
        # Name the output after the first input, which is the one the operator
        # thinks of as "the project".
        first = self._members(self.inputs[0])[0]
        suffix = self.suffix.get() or "_synced"
        stem = first.stem if first.is_file() else first.name
        self.output_path.set(str(first.with_name(f"{stem}{suffix}.xml")
                                 if first.is_file() else
                                 first.parent / f"{stem}{suffix}.xml"))
        self._preview()

    def _remove_inputs(self) -> None:
        for position in sorted(self.input_list.curselection(), reverse=True):
            del self.inputs[position]
        self._refresh_inputs()
        if self.inputs:
            self._preview()
        else:
            self.before_view.clear()
            self.after_view.clear()

    def _refresh_inputs(self) -> None:
        self.input_list.delete(0, "end")
        for entry in self.inputs:
            if isinstance(entry, list):
                names = " + ".join(p.name for p in entry)
                self.input_list.insert("end", f"[1 camera]  {entry[0].parent}  ->  {names}")
            else:
                mark = "folder" if entry.is_dir() else "xml"
                self.input_list.insert("end", f"[{mark}]  {entry}")
        self._update_merge_button()

    def _update_merge_button(self) -> None:
        """One button, two jobs, named for whichever it would do now."""
        selected = [self.inputs[i] for i in self.input_list.curselection()]
        splitting = len(selected) == 1 and isinstance(selected[0], list)
        self.merge_button.configure(text="Split" if splitting else "Merge")
        usable = splitting or (len(selected) > 1
                               and all(p.is_dir() for e in selected
                                       for p in self._members(e)))
        self.merge_button.state(["!disabled"] if usable else ["disabled"])

    def _merge_inputs(self) -> None:
        """Combine the selected folders into one camera, or split one back.

        For a camera that filled several cards: read separately they would be
        separate devices, and clips from one device are never matched against
        each other -- a rule that has to hold across the card boundary too.
        """
        positions = sorted(self.input_list.curselection())
        if not positions:
            return
        selected = [self.inputs[i] for i in positions]

        if len(selected) == 1 and isinstance(selected[0], list):
            parts = selected[0]
            self.inputs[positions[0]:positions[0] + 1] = list(parts)
        else:
            if any(not p.is_dir() for entry in selected for p in self._members(entry)):
                messagebox.showwarning(
                    "Cannot merge",
                    "Only folders can be merged into one camera. A timeline has "
                    "to stay an input of its own.")
                return
            folders = [p for entry in selected for p in self._members(entry)]
            for index in reversed(positions):
                del self.inputs[index]
            self.inputs.insert(positions[0], folders)

        self._refresh_inputs()
        self._preview()

    def _preview(self) -> None:
        """Draw what was chosen straight away, before any sync runs.

        Reading folders means probing every file in them, which takes long
        enough on a full card to freeze the window, so the whole preview -- not
        just the waveforms -- happens off the UI thread.
        """
        inputs = list(self.inputs)
        if not inputs:
            return
        self.status.set("Reading inputs…")
        self.tabs.select(0)

        def work() -> None:
            try:
                loaded = load_inputs(
                    inputs,
                    progress=lambda message: self.messages.put(("status", message)))
                snap = snapshot(loaded.timeline.sequence)
            except Exception as exc:  # noqa: BLE001 - a preview must never block a run
                self.messages.put(("log", f"Could not preview: {exc}"))
                self.messages.put(("status", "Ready"))
                return
            self.messages.put(("preview-summary", loaded))
            self.messages.put(("preview", snap))
            load_peaks(snap, FeatureCache())
            self.messages.put(("preview", snap))
            self.messages.put(("status", "Ready"))

        threading.Thread(target=work, daemon=True).start()

    def _choose_output(self) -> None:
        chosen = filedialog.asksaveasfilename(
            title="Save synced XML as", defaultextension=".xml",
            filetypes=[("Final Cut Pro XML", "*.xml")],
        )
        if chosen:
            self.output_path.set(chosen)

    # ------------------------------------------------------------------ running
    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        target = self.output_path.get().strip()
        if not self.inputs:
            messagebox.showwarning(
                "No input", "Add at least one timeline XML or camera folder.")
            return
        if not target:
            messagebox.showwarning("No output", "Choose where to save the result.")
            return
        try:
            confidence = float(self.confidence.get())
        except ValueError:
            messagebox.showwarning("Bad value", "Min confidence must be a number, e.g. 0.45.")
            return

        colour = self.label_colour.get()
        options = SyncOptions(
            min_confidence=confidence,
            reference=self.reference.get().strip() or None,
            close_gaps=self.close_gaps.get(),
            correct_drift=self.correct_drift.get(),
            unsynced_placement=self.placement.get(),
            unsynced_label=None if colour == "(none)" else colour,
            sequence_suffix=self.suffix.get(),
            unsynced_audio=self.unsynced_audio.get(),
            unsynced_video=self.unsynced_video.get(),
            trust_synced=self.trust_synced.get(),
        )

        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.run_button.state(["disabled"])
        self.progress.configure(value=0)
        self._started = time.monotonic()
        self.status.set("Starting…")

        self.worker = threading.Thread(
            target=self._run, args=(list(self.inputs), Path(target), options), daemon=True
        )
        self.worker.start()

    def _run(self, inputs: list[Path], target: Path, options: SyncOptions) -> None:
        """Worker thread.  Communicates only through the queue."""
        handler = _QueueHandler(self.messages)
        logger = logging.getLogger("syncmyvids")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            self.messages.put(("log", f"Reading {len(inputs)} input(s)…"))
            loaded = load_inputs(
                inputs, progress=lambda message: self.messages.put(("progress", (0.01, message))))
            for warning in loaded.warnings:
                self.messages.put(("log", f"note: {warning}"))
            if loaded.built:
                self.messages.put(("log", loaded.summary))
            timeline = loaded.timeline
            # Snapshot first: sync_timeline moves the very clips we want to show.
            before = snapshot(timeline.sequence)
            report = sync_timeline(
                timeline, options,
                progress=lambda f, m: self.messages.put(("progress", (f, m))),
                rigid_clip_ids=loaded.rigid_clip_ids,
                marker_hints=loaded.marker_hints,
            )
            unsynced = {cid for p in report.placements
                        if p["status"] == "unsynced" for cid in p["clip_ids"]}
            after = snapshot(timeline.sequence, unsynced)
            fcp7.write(timeline, target)
            # Waveforms come from the cache the sync just filled, so this is
            # cheap here and would have been slow before the run.
            cache = FeatureCache(options.cache_dir, sample_rate=options.sample_rate)
            load_peaks(before, cache)
            load_peaks(after, cache)
            self.messages.put(("timelines", (before, after)))
            self.messages.put(("log", ""))
            self.messages.put(("log", report.render()))
            self.messages.put(("log", ""))
            self.messages.put(("log", f"Wrote {target}"))
            self.messages.put(("done", (
                f"{report.synced_count + report.island_count} of {report.unit_count} "
                f"clips synced - saved to {target.name}"
            )))
        except SyncMyVidsError as exc:
            self.messages.put(("error", str(exc)))
        except Exception as exc:  # noqa: BLE001 - a crash must not kill the window
            self.messages.put(("log", traceback.format_exc()))
            self.messages.put(("error", f"{type(exc).__name__}: {exc}"))
        finally:
            logger.removeHandler(handler)

    def _drain(self) -> None:
        """Pump worker messages onto the UI thread."""
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    self._write(str(payload))
                elif kind == "progress":
                    self._show_progress(*payload)
                elif kind == "preview":
                    self.before_view.show(payload)
                    self.after_view.clear()
                elif kind == "preview-summary":
                    if payload.built:
                        self._write(payload.summary)
                    for warning in payload.warnings:
                        self._write(f"note: {warning}")
                elif kind == "status":
                    self.status.set(str(payload))
                elif kind == "timelines":
                    before, after = payload
                    # One scale for both, so the two strips are comparable.
                    span = max(before.duration, after.duration)
                    self.before_view.show(before, span)
                    self.after_view.show(after, span)
                    self.tabs.select(0)
                elif kind == "done":
                    self._finish(str(payload))
                elif kind == "error":
                    self._finish("Failed")
                    messagebox.showerror("Sync failed", str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def _show_progress(self, fraction: float, message: str) -> None:
        self.progress.configure(value=int(fraction * 1000))
        elapsed = time.monotonic() - self._started
        # Only estimate once there is enough of the run behind us for the
        # remaining time to mean anything; an ETA from the first two per cent
        # is worse than none at all.
        if fraction > 0.08 and elapsed > 2.0:
            remaining = elapsed * (1.0 - fraction) / fraction
            self.status.set(f"{message}  —  about {_pretty(remaining)} left")
        else:
            self.status.set(message)

    def _finish(self, status: str) -> None:
        self.progress.configure(value=1000 if "saved" in status else 0)
        self.run_button.state(["!disabled"])
        elapsed = time.monotonic() - self._started
        if self._started and "saved" in status:
            status = f"{status}   (took {_pretty(elapsed)})"
        self.status.set(status)


def _pretty(seconds: float) -> str:
    """Human-readable duration, rounded so it does not twitch every tick."""
    seconds = max(0.0, seconds)
    if seconds < 10:
        return "a few seconds"
    if seconds < 90:
        return f"{int(round(seconds / 5) * 5)}s"
    minutes = seconds / 60
    if minutes < 10:
        return f"{minutes:.1f} min"
    return f"{int(round(minutes))} min"


class _QueueHandler(logging.Handler):
    def __init__(self, target: queue.Queue) -> None:
        super().__init__()
        self.target = target

    def emit(self, record: logging.LogRecord) -> None:
        self.target.put(("log", self.format(record)))


def main() -> int:
    root = tk.Tk()
    try:
        root.call("::tk::unsupported::MacWindowStyle", "style", root, "document")
    except tk.TclError:
        pass  # Not macOS, or the style is unavailable; neither is fatal.
    SyncApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
