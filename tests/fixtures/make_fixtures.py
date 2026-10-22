# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate synthetic test media with known ground truth.

A sync tool cannot be trusted on footage whose true alignment nobody knows, so
the fixtures are built from one master "room recording" that every device then
captures a different window of, through a different simulated microphone:

    real time  0 ....................................................... 400s
    camB       [0 --------------------------------- 360]
    camA                [20 ------------------------------------- 390]
    recorder        [10 -------------------------------------------- 395]  +20 ppm

The true answers therefore are, in timeline seconds:

    position(camA)     - position(camB) = +20
    position(recorder) - position(camB) = +10

Plus two clips that must *not* match anything: 15 s of silent B-roll on V2, and
20 s of unrelated audio on A3.

Run directly to (re)build everything:  python tests/fixtures/make_fixtures.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
from lxml import etree
from scipy import signal
from scipy.io import wavfile

FIXTURE_DIR = Path(__file__).parent
MEDIA_DIR = FIXTURE_DIR / "media"
OUT_DIR = FIXTURE_DIR / "out"

SAMPLE_RATE = 48000
TIMEBASE = 30
NTSC = True
FPS = 30000 / 1001

MASTER_SECONDS = 400.0
DRIFT_PPM = 20.0

# (name, real start, real end) for each device.
CAM_A = ("camA", 20.0, 390.0)
CAM_B = ("camB", 0.0, 360.0)
RECORDER = ("recorder", 10.0, 395.0)

BROLL_SECONDS = 15.0
ORPHAN_SECONDS = 20.0


def seconds_to_frames(seconds: float) -> int:
    return int(round(seconds * FPS))


# --------------------------------------------------------------------------- #
# audio generation
# --------------------------------------------------------------------------- #

def make_master(rng: np.random.Generator) -> np.ndarray:
    """A transient-rich "room": impulsive events over a quiet noise floor.

    Sync keys off transients, so the master needs plenty of them at irregular
    intervals -- regular spacing would create a periodic correlation function
    and make the peak-ratio test meaningless.
    """
    n = int(MASTER_SECONDS * SAMPLE_RATE)
    audio = rng.normal(0.0, 0.005, n).astype(np.float32)

    for onset in rng.uniform(0.0, MASTER_SECONDS - 1.0, 1400):
        index = int(onset * SAMPLE_RATE)
        length = int(rng.uniform(0.03, 0.25) * SAMPLE_RATE)
        envelope = np.exp(-np.linspace(0.0, rng.uniform(4.0, 12.0), length))
        frequency = rng.uniform(120.0, 3500.0)
        tone = np.sin(2 * np.pi * frequency * np.arange(length) / SAMPLE_RATE)
        # Mixing a click into each tone burst widens the spectrum, which is what
        # makes the onset envelope robust across different simulated mics.
        click = rng.normal(0.0, 1.0, length) * np.exp(-np.linspace(0.0, 60.0, length))
        audio[index:index + length] += (
            envelope * (tone * 0.6 + click * 0.4) * rng.uniform(0.2, 1.0)
        ).astype(np.float32)

    return audio / (np.max(np.abs(audio)) + 1e-9) * 0.7


def through_microphone(
    audio: np.ndarray,
    rng: np.random.Generator,
    *,
    low: float | None,
    high: float | None,
    gain: float,
    noise: float,
) -> np.ndarray:
    """Simulate a different microphone: band-limit, re-gain, add its own noise."""
    result = audio.astype(np.float64)
    if high is not None:
        b, a = signal.butter(4, high / (SAMPLE_RATE / 2), "low")
        result = signal.lfilter(b, a, result)
    if low is not None:
        b, a = signal.butter(2, low / (SAMPLE_RATE / 2), "high")
        result = signal.lfilter(b, a, result)
    result = result * gain + rng.normal(0.0, noise, result.size)
    return np.clip(result, -1.0, 1.0).astype(np.float32)


def apply_drift(audio: np.ndarray, ppm: float) -> np.ndarray:
    """Stretch audio as a fast-running recorder clock would.

    A recorder whose clock runs ``ppm`` parts-per-million fast writes more
    samples per real second while labelling the file at the nominal rate, so the
    recording plays back slightly long.  Linear interpolation is exact enough at
    20 ppm, where the resampling ratio differs from 1 in the fifth decimal.
    """
    source_length = audio.size
    target_length = int(round(source_length * (1.0 + ppm * 1e-6)))
    positions = np.linspace(0.0, source_length - 1, target_length)
    return np.interp(positions, np.arange(source_length), audio).astype(np.float32)


