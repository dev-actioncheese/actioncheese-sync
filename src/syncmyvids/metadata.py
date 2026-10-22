# SPDX-License-Identifier: GPL-3.0-or-later
"""Recording timestamps read from the media itself.

Cameras stamp each file with when it started recording.  Two devices' clocks are
rarely set to the same time, but each device's *own* clock is consistent, so
once audio has matched even one clip from a device the offset between that
device's clock and the timeline is known -- and every other clip from that
device can be placed from its timestamp alone, including the ones whose audio
never matched anything.
"""

from __future__ import annotations

import datetime
import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .audio import ffmpeg

log = logging.getLogger(__name__)

_cache: dict[Path, Optional[float]] = {}


def creation_time(path: Path) -> Optional[float]:
    """When the camera started recording this file, as a Unix timestamp.

    ``None`` when the file carries no usable timestamp, which is normal for
    transcoded or re-wrapped media.
    """
    if path in _cache:
        return _cache[path]

    value: Optional[float] = None
    try:
        _, ffprobe_bin = ffmpeg.ensure_available()
        result = subprocess.run(
            [ffprobe_bin, "-v", "error", "-print_format", "json",
             "-show_entries", "format_tags=creation_time", str(path)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode == 0:
            tags = json.loads(result.stdout or "{}").get("format", {}).get("tags", {})
            raw = tags.get("creation_time")
            if raw:
                stamp = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=datetime.timezone.utc)
                value = stamp.timestamp()
    except Exception as exc:  # noqa: BLE001 - a missing timestamp is not fatal
        log.debug("no creation time for %s: %s", path, exc)

    _cache[path] = value
    return value


@dataclass(slots=True)
class MediaInfo:
    """What a folder-ingested file needs to become a clip on a timeline."""

    path: Path
    duration: float
    """Seconds. 0.0 for a still."""
    has_video: bool = False
    has_audio: bool = False
    frame_rate: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    audio_channels: int = 0
    started: Optional[float] = None
    """Unix timestamp of when recording started, when the file or its sidecar
    says so."""
    source: str = "ffprobe"
    """Where the timing came from -- useful in the report when a camera's clock
    turns out to be wrong."""


#: Sony writes one of these beside every clip on an XAVC card. Reading it is
#: both faster than probing the movie and more trustworthy: CreationDate is the
#: moment recording started, where a container's creation_time is whatever the
#: camera chose to stamp.
_SONY_NS = {"nrt": "urn:schemas-professionalDisc:nonRealTimeMeta:ver.2.20"}


def sony_sidecar(path: Path) -> Optional[Path]:
    """The ``*M01.XML`` Sony writes next to ``*.MP4``, if it is there."""
    for suffix in ("M01.XML", "M01.xml"):
        candidate = path.with_name(path.stem + suffix)
        if candidate.is_file():
            return candidate
    return None


def _read_sony_sidecar(movie: Path, sidecar: Path) -> Optional[MediaInfo]:
    from lxml import etree

    try:
        root = etree.parse(str(sidecar)).getroot()
    except Exception as exc:  # noqa: BLE001 - a bad sidecar just means we probe
        log.debug("unreadable Sony sidecar %s: %s", sidecar, exc)
        return None

    def find(tag: str):
        # The namespace is versioned (ver.2.20 here), and cameras differ, so
        # match on local name rather than pinning a version we happen to have
        # seen.
        for element in root.iter():
            if etree.QName(element).localname == tag:
                return element
        return None

    duration_el, format_el = find("Duration"), find("VideoFrame")
    created_el, layout_el = find("CreationDate"), find("VideoLayout")
    audio_el = find("AudioFormat")
    if duration_el is None or created_el is None:
        return None

    frames = int(duration_el.get("value", "0"))
    fps: Optional[float] = None
    if format_el is not None:
        # captureFps is "50.00p"; for slow motion it differs from formatFps,
        # and it is the capture rate that says how long the file really is.
        raw = (format_el.get("captureFps") or format_el.get("formatFps") or "").rstrip("pi")
        try:
            fps = float(raw)
        except ValueError:
            fps = None
    if not fps:
        return None

    try:
        stamp = datetime.datetime.fromisoformat(created_el.get("value", ""))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)

    return MediaInfo(
        path=movie,
        duration=frames / fps,
        has_video=True,
        has_audio=audio_el is not None and int(audio_el.get("numOfChannel", "0")) > 0,
        frame_rate=fps,
        width=int(layout_el.get("pixel", "0")) or None if layout_el is not None else None,
        height=int(layout_el.get("numOfVerticalLine", "0")) or None if layout_el is not None else None,
        audio_channels=int(audio_el.get("numOfChannel", "0")) if audio_el is not None else 0,
        started=stamp.timestamp(),
        source="sony-sidecar",
    )


