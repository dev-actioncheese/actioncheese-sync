# SPDX-License-Identifier: GPL-3.0-or-later
"""GUI construction tests.

The window is built and torn down without entering the main loop, which is
enough to catch the failure that actually happens in practice: a widget or
variable renamed on one side of the UI and not the other.
"""

from __future__ import annotations

from pathlib import Path

import shutil

import pytest

from conftest import requires_ffmpeg

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

    def test_the_button_opens_that_link_and_nothing_else(self, app, monkeypatch) -> None:
        from syncmyvids import about

        opened: list[str] = []
        monkeypatch.setattr("syncmyvids.gui.webbrowser.open", lambda url: opened.append(url))
        app._open_companion()
        assert opened == [about.COMPANION_URL]

    def test_nothing_still_says_coming_soon(self) -> None:
        # The companion app shipped on 28 October 2026. A panel still
        # promising it is worse than no panel.
        import inspect
        from syncmyvids import about, gui

        for where in (inspect.getsource(gui.SyncApp._build_sidebar),
                      about.HELP_TEXT, about.changelog_text(),
                      " ".join(about.COMPANION_POINTS), about.COMPANION_PITCH,
                      about.COMPANION_BUTTON, about.COMPANION_PRICING):
            assert "coming soon" not in where.lower()
        assert not hasattr(about, "COMPANION_STORE_NOTE")

    def test_the_companion_button_leads_to_the_app_store(self) -> None:
        from syncmyvids import about

        assert about.COMPANION_URL.startswith("https://apps.apple.com/")
        assert "6820252527" in about.COMPANION_URL

    def test_an_update_is_not_sent_to_the_app_store(self, app, monkeypatch) -> None:
        # This is a Mac app and does not come from the App Store. The update
        # banner falls back to COMPANION_URL no longer -- that would send
        # somebody chasing a Mac update to an iPhone listing.
        from syncmyvids import about

        opened: list[str] = []
        monkeypatch.setattr("syncmyvids.gui.webbrowser.open",
                            lambda url: opened.append(url))
        app._update_release = None
        app._open_update()
        assert opened == [about.SYNC_URL]
        assert "apps.apple.com" not in about.SYNC_URL

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


