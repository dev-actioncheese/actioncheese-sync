# SPDX-License-Identifier: GPL-3.0-or-later
"""Second-chance pass for matches that fall just below the confidence floor.

Correlation confidence alone cannot separate a correct match from a spurious
one.  Measured on a real project against a hand-verified timeline, two *correct*
matches scored 0.364 and 0.383 while two *wrong* ones scored 0.403 and 0.446 --
the true matches ranked below the false ones, so no threshold can divide them.

What does divide them is physics: a camera cannot be in two places at once.  A
clip whose tentative position would overlap another clip from the same device is
impossible regardless of how well its audio correlates.  That test rejects the
false matches outright and admits the true ones, taking recall from 10/13 to
12/13 on the project it was calibrated against, with no false positives.

The pass is deliberately allowed **one attempt per clip**.  If a clip's strongest
candidate turns out to be impossible, its weaker candidates are not tried: the
best evidence having failed a physical check is reason to distrust the rest, and
a clip left unmatched merely lands on an unsynced track, where it is easy to fix
by hand.  (On the calibration project this is exactly what saved a clip whose
0.446 match was rejected and whose 0.353 fallback would have been wrong.)
"""

from __future__ import annotations

import logging
from typing import Optional

from .graph import Edge, UnitFeature
from .solve import Solution

log = logging.getLogger(__name__)

#: Candidates below the main floor but above this are eligible for a retry.
DEFAULT_FLOOR = 0.30


def _overlaps(start_a: float, duration_a: float, start_b: float, duration_b: float) -> bool:
    return start_a < start_b + duration_b and start_b < start_a + duration_a


def corroborate(
    features: list[UnitFeature],
    solution: Solution,
    edges: list[Edge],
    *,
    min_confidence: float,
    floor: float = DEFAULT_FLOOR,
) -> tuple[list[Edge], list[dict]]:
    """Admit weak edges that survive a same-device collision check.

    ``edges`` must include the weak candidates (matched down to ``floor``).
    Returns the newly accepted edges and a record of every decision.
    """
    placed: dict[int, tuple[int, float]] = {}
    for index, component in enumerate(solution.components):
        for node in component.nodes:
            placed[node] = (index, component.positions[node])
    if not placed:
        return [], []

    # Strongest eligible candidate per unplaced unit -- one attempt each.
    best: dict[int, tuple[float, int, float, Edge]] = {}
    for edge in edges:
        if edge.confidence >= min_confidence or edge.confidence < floor:
            continue
        for new, anchor, delta in (
            (edge.a, edge.b, -edge.delta_seconds),
            (edge.b, edge.a, edge.delta_seconds),
        ):
            if new in placed or anchor not in placed:
                continue
            current = best.get(new)
            if current is None or edge.confidence > current[0]:
                best[new] = (edge.confidence, anchor, delta, edge)

    accepted: list[Edge] = []
    decisions: list[dict] = []

    for new, (confidence, anchor, delta, edge) in sorted(
        best.items(), key=lambda item: -item[1][0]
    ):
        component_index, anchor_position = placed[anchor]
        position = anchor_position + delta
        duration = features[new].duration or 0.0
        tracks = features[new].unit.source_tracks

        clash: Optional[str] = None
        for node, (node_component, node_position) in placed.items():
            if node_component != component_index:
                continue
            if not (features[node].unit.source_tracks & tracks):
                continue
            if _overlaps(position, duration, node_position, features[node].duration or 0.0):
                clash = features[node].unit.label
                break

        decisions.append({
            "clip": features[new].unit.label,
            "via": features[anchor].unit.label,
            "confidence": round(confidence, 4),
            "accepted": clash is None,
            "rejected_because": None if clash is None else f"would overlap {clash} on the same device",
        })

        if clash is not None:
            log.debug("corroboration rejected %s: overlaps %s", features[new].unit.label, clash)
            continue

        accepted.append(edge)
        placed[new] = (component_index, position)

    return accepted, decisions
