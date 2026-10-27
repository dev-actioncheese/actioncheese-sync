#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Measure the matcher against footage whose answer is known by construction.

After Guilley, Khalfallah, Lomne and Danger, "Formal Framework for the
Evaluation of Waveform Resynchronization Algorithms" (WISTP 2011). Their
insight is that a resynchronisation algorithm can be graded without any
reference alignment: shift a waveform by an amount you chose yourself, and
see whether the algorithm gives it back.

That matters here because hand-synced references are scarce and not always
right -- on the one wedding we had, the reference disagreed with the camera's
own clock at every recorder boundary, where the editor had nothing to align
to. This needs no reference at all, so any footage becomes test data.

Two kinds of trial:

  positives  two windows that really do share material, offset known
  negatives  two windows that share no moment, so any match is wrong

Negatives are the half the thresholds never had. Without them a gate can only
be judged on what it throws away, never on what it lets through.

Usage:
    python tools/calibrate.py <media-or-folder> [more...] [--window 300]

Not shipped in the app: a bench instrument, not a feature.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from syncmyvids.audio.correlate import match_envelopes  # noqa: E402
from syncmyvids.audio.features import FeatureCache  # noqa: E402
from syncmyvids.engine import SyncOptions  # noqa: E402

RATE = 100
#: Within this of the true offset counts as recovered. Two frames at 25 fps.
TOLERANCE_SECONDS = 0.2
MEDIA = {".mp4", ".mov", ".wav", ".mxf", ".m4a", ".mp3", ".avi", ".mts"}


def positives(envelope, *, window, overlap, trials, rng):
    """Windows that share `overlap` frames, at an offset we chose."""
    shift = window - overlap
    out = []
    for _ in range(trials):
        room = envelope.size - window - shift
        if room <= 1:
            break
        at = int(rng.integers(0, room))
        result = match_envelopes(envelope[at: at + window],
                                 envelope[at + shift: at + shift + window],
                                 envelope_rate=RATE, min_overlap_seconds=3.0)
        if result is not None:
            out.append((abs(result.offset_seconds * RATE - shift) / RATE,
                        result.peak_ratio, result.confidence))
    return out


def negatives(envelope, *, window, trials, rng):
    """Windows far enough apart that no alignment of them is correct."""
    out = []
    for _ in range(trials):
        if envelope.size < 3 * window:
            break
        a = int(rng.integers(0, envelope.size - 2 * window))
        b = int(rng.integers(a + window, envelope.size - window))
        result = match_envelopes(envelope[a: a + window], envelope[b: b + window],
                                 envelope_rate=RATE, min_overlap_seconds=3.0)
        if result is not None:
            out.append((result.peak_ratio, result.confidence))
    return out


def gather(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found += sorted(p for p in path.iterdir()
                            if p.suffix.lower() in MEDIA)
        elif path.suffix.lower() in MEDIA:
            found.append(path)
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--window", type=float, default=300.0, metavar="SECONDS")
    parser.add_argument("--trials", type=int, default=120)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)

    cache = FeatureCache(args.cache_dir)
    rng = np.random.default_rng(args.seed)
    window = int(args.window * RATE)

    envelopes = []
    for path in gather(args.inputs):
        try:
            envelope = cache.envelope(path)
        except Exception as exc:  # noqa: BLE001 - one bad file is not fatal
            print(f"  skipped {path.name}: {exc}")
            continue
        if envelope.size >= 3 * window:
            envelopes.append((path.name, envelope))
    if not envelopes:
        print(f"Nothing long enough to test at a {args.window:.0f}s window.")
        return 1
    print(f"{len(envelopes)} recording(s), {args.window:.0f}s windows\n")

    print("Recovering an offset we chose, by how much material is shared:")
    print(f"  {'shared':>8} {'trials':>7} {'recovered':>11} {'median peak':>12}")
    good: list[tuple] = []
    for shared in (6, 12, 30, 60, 120, 240):
        if shared * RATE >= window:
            break
        rows: list[tuple] = []
        for _, envelope in envelopes:
            rows += positives(envelope, window=window, overlap=shared * RATE,
                              trials=args.trials, rng=rng)
        if not rows:
            continue
        err = np.array([r[0] for r in rows])
        hit = np.mean(err <= TOLERANCE_SECONDS) * 100
        print(f"  {shared:7d}s {len(rows):7d} {hit:10.1f}% "
              f"{np.median([r[1] for r in rows]):12.3f}")
        good += [r for r in rows if r[0] <= TOLERANCE_SECONDS]

    bad: list[tuple] = []
    for _, envelope in envelopes:
        bad += negatives(envelope, window=window, trials=args.trials * 3, rng=rng)
    print(f"\nWindows sharing no moment: {len(bad)} trials, "
          f"median peak {np.median([b[0] for b in bad]):.3f}")

    if not good or not bad:
        return 0
    print(f"\nWhat the peak-ratio gate buys (shipping default "
          f"{SyncOptions().min_peak_ratio}):")
    print(f"  {'gate':>6} {'keeps good':>12} {'admits noise':>13}")
    for gate in (1.00, 1.05, 1.10, 1.20, 1.30, 1.50, 2.00):
        keeps = np.mean([r[1] >= gate for r in good]) * 100
        admits = np.mean([b[0] >= gate for b in bad]) * 100
        mark = "  <- shipping" if abs(gate - SyncOptions().min_peak_ratio) < 1e-9 else ""
        print(f"  {gate:6.2f} {keeps:11.1f}% {admits:12.1f}%{mark}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
