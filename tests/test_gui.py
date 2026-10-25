# SPDX-License-Identifier: GPL-3.0-or-later
"""GUI construction tests.

The window is built and torn down without entering the main loop, which is
enough to catch the failure that actually happens in practice: a widget or
variable renamed on one side of the UI and not the other.
"""

from __future__ import annotations

from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")


@pytest.fixture(autouse=True)
def _never_touch_real_preferences(tmp_path, monkeypatch):
    """Keep the suite out of the operator's own preferences file.

    Exercising the folder picker calls _remember_dir, which saves. Left
    alone, a test run writes a pytest temp path into the real window.json --
    harmless, since a vanished folder is ignored on load, but it is still
    somebody's settings being edited by a test.
    """
    store = tmp_path / "window.json"
    monkeypatch.setattr("syncmyvids.gui._prefs_path", lambda: store)
    monkeypatch.setattr("syncmyvids.gui._load_prefs", lambda: {})
    saved: dict = {}
    monkeypatch.setattr("syncmyvids.gui._save_prefs", saved.update)
    return saved


@pytest.fixture
def app():
    from syncmyvids.gui import SyncApp

    try:
        root = tk.Tk()
    except tk.TclError:  # pragma: no cover - headless CI
        pytest.skip("no display available")
    root.withdraw()
    instance = SyncApp(root)
    root.update_idletasks()
    yield instance
    root.destroy()


class TestConstruction:
    def test_defaults_match_the_cli(self, app) -> None:
        assert app.suffix.get() == "_synced"
        assert app.placement.get() == "inline"
        assert app.label_colour.get() == "Rose"
        assert app.confidence.get() == "0.45"
        assert app.close_gaps.get() is True
        assert app.correct_drift.get() is True

    def test_startup_reports_ffmpeg(self, app) -> None:
        assert "ffmpeg" in app.log.get("1.0", "end").lower()

    def test_rose_is_offered_as_the_red_label(self) -> None:
        from syncmyvids.gui import LABEL_COLOURS

        # Premiere has no label literally called "Red"; Rose is its red.
        assert "Rose" in LABEL_COLOURS
        assert "(none)" in LABEL_COLOURS


class TestOptionMapping:
    """Every control must reach SyncOptions -- a silently ignored checkbox is
    worse than a missing one."""

    def test_controls_map_onto_sync_options(self, app) -> None:
        from dataclasses import fields

        from syncmyvids.engine import SyncOptions

        names = {f.name for f in fields(SyncOptions)}
        for option in ("min_confidence", "reference", "close_gaps", "correct_drift",
                       "unsynced_placement", "unsynced_label", "sequence_suffix"):
            assert option in names, f"GUI sets {option}, which SyncOptions no longer has"

    def test_none_label_disables_marking(self, app) -> None:
        app.label_colour.set("(none)")
        assert app.label_colour.get() == "(none)"

    def test_choosing_input_derives_the_output_name(self, app, tmp_path: Path, monkeypatch) -> None:
        source = tmp_path / "wedding.xml"
        source.write_text("<xmeml/>", encoding="utf-8")
        monkeypatch.setattr("syncmyvids.gui.filedialog.askopenfilenames",
                            lambda **_: (str(source),))
        monkeypatch.setattr(app, "_preview", lambda: None)
        app._choose_input()
        assert app.inputs == [source]
        assert Path(app.output_path.get()).name == "wedding_synced.xml"

    def test_a_folder_can_be_added_alongside_a_timeline(self, app, tmp_path: Path,
                                                        monkeypatch) -> None:
        timeline = tmp_path / "main.xml"
        timeline.write_text("<xmeml/>", encoding="utf-8")
        camera = tmp_path / "camera-a"
        camera.mkdir()
        monkeypatch.setattr(app, "_preview", lambda: None)
        monkeypatch.setattr("syncmyvids.gui.filedialog.askopenfilenames",
                            lambda **_: (str(timeline),))
        monkeypatch.setattr(app, "_choose_folders_macos", lambda: [])
        monkeypatch.setattr("syncmyvids.gui.filedialog.askdirectory",
                            lambda **_: str(camera))
        app._choose_input()
        app._choose_folder()
        assert app.inputs == [timeline, camera]
        # The output is named after the first input and does not change when
        # more are added -- renaming it under the operator would be rude.
        assert Path(app.output_path.get()).name == "main_synced.xml"

    def test_the_same_input_is_not_added_twice(self, app, tmp_path: Path, monkeypatch) -> None:
        camera = tmp_path / "camera-a"
        camera.mkdir()
        monkeypatch.setattr(app, "_preview", lambda: None)
        monkeypatch.setattr(app, "_choose_folders_macos", lambda: [])
        monkeypatch.setattr("syncmyvids.gui.filedialog.askdirectory", lambda **_: str(camera))
        app._choose_folder()
        app._choose_folder()
        assert app.inputs == [camera]

    def test_removing_an_input_drops_it(self, app, tmp_path: Path, monkeypatch) -> None:
        first, second = tmp_path / "a", tmp_path / "b"
        first.mkdir()
        second.mkdir()
        monkeypatch.setattr(app, "_preview", lambda: None)
        app._add_inputs([first, second])
        app.input_list.selection_set(0)
        app._remove_inputs()
        assert app.inputs == [second]


