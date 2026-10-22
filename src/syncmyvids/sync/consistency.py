# SPDX-License-Identifier: GPL-3.0-or-later
"""Reject solved positions that contradict a device's own shooting order.

One camera cannot record two things at once, so its clips must appear on the
timeline in the order they were shot, spaced by at least their own durations.
The audio solver knows nothing about this: it places each clip from correlation
alone, and a single wrong match can drop a clip into a slot its own device was
already occupying.

Measured on a real four-camera project, this catches matches that place a take
267 seconds after its predecessor when 1193 seconds of that camera's own footage
belongs in between -- impossible, and the cause of both same-device overlaps and
unmatched clips being laid straight through a synced take.

The check is a pure filter: it never moves anything, it only demotes the
matches it cannot believe, which sends those clips to the unsynced tracks where
they are visible and easy to place by hand.
"""

from __future__ import annotations

import logging
from typing import Optional

from .graph import Edge, UnitFeature
from .solve import Component, Solution

log = logging.getLogger(__name__)

#: Slack allowed before two same-device clips count as overlapping.  Sync is
#: frame-accurate, so anything beyond a second is a real contradiction.
TOLERANCE_SECONDS = 1.0

#: Slack allowed between two clips' spacing on the timeline and the spacing
#: their own device's clock reports.  Camera timestamps are whole seconds, and a
#: clock can drift a little over a long shoot, so a few seconds is noise -- but
#: a genuinely wrong match is out by minutes.
CLOCK_TOLERANCE_SECONDS = 5.0


def _device(feature: UnitFeature) -> tuple[str, int]:
    clip = feature.unit.primary_clip
    return clip.media_type, clip.track_index


def enforce_device_order(
    features: list[UnitFeature],
    solution: Solution,
    edges: list[Edge],
    shot_order: dict[int, int],
    *,
    stamps: Optional[dict[int, float]] = None,
    tolerance: float = TOLERANCE_SECONDS,
    clock_tolerance: float = CLOCK_TOLERANCE_SECONDS,
) -> tuple[Solution, list[dict]]:
    """Demote solved units whose position contradicts their device's ordering.

    ``shot_order`` maps a unit index to its position in the original timeline,
    which is the order the operator laid the footage out in and therefore the
    order it was shot.

    ``stamps`` are recording timestamps.  Where two clips of one device are in
    the same component, their spacing on the timeline must match the spacing
    their own clock reports -- no offset estimation needed, because the unknown
    device-to-timeline offset cancels in the difference.  This is a far sharper
    test than ordering alone: it catches a match that is in the right order and
    does not overlap anything, yet sits minutes from where the camera says it
    was shot.
    """
    stamps = stamps or {}
    # Evidence behind each unit, used to decide which side of a contradiction
    # to keep: a placement backed by a long, confident match outranks one held
    # up by a single weak edge.
    evidence: dict[int, float] = {}
    for edge in edges:
        for node in (edge.a, edge.b):
            evidence[node] = evidence.get(node, 0.0) + edge.weight

    rejected: set[int] = set()
    reports: list[dict] = []

    for component in solution.components:
        by_device: dict[tuple[str, int], list[int]] = {}
        for node in component.nodes:
            by_device.setdefault(_device(features[node]), []).append(node)

        for device, nodes in by_device.items():
            if len(nodes) < 2:
                continue
            # Strongest evidence first; a unit joins only if it agrees with
            # everything already accepted for this device.
            # Everything this device shot, matched or not: the clips between
            # two synced takes still have to fit between them.
            siblings = [
                (shot_order[i], features[i].duration or 0.0)
                for i, f in enumerate(features)
                if _device(f) == device
            ]

            def room_needed(first: int, second: int,
                            _siblings: list[tuple[int, float]] = siblings) -> float:
                """Footage this device shot from ``first`` up to ``second``."""
                lo, hi = sorted((shot_order[first], shot_order[second]))
                return sum(d for order, d in _siblings if lo <= order < hi)

            ranked = sorted(nodes, key=lambda n: -evidence.get(n, 0.0))
            kept: list[int] = []
            for node in ranked:
                start = component.positions[node]
                conflict: Optional[int] = None
                reason = "position contradicts this device's shooting order"
                for other in kept:
                    other_start = component.positions[other]
                    # Shot earlier must also start earlier.
                    inverted = (
                        (shot_order[node] < shot_order[other]) != (start < other_start)
                    )
                    # And far enough apart for everything shot in between --
                    # including the clips that matched nothing, which still
                    # have to go somewhere on this device's own track.
                    earlier, later = ((node, other) if shot_order[node] < shot_order[other]
                                      else (other, node))
                    gap = abs(start - other_start)
                    cramped = gap + tolerance < room_needed(earlier, later)

                    # The device's own clock, if both clips carry a timestamp.
                    drifted = False
                    if node in stamps and other in stamps:
                        expected = stamps[node] - stamps[other]
                        measured = start - other_start
                        drifted = abs(measured - expected) > clock_tolerance

                    if inverted or cramped or drifted:
                        conflict = other
                        reason = ("position disagrees with this device's clock"
                                  if drifted and not (inverted or cramped)
                                  else "position contradicts this device's shooting order")
                        break
                if conflict is None:
                    kept.append(node)
                    continue
                rejected.add(node)
                reports.append({
                    "clip": features[node].unit.label,
                    "device": f"{'V' if device[0] == 'video' else 'A'}{device[1]}",
                    "conflicts_with": features[conflict].unit.label,
                    "reason": reason,
                })

    if not rejected:
        return solution, reports

    log.info("device-order check demoted %d match(es)", len(rejected))
    components: list[Component] = []
    singletons = list(solution.singletons)
    for component in solution.components:
        remaining = [n for n in component.nodes if n not in rejected]
        singletons.extend(n for n in component.nodes if n in rejected)
        if len(remaining) >= 2:
            components.append(Component(
                nodes=remaining,
                positions={n: component.positions[n] for n in remaining},
            ))
        else:
            singletons.extend(remaining)

    components.sort(key=lambda c: (-c.size, min(c.nodes)))
    return (
        Solution(components=components, conflicts=solution.conflicts,
                 singletons=sorted(set(singletons))),
        reports,
    )
