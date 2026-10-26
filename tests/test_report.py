# SPDX-License-Identifier: GPL-3.0-or-later
"""Writing the run's own report out, which nothing covered before.

The comparison bundle failed to save because a numpy scalar reached json.
This writer takes the same values from the same matcher and had no test at
all, so it is the same bug waiting in the CLI's --report-json.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncmyvids.report import SyncReport
from syncmyvids.timebase import Rate


def report(**kwargs) -> SyncReport:
    return SyncReport(
        source="in.xml", sequence_name="seq", rate=Rate(timebase=25),
        unit_count=2, reference_track="V1", last_synced_end_frames=100,
        sequence_duration_frames=200, **kwargs)


class TestWritingTheReport:
    def test_it_writes_json_that_reads_back(self, tmp_path: Path) -> None:
        path = tmp_path / "r.json"
        report().write_json(path)
        assert json.loads(path.read_text(encoding="utf-8"))["sequence"]

    def test_it_makes_the_folder_it_is_pointed_at(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "down" / "r.json"
        report().write_json(path)
        assert path.exists()

    def test_numpy_values_from_the_matcher_do_not_stop_it(
            self, tmp_path: Path) -> None:
        # np.float64 passes for a float; np.bool_ does not pass for a bool,
        # and json refuses it. Either can arrive here.
        np = pytest.importorskip("numpy")
        path = tmp_path / "r.json"
        report(pairs=[{
            "a": "a.mov", "b": "b.mov",
            "confidence": np.float32(0.9), "offset": np.float64(1.25),
            "accepted": np.bool_(True),
        }]).write_json(path)
        pair = json.loads(path.read_text(encoding="utf-8"))["pairs"][0]
        assert pair["accepted"] is True
        assert pair["offset"] == pytest.approx(1.25)


def island(label: str, reason: str) -> dict:
    return {"label": label, "status": "island", "start_frames": 0,
            "shift_frames": 0, "reason": reason}


class TestSayingWhereAGroupsPositionCameFrom:
    """A group that sounds alone has to be reported as such.

    Two recordings from one device never overlap, so audio can never relate
    them. On a two-device wedding that left 24 groups whose positions came
    from a camera clock, and the report said only "24 in separate synced
    groups" -- which reads as a sync failure. It is not: inside each group
    the clips are frame-accurate. What the operator needed was to know that
    the joins *between* groups, and only those, were open to question.
    """

    def test_it_counts_how_each_group_was_positioned(self) -> None:
        r = report(placements=[island("a", "clock"), island("b", "clock"),
                               island("c", "appended")])
        assert r.island_placement == {"clock": 2, "appended": 1}

    def test_a_clean_run_says_nothing_about_groups(self) -> None:
        r = report(placements=[{"label": "a", "status": "synced",
                                "start_frames": 0, "shift_frames": 0}])
        assert r.island_placement == {}
        assert "Groups that sound alone" not in r.render()

    def test_the_report_explains_what_is_and_is_not_in_doubt(self) -> None:
        text = report(placements=[island("a", "clock")]).render()
        assert "Groups that sound alone" in text
        assert "synced to each other" in text, (
            "it must say the clips inside a group are right, or the operator "
            "reads the whole timeline as broken")

    def test_an_appended_group_admits_it_is_not_positioned(self) -> None:
        text = report(placements=[island("a", "appended")]).render()
        assert "no" in text.lower() and "clock" in text.lower()
        assert "Drag the block into place." in text

    def test_it_survives_a_placement_with_no_reason_recorded(self) -> None:
        r = report(placements=[{"label": "a", "status": "island",
                                "start_frames": 0, "shift_frames": 0}])
        assert r.island_placement == {"unknown": 1}
        r.render()

    def test_the_counts_reach_the_json(self) -> None:
        r = report(placements=[island("a", "clock")])
        assert r.to_dict()["summary"]["island_placement"] == {"clock": 1}