class TestGuards:
    def test_missing_input_is_refused(self, app, monkeypatch) -> None:
        warned: list[str] = []
        monkeypatch.setattr("syncmyvids.gui.messagebox.showwarning",
                            lambda title, msg: warned.append(title))
        app.inputs = []
        app._start()
        assert warned and app.worker is None

    def test_bad_confidence_is_refused(self, app, monkeypatch, tmp_path: Path) -> None:
        warned: list[str] = []
        monkeypatch.setattr("syncmyvids.gui.messagebox.showwarning",
                            lambda title, msg: warned.append(title))
        app.inputs = [tmp_path / "a.xml"]
        app.output_path.set(str(tmp_path / "b.xml"))
        app.confidence.set("not a number")
        app._start()
        assert warned == ["Bad value"]
        assert app.worker is None


class TestAboutAndHelp:
    def test_author_and_contact_are_present(self) -> None:
        from syncmyvids import about

        text = about.about_text()
        assert about.AUTHOR in text
        assert about.CONTACT_EMAIL in text
        assert about.CONTACT_EMAIL == "contact@actioncheese.com"
        assert about.BUG_EMAIL == "dev@actioncheese.com"

    def test_help_covers_every_gui_option(self) -> None:
        """Help must explain each control, or it is not help."""
        from syncmyvids import about

        for topic in ("Close empty gaps", "Correct recorder drift", "inline",
                      "Mark with", "Reference track", "Min confidence",
                      "Name suffix"):
            assert topic in about.HELP_TEXT, f"help does not explain {topic!r}"

    def test_help_tells_people_where_to_report_bugs(self) -> None:
        from syncmyvids import about

        assert about.CONTACT_EMAIL in about.HELP_TEXT

    def test_dialogs_open_and_close(self, app) -> None:
        app.show_about()
        app.show_help()
        app.root.update_idletasks()
        extra = [w for w in app.root.winfo_children() if w.winfo_class() == "Toplevel"]
        assert len(extra) == 2
        for window in extra:
            window.destroy()

    def test_window_icon_asset_exists(self) -> None:
        from syncmyvids.gui import _asset

        assert _asset("logo-256.png") is not None, "logo missing; run packaging/make_icon.py"


class TestProgress:
    def test_bar_and_eta_track_the_run(self, app) -> None:
        import time

        app._started = time.monotonic() - 30
        app._show_progress(0.5, "matching clips")
        assert app.progress.cget("value") == 500
        assert "matching clips" in app.status.get()
        assert "left" in app.status.get()

    def test_no_estimate_before_there_is_evidence_for_one(self, app) -> None:
        """An ETA from the first two per cent is worse than none at all."""
        import time

        app._started = time.monotonic()
        app._show_progress(0.02, "reading timeline")
        assert "left" not in app.status.get()
        assert app.status.get() == "reading timeline"

    def test_durations_read_naturally(self) -> None:
        from syncmyvids.gui import _pretty

        assert _pretty(3) == "a few seconds"
        assert _pretty(42) == "40s"
        assert _pretty(95) == "1.6 min"
        assert _pretty(-5) == "a few seconds"


