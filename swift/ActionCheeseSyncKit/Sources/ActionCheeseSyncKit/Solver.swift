// SPDX-License-Identifier: MIT
import Foundation

/// Global offset solve.
///
/// Pairwise matches disagree slightly -- three cameras give three offsets that
/// do not quite compose to zero around the loop.  Rather than trusting a chain
/// of pairwise answers, where one bad link drags everything downstream, all
/// constraints are solved at once by weighted least squares so every edge gets
/// a vote proportional to how far it should be trusted.
///
/// Anchors are held fixed: ActionCheese already knows where its iPhone clips
/// belong from their shared time-of-day timecode, and the sound is only being
/// asked to place everything else around them.
public enum Solver {

    public struct Edge: Sendable {
        public let a: Int
        public let b: Int
        /// Required `start(b) - start(a)`.
        public let delta: Double
        public let confidence: Double
        public let overlapSeconds: Double

        public init(a: Int, b: Int, delta: Double, confidence: Double, overlapSeconds: Double) {
            self.a = a; self.b = b; self.delta = delta
            self.confidence = confidence; self.overlapSeconds = overlapSeconds
        }

        /// Confidence squared, scaled by overlap: a match backed by ten minutes
        /// of common material deserves more say than one backed by twenty
        /// seconds.
        var weight: Double { max(confidence, 0) * max(confidence, 0) * min(overlapSeconds, 300) }
    }

    public struct Solution: Sendable {
        /// Absolute start per node, for nodes reachable from an anchor.
        public let positions: [Int: Double]
        /// Nodes that matched something but never reached an anchor, so their
        /// absolute time is unknowable from sound alone.
        public let floating: [[Int]]
        public let unmatched: [Int]
    }

    /// Residual beyond which an edge is treated as an outlier.
    static let conflictThreshold = 0.05

    struct UnionFind {
        var parent: [Int]
        init(_ size: Int) { parent = Array(0..<size) }
        mutating func find(_ x: Int) -> Int {
            var root = x
            while parent[root] != root { parent[root] = parent[parent[root]]; root = parent[root] }
            return root
        }
        @discardableResult
        mutating func union(_ a: Int, _ b: Int) -> Bool {
            let ra = find(a), rb = find(b)
            if ra == rb { return false }
            parent[rb] = ra
            return true
        }
    }

    /// Solve for absolute positions, holding `anchors` fixed.
    public static func solve(
        nodeCount: Int,
        edges: [Edge],
        anchors: [Int: Double]
    ) -> Solution {
        var components = UnionFind(nodeCount)
        for edge in edges { components.union(edge.a, edge.b) }

        var grouped: [Int: [Int]] = [:]
        for node in 0..<nodeCount { grouped[components.find(node), default: []].append(node) }
        var edgesByRoot: [Int: [Edge]] = [:]
        for edge in edges { edgesByRoot[components.find(edge.a), default: []].append(edge) }

        var positions: [Int: Double] = [:]
        var floating: [[Int]] = []
        var unmatched: [Int] = []

        for (root, nodes) in grouped {
            let anchored = nodes.filter { anchors[$0] != nil }
            let member = edgesByRoot[root] ?? []

            if nodes.count == 1 {
                let only = nodes[0]
                if let fixed = anchors[only] { positions[only] = fixed } else { unmatched.append(only) }
                continue
            }

            let seeded = spanningTreeSeed(nodes: nodes.sorted(), edges: member)
            let solved = leastSquares(nodes: nodes.sorted(), edges: member,
                                      seed: seeded, anchors: anchors)
            if anchored.isEmpty {
                // Internally in sync, but nothing ties it to a known time.
                floating.append(nodes.sorted())
            } else {
                for node in nodes { positions[node] = solved[node] ?? 0 }
            }
        }
        return Solution(positions: positions, floating: floating, unmatched: unmatched.sorted())
    }

    /// Seed positions from a maximum-confidence spanning tree.
    static func spanningTreeSeed(nodes: [Int], edges: [Edge]) -> [Int: Double] {
        var index: [Int: Int] = [:]
        for (offset, node) in nodes.enumerated() { index[node] = offset }
        var forest = UnionFind(nodes.count)
        var tree: [Edge] = []
        for edge in edges.sorted(by: { $0.weight > $1.weight }) {
            if forest.union(index[edge.a]!, index[edge.b]!) { tree.append(edge) }
        }

        var adjacency: [Int: [(Int, Double)]] = [:]
        for edge in tree {
            adjacency[edge.a, default: []].append((edge.b, edge.delta))
            adjacency[edge.b, default: []].append((edge.a, -edge.delta))
        }

        var seed: [Int: Double] = [nodes[0]: 0]
        var queue = [nodes[0]]
        while let current = queue.popLast() {
            for (neighbour, delta) in adjacency[current] ?? [] where seed[neighbour] == nil {
                seed[neighbour] = seed[current]! + delta
                queue.append(neighbour)
            }
        }
        for node in nodes where seed[node] == nil { seed[node] = 0 }
        return seed
    }