_info_cache: dict[Path, Optional[MediaInfo]] = {}


def probe(path: Path) -> Optional[MediaInfo]:
    """Everything we need to know about one media file.

    Returns ``None`` when ffprobe cannot make sense of it, which is how
    non-media files in a camera folder get filtered out.
    """
    path = Path(path)
    if path in _info_cache:
        return _info_cache[path]

    sidecar = sony_sidecar(path)
    if sidecar is not None:
        info = _read_sony_sidecar(path, sidecar)
        if info is not None:
            _info_cache[path] = info
            return info

    info = None
    try:
        _, ffprobe_bin = ffmpeg.ensure_available()
        result = subprocess.run(
            [ffprobe_bin, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode == 0:
            info = _parse_ffprobe(path, json.loads(result.stdout or "{}"))
    except Exception as exc:  # noqa: BLE001 - unreadable means "not media"
        log.debug("cannot probe %s: %s", path, exc)

    _info_cache[path] = info
    return info


#: Rates a timeline is ever built at. Used to sanity-check what ffprobe says,
#: not to reject anything: a clip shot at some other rate keeps its own rate.
_STANDARD_RATES = (23.976, 24.0, 25.0, 29.97, 30.0, 48.0, 50.0, 59.94, 60.0,
                   100.0, 119.88, 120.0)


def _ratio(raw: Optional[str]) -> Optional[float]:
    if not raw or "/" not in raw:
        return None
    num, _, den = raw.partition("/")
    try:
        return float(num) / float(den) if float(den) else None
    except ValueError:
        return None


def _frame_rate(video: Optional[dict]) -> Optional[float]:
    """The rate to put on the timeline, not the average one.

    iPhones record variable frame rate, and ffprobe's ``avg_frame_rate`` is
    then the *measured* average across the file -- 21.6 fps for a 30 fps clip
    with dropped frames in it. Putting that on a timeline is nonsense.
    ``r_frame_rate`` carries the nominal rate (30/1) and is what we want,
    except for the odd container that reports something absurd like 600/1, so
    it is only preferred when it looks like a rate anyone shoots at.
    """
    if video is None:
        return None
    nominal, average = _ratio(video.get("r_frame_rate")), _ratio(video.get("avg_frame_rate"))
    if nominal and any(abs(nominal - rate) < 0.01 for rate in _STANDARD_RATES):
        return nominal
    return average or nominal


def _parse_ffprobe(path: Path, payload: dict) -> Optional[MediaInfo]:
    streams = payload.get("streams") or []
    container = payload.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None and audio is None:
        return None

    # A still photo is a one-frame "video" stream. It has no audio to sync on
    # and no duration to occupy, so callers drop it -- but it must be
    # recognisable rather than mistaken for a one-frame clip.
    duration = float(container.get("duration") or 0.0)
    if not duration and video is not None:
        duration = float(video.get("duration") or 0.0)

    fps = _frame_rate(video)

    return MediaInfo(
        path=path,
        duration=duration,
        has_video=video is not None and duration > 0,
        has_audio=audio is not None,
        frame_rate=fps,
        width=int(video["width"]) if video and video.get("width") else None,
        height=int(video["height"]) if video and video.get("height") else None,
        audio_channels=int(audio.get("channels") or 0) if audio else 0,
        started=creation_time(path),
        source="ffprobe",
    )