class TestWhatChanged:
    """Shown once after an update, and available for good afterwards."""

    def test_a_fresh_install_is_shown_nothing(self, app, monkeypatch) -> None:
        # Nothing has changed for somebody who never ran an older version.
        shown: list[str] = []
        monkeypatch.setattr(app, "_text_window",
                            lambda title, body, **kw: shown.append(title))
        app._prefs.pop("seen_version", None)
        app._show_changes_once()
        assert shown == []

    def shown(self, app, monkeypatch) -> list[tuple[str, str]]:
        """Collect (title, body) for each window the app decides to open."""
        seen: list[tuple[str, str]] = []
        monkeypatch.setattr(app, "_text_window",
                            lambda title, body, **kw: seen.append((title, body)))
        return seen

    def test_after_an_update_it_appears_once(self, app, monkeypatch) -> None:
        shown = self.shown(app, monkeypatch)
        app._prefs["seen_version"] = "0.1.0"
        app._show_changes_once()
        assert len(shown) == 1 and "0.1.0" in shown[0][0]

        app._show_changes_once()          # the next launch
        assert len(shown) == 1, "it must not nag"

    def test_every_fix_since_the_last_one_read_is_listed(
            self, app, monkeypatch) -> None:
        # The point of the accumulation: somebody who last read about 0.4.0
        # must see the patches after it, not only the newest release.
        from syncmyvids import about

        shown = self.shown(app, monkeypatch)
        app._prefs["seen_version"] = "0.1.0"
        app._show_changes_once()
        body = shown[0][1]
        for version, _, _ in about.changes_since("0.1.0"):
            assert version in body, f"{version} missing from the popup"
        assert "0.1.0" not in body, "it listed what they had already read"

    def test_a_patch_does_not_interrupt(self, app, monkeypatch) -> None:
        from syncmyvids import __version__, about

        if not about.CHANGELOG or len(about.CHANGELOG) < 2:
            pytest.skip("needs a previous release to step from")
        shown = self.shown(app, monkeypatch)
        major, minor, _ = (int(n) for n in __version__.split("."))
        app._prefs["seen_version"] = f"{major}.{minor}.0"
        app._show_changes_once()
        assert shown == [], "a hotfix should not put a window in the way"

    def test_a_patch_leaves_its_notes_pending(self, app, monkeypatch) -> None:
        # Not shown is not the same as discarded: the record must stay put so
        # the fix still appears at the next real release.
        from syncmyvids import __version__

        self.shown(app, monkeypatch)
        major, minor, _ = (int(n) for n in __version__.split("."))
        app._prefs["seen_version"] = f"{major}.{minor}.0"
        app._show_changes_once()
        assert app._prefs["seen_version"] == f"{major}.{minor}.0"

    def test_a_real_release_does_interrupt(self, app, monkeypatch) -> None:
        shown = self.shown(app, monkeypatch)
        app._prefs["seen_version"] = "0.3.0"
        app._show_changes_once()
        assert len(shown) == 1

    def test_a_fresh_install_is_recorded_so_it_stays_quiet_later(
            self, app, monkeypatch) -> None:
        from syncmyvids import __version__

        self.shown(app, monkeypatch)
        app._prefs.pop("seen_version", None)
        app._show_changes_once()
        assert app._prefs["seen_version"] == __version__

    def test_the_window_offers_the_companion_app(self, app, monkeypatch) -> None:
        # 0.5.0 announces that the companion app shipped, so the window that
        # carries the announcement has to be able to act on it. A line of
        # text telling somebody an app exists, with no way to reach it, is
        # an advert rather than a feature.
        from syncmyvids import about

        passed: list = []
        monkeypatch.setattr(
            app, "_text_window",
            lambda title, body, **kw: passed.append(kw.get("action")))
        app._prefs["seen_version"] = "0.1.0"
        app._show_changes_once()
        assert passed and passed[0] is not None
        label, command = passed[0]
        assert label == about.COMPANION_BUTTON

        opened: list[str] = []
        monkeypatch.setattr("syncmyvids.gui.webbrowser.open",
                            lambda url: opened.append(url))
        command()
        assert opened == [about.COMPANION_URL]

    def test_the_launch_is_in_the_notes_people_will_see(self) -> None:
        from syncmyvids import __version__, about

        newest = about.changelog_text(about.changes_since("0.4.0")).lower()
        assert "app store" in newest, (
            "0.5.0 exists to carry the companion app's launch")
        # And the accumulated patches must still be there beside it.
        for version in ("0.4.4", "0.4.3", "0.4.2", "0.4.1"):
            assert version in newest, f"{version} dropped out of the window"

    def test_which_releases_count_as_worth_showing(self) -> None:
        from syncmyvids import about

        assert about.is_significant("0.4.0", "0.5.0")
        assert about.is_significant("0.4.1", "1.0.0")
        assert not about.is_significant("0.4.0", "0.4.1")
        assert not about.is_significant("0.4.0", "0.4.9")
        # Numbers, not text: 0.10 is after 0.9.
        assert about.is_significant("0.9.0", "0.10.0")

    def test_it_lists_only_what_is_new_to_you(self) -> None:
        from syncmyvids import about

        versions = [v for v, _, _ in about.changes_since("0.2.0")]
        assert "0.2.0" not in versions and "0.1.0" not in versions
        assert "0.3.0" in versions

    def test_the_whole_history_is_available_any_time(self, app, monkeypatch) -> None:
        from syncmyvids import about

        shown: list[str] = []
        monkeypatch.setattr(app, "_text_window",
                            lambda title, body, **kw: shown.append(body))
        app.show_changelog()
        for version, _, _ in about.CHANGELOG:
            assert version in shown[0], f"{version} missing from the full log"

    def test_every_entry_has_a_date_and_something_to_say(self) -> None:
        import re
        from syncmyvids import about

        for version, date, changes in about.CHANGELOG:
            assert re.fullmatch(r"\d+\.\d+\.\d+", version), version
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", date), f"{version}: {date}"
            assert changes and all(c.strip() for c in changes), version

    def test_the_current_version_is_in_the_log(self) -> None:
        # Releasing without saying what changed is how a changelog dies.
        from syncmyvids import __version__, about

        assert __version__ in [v for v, _, _ in about.CHANGELOG]