def write_wav(path: Path, audio: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wavfile.write(path, SAMPLE_RATE, (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16))


def mux_video(audio_path: Path, output: Path, duration: float) -> None:
    """Wrap an audio file with a tiny synthetic picture, as a camera would."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=160x90:rate={TIMEBASE}000/1001",
            "-i", str(audio_path),
            "-map", "0:v", "-map", "1:a",
            "-t", f"{duration:.6f}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "pcm_s16le",
            str(output),
        ],
        check=True,
    )


def make_silent_video(output: Path, duration: float) -> None:
    """B-roll with no audio at all -- unmatchable by design."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=160x90:rate={TIMEBASE}000/1001",
            "-t", f"{duration:.6f}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-an", str(output),
        ],
        check=True,
    )


# --------------------------------------------------------------------------- #
# xmeml generation
# --------------------------------------------------------------------------- #

def _rate(parent: etree._Element) -> None:
    rate = etree.SubElement(parent, "rate")
    etree.SubElement(rate, "timebase").text = str(TIMEBASE)
    etree.SubElement(rate, "ntsc").text = "TRUE" if NTSC else "FALSE"


def _file_element(parent: etree._Element, file_id: str, path: Path,
                  duration: int, declared: set[str], has_audio: bool = True) -> None:
    """Emit a <file>, as a full declaration the first time and a stub after.

    This mirrors what Premiere exports and exercises the reader's
    back-reference resolution.
    """
    element = etree.SubElement(parent, "file", id=file_id)
    if file_id in declared:
        return
    declared.add(file_id)
    etree.SubElement(element, "name").text = path.name
    etree.SubElement(element, "pathurl").text = f"file://localhost{path}"
    _rate(element)
    etree.SubElement(element, "duration").text = str(duration)
    media = etree.SubElement(element, "media")
    video = etree.SubElement(media, "video")
    characteristics = etree.SubElement(video, "samplecharacteristics")
    etree.SubElement(characteristics, "width").text = "160"
    etree.SubElement(characteristics, "height").text = "90"
    if has_audio:
        audio = etree.SubElement(media, "audio")
        etree.SubElement(audio, "channelcount").text = "1"


def _clipitem(
    track: etree._Element,
    *,
    clip_id: str,
    name: str,
    start: int,
    end: int,
    in_point: int,
    out_point: int,
    file_id: str,
    file_path: Path,
    file_duration: int,
    declared: set[str],
    media_type: str,
    source_track: int = 1,
    links: list[tuple[str, str, int, int]] | None = None,
    has_audio: bool = True,
) -> None:
    clip = etree.SubElement(track, "clipitem", id=clip_id)
    etree.SubElement(clip, "masterclipid").text = f"masterclip-{file_id}"
    etree.SubElement(clip, "name").text = name
    etree.SubElement(clip, "enabled").text = "TRUE"
    etree.SubElement(clip, "duration").text = str(file_duration)
    _rate(clip)
    etree.SubElement(clip, "start").text = str(start)
    etree.SubElement(clip, "end").text = str(end)
    etree.SubElement(clip, "in").text = str(in_point)
    etree.SubElement(clip, "out").text = str(out_point)
    # Premiere writes tick values alongside the frame values; including them
    # here exercises the writer's obligation to keep the two in step.
    etree.SubElement(clip, "pproTicksIn").text = str(int(in_point / FPS * 254016000000))
    etree.SubElement(clip, "pproTicksOut").text = str(int(out_point / FPS * 254016000000))
    _file_element(clip, file_id, file_path, file_duration, declared, has_audio)

    source = etree.SubElement(clip, "sourcetrack")
    etree.SubElement(source, "mediatype").text = media_type
    etree.SubElement(source, "trackindex").text = str(source_track)

    for ref, link_media, track_index, clip_index in links or []:
        link = etree.SubElement(clip, "link")
        etree.SubElement(link, "linkclipref").text = ref
        etree.SubElement(link, "mediatype").text = link_media
        etree.SubElement(link, "trackindex").text = str(track_index)
        etree.SubElement(link, "clipindex").text = str(clip_index)


