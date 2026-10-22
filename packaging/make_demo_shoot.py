# SPDX-License-Identifier: GPL-3.0-or-later
"""Build a demo shoot for screenshots, from synthetic audio only.

Nothing here is real footage: the "room" is generated noise with transients in
it, and each device hears the same room through a different microphone, at its
own clock offset. That makes it safe to publish and exactly reproducible, and
it tells the story the app is for:

    an ActionCheese shoot (two phones, already in sync with each other)
    + a camera card whose clock is 41 s fast
    + a recorder whose clock runs 20 ppm long
    + one silent clip nothing can match

    python packaging/make_demo_shoot.py [destination]

The destination defaults to /Volumes/T7/actioncheese/2026-09-27 so screenshots
show a neutral path rather than somebody's home directory. Any folder works.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests" / "fixtures"))

import numpy as np  # noqa: E402
import make_fixtures  # noqa: E402
from make_fixtures import (  # noqa: E402
    SAMPLE_RATE, apply_drift, make_master, make_silent_video, mux_video,
    through_microphone, write_wav,
)

DEFAULT_ROOT = Path("/Volumes/T7/actioncheese/2026-09-27")

#: Seconds since the Unix epoch to Apple's, which is what the shoot index uses.
APPLE_EPOCH = 978307200.0

#: When the scene was shot, in the story. Fixed so runs are reproducible.
SCENE_START = datetime(2026, 9, 27, 14, 12, 0, tzinfo=timezone.utc)

SCENE_SECONDS = 900.0
RECORDER_DRIFT_PPM = 20.0
#: How far the camera card's clock is out. It is what makes the card visibly
#: slide into place between the imported and exported strips.
CARD_CLOCK_ERROR = 41.3

#: (device, file, start, end) in scene seconds. The two phones are a take
#: apart by a fraction of a second, as two handsets started by one tap are.
#: Long takes on purpose. Drift is measured against whichever peer the matcher
#: liked best, over their common span, and the measurement needs 300 s of
#: baseline -- so if any plausible peer is short, the recorder's drift goes
#: unmeasured and the demo loses the one number that shows the feature works.
PHONE_TAKES = [
    ("iPhone A", "Take 01 - iPhone A.mov", 10.0, 440.0),
    ("iPhone B", "Take 01 - iPhone B.mov", 10.31, 437.0),
    ("iPhone A", "Take 02 - iPhone A.mov", 460.0, 890.0),
    ("iPhone B", "Take 02 - iPhone B.mov", 460.18, 888.0),
]
#: One continuous take, as a locked-off wide would be. It has to run the whole
#: scene for the recorder's drift to be measurable at all: drift is measured
#: against a peer over their *common* span, and two 3-minute takes never give
#: the 300 s of overlap the measurement needs.
CARD_CLIPS = [("A001.MP4", 5.0, 895.0)]
RECORDER_CLIP = ("ZOOM0001.WAV", 0.0, 898.0)
SILENT_CLIP = ("B001.MP4", 14.0)

#: Marker times in scene seconds, and who was rolling.
MARKERS = [(150.0, ["iPhone A", "iPhone B"]), (612.0, ["iPhone A", "iPhone B"])]


def slice_of(master: np.ndarray, start: float, end: float) -> np.ndarray:
    return master[int(start * SAMPLE_RATE):int(end * SAMPLE_RATE)]


def stamp(path: Path, when: datetime) -> None:
    """Re-wrap a file so it carries a recording time, as a camera's would."""
    # Keep the real extension: ffmpeg picks the container from it, and a
    # ".MP4.tmp" output makes it give up rather than guess.
    temporary = path.with_name(f"{path.stem}-stamped{path.suffix}")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(path), "-c", "copy",
         "-metadata", f"creation_time={when.isoformat()}", str(temporary)],
        check=True)
    temporary.replace(path)


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ROOT
    card = root / "sony-a"
    recorder = root / "zoom-h6"
    for folder in (root, card, recorder):
        folder.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(20260927)
    # make_master() generates MASTER_SECONDS of room, which is shorter than
    # this scene. Left alone it silently returns a short array, and every take
    # that starts past its end comes out silent -- which shows up much later as
    # "no usable audio" on clips that should have matched.
    make_fixtures.MASTER_SECONDS = SCENE_SECONDS
    master = make_master(rng)
    assert master.size >= int(SCENE_SECONDS * SAMPLE_RATE), "master is too short"
    scratch = root / ".build"
    scratch.mkdir(exist_ok=True)

    clips: list[dict] = []
    for device, name, start, end in PHONE_TAKES:
        # A phone: a small mic, band-limited and a bit noisy.
        audio = through_microphone(slice_of(master, start, end), rng,
                                   low=90.0, high=7000.0, gain=0.8, noise=0.004)
        wav = scratch / f"{name}.wav"
        write_wav(wav, audio)
        mux_video(wav, root / name, end - start)
        clips.append({
            "camera": device,
            "file": name,
            "start": SCENE_START.timestamp() + start - APPLE_EPOCH,
            "duration": end - start,
            "isVideo": True,
            "frameRate": 30,
            "width": 1920,
            "height": 1080,
        })
        print(f"  {device:9s} {name}")

    index = {
        "clips": clips,
        "markers": [
            {"number": number,
             "date": SCENE_START.timestamp() + at - APPLE_EPOCH,
             "cameras": cameras}
            for number, (at, cameras) in enumerate(MARKERS, start=1)
        ],
    }
    (root / ".saycheese-shoot.json").write_text(json.dumps(index, indent=2))

    for name, start, end in CARD_CLIPS:
        # A camera body: wider response, cleaner, and a clock running fast.
        audio = through_microphone(slice_of(master, start, end), rng,
                                   low=45.0, high=14000.0, gain=1.0, noise=0.002)
        wav = scratch / f"{name}.wav"
        write_wav(wav, audio)
        mux_video(wav, card / name, end - start)
        stamp(card / name, SCENE_START + timedelta(seconds=start + CARD_CLOCK_ERROR))
        print(f"  Sony A    {name}  (clock {CARD_CLOCK_ERROR:+.1f}s)")

    # B-roll with no sound: nothing can place it, so it comes out marked.
    make_silent_video(card / SILENT_CLIP[0], SILENT_CLIP[1])
    print(f"  Sony A    {SILENT_CLIP[0]}  (silent)")

    name, start, end = RECORDER_CLIP
    audio = through_microphone(slice_of(master, start, end), rng,
                               low=30.0, high=18000.0, gain=1.0, noise=0.0015)
    write_wav(recorder / name, apply_drift(audio, RECORDER_DRIFT_PPM))
    print(f"  Zoom H6   {name}  (drift {RECORDER_DRIFT_PPM:+.0f} ppm)")

    for leftover in scratch.iterdir():
        leftover.unlink()
    scratch.rmdir()
    print(f"\nwrote {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