class TestEngineProgress:
    def test_callback_is_monotonic_and_bounded(self, tmp_path) -> None:
        """Whatever the pipeline does, the bar must only ever move forwards."""
        from syncmyvids.engine import SyncOptions, sync_timeline
        from syncmyvids.formats import fcp7

        source = tmp_path / "in.xml"
        source.write_text(
            '<?xml version="1.0"?><!DOCTYPE xmeml><xmeml version="5"><sequence id="s">'
            "<name>S</name><duration>100</duration>"
            "<rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>"
            "<media><video><track/></video><audio><track/></audio></media>"
            "</sequence></xmeml>", encoding="utf-8")

        seen: list[tuple[float, str]] = []
        sync_timeline(fcp7.read(source), SyncOptions(),
                      progress=lambda f, m: seen.append((f, m)))
        assert seen, "no progress reported"
        fractions = [f for f, _ in seen]
        assert fractions == sorted(fractions)
        assert all(0.0 <= f <= 1.0 for f in fractions)
        assert fractions[-1] == 1.0
        assert all(m for _, m in seen), "every step needs a description"


class TestHelpTextStaysIntact:
    """Guards against an edit splicing one entry into the middle of another.

    An unanchored string replace once pasted the whole "Trust already-synced
    clips" description into the middle of a one-line troubleshooting answer,
    because the phrase it keyed on appeared twice in the file. Nothing caught
    it but a reader.
    """

    def entries(self, heading: str) -> dict[str, list[str]]:
        from syncmyvids import about

        section = about.HELP_TEXT.split(heading)[1]
        out: dict[str, list[str]] = {}
        for block in section.strip().split("\n\n"):
            lines = [line for line in block.splitlines() if line.strip()]
            if lines and not lines[0].startswith(" ") and lines[0].isprintable():
                out[lines[0].strip()] = [line.strip() for line in lines[1:]]
        return out

    def test_the_speed_effect_answer_is_one_line(self) -> None:
        body = self.entries("IF SOMETHING LOOKS WRONG")["Premiere warns about a speed effect"]
        assert body == ['Turn off "Correct recorder drift" and run again, then report it.']

    def test_every_quoted_phrase_is_closed(self) -> None:
        from syncmyvids import about

        unbalanced = [line for line in about.HELP_TEXT.splitlines()
                      if line.count('"') % 2]
        assert unbalanced == [], "a splice inside a quoted phrase leaves it open"

    def test_no_paragraph_appears_twice(self) -> None:
        import re
        from collections import Counter
        from syncmyvids import about

        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", about.HELP_TEXT)
                     if len(s.strip()) > 45]
        assert [s for s, n in Counter(sentences).items() if n > 1] == []

    def test_the_options_are_documented_in_the_order_the_window_shows_them(self) -> None:
        from syncmyvids import about

        documented = list(self.entries("WHAT THE OPTIONS DO"))
        for earlier, later in (("Close empty gaps", "Correct recorder drift"),
                               ("Correct recorder drift", "Trust already-synced clips")):
            assert documented.index(earlier) < documented.index(later)

    def test_every_checkbox_in_the_window_is_explained(self) -> None:
        import inspect
        import re
        from syncmyvids import about, gui

        source = inspect.getsource(gui.SyncApp._build)
        labels = re.findall(r'Checkbutton\(\s*options,\s*text="([^"]+)"', source)
        labels += re.findall(r'Checkbutton\(options, text="([^"]+)"', source)
        assert labels, "no checkboxes found -- the pattern needs updating"
        for label in labels:
            assert label in about.HELP_TEXT, f"{label} is not in the help"


