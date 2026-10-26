# SPDX-License-Identifier: GPL-3.0-or-later
"""Measuring our sync against one known to be right, without the footage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncmyvids import compare
from syncmyvids.formats import fcp7

TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<xmeml version="5">
  <sequence id="s"><name>t</name><duration>100000</duration>
    <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
    <media><video><track>{items}</track></video><audio><track/></audio></media>
  </sequence>
</xmeml>
"""
ITEM = """<clipitem id="c{n}"><name>{file}</name><duration>250</duration>
  <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
  <start>{start}</start><end>{end}</end><in>0</in><out>250</out>
  <file id="f{n}"><name>{file}</name>
    <pathurl>file://localhost/Clients/Wedding%20Smith/{device}/{file}</pathurl></file>
</clipitem>"""


def timeline(tmp_path: Path, name: str, clips) -> object:
    items = "".join(
        ITEM.format(n=i, file=f, device=d, start=int(s * 25), end=int(s * 25) + 250)
        for i, (f, d, s) in enumerate(clips))
    path = tmp_path / name
    path.write_text(TEMPLATE.format(items=items), encoding="utf-8")
    return fcp7.read(path)


class TestComparing:
    def test_identical_timelines_agree_exactly(self, tmp_path: Path) -> None:
        clips = [("a.mov", "camA", 0.0), ("b.mov", "camB", 10.0)]
        result = compare.compare(timeline(tmp_path, "1.xml", clips),
                                 timeline(tmp_path, "2.xml", clips))
        assert len(result.agreed) == 2
        assert not result.wrong

    def test_a_whole_timeline_shift_is_not_an_error(self, tmp_path: Path) -> None:
        """Two syncs of one shoot routinely start at different zeros."""
        ours = [("a.mov", "camA", 30.0), ("b.mov", "camB", 40.0)]
        theirs = [("a.mov", "camA", 0.0), ("b.mov", "camB", 10.0)]
        result = compare.compare(timeline(tmp_path, "1.xml", ours),
                                 timeline(tmp_path, "2.xml", theirs))
        assert result.offset == pytest.approx(30.0)
        assert len(result.agreed) == 2, "the relationship is identical"

    def test_one_clip_in_the_wrong_place_is_found(self, tmp_path: Path) -> None:
        ours = [("a.mov", "camA", 0.0), ("b.mov", "camB", 10.0), ("c.mov", "camB", 25.0)]
        theirs = [("a.mov", "camA", 0.0), ("b.mov", "camB", 10.0), ("c.mov", "camB", 20.0)]
        result = compare.compare(timeline(tmp_path, "1.xml", ours),
                                 timeline(tmp_path, "2.xml", theirs))
        assert [d.name for d in result.wrong] == ["c.mov"]
        assert result.wrong[0].delta == pytest.approx(5.0)

    def test_one_device_sitting_apart_from_another_shows_up(self, tmp_path: Path) -> None:
        """The shape of the error says what kind of fault it is.

        Only the *gap* between devices is meaningful. Two timelines give
        relative positions and nothing else, so whichever device carries most
        of the clips ends up as the zero -- which device is "really" wrong is
        not a question the comparison can answer, and should not pretend to.
        """
        ours = [("a.mov", "camA", 0.0), ("b.mov", "camB", 12.0), ("c.mov", "camB", 22.0)]
        theirs = [("a.mov", "camA", 0.0), ("b.mov", "camB", 10.0), ("c.mov", "camB", 20.0)]
        result = compare.compare(timeline(tmp_path, "1.xml", ours),
                                 timeline(tmp_path, "2.xml", theirs))
        offsets = result.device_offsets()
        assert abs(offsets["camA"] - offsets["camB"]) == pytest.approx(2.0, abs=0.01)

    def test_errors_scattered_inside_one_device_are_not_a_constant(
            self, tmp_path: Path) -> None:
        ours = [("a.mov", "cam", 0.0), ("b.mov", "cam", 13.0), ("c.mov", "cam", 18.0)]
        theirs = [("a.mov", "cam", 0.0), ("b.mov", "cam", 10.0), ("c.mov", "cam", 20.0)]
        result = compare.compare(timeline(tmp_path, "1.xml", ours),
                                 timeline(tmp_path, "2.xml", theirs))
        spread = max(d.delta for d in result.differences) - min(d.delta for d in result.differences)
        assert spread > 4.0, "a scattered error, not a shift of the whole device"

    def test_clips_missing_from_one_side_are_listed(self, tmp_path: Path) -> None:
        ours = [("a.mov", "camA", 0.0), ("extra.mov", "camA", 5.0)]
        theirs = [("a.mov", "camA", 0.0)]
        result = compare.compare(timeline(tmp_path, "1.xml", ours),
                                 timeline(tmp_path, "2.xml", theirs))
        assert result.only_ours == ["extra.mov"]
        assert result.only_theirs == []

    def test_nothing_in_common_does_not_raise(self, tmp_path: Path) -> None:
        result = compare.compare(timeline(tmp_path, "1.xml", [("a.mov", "x", 0.0)]),
                                 timeline(tmp_path, "2.xml", [("z.mov", "y", 0.0)]))
        assert result.differences == []


