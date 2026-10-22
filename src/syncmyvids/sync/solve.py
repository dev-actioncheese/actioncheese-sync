# SPDX-License-Identifier: GPL-3.0-or-later
"""Global offset solve.

Pairwise matches disagree slightly -- three cameras give three offsets that do
not quite compose to zero around the loop.  Rather than trusting a chain of
pairwise answers (where one bad link drags everything downstream with it), we
solve all constraints at once by weighted least squares, so every edge gets a
vote proportional to how much it should be trusted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np

from .graph import Edge

#: Residuals beyond this are reported as conflicts for the operator to inspect.
CONFLICT_THRESHOLD_SECONDS = 0.05


class _UnionFind:
    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        self._parent[rb] = ra
        return True


@dataclass(slots=True)
class Component:
    """A set of units that are mutually synced."""

    nodes: list[int]
    positions: dict[int, float] = field(default_factory=dict)
    """Timeline start per node, in seconds, relative to the component's anchor."""

    @property
    def size(self) -> int:
        return len(self.nodes)


@dataclass(slots=True)
class Solution:
    components: list[Component]
    conflicts: list[dict]
    singletons: list[int]


def _spanning_tree_positions(nodes: list[int], edges: list[Edge]) -> dict[int, float]:
    """Seed positions from a maximum-confidence spanning tree.

    Starting least squares from a good guess matters less for accuracy than for
    interpretability: the tree gives a position even for a component whose
    least-squares system is degenerate.
    """
    index = {node: i for i, node in enumerate(nodes)}
    union_find = _UnionFind(len(nodes))
    tree: list[Edge] = []
    for edge in sorted(edges, key=lambda e: e.weight, reverse=True):
        if union_find.union(index[edge.a], index[edge.b]):
            tree.append(edge)

    adjacency: dict[int, list[tuple[int, float]]] = {node: [] for node in nodes}
    for edge in tree:
        adjacency[edge.a].append((edge.b, edge.delta_seconds))
        adjacency[edge.b].append((edge.a, -edge.delta_seconds))

    positions: dict[int, float] = {nodes[0]: 0.0}
    queue = [nodes[0]]
    while queue:
        current = queue.pop()
        for neighbour, delta in adjacency[current]:
            if neighbour not in positions:
                positions[neighbour] = positions[current] + delta
                queue.append(neighbour)

    # Any node the tree failed to reach (should not happen within a component)
    # falls back to the anchor rather than vanishing.
    for node in nodes:
        positions.setdefault(node, 0.0)
    return positions


def _least_squares(
    nodes: list[int],
    edges: list[Edge],
    seed: dict[int, float],
    *,
    irls_passes: int = 1,
) -> tuple[dict[int, float], list[Edge]]:
    """Reconcile all edges at once; returns positions and the outlier edges."""
    if len(nodes) < 2 or not edges:
        return seed, []

    index = {node: i for i, node in enumerate(nodes)}
    n = len(nodes)

    weights = np.array([max(edge.weight, 1e-6) for edge in edges], dtype=np.float64)
    targets = np.array([edge.delta_seconds for edge in edges], dtype=np.float64)
    # Stated relationships are constraints, not measurements: the robustifying
    # pass below must not decide one of them is an outlier because the audio
    # disagrees. When they disagree, the audio is what is wrong.
    is_rigid = np.array([edge.rigid for edge in edges], dtype=bool)

    outliers: list[Edge] = []
    positions = seed

    for _ in range(irls_passes + 1):
        matrix = np.zeros((len(edges) + 1, n), dtype=np.float64)
        rhs = np.zeros(len(edges) + 1, dtype=np.float64)
        for row, edge in enumerate(edges):
            scale = np.sqrt(weights[row])
            matrix[row, index[edge.a]] = -scale
            matrix[row, index[edge.b]] = scale
            rhs[row] = targets[row] * scale

        # Gauge constraint: pin the first node, otherwise the system is only
        # determined up to a global translation.
        gauge = float(np.sqrt(weights.sum()) + 1.0)
        matrix[-1, 0] = gauge
        rhs[-1] = 0.0

        solution, *_ = np.linalg.lstsq(matrix, rhs, rcond=None)
        positions = {node: float(solution[index[node]]) for node in nodes}

        residuals = np.array(
            [positions[e.b] - positions[e.a] - e.delta_seconds for e in edges],
            dtype=np.float64,
        )
        # A stated relationship that the audio moved is not a conflict to
        # report -- it is the system working. Only measured matches that
        # disagree with the consensus are worth an operator's attention.
        outliers = [
            e for e, r in zip(edges, residuals, strict=True)
            if abs(r) > CONFLICT_THRESHOLD_SECONDS and not e.stated
        ]

        # Robustify: down-weight edges that disagree with the consensus, using a
        # median-absolute-deviation scale so a few bad matches cannot define it.
        absolute = np.abs(residuals)
        median = float(np.median(absolute)) if absolute.size else 0.0
        scale = max(median * 1.4826, 0.005)
        reweighted = weights / (1.0 + (residuals / (3.0 * scale)) ** 2)
        weights = np.where(is_rigid, weights, reweighted)

    return positions, outliers


def snap_rigid(solution: Solution, groups: Iterable[dict[int, float]]) -> int:
    """Restore each rigid group's stated geometry exactly.

    A huge least-squares weight gets a group to within microseconds, which is
    five orders of magnitude finer than a frame and would do. But "we were told
    where these sit relative to each other" deserves to come out the other end
    exactly true rather than nearly true, so the group is placed as one body:
    its members keep the offsets they were given, and only the body's position
    is taken from the solve.

    Returns the number of groups snapped.
    """
    snapped = 0
    for group in groups:
        for component in solution.components:
            members = [node for node in group if node in component.positions]
            if len(members) < 2:
                continue
            # Where the solve thinks the body sits, averaged over its members
            # so no single one of them dictates it.
            shift = sum(component.positions[node] - group[node] for node in members) / len(members)
            for node in members:
                component.positions[node] = shift + group[node]
            snapped += 1
    return snapped


def solve(unit_count: int, edges: Iterable[Edge]) -> Solution:
    """Group units into components and place each component's members."""
    edges = list(edges)
    union_find = _UnionFind(unit_count)
    for edge in edges:
        union_find.union(edge.a, edge.b)

    grouped: dict[int, list[int]] = {}
    for node in range(unit_count):
        grouped.setdefault(union_find.find(node), []).append(node)

    edges_by_root: dict[int, list[Edge]] = {}
    for edge in edges:
        edges_by_root.setdefault(union_find.find(edge.a), []).append(edge)

    components: list[Component] = []
    singletons: list[int] = []
    conflicts: list[dict] = []

    for root, nodes in grouped.items():
        if len(nodes) == 1:
            singletons.append(nodes[0])
            continue
        nodes.sort()
        member_edges = edges_by_root.get(root, [])
        seed = _spanning_tree_positions(nodes, member_edges)
        positions, outliers = _least_squares(nodes, member_edges, seed)
        components.append(Component(nodes=nodes, positions=positions))
        for edge in outliers:
            conflicts.append({
                "a": edge.a,
                "b": edge.b,
                "expected_delta": round(edge.delta_seconds, 4),
                "solved_delta": round(positions[edge.b] - positions[edge.a], 4),
                "confidence": round(edge.confidence, 4),
            })

    # Largest component first: that is the main synced block.
    components.sort(key=lambda c: (-c.size, min(c.nodes)))
    return Solution(components=components, conflicts=conflicts, singletons=sorted(singletons))
