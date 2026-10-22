# SPDX-License-Identifier: GPL-3.0-or-later
"""A device cannot record two things at once."""

from __future__ import annotations

from syncmyvids.model import Clip, SyncUnit
from syncmyvids.sync.consistency import enforce_device_order
from syncmyvids.sync.graph import Edge, UnitFeature
from syncmyvids.sync.solve import Component, Solution
from syncmyvids.timebase import Rate

RATE = Rate(25, False)


def _feature(index: int, name: str, track: int, duration: float) -> UnitFeature:
    clip = Clip(clip_id=name, name=name, media_type="video", track_index=track,
                start=0, end=int(duration * 25), in_point=0,
                out_point=int(duration * 25), rate=RATE, file=None)
    return UnitFeature(index, SyncUnit(key=name, clips=[clip]), None, 0.0, duration)


def _edge(a: int, b: int, weight_conf: float) -> Edge:
    return Edge(a=a, b=b, delta_seconds=0.0, confidence=weight_conf,
                peak_ratio=1.0, overlap_seconds=100.0)


class TestDeviceOrder:
    def test_overlapping_same_device_clips_are_demoted(self) -> None:
        """Two takes from one camera cannot occupy the same moment."""
        features = [_feature(0, "a", 1, 100.0), _feature(1, "b", 1, 100.0)]
        solution = Solution(
            components=[Component(nodes=[0, 1], positions={0: 0.0, 1: 50.0})],
            conflicts=[], singletons=[],
        )
        edges = [_edge(0, 1, 0.9)]
        result, reports = enforce_device_order(
            features, solution, edges, {0: 0, 1: 1})
        assert len(reports) == 1
        assert reports[0]["clip"] == "V1:b"
        # Demoting one of a pair leaves a single node, which is no longer a
        # component either, so both fall out.
        assert 1 in result.singletons
        assert result.components == []

    def test_inverted_order_is_demoted(self) -> None:
        """Shot first must appear first."""
        features = [_feature(0, "a", 1, 10.0), _feature(1, "b", 1, 10.0)]
        solution = Solution(
            components=[Component(nodes=[0, 1], positions={0: 500.0, 1: 0.0})],
            conflicts=[], singletons=[],
        )
        result, reports = enforce_device_order(
            features, solution, [_edge(0, 1, 0.9)], {0: 0, 1: 1})
        assert reports and reports[0]["clip"] == "V1:b"
        assert 1 in result.singletons

    def test_unmatched_footage_between_takes_still_needs_room(self) -> None:
        """The clips in between have to fit, even though they matched nothing.

        Two synced takes 60 s apart cannot be right when 600 s of that camera's
        own footage belongs between them.
        """
        features = [
            _feature(0, "first", 1, 30.0),
            _feature(1, "orphan", 1, 600.0),   # unmatched, sits between them
            _feature(2, "second", 1, 30.0),
        ]
        solution = Solution(
            components=[Component(nodes=[0, 2], positions={0: 0.0, 2: 60.0})],
            conflicts=[], singletons=[1],
        )
        result, reports = enforce_device_order(
            features, solution, [_edge(0, 2, 0.9)], {0: 0, 1: 1, 2: 2})
        assert reports, "cramped spacing was not detected"
        assert 2 in result.singletons

    def test_consistent_layout_is_left_alone(self) -> None:
        features = [_feature(0, "a", 1, 30.0), _feature(1, "b", 1, 30.0)]
        solution = Solution(
            components=[Component(nodes=[0, 1], positions={0: 0.0, 1: 100.0})],
            conflicts=[], singletons=[],
        )
        result, reports = enforce_device_order(
            features, solution, [_edge(0, 1, 0.9)], {0: 0, 1: 1})
        assert reports == []
        assert result.components[0].nodes == [0, 1]

    def test_different_devices_are_never_compared(self) -> None:
        """Two cameras may of course record the same moment."""
        features = [_feature(0, "a", 1, 100.0), _feature(1, "b", 2, 100.0)]
        solution = Solution(
            components=[Component(nodes=[0, 1], positions={0: 0.0, 1: 0.0})],
            conflicts=[], singletons=[],
        )
        result, reports = enforce_device_order(
            features, solution, [_edge(0, 1, 0.9)], {0: 0, 1: 1})
        assert reports == []
        assert len(result.components[0].nodes) == 2

    def test_the_better_evidenced_placement_survives(self) -> None:
        features = [_feature(0, "weak", 1, 100.0), _feature(1, "strong", 1, 100.0)]
        solution = Solution(
            components=[Component(nodes=[0, 1], positions={0: 0.0, 1: 50.0})],
            conflicts=[], singletons=[],
        )
        # unit 1 is held up by a much stronger match, so unit 0 should give way
        edges = [_edge(0, 1, 0.2), _edge(1, 1, 0.95)]
        _, reports = enforce_device_order(features, solution, edges, {0: 0, 1: 1})
        assert [r["clip"] for r in reports] == ["V1:weak"]


