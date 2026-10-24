# SPDX-License-Identifier: GPL-3.0-or-later
"""Thin wrapper over the ffmpeg/ffprobe binaries.

We shell out rather than binding a library: ffmpeg's CLI is stable, handles
every container an NLE can throw at it, and streaming raw ``f32le`` on stdout
straight into numpy is about as fast as anything a binding would do.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np

from ..errors import FFmpegFailedError, FFmpegMissingError

#: Sample rate used for all analysis.  Sync cues live in transients, not in
#: high frequencies, and 8 kHz keeps correlation cheap while still resolving
#: offsets far finer than a video frame.
DEFAULT_SAMPLE_RATE = 8000


@dataclass(frozen=True, slots=True)
class AudioStreamInfo:
    index: int
    channels: int
    sample_rate: int
    duration: Optional[float]


@dataclass(frozen=True, slots=True)
class MediaInfo:
    duration: Optional[float]
    audio_streams: tuple[AudioStreamInfo, ...]

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_streams)


def _bundled(name: str) -> Optional[str]:
    """Look for a binary shipped alongside a frozen build.

    A packaged app cannot assume the recipient has ffmpeg installed, so the
    bundle may carry its own copy; PyInstaller unpacks it to ``sys._MEIPASS``.
    """
    # Windows calls them ffmpeg.exe and ffprobe.exe, and a bundle built there
    # carries that name. Looking only for the bare name finds nothing, and the
    # app then reports ffmpeg as missing on a machine that is carrying it.
    names = [f"{name}.exe", name] if sys.platform == "win32" else [name]

    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots += [Path(meipass), Path(meipass) / "bin"]
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).parent
        # .app/Contents/MacOS/<exe> -> .app/Contents/Resources
        roots += [executable, executable.parent / "Resources"]

    for root in roots:
        for candidate_name in names:
            candidate = root / candidate_name
            # Windows has no execute bit; os.access(X_OK) is true for any
            # readable file there, which is the answer we want anyway.
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


@lru_cache(maxsize=1)
def ensure_available() -> tuple[str, str]:
    """Locate ffmpeg and ffprobe, or explain how to install them.

    A bundled copy wins over one on PATH, so a shared app behaves the same on a
    machine that happens to have a different ffmpeg installed.
    """
    ffmpeg_bin = _bundled("ffmpeg") or shutil.which("ffmpeg")
    ffprobe_bin = _bundled("ffprobe") or shutil.which("ffprobe")
    missing = [n for n, p in (("ffmpeg", ffmpeg_bin), ("ffprobe", ffprobe_bin)) if not p]
    if missing:
        raise FFmpegMissingError(
            f"{' and '.join(missing)} not found. "
            "Install with `brew install ffmpeg` (macOS) or `apt install ffmpeg` (Debian/Ubuntu)."
        )
    return ffmpeg_bin, ffprobe_bin  # type: ignore[return-value]


def probe(path: Path) -> MediaInfo:
    """Inspect a media file's duration and audio streams."""
    _, ffprobe_bin = ensure_available()
    result = subprocess.run(
        [ffprobe_bin, "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise FFmpegFailedError(f"ffprobe failed on {path}: {result.stderr.strip()}")

    payload = json.loads(result.stdout or "{}")

    duration: Optional[float] = None
    raw_duration = payload.get("format", {}).get("duration")
    if raw_duration is not None:
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError):
            duration = None

    streams: list[AudioStreamInfo] = []
    for stream in payload.get("streams", []):
        if stream.get("codec_type") != "audio":
            continue
        stream_duration: Optional[float] = None
        if stream.get("duration") is not None:
            try:
                stream_duration = float(stream["duration"])
            except (TypeError, ValueError):
                stream_duration = None
        streams.append(
            AudioStreamInfo(
                # Index among *audio* streams, which is what `-map 0:a:N` wants.
                index=len(streams),
                channels=int(stream.get("channels") or 1),
                sample_rate=int(stream.get("sample_rate") or 0),
                duration=stream_duration,
            )
        )

    return MediaInfo(duration=duration, audio_streams=tuple(streams))


def decode_mono(
    path: Path,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    start: Optional[float] = None,
    duration: Optional[float] = None,
    stream_index: Optional[int] = None,
) -> np.ndarray:
    """Decode a span of audio to mono float32 at ``sample_rate``.

    Returns an empty array when the file has no audio, which is a normal
    condition (a B-roll clip with no sound cannot contribute to sync) rather
    than an error.
    """
    ffmpeg_bin, _ = ensure_available()

    command = [ffmpeg_bin, "-nostdin", "-v", "error"]
    if start is not None and start > 0:
        # Input-side seek: fast, and accurate in modern ffmpeg.
        command += ["-ss", f"{start:.6f}"]
    command += ["-i", str(path)]
    if duration is not None and duration > 0:
        command += ["-t", f"{duration:.6f}"]

    command += ["-vn", "-map", f"0:a:{stream_index if stream_index is not None else 0}"]
    command += ["-ac", "1", "-ar", str(sample_rate), "-f", "f32le", "-"]

    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        if "Stream map" in stderr or "does not contain any stream" in stderr:
            return np.zeros(0, dtype=np.float32)
        raise FFmpegFailedError(f"ffmpeg failed decoding {path}: {stderr}")

    samples = np.frombuffer(result.stdout, dtype="<f4")
    # frombuffer gives a read-only view onto the subprocess buffer; downstream
    # code normalises in place, so hand back a writable copy.
    return np.array(samples, dtype=np.float32, copy=True)