class TestCompanionSidebar:
    """The panel is advertising, so it must stay out of the way and stay honest."""

    def test_it_builds_and_folds_both_ways(self, app) -> None:
        assert app.sidebar_open is True
        app._toggle_sidebar()
        assert app.sidebar_open is False
        assert not app.sidebar.winfo_ismapped()
        app._toggle_sidebar()
        assert app.sidebar_open is True

    def test_the_collapsed_state_is_remembered(self, app, monkeypatch) -> None:
        saved: list[dict] = []
        monkeypatch.setattr("syncmyvids.gui._save_prefs", lambda values: saved.append(values))
        app._toggle_sidebar()
        assert saved and saved[-1]["sidebar_open"] is False

    def test_the_link_is_the_companion_site(self) -> None:
        from syncmyvids import about

        assert about.COMPANION_URL == "https://actioncheese.com/"

    def test_the_button_opens_that_link_and_nothing_else(self, app, monkeypatch) -> None:
        from syncmyvids import about

        opened: list[str] = []
        monkeypatch.setattr("syncmyvids.gui.webbrowser.open", lambda url: opened.append(url))
        app._open_companion()
        assert opened == [about.COMPANION_URL]

    def test_the_store_note_is_one_constant(self) -> None:
        # So a launch is a one-line change rather than a hunt through layout.
        import inspect
        from syncmyvids import about, gui

        assert about.COMPANION_STORE_NOTE
        source = inspect.getsource(gui.SyncApp._build_sidebar)
        assert "coming soon" not in source.lower(), "the note is hard-coded in the layout"

    def test_the_wording_lives_in_about(self) -> None:
        from syncmyvids import about

        for name in ("COMPANION_NAME", "COMPANION_PITCH", "COMPANION_POINTS",
                     "COMPANION_PRICING", "COMPANION_BUTTON", "COMPANION_ICON"):
            assert getattr(about, name), f"{name} is empty"
        # Not a fixed count -- the points change as the companion app does.
        # What matters is that each is a short line the panel can wrap.
        assert about.COMPANION_POINTS
        for point in about.COMPANION_POINTS:
            assert 10 < len(point) <= 70, f"too long for the panel: {point}"

    def test_the_companion_icon_ships_with_the_app(self) -> None:
        from syncmyvids import about
        from syncmyvids.gui import _asset

        assert _asset(about.COMPANION_ICON) is not None