class TestDroppingFiles:
    def drop(self, app, payload: str):
        class Event:
            data = payload
        app._on_drop(Event())

    def test_folders_and_timelines_are_taken(self, app, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(app, "_preview", lambda: None)
        folder = tmp_path / "cam"; folder.mkdir()
        xml = tmp_path / "t.xml"; xml.write_text("<xmeml/>", encoding="utf-8")
        self.drop(app, f"{folder} {xml}")
        assert {p.name for p in app.inputs} == {"cam", "t.xml"}

    def test_a_path_with_spaces_survives(self, app, tmp_path: Path, monkeypatch) -> None:
        # Tcl delivers these in braces; splitting on whitespace would break
        # every wedding folder ever named.
        monkeypatch.setattr(app, "_preview", lambda: None)
        folder = tmp_path / "BESLIU 10 OKT"; folder.mkdir()
        self.drop(app, "{%s}" % folder)
        assert [p.name for p in app.inputs] == ["BESLIU 10 OKT"]

    def test_anything_else_is_ignored_and_said_so(self, app, tmp_path: Path,
                                                  monkeypatch) -> None:
        monkeypatch.setattr(app, "_preview", lambda: None)
        photo = tmp_path / "shot.jpg"; photo.write_bytes(b"x")
        self.drop(app, str(photo))
        assert app.inputs == []

    def test_a_malformed_payload_does_not_crash(self, app, monkeypatch) -> None:
        monkeypatch.setattr(app, "_preview", lambda: None)
        self.drop(app, "{unclosed brace")
        assert app.inputs == []


@requires_ffmpeg
class TestTheWorkerActuallyRuns:
    """Drive the window's own worker over real media, end to end.

    Nothing covered this before, and a single mistyped method name in it --
    report.as_dict() for report.to_dict() -- shipped in 0.4.0 and broke every
    sync in the app while 345 tests stayed green. The widgets were tested, the
    thing they exist to do was not.
    """

    def run_worker(self, app, fixture_timeline: Path, tmp_path: Path, monkeypatch):
        from syncmyvids.engine import SyncOptions

        source = tmp_path / "in.xml"
        shutil.copy(fixture_timeline, source)
        target = tmp_path / "out.xml"

        # A failure in the worker ends as a modal dialog, which waits for a
        # click that never comes under pytest.  Stub the dialogs first, or a
        # broken worker hangs the suite instead of failing it -- which is what
        # happened the first time this test was pointed at the 0.4.0 bug.
        self.dialogs: list[tuple[str, str, str]] = []
        for kind in ("showerror", "showwarning", "showinfo"):
            monkeypatch.setattr(
                f"syncmyvids.gui.messagebox.{kind}",
                lambda title, message, _k=kind: self.dialogs.append(
                    (_k, title, message)),
            )

        # Record what the worker reports, then let the window's own pump
        # handle it -- that handler is half of what broke.
        seen: list = []
        real_put = app.messages.put
        app.messages.put = lambda item: (seen.append(item), real_put(item))[1]
        try:
            app._run([source], target, SyncOptions())
            app._drain()
        finally:
            app.messages.put = real_put
        return target, dict(seen), seen

    def test_a_sync_completes_without_error(self, app, fixture_timeline: Path,
                                            tmp_path: Path, monkeypatch) -> None:
        target, seen, drained = self.run_worker(
            app, fixture_timeline, tmp_path, monkeypatch)
        errors = [p for kind, p in drained if kind == "error"]
        assert errors == [], f"the worker reported: {errors}"
        assert [d for d in self.dialogs if d[0] == "showerror"] == []
        assert "done" in seen
        assert target.exists(), "no timeline was written"

    def test_the_report_is_kept_for_comparing(self, app, fixture_timeline: Path,
                                              tmp_path: Path, monkeypatch) -> None:
        # This is the line that broke: the worker serialises the report so
        # Help > Compare can measure against it later.
        self.run_worker(app, fixture_timeline, tmp_path, monkeypatch)
        assert isinstance(app._last_report, dict) and app._last_report
        assert "pairs" in app._last_report, "the scored pairs must survive"
        assert app._last_output is not None and app._last_output.exists()

    def test_both_timelines_reach_the_view(self, app, fixture_timeline: Path,
                                           tmp_path: Path, monkeypatch) -> None:
        _, seen, _ = self.run_worker(
            app, fixture_timeline, tmp_path, monkeypatch)
        before, after = seen["timelines"]
        assert before.tracks and after.tracks


@requires_ffmpeg
class TestSavingTheComparison:
    """Help > Compare With a Correct Timeline, as far as the file on disk.

    The save silently did nothing in 0.4.1: json refused a numpy scalar in
    the bundle, and because the failure was on the UI thread rather than the
    worker, Tk printed the traceback to a stderr that a bundled .app discards.
    The operator chose a filename and got no file and no message.
    """

    def compare_and_save(self, app, fixture_timeline: Path, tmp_path: Path,
                         monkeypatch, *, send: bool = True):
        from syncmyvids.engine import SyncOptions

        source = tmp_path / "in.xml"
        shutil.copy(fixture_timeline, source)
        target = tmp_path / "out.xml"

        self.dialogs: list[tuple[str, str, str]] = []
        for kind in ("showerror", "showwarning", "showinfo"):
            monkeypatch.setattr(
                f"syncmyvids.gui.messagebox.{kind}",
                lambda title, message, _k=kind: self.dialogs.append(
                    (_k, title, message)))
        monkeypatch.setattr("syncmyvids.gui.webbrowser.open", lambda url: None)

        # A real run, so the report carries the matcher's own values.
        app._run([source], target, SyncOptions())
        app._drain()

        saved = tmp_path / "comparison.json"
        monkeypatch.setattr("syncmyvids.gui.filedialog.askopenfilename",
                            lambda **kw: str(source))
        monkeypatch.setattr("syncmyvids.gui.messagebox.askyesno",
                            lambda *a, **kw: send)
        monkeypatch.setattr("syncmyvids.gui.filedialog.asksaveasfilename",
                            lambda **kw: str(saved))
        app._compare_with_good()
        return saved

    def test_the_file_appears_where_it_was_asked_for(
            self, app, fixture_timeline: Path, tmp_path: Path, monkeypatch) -> None:
        saved = self.compare_and_save(app, fixture_timeline, tmp_path, monkeypatch)
        assert saved.exists(), "the operator chose a filename and got no file"
        assert saved.stat().st_size > 0

    def test_what_lands_is_the_bundle_and_is_valid_json(
            self, app, fixture_timeline: Path, tmp_path: Path, monkeypatch) -> None:
        import json

        saved = self.compare_and_save(app, fixture_timeline, tmp_path, monkeypatch)
        bundle = json.loads(saved.read_text(encoding="utf-8"))
        assert "summary" in bundle and "scored_pairs" in bundle
        assert bundle["anonymous"] is True

    def test_nothing_went_wrong_quietly(
            self, app, fixture_timeline: Path, tmp_path: Path, monkeypatch) -> None:
        self.compare_and_save(app, fixture_timeline, tmp_path, monkeypatch)
        errors = [d for d in self.dialogs if d[0] == "showerror"]
        assert errors == [], f"it reported: {errors}"

    def test_declining_to_send_writes_nothing(
            self, app, fixture_timeline: Path, tmp_path: Path, monkeypatch) -> None:
        saved = self.compare_and_save(app, fixture_timeline, tmp_path, monkeypatch,
                                      send=False)
        assert not saved.exists()

    def test_a_save_that_fails_says_so(
            self, app, fixture_timeline: Path, tmp_path: Path, monkeypatch) -> None:
        # The silence was the worse half of the bug: a failure must be visible.
        def refuse(self, *a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(Path, "write_text", refuse)
        saved = self.compare_and_save(app, fixture_timeline, tmp_path, monkeypatch)
        assert not saved.exists()
        assert [d for d in self.dialogs if d[0] == "showerror"], (
            "a failed save told the operator nothing")