class TestDeviceClock:
    """Recording timestamps are a far sharper test than ordering alone.

    A wrong match can be in the right order, overlap nothing, and still sit
    minutes from where the camera says the clip was shot.  Within a component
    the unknown device-to-timeline offset cancels in the difference between two
    clips, so no offset has to be estimated.
    """

    def _solution(self, first: float, second: float) -> Solution:
        return Solution(
            components=[Component(nodes=[0, 1], positions={0: first, 1: second})],
            conflicts=[], singletons=[],
        )

    def test_spacing_must_match_what_the_clock_reports(self) -> None:
        features = [_feature(0, "a", 1, 30.0), _feature(1, "b", 1, 30.0)]
        # Shot 600 s apart, but the sync placed them 100 s apart.
        stamps = {0: 1000.0, 1: 1600.0}
        _, reports = enforce_device_order(
            features, self._solution(0.0, 100.0),
            [_edge(0, 1, 0.9)], {0: 0, 1: 1}, stamps=stamps)
        assert reports, "clock disagreement was not detected"
        assert reports[0]["reason"] == "position disagrees with this device's clock"

    def test_agreeing_spacing_is_accepted(self) -> None:
        features = [_feature(0, "a", 1, 30.0), _feature(1, "b", 1, 30.0)]
        stamps = {0: 1000.0, 1: 1600.0}
        result, reports = enforce_device_order(
            features, self._solution(0.0, 600.0),
            [_edge(0, 1, 0.9)], {0: 0, 1: 1}, stamps=stamps)
        assert reports == []
        assert result.components[0].nodes == [0, 1]

    def test_second_granular_timestamps_are_not_treated_as_error(self) -> None:
        """Camera clocks tick in whole seconds, and drift a little over hours."""
        features = [_feature(0, "a", 1, 30.0), _feature(1, "b", 1, 30.0)]
        stamps = {0: 1000.0, 1: 1600.0}
        _, reports = enforce_device_order(
            features, self._solution(0.0, 602.0),
            [_edge(0, 1, 0.9)], {0: 0, 1: 1}, stamps=stamps)
        assert reports == []

    def test_clips_without_timestamps_fall_back_to_ordering(self) -> None:
        features = [_feature(0, "a", 1, 30.0), _feature(1, "b", 1, 30.0)]
        result, reports = enforce_device_order(
            features, self._solution(0.0, 100.0),
            [_edge(0, 1, 0.9)], {0: 0, 1: 1}, stamps={})
        # 100 s apart with 30 s clips is fine on ordering grounds alone.
        assert reports == []
        assert result.components[0].nodes == [0, 1]

    def test_different_devices_keep_their_own_clocks(self) -> None:
        """Two cameras' clocks are never set alike; only same-device pairs compare."""
        features = [_feature(0, "a", 1, 30.0), _feature(1, "b", 2, 30.0)]
        stamps = {0: 1000.0, 1: 99999.0}      # wildly different clocks
        _, reports = enforce_device_order(
            features, self._solution(0.0, 10.0),
            [_edge(0, 1, 0.9)], {0: 0, 1: 1}, stamps=stamps)
        assert reports == []