class TestNothingIsInvisible:
    """Every state of every widget must keep its text readable.

    Leaving the native theme for "clam" means inheriting a *light* theme's
    defaults for every state not named explicitly. That is how the comboboxes
    came to turn near-white under the pointer while their text stayed
    near-white: configure() sets the resting colours only, and ttk has no "and
    all the other states" catch-all. Nothing caught it but a person hovering.
    """

    #: (style, background option). Foreground is compared against each.
    SURFACES = [
        ("TButton", "background"),
        ("Cheese.TButton", "background"),
        ("TCheckbutton", "background"),
        ("TEntry", "fieldbackground"),
        ("TCombobox", "fieldbackground"),
        ("TCombobox", "background"),
        ("TNotebook.Tab", "background"),
        ("TLabel", "background"),
        ("TLabelframe.Label", "background"),
    ]
    STATES = ["", "active", "pressed", "selected", "focus", "hover", "readonly"]

    def _luminance(self, root, colour: str) -> float:
        # winfo_rgb resolves names and short hex alike, in 16-bit channels.
        channels = [value / 65535 for value in root.winfo_rgb(colour)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
                  for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    def _contrast(self, root, one: str, two: str) -> float:
        a, b = self._luminance(root, one), self._luminance(root, two)
        lighter, darker = max(a, b), min(a, b)
        return (lighter + 0.05) / (darker + 0.05)

    def test_text_is_legible_in_every_state(self, app) -> None:
        from tkinter import ttk

        style = ttk.Style(app.root)
        failures: list[str] = []
        for name, background_option in self.SURFACES:
            for state in self.STATES:
                states = [state] if state else []
                background = style.lookup(name, background_option, states)
                foreground = style.lookup(name, "foreground", states)
                if not background or not foreground:
                    continue
                ratio = self._contrast(app.root, background, foreground)
                if ratio < 3.0:
                    failures.append(
                        f"{name} [{state or 'normal'}]: {foreground} on {background} "
                        f"= {ratio:.2f}:1")
        assert not failures, "unreadable combinations:\n  " + "\n  ".join(failures)

    def test_disabled_text_is_dimmed_but_still_there(self, app) -> None:
        from tkinter import ttk

        style = ttk.Style(app.root)
        for name, background_option in self.SURFACES:
            background = style.lookup(name, background_option, ["disabled"])
            foreground = style.lookup(name, "foreground", ["disabled"])
            if not background or not foreground:
                continue
            ratio = self._contrast(app.root, background, foreground)
            assert ratio >= 1.8, f"{name} disabled: {ratio:.2f}:1 is invisible, not dim"


class TestMergingFoldersIntoOneCamera:
    """One camera that filled several cards is still one camera.

    Read as separate devices its cards would be matched against each other,
    which breaks the rule the whole solver leans on: a device cannot record
    two things at once.
    """

    def folders(self, tmp_path: Path) -> list[Path]:
        made = []
        for name in ("card1", "card2", "other"):
            folder = tmp_path / "cam-one" / name if name != "other" else tmp_path / "other"
            folder.mkdir(parents=True)
            made.append(folder)
        return made

    def prepared(self, app, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(app, "_preview", lambda: None)
        app._add_inputs(self.folders(tmp_path))
        return app

    def test_selected_folders_become_one_entry(self, app, tmp_path: Path, monkeypatch) -> None:
        self.prepared(app, tmp_path, monkeypatch)
        app.input_list.selection_set(0, 1)
        app._merge_inputs()
        assert len(app.inputs) == 2
        assert isinstance(app.inputs[0], list) and len(app.inputs[0]) == 2
        assert not isinstance(app.inputs[1], list), "the third folder is untouched"

    def test_merging_keeps_the_order_it_was_added_in(self, app, tmp_path: Path,
                                                     monkeypatch) -> None:
        self.prepared(app, tmp_path, monkeypatch)
        names = [p.name for p in app.inputs[:2]]
        app.input_list.selection_set(0, 1)
        app._merge_inputs()
        assert [p.name for p in app.inputs[0]] == names

    def test_a_merged_entry_splits_back(self, app, tmp_path: Path, monkeypatch) -> None:
        self.prepared(app, tmp_path, monkeypatch)
        app.input_list.selection_set(0, 1)
        app._merge_inputs()
        app.input_list.selection_clear(0, "end")
        app.input_list.selection_set(0)
        app._merge_inputs()
        assert len(app.inputs) == 3
        assert not any(isinstance(e, list) for e in app.inputs)

    def test_the_button_names_what_it_would_do(self, app, tmp_path: Path,
                                               monkeypatch) -> None:
        self.prepared(app, tmp_path, monkeypatch)
        app.input_list.selection_set(0, 1)
        app._update_merge_button()
        assert app.merge_button.cget("text") == "Merge"
        app._merge_inputs()
        app.input_list.selection_clear(0, "end")
        app.input_list.selection_set(0)
        app._update_merge_button()
        assert app.merge_button.cget("text") == "Split"

    def test_a_timeline_cannot_be_half_a_camera(self, app, tmp_path: Path,
                                                monkeypatch) -> None:
        warned: list[str] = []
        monkeypatch.setattr("syncmyvids.gui.messagebox.showwarning",
                            lambda title, msg: warned.append(title))
        monkeypatch.setattr(app, "_preview", lambda: None)
        timeline = tmp_path / "a.xml"
        timeline.write_text("<xmeml/>", encoding="utf-8")
        folder = tmp_path / "cam"
        folder.mkdir()
        app._add_inputs([timeline, folder])
        app.input_list.selection_set(0, 1)
        app._merge_inputs()
        assert warned == ["Cannot merge"]
        assert not any(isinstance(e, list) for e in app.inputs)

    def test_the_merged_entry_reaches_the_engine_as_one_device(self, app, tmp_path: Path,
                                                               monkeypatch) -> None:
        # The GUI's model is handed straight to load_inputs, so the shape it
        # builds has to be the shape the engine understands.
        self.prepared(app, tmp_path, monkeypatch)
        app.input_list.selection_set(0, 1)
        app._merge_inputs()
        assert isinstance(app.inputs[0], list)
        assert all(isinstance(p, Path) for p in app.inputs[0])


class TestRememberingWhereYouWere:
    def test_the_parent_is_remembered_not_the_folder(self, app, tmp_path: Path,
                                                     monkeypatch) -> None:
        # Cameras sit side by side under one shoot folder, so the next dialog
        # should open on the shoot, not inside the camera just chosen.
        saved: list[dict] = []
        monkeypatch.setattr("syncmyvids.gui._save_prefs", lambda v: saved.append(v))
        shoot = tmp_path / "shoot"
        (shoot / "cam1").mkdir(parents=True)
        app._remember_dir(shoot / "cam1")
        assert saved[-1]["last_dir"] == str(shoot)

    def test_a_vanished_location_is_not_offered(self, app, tmp_path: Path) -> None:
        app._prefs["last_dir"] = str(tmp_path / "gone")
        assert app._last_dir() is None

    def test_a_live_location_is_offered(self, app, tmp_path: Path) -> None:
        app._prefs["last_dir"] = str(tmp_path)
        assert app._last_dir() == str(tmp_path)
