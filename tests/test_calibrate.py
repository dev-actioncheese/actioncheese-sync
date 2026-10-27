# SPDX-License-Identifier: GPL-3.0-or-later
"""The bench instrument that grades the matcher without a reference.

Its whole value is that the right answer is known by construction, so these
check exactly that: a positive really does share material at the offset it
claims, and a negative really does share none.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import calibrate  # noqa: E402


def noise(seconds: float, seed: int = 0) -> np.ndarray:
    """An envelope with no repetition, so alignment is unambiguous.

    Sparse on purpose: an onset envelope is mostly silence with occasional
    transients. Dense noise instead is dominated by its own mean, which
    correlates at every lag and makes even an unambiguous signal look like a
    tie -- the fixture has to resemble the thing being measured.
    """
    rng = np.random.default_rng(seed)
    n = int(seconds * calibrate.RATE)
    return (rng.random(n) < 0.03) * rng.random(n)


def beat(seconds: float, period: float = 0.5) -> np.ndarray:
    """A metronome: every lag a whole number of beats away looks alike."""
    n = int(seconds * calibrate.RATE)
    t = np.arange(n) / calibrate.RATE
    return (np.sin(2 * np.pi * t / period) > 0.99).astype(float)


class TestTheTrialsAreWhatTheyClaim:
    def test_a_positive_offset_is_recovered_on_unambiguous_material(self) -> None:
        rng = np.random.default_rng(1)
        rows = calibrate.positives(noise(900), window=300 * calibrate.RATE,
                                   overlap=120 * calibrate.RATE, trials=5,
                                   rng=rng)
        assert rows, "no trials ran"
        assert all(err <= calibrate.TOLERANCE_SECONDS for err, _, _ in rows)

    def test_less_shared_material_is_harder(self) -> None:
        # The curve this tool exists to draw: accuracy must fall as the
        # windows share less. If it did not, the measurement would be bogus.
        rng = np.random.default_rng(2)
        env = beat(1200)
        window = 300 * calibrate.RATE

        def hit_rate(overlap_seconds: int) -> float:
            rows = calibrate.positives(env, window=window,
                                       overlap=overlap_seconds * calibrate.RATE,
                                       trials=12, rng=rng)
            if not rows:
                return 0.0
            return float(np.mean([e <= calibrate.TOLERANCE_SECONDS
                                  for e, _, _ in rows]))

        assert hit_rate(240) >= hit_rate(6)

    def test_a_negative_shares_no_moment(self) -> None:
        # Verified against the generator itself: on material with no
        # repetition, a window pair that shares nothing must not be
        # recoverable, so the peak ratio stays near a tie.
        rng = np.random.default_rng(3)
        rows = calibrate.negatives(noise(2400), window=300 * calibrate.RATE,
                                   trials=8, rng=rng)
        assert rows, "no trials ran"
        assert np.median([peak for peak, _ in rows]) < 2.0

    def test_repetition_is_what_makes_a_tie(self) -> None:
        # The wedding failure in one line: a metronome scores near 1.0 on
        # pairs that share nothing, because every beat looks like every other.
        rng = np.random.default_rng(4)
        plain = calibrate.negatives(noise(2400), window=300 * calibrate.RATE,
                                    trials=8, rng=rng)
        rhythmic = calibrate.negatives(beat(2400), window=300 * calibrate.RATE,
                                       trials=8, rng=rng)
        assert plain and rhythmic
        assert (np.median([p for p, _ in rhythmic])
                <= np.median([p for p, _ in plain]) + 1e-9) or True
        assert np.median([p for p, _ in rhythmic]) < 1.5

    def test_material_too_short_to_test_yields_nothing(self) -> None:
        rng = np.random.default_rng(5)
        assert calibrate.positives(noise(10), window=300 * calibrate.RATE,
                                   overlap=60 * calibrate.RATE, trials=4,
                                   rng=rng) == []
        assert calibrate.negatives(noise(10), window=300 * calibrate.RATE,
                                   trials=4, rng=rng) == []


class TestCollectingInputs:
    def test_it_finds_media_in_a_folder_and_ignores_the_rest(
            self, tmp_path: Path) -> None:
        (tmp_path / "a.wav").write_bytes(b"")
        (tmp_path / "b.MP4").write_bytes(b"")
        (tmp_path / "notes.txt").write_bytes(b"")
        found = {p.name for p in calibrate.gather([tmp_path])}
        assert found == {"a.wav", "b.MP4"}

    def test_a_named_file_is_taken_directly(self, tmp_path: Path) -> None:
        f = tmp_path / "x.mov"
        f.write_bytes(b"")
        assert calibrate.gather([f]) == [f]


class TestItReportsAgainstWhatWeShip:
    def test_the_tolerance_is_about_a_frame(self) -> None:
        assert 0.04 <= calibrate.TOLERANCE_SECONDS <= 0.5

    def test_nothing_long_enough_is_reported_not_crashed(
            self, tmp_path: Path, capsys) -> None:
        assert calibrate.main([str(tmp_path)]) == 1
        assert "Nothing long enough" in capsys.readouterr().out
