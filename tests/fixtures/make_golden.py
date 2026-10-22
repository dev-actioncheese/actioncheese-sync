# SPDX-License-Identifier: GPL-3.0-or-later
"""Write the golden file the Swift port is checked against.

The Swift engine is a translation, not a reimplementation, so it has to agree
with this one numerically.  Everything here comes from the Python engine and is
recorded to be replayed in Swift: a small deterministic DSP probe, the pairwise
offsets on the synthetic multicam fixture, the measured drift, and the "action"
callout -- one spoken word shared by three devices that have nothing else in
common, which is how a crew without timecode actually syncs.

    python tests/fixtures/make_golden.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from syncmyvids.audio import ffmpeg                              # noqa: E402
from syncmyvids.audio.correlate import gcc_phat, match_envelopes  # noqa: E402
from syncmyvids.audio.features import ENVELOPE_RATE, onset_envelope  # noqa: E402

MEDIA = Path(__file__).parent / "media"
OUT = ROOT / "swift" / "ActionCheeseSyncKit" / "Tests" / "ActionCheeseSyncKitTests" / "Golden"
SAMPLE_RATE = 8000


def probe_signal() -> np.ndarray:
    """A short deterministic signal, so the DSP can be compared on its own."""
    rng = np.random.default_rng(1234)
    n = SAMPLE_RATE * 4
    audio = rng.normal(0.0, 0.01, n).astype(np.float32)
    for onset in (0.35, 1.10, 1.95, 2.60, 3.20):
        index = int(onset * SAMPLE_RATE)
        length = int(0.09 * SAMPLE_RATE)
        decay = np.exp(-np.linspace(0.0, 7.0, length))
        tone = np.sin(2 * np.pi * 440 * np.arange(length) / SAMPLE_RATE)
        audio[index:index + length] += (decay * tone).astype(np.float32)
    return audio


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    golden: dict = {
        "sample_rate": SAMPLE_RATE,
        "envelope_rate": ENVELOPE_RATE,
    }

    # 1. Raw DSP: the same samples must give the same envelope.
    signal = probe_signal()
    envelope = onset_envelope(signal, SAMPLE_RATE)
    golden["dsp_probe"] = {
        "samples": [round(float(v), 7) for v in signal],
        "envelope": [round(float(v), 7) for v in envelope],
    }

    # 2. GCC-PHAT on a known integer shift.
    rng = np.random.default_rng(99)
    noise = rng.normal(0, 1, SAMPLE_RATE * 6).astype(np.float32)
    shifted = np.roll(noise, -37)          # b[t] == a[t + 37 samples]
    offset, peak = gcc_phat(noise[2000:-2000], shifted[2000:-2000],
                            sample_rate=SAMPLE_RATE, max_lag_seconds=0.05)
    golden["gcc_phat_probe"] = {
        "samples_a": [round(float(v), 7) for v in noise[2000:2000 + SAMPLE_RATE]],
        "samples_b": [round(float(v), 7) for v in shifted[2000:2000 + SAMPLE_RATE]],
        "expected_offset_seconds": 37 / SAMPLE_RATE,
        "python_offset_seconds": offset,
        "python_peak": peak,
    }

    # 3. Pairwise offsets on the multicam fixture and on the callout.
    def envelope_of(name: str) -> np.ndarray:
        samples = ffmpeg.decode_mono(MEDIA / name, sample_rate=SAMPLE_RATE)
        return onset_envelope(samples, SAMPLE_RATE)

    scenes = {
        "multicam": ["camA.mov", "camB.mov", "recorder.wav"],
        "callout": ["callout_iphone.wav", "callout_sonyA.wav", "callout_sonyB.wav"],
    }
    golden["pairs"] = []
    for scene, names in scenes.items():
        envelopes = {name: envelope_of(name) for name in names}
        for i, first in enumerate(names):
            for second in names[i + 1:]:
                match = match_envelopes(envelopes[first], envelopes[second],
                                        envelope_rate=ENVELOPE_RATE,
                                        min_overlap_seconds=20.0)
                if match is None:
                    continue
                golden["pairs"].append({
                    "scene": scene,
                    "a": first, "b": second,
                    "offset_seconds": match.offset_seconds,
                    "confidence": match.confidence,
                    "overlap_seconds": match.overlap_seconds,
                })

    # The callout's true device offsets, so Swift can check the answer and not
    # merely that the two engines agree with each other.
    sys.path.insert(0, str(Path(__file__).parent))
    import make_fixtures  # noqa: PLC0415

    golden["callout_truth"] = {
        f"{name}.wav": start for name, start, *_ in make_fixtures.CALLOUT_DEVICES
    }
    golden["drift_truth_ppm"] = make_fixtures.DRIFT_PPM

    path = OUT / "golden.json"
    path.write_text(json.dumps(golden, indent=1), encoding="utf-8")
    size = path.stat().st_size / 1024
    print(f"wrote {path.relative_to(ROOT)} ({size:.0f} kB)")
    print(f"  dsp probe: {len(signal)} samples -> {len(envelope)} envelope frames")
    print(f"  pairs: {len(golden['pairs'])}")
    for pair in golden["pairs"]:
        print(f"    {pair['scene']:<9} {pair['a'][:20]:<21} {pair['b'][:20]:<21} "
              f"{pair['offset_seconds']:+9.4f}s conf={pair['confidence']:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
