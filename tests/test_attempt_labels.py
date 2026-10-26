# SPDX-License-Identifier: GPL-3.0-or-later
"""What the report says became of each pair we scored.

The matching pass runs at a lowered confidence floor so the corroboration
pass has candidates. Its own verdict is therefore provisional, and a report
that presents it as the decision makes the matcher look like it accepted
matches it actually discarded -- the opposite of what the tester's report is
for. These pin the distinction.
"""

from __future__ import annotations

from syncmyvids.engine import _label_attempts
from syncmyvids.sync.graph import Edge


def attempt(a: int, b: int, *, passed: bool) -> dict:
    return {"a_index": a, "b_index": b, "passed_floor": passed,
            "accepted": passed}


def edge(a: int, b: int) -> Edge:
    return Edge(a=a, b=b, delta_seconds=1.0, confidence=0.8,
                peak_ratio=1.5, overlap_seconds=100.0)


class TestLabellingWhatHappened:
    def test_a_match_that_reached_the_solver_is_accepted(self) -> None:
        rows = [attempt(0, 1, passed=True)]
        _label_attempts(rows, [edge(0, 1)], [])
        assert rows[0]["stage"] == "solved" and rows[0]["accepted"] is True

    def test_a_match_admitted_by_corroboration_is_accepted(self) -> None:
        rows = [attempt(0, 1, passed=True)]
        _label_attempts(rows, [], [edge(0, 1)])
        assert rows[0]["stage"] == "corroborated"
        assert rows[0]["accepted"] is True

    def test_passing_the_lowered_floor_alone_is_not_acceptance(self) -> None:
        # The bug this guards: such a pair used to be reported as accepted,
        # so a comparison blamed the matcher for a match it never used.
        rows = [attempt(0, 1, passed=True)]
        _label_attempts(rows, [], [])
        assert rows[0]["stage"] == "weak" and rows[0]["accepted"] is False

    def test_a_pair_never_in_contention_says_so(self) -> None:
        rows = [attempt(0, 1, passed=False)]
        _label_attempts(rows, [], [])
        assert rows[0]["stage"] == "below-floor"
        assert rows[0]["accepted"] is False

    def test_stated_relationships_are_not_credited_to_the_matcher(self) -> None:
        # A "prior" edge comes from the ActionCheese app's own clock, not from
        # audio. Counting it as a match the matcher made would flatter it.
        told = Edge(a=0, b=1, delta_seconds=1.0, confidence=1.0,
                    peak_ratio=1.0, overlap_seconds=0.0, kind="prior")
        rows = [attempt(0, 1, passed=False)]
        _label_attempts(rows, [told], [])
        assert rows[0]["accepted"] is False

    def test_every_pair_gets_a_verdict(self) -> None:
        rows = [attempt(0, 1, passed=True), attempt(1, 2, passed=False),
                attempt(0, 2, passed=True)]
        _label_attempts(rows, [edge(0, 1)], [edge(0, 2)])
        assert [r["stage"] for r in rows] == ["solved", "below-floor",
                                              "corroborated"]
        assert [r["accepted"] for r in rows] == [True, False, True]