    /// Reconcile every edge at once, with one reweighting pass to demote
    /// whichever of them disagree with the consensus.
    ///
    /// Anchors are *substituted out* rather than weighted heavily: ActionCheese
    /// already knows where its iPhone clips belong, and a clip that is known is
    /// not a thing to be fitted.  Pinning them with a large weight instead lets
    /// a confidently wrong match drag them by a few microseconds, which is
    /// harmless in samples and wrong in principle.
    static func leastSquares(
        nodes: [Int], edges: [Edge], seed: [Int: Double], anchors: [Int: Double]
    ) -> [Int: Double] {
        guard nodes.count >= 2, !edges.isEmpty else { return seed }

        var result = seed
        for node in nodes { if let fixed = anchors[node] { result[node] = fixed } }

        let free = nodes.filter { anchors[$0] == nil }
        guard !free.isEmpty else { return result }
        var index: [Int: Int] = [:]
        for (offset, node) in free.enumerated() { index[node] = offset }
        let size = free.count

        var weights = edges.map { max($0.weight, 1e-6) }

        for pass in 0...1 {
            var rows: [[Double]] = []
            var target: [Double] = []

            for (offset, edge) in edges.enumerated() {
                let scale = weights[offset].squareRoot()
                let fixedA = anchors[edge.a], fixedB = anchors[edge.b]
                if fixedA != nil && fixedB != nil { continue }   // nothing to learn

                var row = [Double](repeating: 0, count: size)
                var value = edge.delta
                if let fixedA {                       // x_b = delta + A_a
                    row[index[edge.b]!] = 1
                    value += fixedA
                } else if let fixedB {                // x_a = A_b - delta
                    row[index[edge.a]!] = 1
                    value = fixedB - edge.delta
                } else {
                    row[index[edge.a]!] = -1
                    row[index[edge.b]!] = 1
                }
                rows.append(row.map { $0 * scale })
                target.append(value * scale)
            }

            // A group with no anchor at all is only determined up to a global
            // translation, so one node is pinned to keep the system solvable.
            if !rows.contains(where: { row in row.filter { $0 != 0 }.count == 1 }) {
                var row = [Double](repeating: 0, count: size)
                row[0] = weights.reduce(0, +).squareRoot() + 1
                rows.append(row)
                target.append(seed[free[0]]! * row[0])
            }

            guard let solved = solveNormalEquations(rows: rows, target: target, size: size) else {
                return result
            }
            for node in free { result[node] = solved[index[node]!] }
            if pass == 1 { break }

            // Robustify: down-weight edges that disagree with the consensus,
            // using a median-absolute-deviation scale so a few bad matches
            // cannot define it.
            let residuals = edges.map { abs((result[$0.b]! - result[$0.a]!) - $0.delta) }
            let sorted = residuals.sorted()
            let median = sorted.isEmpty ? 0 : sorted[sorted.count / 2]
            let scale = max(median * 1.4826, 0.005)
            for offset in 0..<weights.count {
                let ratio = residuals[offset] / (3 * scale)
                weights[offset] /= (1 + ratio * ratio)
            }
        }
        return result
    }

    /// Normal equations with Gaussian elimination; components are small.
    static func solveNormalEquations(
        rows: [[Double]], target: [Double], size: Int
    ) -> [Double]? {
        var normal = [[Double]](repeating: [Double](repeating: 0, count: size + 1), count: size)
        for (rowIndex, row) in rows.enumerated() {
            for i in 0..<size where row[i] != 0 {
                for j in 0..<size where row[j] != 0 {
                    normal[i][j] += row[i] * row[j]
                }
                normal[i][size] += row[i] * target[rowIndex]
            }
        }
        // A touch of regularisation keeps a degenerate component solvable.
        for i in 0..<size { normal[i][i] += 1e-9 }

        for column in 0..<size {
            var pivot = column
            for row in (column + 1)..<size where abs(normal[row][column]) > abs(normal[pivot][column]) {
                pivot = row
            }
            if abs(normal[pivot][column]) < 1e-12 { return nil }
            normal.swapAt(column, pivot)
            let divisor = normal[column][column]
            for j in column...size { normal[column][j] /= divisor }
            for row in 0..<size where row != column {
                let factor = normal[row][column]
                if factor == 0 { continue }
                for j in column...size { normal[row][j] -= factor * normal[column][j] }
            }
        }
        return (0..<size).map { normal[$0][size] }
    }
}