class TestJudgingTheMatcher:
    """The part that improves the algorithm rather than merely scoring it."""

    def truth(self, tmp_path: Path):
        return compare.placements(timeline(tmp_path, "t.xml", [
            ("a.mov", "camA", 0.0), ("b.mov", "camB", 10.0)]))

    def test_a_rejected_match_that_was_right_is_flagged(self, tmp_path: Path) -> None:
        judged = compare.judge_attempts([{
            "a_file": "a.mov", "b_file": "b.mov", "confidence": 0.19,
            "peak_ratio": 17.0, "overlap_seconds": 900.0,
            "delta_seconds": 10.0, "accepted": False,
        }], self.truth(tmp_path))
        assert judged[0]["correct"] is True and judged[0]["accepted"] is False

    def test_an_accepted_match_that_was_wrong_is_flagged(self, tmp_path: Path) -> None:
        judged = compare.judge_attempts([{
            "a_file": "a.mov", "b_file": "b.mov", "confidence": 0.8,
            "peak_ratio": 1.1, "overlap_seconds": 30.0,
            "delta_seconds": 48.0, "accepted": True,
        }], self.truth(tmp_path))
        assert judged[0]["correct"] is False and judged[0]["accepted"] is True
        assert judged[0]["error_seconds"] == pytest.approx(38.0)

    def test_pairs_the_good_timeline_knows_nothing_about_are_skipped(
            self, tmp_path: Path) -> None:
        judged = compare.judge_attempts([{
            "a_file": "a.mov", "b_file": "unknown.mov", "delta_seconds": 1.0,
            "accepted": True,
        }], self.truth(tmp_path))
        assert judged == []


class TestTheBundleRevealsNothing:
    """A wedding timeline names the client in every path."""

    def bundle(self, tmp_path: Path, **kwargs) -> dict:
        clips = [("a.mov", "camA", 0.0), ("b.mov", "camB", 10.0)]
        return compare.build_bundle(timeline(tmp_path, "1.xml", clips),
                                    timeline(tmp_path, "2.xml", clips), **kwargs)

    def test_no_path_no_filename_no_folder(self, tmp_path: Path) -> None:
        text = json.dumps(self.bundle(tmp_path))
        for secret in ("Wedding", "Smith", "Clients", "a.mov", "b.mov", "camA", "camB"):
            assert secret not in text, f"{secret} reached the bundle"

    def test_the_same_clip_gets_the_same_id_throughout(self, tmp_path: Path) -> None:
        bundle = self.bundle(tmp_path)
        ids = [row["clip"] for row in bundle["clips"]]
        assert len(ids) == len(set(ids)) == 2

    def test_a_tester_can_choose_to_send_real_names(self, tmp_path: Path) -> None:
        text = json.dumps(self.bundle(tmp_path, anonymous=False))
        assert "a.mov" in text

    def test_hashing_is_salted_per_project(self, tmp_path: Path) -> None:
        # So one tester's footage cannot be recognised in another's bundle,
        # and a guessed filename cannot be confirmed.
        assert compare.anonymise("a.mov", "salt-one") != compare.anonymise("a.mov", "salt-two")

    def test_the_summary_counts_what_matters(self, tmp_path: Path) -> None:
        summary = self.bundle(tmp_path)["summary"]
        assert summary["clips_compared"] == 2
        assert summary["agreed_within_a_frame"] == 2
        assert summary["wrong_by_over_a_second"] == 0


