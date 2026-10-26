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