def build_xml(durations: dict[str, float], output: Path) -> None:
    """Write an input timeline with every clip laid out from zero.

    This is the realistic starting point: footage dropped onto per-device tracks
    with no alignment yet, which is exactly what an operator hands to PluralEyes.
    """
    root = etree.Element("xmeml", version="5")
    sequence = etree.SubElement(root, "sequence", id="sequence-1")
    etree.SubElement(sequence, "uuid").text = "d4e6c0a2-0000-4000-8000-000000000001"
    etree.SubElement(sequence, "name").text = "Sync Test"
    total = seconds_to_frames(max(durations.values()) + ORPHAN_SECONDS)
    etree.SubElement(sequence, "duration").text = str(total)
    _rate(sequence)
    timecode = etree.SubElement(sequence, "timecode")
    _rate(timecode)
    etree.SubElement(timecode, "frame").text = "0"
    etree.SubElement(timecode, "displayformat").text = "DF"

    media = etree.SubElement(sequence, "media")
    video = etree.SubElement(media, "video")
    audio = etree.SubElement(media, "audio")

    v1 = etree.SubElement(video, "track")
    v2 = etree.SubElement(video, "track")
    a1 = etree.SubElement(audio, "track")
    a2 = etree.SubElement(audio, "track")
    a3 = etree.SubElement(audio, "track")

    declared: set[str] = set()

    cam_a_frames = seconds_to_frames(durations["camA"])
    cam_b_frames = seconds_to_frames(durations["camB"])
    recorder_frames = seconds_to_frames(durations["recorder"])
    broll_frames = seconds_to_frames(BROLL_SECONDS)
    orphan_frames = seconds_to_frames(ORPHAN_SECONDS)

    # V1 + A1: camera A, picture linked to its own audio.
    _clipitem(v1, clip_id="clip-camA-v", name="camA.mov", start=0, end=cam_a_frames,
              in_point=0, out_point=cam_a_frames, file_id="file-camA",
              file_path=MEDIA_DIR / "camA.mov", file_duration=cam_a_frames,
              declared=declared, media_type="video",
              links=[("clip-camA-v", "video", 1, 1), ("clip-camA-a", "audio", 1, 1)])
    _clipitem(a1, clip_id="clip-camA-a", name="camA.mov", start=0, end=cam_a_frames,
              in_point=0, out_point=cam_a_frames, file_id="file-camA",
              file_path=MEDIA_DIR / "camA.mov", file_duration=cam_a_frames,
              declared=declared, media_type="audio",
              links=[("clip-camA-v", "video", 1, 1), ("clip-camA-a", "audio", 1, 1)])

    # V2 + A2: camera B, plus silent B-roll further along V2.
    _clipitem(v2, clip_id="clip-camB-v", name="camB.mov", start=0, end=cam_b_frames,
              in_point=0, out_point=cam_b_frames, file_id="file-camB",
              file_path=MEDIA_DIR / "camB.mov", file_duration=cam_b_frames,
              declared=declared, media_type="video",
              links=[("clip-camB-v", "video", 2, 1), ("clip-camB-a", "audio", 2, 1)])
    _clipitem(a2, clip_id="clip-camB-a", name="camB.mov", start=0, end=cam_b_frames,
              in_point=0, out_point=cam_b_frames, file_id="file-camB",
              file_path=MEDIA_DIR / "camB.mov", file_duration=cam_b_frames,
              declared=declared, media_type="audio",
              links=[("clip-camB-v", "video", 2, 1), ("clip-camB-a", "audio", 2, 1)])
    _clipitem(v2, clip_id="clip-broll", name="broll.mov",
              start=cam_b_frames, end=cam_b_frames + broll_frames,
              in_point=0, out_point=broll_frames, file_id="file-broll",
              file_path=MEDIA_DIR / "broll.mov", file_duration=broll_frames,
              declared=declared, media_type="video", has_audio=False)

    # A3: the field recorder, followed by an unrelated take.
    _clipitem(a3, clip_id="clip-recorder", name="recorder.wav", start=0, end=recorder_frames,
              in_point=0, out_point=recorder_frames, file_id="file-recorder",
              file_path=MEDIA_DIR / "recorder.wav", file_duration=recorder_frames,
              declared=declared, media_type="audio")
    _clipitem(a3, clip_id="clip-orphan", name="orphan.wav",
              start=recorder_frames, end=recorder_frames + orphan_frames,
              in_point=0, out_point=orphan_frames, file_id="file-orphan",
              file_path=MEDIA_DIR / "orphan.wav", file_duration=orphan_frames,
              declared=declared, media_type="audio")

    output.parent.mkdir(parents=True, exist_ok=True)
    etree.ElementTree(root).write(
        str(output), encoding="UTF-8", xml_declaration=True,
        doctype="<!DOCTYPE xmeml>", pretty_print=True,
    )


# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# the "action" callout
# --------------------------------------------------------------------------- #

#: Every device hears the same short word and nothing else in common.  This is
#: how a crew without timecode actually syncs -- somebody says "action" -- and
#: it is the hardest honest case for the matcher: one transient in a minute of
#: otherwise unrelated room noise.
CALLOUT_DEVICES = [
    ("callout_iphone", 0.0, 60.0, 4000.0, 0.9),
    ("callout_sonyA", -3.4, 60.0, 6000.0, 0.5),
    ("callout_sonyB", 7.9, 60.0, 2500.0, 0.35),
]
CALLOUT_AT = 22.0          # seconds of real time when the word is spoken