class TestKeepingTheBundleSendable:
    """A day's wedding is 553 clips, which is 152,628 pairs and 34 MB of JSON.

    Nobody can email that, and almost all of it is the dull case: pairs
    correctly rejected. The rows that teach us something are rare and must
    always survive.
    """

    def judged(self, interesting: int, dull: int) -> list[dict]:
        rows = []
        for i in range(interesting):
            rows.append({"a": f"i{i}", "b": "x", "confidence": 0.2, "peak_ratio": 9.0,
                         "overlap_seconds": 600.0, "accepted": False,
                         "error_seconds": 0.01, "correct": True})
        for i in range(dull):
            rows.append({"a": f"d{i}", "b": "x", "confidence": 0.1, "peak_ratio": 1.0,
                         "overlap_seconds": 40.0, "accepted": False,
                         "error_seconds": 30.0, "correct": False})
        return rows

    def test_every_informative_row_is_kept(self) -> None:
        kept = compare.choose_pair_rows(self.judged(40, 150_000), limit=3000)
        assert sum(1 for r in kept if r["accepted"] != r["correct"]) == 40

    def test_the_dull_bulk_is_sampled_away(self) -> None:
        kept = compare.choose_pair_rows(self.judged(40, 150_000), limit=3000)
        assert len(kept) <= 3000
        assert len(kept) > 100, "a sample, not nothing"

    def test_a_small_project_keeps_everything(self) -> None:
        rows = self.judged(2, 100)
        assert len(compare.choose_pair_rows(rows, limit=3000)) == len(rows)

    def test_informative_rows_win_even_past_the_limit(self) -> None:
        # If there are that many, we want all of them: something is badly
        # wrong and this is the evidence.
        kept = compare.choose_pair_rows(self.judged(5000, 100_000), limit=3000)
        assert sum(1 for r in kept if r["accepted"] != r["correct"]) == 5000

    def test_the_distribution_survives_the_sampling(self) -> None:
        """Thresholds are chosen from the shape, which must stay true."""
        tables = compare.summarise_pairs(self.judged(40, 1000))
        assert set(tables) == {"confidence", "peak_ratio", "overlap_seconds"}
        counted = sum(cell["right"] + cell["wrong"]
                      for cell in tables["confidence"].values())
        assert counted == 1040, "every pair counted, kept or not"

    def test_a_band_records_right_against_wrong(self) -> None:
        tables = compare.summarise_pairs(self.judged(40, 1000))
        band = tables["peak_ratio"]["6-10"]
        assert band["right"] == 40 and band["wrong"] == 0


class TestTheBundleCanActuallyBeWritten:
    """The matcher hands us numpy scalars, and json refuses some of them.

    This was shipped broken: `correct` came out of a numpy comparison as
    np.bool_, which is not a bool, so json raised and the tester's report was
    never written -- while np.float64 beside it passed for a float and hid how
    close to the surface the problem was. Every earlier test built its bundle
    without a report, which is the one argument that carries these values.
    """

    def judged(self, tmp_path: Path):
        np = pytest.importorskip("numpy")
        theirs = compare.placements(timeline(tmp_path, "t.xml", [
            ("a.mov", "camA", 0.0), ("b.mov", "camB", 10.0)]))
        return np, compare.judge_attempts([{
            "a_file": "a.mov", "b_file": "b.mov",
            "confidence": np.float32(0.19), "peak_ratio": np.float64(17.0),
            "overlap_seconds": np.float32(900.0),
            "delta_seconds": np.float64(10.0), "accepted": np.bool_(False),
        }], theirs)

    def test_numpy_scalars_do_not_survive_into_the_report(self, tmp_path: Path) -> None:
        np, judged = self.judged(tmp_path)
        for key, value in judged[0].items():
            assert type(value) in (str, float, bool, int, type(None)), (
                f"{key} is {type(value).__module__}.{type(value).__name__}, "
                f"which json may refuse")

    def test_a_judged_pair_serialises_with_no_help(self, tmp_path: Path) -> None:
        # Plain dumps, no default= hook: the values themselves must be clean.
        _, judged = self.judged(tmp_path)
        assert json.loads(json.dumps(judged)) == judged

    def test_the_flags_stay_real_booleans(self, tmp_path: Path) -> None:
        # np.bool_(False) is falsy but `is False` is untrue of it, and code
        # downstream counts with `if row["correct"]`.
        _, judged = self.judged(tmp_path)
        assert judged[0]["correct"] is True
        assert judged[0]["accepted"] is False

    def test_to_json_rescues_a_scalar_we_failed_to_convert(self) -> None:
        np = pytest.importorskip("numpy")
        text = compare.to_json({"odd": np.float32(1.5), "flag": np.bool_(True)})
        assert json.loads(text) == {"odd": 1.5, "flag": True}

    def test_to_json_still_refuses_what_it_cannot_convert(self) -> None:
        with pytest.raises(TypeError):
            compare.to_json({"path": object()})
