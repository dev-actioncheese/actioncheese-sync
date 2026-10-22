# SPDX-License-Identifier: GPL-3.0-or-later
"""Frame-rate arithmetic, including the NTSC and drop-frame cases that make
xmeml timing subtly wrong when handled naively."""

from __future__ import annotations

import pytest

from syncmyvids.timebase import Rate, format_timecode, parse_timecode


class TestRate:
    @pytest.mark.parametrize(
        ("timebase", "ntsc", "expected"),
        [
            (24, False, 24.0),
            (24, True, 23.976023976),
            (25, False, 25.0),
            (30, True, 29.970029970),
            (30, False, 30.0),
            (60, True, 59.940059940),
        ],
    )
    def test_fps(self, timebase: int, ntsc: bool, expected: float) -> None:
        assert Rate(timebase, ntsc).fps_float == pytest.approx(expected, abs=1e-6)

    def test_ntsc_is_exactly_1000_over_1001(self) -> None:
        # Float arithmetic would drift over a feature-length timeline.
        assert Rate(30, True).fps.numerator == 30000
        assert Rate(30, True).fps.denominator == 1001

    def test_round_trip_seconds(self) -> None:
        rate = Rate(30, True)
        for frames in (0, 1, 1000, 107657):
            assert rate.seconds_to_frames(rate.frames_to_seconds(frames)) == frames

    def test_rounding_is_half_away_from_zero(self) -> None:
        # Banker's rounding would send .5 to the nearest *even* frame, making an
        # offset's direction depend on parity.
        rate = Rate(25, False)
        assert rate.seconds_to_frames(0.5 / 25) == 1
        assert rate.seconds_to_frames(-0.5 / 25) == -1
        assert rate.seconds_to_frames(1.5 / 25) == 2

    def test_drop_frame_only_where_defined(self) -> None:
        assert Rate(30, True).supports_drop_frame
        assert Rate(60, True).supports_drop_frame
        assert not Rate(24, True).supports_drop_frame
        assert not Rate(25, False).supports_drop_frame


class TestTimecode:
    def test_non_drop(self) -> None:
        rate = Rate(25, False)
        assert format_timecode(0, rate) == "00:00:00:00"
        assert format_timecode(25, rate) == "00:00:01:00"
        assert format_timecode(1500, rate) == "00:01:00:00"

    def test_drop_frame_uses_semicolon(self) -> None:
        assert ";" in format_timecode(1800, Rate(30, True))

    def test_drop_frame_known_value(self) -> None:
        # One hour of drop-frame timecode is 108000 nominal labels minus the
        # 108 that get skipped, so 01:00:00;00 is frame 107892.
        rate = Rate(30, True)
        assert format_timecode(107892, rate) == "01:00:00;00"
        assert parse_timecode("01:00:00;00", rate) == 107892
        assert parse_timecode("01:00:02;05", rate) == 107892 + 2 * 30 + 5

    def test_drop_frame_tracks_wall_clock(self) -> None:
        # The whole point of drop-frame: an hour of timecode is an hour of real
        # time, to within one frame.
        rate = Rate(30, True)
        assert rate.frames_to_seconds(107892) == pytest.approx(3600.0, abs=0.04)

    def test_drop_frame_skips_the_dropped_labels(self) -> None:
        rate = Rate(30, True)
        # Frame 1798 is the last of minute 0; labels :00 and :01 of minute 1
        # are skipped, so the next label is 00:01:00;02.
        assert format_timecode(1798, rate) == "00:00:59;28"
        assert format_timecode(1800, rate) == "00:01:00;02"
        # ...but the tenth minute keeps its frames.
        assert format_timecode(17982, rate) == "00:10:00;00"

    @pytest.mark.parametrize("frames", [0, 1, 999, 1800, 17982, 107657, 300000])
    def test_round_trip_drop_frame(self, frames: int) -> None:
        rate = Rate(30, True)
        assert parse_timecode(format_timecode(frames, rate), rate) == frames

    @pytest.mark.parametrize("frames", [0, 1, 999, 25000, 300000])
    def test_round_trip_non_drop(self, frames: int) -> None:
        rate = Rate(25, False)
        assert parse_timecode(format_timecode(frames, rate), rate) == frames

    def test_negative(self) -> None:
        rate = Rate(25, False)
        assert format_timecode(-25, rate) == "-00:00:01:00"
        assert parse_timecode("-00:00:01:00", rate) == -25

    def test_rejects_drop_frame_at_undefined_rate(self) -> None:
        with pytest.raises(ValueError, match="drop-frame"):
            format_timecode(100, Rate(25, False), drop_frame=True)
        with pytest.raises(ValueError, match="drop-frame"):
            parse_timecode("00:00:01;00", Rate(25, False))

    def test_rejects_malformed(self) -> None:
        with pytest.raises(ValueError, match="malformed"):
            parse_timecode("01:00", Rate(25, False))
