# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared fixtures.

The end-to-end tests need real media.  Rather than committing binaries, we
generate them once per session from ``make_fixtures.py``, which also owns the
ground truth the assertions check against.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"
MEDIA_DIR = FIXTURE_DIR / "media"
INPUT_XML = FIXTURE_DIR / "out" / "input.xml"

REQUIRED_MEDIA = ["camA.mov", "camB.mov", "recorder.wav", "orphan.wav", "broll.mov"]


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


requires_ffmpeg = pytest.mark.skipif(
    not ffmpeg_available(), reason="ffmpeg/ffprobe not installed"
)


@pytest.fixture(scope="session")
def fixture_timeline() -> Path:
    """Path to the generated input timeline, building the media if needed."""
    if not ffmpeg_available():
        pytest.skip("ffmpeg/ffprobe not installed")

    complete = INPUT_XML.exists() and all((MEDIA_DIR / n).exists() for n in REQUIRED_MEDIA)
    if not complete:
        subprocess.run(
            [sys.executable, str(FIXTURE_DIR / "make_fixtures.py")],
            check=True, capture_output=True,
        )
    return INPUT_XML


@pytest.fixture(scope="session")
def ground_truth() -> dict[str, float]:
    """Known-correct answers, imported from the generator itself."""
    sys.path.insert(0, str(FIXTURE_DIR))
    import make_fixtures  # noqa: PLC0415

    return {
        "camA_minus_camB": make_fixtures.CAM_A[1] - make_fixtures.CAM_B[1],
        "recorder_minus_camB": make_fixtures.RECORDER[1] - make_fixtures.CAM_B[1],
        "drift_ppm": make_fixtures.DRIFT_PPM,
        "fps": make_fixtures.FPS,
    }