def make_callout(rng: np.random.Generator) -> None:
    """One spoken word shared by every device, over uncorrelated room noise."""
    word_length = int(0.45 * SAMPLE_RATE)
    envelope = np.exp(-np.linspace(0.0, 5.0, word_length))
    # Two syllables, broadband, so it survives very different microphones.
    word = np.zeros(word_length, dtype=np.float32)
    for start, freq in ((0.0, 320.0), (0.45, 210.0)):
        begin = int(start * word_length)
        span = word_length - begin
        t = np.arange(span) / SAMPLE_RATE
        word[begin:] += (np.sin(2 * np.pi * freq * t)
                         + 0.6 * np.sin(2 * np.pi * freq * 2.4 * t)
                         + 0.5 * rng.normal(0.0, 1.0, span)).astype(np.float32)
    word *= envelope.astype(np.float32)
    word /= np.max(np.abs(word)) + 1e-9

    for name, device_start, length, cutoff, gain in CALLOUT_DEVICES:
        n = int(length * SAMPLE_RATE)
        # Room noise unique to this device: nothing to correlate but the word.
        audio = rng.normal(0.0, 0.02, n).astype(np.float32)
        at = int((CALLOUT_AT - device_start) * SAMPLE_RATE)
        if 0 <= at < n - word_length:
            audio[at:at + word_length] += word * 0.85
        audio = through_microphone(audio, rng, low=120.0, high=cutoff,
                                   gain=gain, noise=0.004)
        write_wav(MEDIA_DIR / f"{name}.wav", audio)


def main() -> int:
    rng = np.random.default_rng(20240611)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    print("generating master room audio ...")
    master = make_master(rng)

    def window(start: float, end: float) -> np.ndarray:
        return master[int(start * SAMPLE_RATE): int(end * SAMPLE_RATE)]

    print("simulating microphones ...")
    cam_a_audio = through_microphone(
        window(CAM_A[1], CAM_A[2]), rng, low=120.0, high=3400.0, gain=0.45, noise=0.010
    )
    cam_b_audio = through_microphone(
        window(CAM_B[1], CAM_B[2]), rng, low=300.0, high=6000.0, gain=0.80, noise=0.004
    )
    recorder_audio = through_microphone(
        apply_drift(window(RECORDER[1], RECORDER[2]), DRIFT_PPM),
        rng, low=None, high=None, gain=0.95, noise=0.001,
    )
    orphan_audio = through_microphone(
        rng.normal(0.0, 0.2, int(ORPHAN_SECONDS * SAMPLE_RATE)).astype(np.float32),
        rng, low=None, high=8000.0, gain=0.6, noise=0.01,
    )

    write_wav(MEDIA_DIR / "_camA.wav", cam_a_audio)
    write_wav(MEDIA_DIR / "_camB.wav", cam_b_audio)
    write_wav(MEDIA_DIR / "recorder.wav", recorder_audio)
    write_wav(MEDIA_DIR / "orphan.wav", orphan_audio)

    durations = {
        "camA": cam_a_audio.size / SAMPLE_RATE,
        "camB": cam_b_audio.size / SAMPLE_RATE,
        "recorder": recorder_audio.size / SAMPLE_RATE,
    }

    print("muxing camera files ...")
    mux_video(MEDIA_DIR / "_camA.wav", MEDIA_DIR / "camA.mov", durations["camA"])
    mux_video(MEDIA_DIR / "_camB.wav", MEDIA_DIR / "camB.mov", durations["camB"])
    make_silent_video(MEDIA_DIR / "broll.mov", BROLL_SECONDS)

    (MEDIA_DIR / "_camA.wav").unlink(missing_ok=True)
    (MEDIA_DIR / "_camB.wav").unlink(missing_ok=True)

    print("generating the \"action\" callout ...")
    make_callout(np.random.default_rng(4242))

    print("writing timeline ...")
    build_xml(durations, OUT_DIR / "input.xml")

    print(f"\nfixtures ready in {MEDIA_DIR} and {OUT_DIR}")
    print("ground truth (timeline seconds):")
    print(f"  position(camA)     - position(camB) = {CAM_A[1] - CAM_B[1]:+.1f}")
    print(f"  position(recorder) - position(camB) = {RECORDER[1] - CAM_B[1]:+.1f}")
    print(f"  recorder drift = {DRIFT_PPM:+.1f} ppm")
    print("callout ground truth (device start in real time):")
    for name, start, *_ in CALLOUT_DEVICES:
        print(f"  {name:<18} {start:+.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
