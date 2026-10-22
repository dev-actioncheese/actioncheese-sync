# SPDX-License-Identifier: GPL-3.0-or-later
"""Turning a pile of inputs into one timeline's worth of devices.

An input is a Final Cut Pro 7 XML file, or a folder. A folder is one camera or
recorder -- everything under it, however the card nests it. Two kinds of folder
get special treatment:

ActionCheese shoot folders
    The ActionCheese remote app records several phones against one shared
    clock and writes ``.saycheese-shoot.json`` beside the footage, giving every
    clip's exact start. Those clips are *already* in sync with each other, so
    their relative positions are taken as given and only their position as a
    group has to be found. They are one source but several devices, one per
    camera.

Sony cards
    Every clip has a ``*M01.XML`` sidecar carrying the moment recording
    started. That is read in preference to probing the movie.

Everything else is a plain folder of media: one device, clips ordered by
whatever timestamp can be found, positions unknown until the audio says.
"""

from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .errors import SyncMyVidsError
from .metadata import MediaInfo, probe
from .timebase import Rate

log = logging.getLogger(__name__)

#: The ActionCheese index, written next to the footage by the remote app.
SHOOT_INDEX = ".saycheese-shoot.json"

#: Apple's reference date. ``.saycheese-shoot.json`` stores Swift ``Date``
#: values, which count seconds from 2001-01-01 UTC rather than from the epoch.
APPLE_EPOCH_OFFSET = 978307200.0

#: Extensions worth handing to ffprobe. Not a whitelist of what we can sync --
#: ffprobe decides that -- just a way to avoid probing a card's thumbnails,
#: sidecars and housekeeping files one by one.
MEDIA_SUFFIXES = {
    ".mov", ".mp4", ".m4v", ".mxf", ".avi", ".mts", ".m2ts", ".mkv", ".braw", ".r3d",
    ".wav", ".aif", ".aiff", ".bwf", ".mp3", ".m4a", ".flac", ".caf", ".ogg",
}

#: Stills live alongside footage in an ActionCheese shoot. They carry no audio
#: and occupy no time, so they are reported and skipped rather than placed.
STILL_SUFFIXES = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".dng", ".arw", ".raf", ".cr2", ".cr3"}

#: Subfolders of a shoot folder that are ActionCheese's own output, not a
#: camera. Everything else under a shoot folder is treated as another camera,
#: which is what lets a whole job -- phones and cards together -- be handed
#: over as one folder.
SHOOT_RESERVED_SUBFOLDERS = {"xml per take", "markers", "proxies", "exports"}

#: Card folders whose own name says nothing about which camera it was. When a
#: folder is called one of these, the parent's name is used instead.
GENERIC_FOLDER_NAMES = {"clip", "clips", "private", "m4root", "xdroot", "bpav",
                        "dcim", "avchd", "stream", "video", "media", "footage"}


@dataclass(slots=True)
class SourceClip:
    """One media file's place on the timeline being built."""

    path: Path
    name: str
    duration: float
    """Seconds of this clip used on the timeline."""
    info: MediaInfo
    started: Optional[float] = None
    """Unix timestamp of the recording's start, when known."""
    in_point: float = 0.0
    """Seconds into the file where the clip begins. Non-zero only for clips
    carried over from an input timeline."""
    timeline_start: Optional[float] = None
    """Where to put the clip, in seconds, when the position is already decided
    -- a clip carried over from an input timeline keeps the position it had
    there. Overrides the wall-clock placement."""

    @property
    def has_video(self) -> bool:
        return self.info.has_video

    @property
    def has_audio(self) -> bool:
        return self.info.has_audio


@dataclass(slots=True)
class Device:
    """One camera or recorder: everything that goes on one pair of tracks."""

    name: str
    clips: list[SourceClip] = field(default_factory=list)
    group: Optional[str] = None
    """Devices sharing a group are already in sync with each other, and their
    relative positions must be preserved."""
    origin: str = "folder"

    @property
    def rigid(self) -> bool:
        return self.group is not None


@dataclass(slots=True)
class ShootMarker:
    """A moment somebody marked while recording."""

    name: str
    at: float
    """Unix timestamp of the moment itself."""
    devices: tuple[str, ...] = ()
    """Cameras rolling when it was taken, in the order the app listed them.
    Empty when it was dropped between takes, which the app allows."""
    group: Optional[str] = None
    """The shoot it came from. Its clips share the marker's clock, so they are
    what an otherwise unanchored marker should follow."""
    comment: str = ""


@dataclass(slots=True)
class Ingest:
    devices: list[Device] = field(default_factory=list)
    markers: list[ShootMarker] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stills: int = 0

    @property
    def clip_count(self) -> int:
        return sum(len(d.clips) for d in self.devices)

    @property
    def groups(self) -> dict[str, list[Device]]:
        out: dict[str, list[Device]] = {}
        for device in self.devices:
            if device.group:
                out.setdefault(device.group, []).append(device)
        return out


def classify(path: Path) -> str:
    """``"timeline"``, ``"actioncheese"``, ``"camera"`` -- or raise."""
    path = Path(path)
    if path.is_dir():
        return "actioncheese" if (path / SHOOT_INDEX).is_file() else "camera"
    if not path.exists():
        raise SyncMyVidsError(f"{path}: no such file or folder")
    if path.suffix.lower() not in (".xml", ".fcpxml"):
        raise SyncMyVidsError(
            f"{path}: expected a Final Cut Pro 7 XML file or a folder of media")

    head = path.read_bytes()[:4096].decode("utf-8", "replace")
    if "<xmeml" in head:
        return "timeline"
    if "NonRealTimeMeta" in head:
        # A Sony clip sidecar. Pointing at one is a natural mistake -- a card's
        # CLIP folder is half XML files -- so say what to do instead.
        raise SyncMyVidsError(
            f"{path.name}: this is a Sony clip sidecar, not a timeline. "
            f"Pass the folder ({path.parent}) instead and every clip in it is used.")
    if "<fcpxml" in head or path.suffix.lower() == ".fcpxml":
        raise SyncMyVidsError(
            f"{path.name}: Final Cut Pro X .fcpxml is a different format and is not "
            "supported. Export Final Cut Pro 7 XML instead "
            "(Premiere: File > Export > Final Cut Pro XML).")
    raise SyncMyVidsError(f"{path.name}: not an xmeml timeline")


def _device_name(folder: Path) -> str:
    """A name the operator will recognise for a card folder.

    A Sony card's footage sits in ``CLIP``, and five cards all called CLIP tell
    nobody anything, so walk up to the first folder with a real name.
    """
    current = folder.resolve()
    while current.name.lower() in GENERIC_FOLDER_NAMES and current.parent != current:
        current = current.parent
    return current.name or str(folder)


def _media_files(folder: Path) -> tuple[list[Path], int]:
    """Every media file under ``folder``, plus how many stills were skipped."""
    files, stills = [], 0
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        suffix = path.suffix.lower()
        if suffix in STILL_SUFFIXES:
            stills += 1
        elif suffix in MEDIA_SUFFIXES:
            files.append(path)
    return files, stills


def split_into_devices(folder: Path) -> list[Path]:
    """One folder in, one folder per device out.

    "A folder is a camera" is the rule, and for a card it is exactly right:
    a Sony card nests its footage in PRIVATE/M4ROOT/CLIP, one chain, one
    device. But a job folder often holds a card *and* a recorder side by side
    -- CLIP next to MUSIC -- and reading that as one device is worse than
    useless: clips from one device are never matched against each other, on
    the sound principle that a device cannot record two things at once, so a
    folder wrongly read as one camera can never sync to anything at all.

    The tell is two or more sibling subfolders that each hold media. A single
    chain of folders is one device however deep it goes.
    """
    if not folder.is_dir():
        return [folder]
    children = [c for c in sorted(folder.iterdir())
                if c.is_dir() and not c.name.startswith(".")
                and c.name.lower() not in SHOOT_RESERVED_SUBFOLDERS]
    with_media = [c for c in children if _media_files(c)[0]]
    loose = [f for f in folder.iterdir()
             if f.is_file() and not f.name.startswith(".")
             and f.suffix.lower() in MEDIA_SUFFIXES]
    if len(with_media) >= 2 and not loose:
        return with_media
    return [folder]


def read_camera_folder(folder: Path, *, name: Optional[str] = None,
                       progress: Optional[Callable[[str], None]] = None) -> tuple[Device, list[str]]:
    """One folder, one device. Clips ordered by when they were recorded."""
    folder = Path(folder)
    device_name = name or _device_name(folder)
    files, stills = _media_files(folder)
    warnings: list[str] = []
    if not files:
        warnings.append(f"{folder.name}: no media found")
        return Device(name=device_name, origin="folder"), warnings

    clips: list[SourceClip] = []
    undated = 0
    for index, path in enumerate(files):
        if progress:
            progress(f"reading {folder.name} ({index + 1}/{len(files)})")
        info = probe(path)
        if info is None:
            warnings.append(f"{path.name}: not readable as media, skipped")
            continue
        if info.duration <= 0:
            warnings.append(f"{path.name}: zero duration, skipped")
            continue
        if info.started is None:
            undated += 1
        clips.append(SourceClip(path=path, name=path.name, duration=info.duration,
                                info=info, started=info.started))

    if undated:
        warnings.append(
            f"{device_name}: {undated} of {len(clips)} clip(s) carry no recording "
            "time; they are ordered by filename and placed by audio alone")
    if stills:
        warnings.append(f"{device_name}: skipped {stills} still image(s)")

    # Dated clips in time order, undated ones after them by name: a camera
    # cannot record two things at once, and the layout stage relies on a
    # device's clips being in shooting order.
    clips.sort(key=lambda c: (c.started is None, c.started or 0.0, c.name))
    return Device(name=device_name, clips=clips, origin="folder"), warnings


def read_shoot_markers(payload: dict, group: Optional[str] = None) -> list[ShootMarker]:
    """The markers an operator dropped while the app was recording."""
    out: list[ShootMarker] = []
    for entry in payload.get("markers") or []:
        at = entry.get("date")
        if at is None:
            continue
        number = entry.get("number")
        out.append(ShootMarker(
            name=f"Marker {number}" if number is not None else "Marker",
            at=float(at) + APPLE_EPOCH_OFFSET,
            devices=tuple(str(c) for c in (entry.get("cameras") or [])),
            group=group,
        ))
    out.sort(key=lambda m: m.at)
    return out


def read_actioncheese_folder(folder: Path, *,
                             progress: Optional[Callable[[str], None]] = None
                             ) -> tuple[list[Device], list[str], list[ShootMarker]]:
    """An ActionCheese shoot: several cameras, already in sync with each other.

    The index is the authority on timing. It records the start of every clip on
    one shared clock, which is the whole point of the remote app, so those
    positions are taken as given rather than rediscovered from the audio.
    """
    folder = Path(folder)
    warnings: list[str] = []
    try:
        payload = json.loads((folder / SHOOT_INDEX).read_text())
    except (OSError, ValueError) as exc:
        raise SyncMyVidsError(f"{folder / SHOOT_INDEX}: unreadable ({exc})") from exc

    markers = read_shoot_markers(payload, group=f"actioncheese:{folder.name}")
    entries = payload.get("clips") or []
    if not entries:
        warnings.append(f"{folder.name}: ActionCheese index lists no clips")
        return [], warnings, markers

    group = f"actioncheese:{folder.name}"
    by_camera: dict[str, Device] = {}
    stills = 0
    missing = 0

    for index, entry in enumerate(sorted(entries, key=lambda e: e.get("start") or 0.0)):
        if progress:
            progress(f"reading {folder.name} ({index + 1}/{len(entries)})")
        if not entry.get("isVideo", True) and not float(entry.get("duration") or 0.0):
            stills += 1
            continue
        path = folder / str(entry.get("file", ""))
        if not path.is_file():
            missing += 1
            continue
        info = probe(path)
        if info is None:
            warnings.append(f"{path.name}: not readable as media, skipped")
            continue

        # The index's frameRate comes from the file's timecode track, so it is
        # the nominal rate even when the recording is variable frame rate and
        # a probe would report the measured average instead.
        indexed_rate = entry.get("frameRate")
        if indexed_rate:
            info.frame_rate = float(indexed_rate)
        duration = float(entry.get("duration") or 0.0) or info.duration
        if duration <= 0:
            stills += 1
            continue
        started = float(entry["start"]) + APPLE_EPOCH_OFFSET if entry.get("start") is not None else None
        camera = str(entry.get("camera") or "Camera")
        device = by_camera.setdefault(
            camera, Device(name=camera, group=group, origin="actioncheese"))
        device.clips.append(SourceClip(path=path, name=path.name, duration=duration,
                                       info=info, started=started))

    if stills:
        warnings.append(f"{folder.name}: skipped {stills} still image(s)")
    if missing:
        warnings.append(f"{folder.name}: {missing} clip(s) in the index are no longer in the folder")

    devices = [by_camera[name] for name in sorted(by_camera)]
    for device in devices:
        device.clips.sort(key=lambda c: (c.started is None, c.started or 0.0, c.name))

    # A shoot folder is a natural place to drop the cards from the other
    # cameras, so anything else under it is read as its own camera. Those are
    # emphatically *not* part of the rigid group -- their clocks are their own,
    # which is the whole reason they need syncing.
    for child in sorted(folder.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child.name.lower() in SHOOT_RESERVED_SUBFOLDERS:
            continue
        # A card dropped into a shoot folder splits the same way one named as
        # an input does -- the CLIP-plus-MUSIC job folder is just as likely
        # here.
        parts = split_into_devices(child)
        for part in parts:
            part_name = None
            if len(parts) > 1:
                part_name = (part.name if part.name.lower().startswith(child.name.lower())
                             else f"{child.name} {part.name}")
            nested, nested_warnings = read_camera_folder(part, name=part_name,
                                                         progress=progress)
            warnings.extend(nested_warnings)
            if nested.clips:
                devices.append(nested)

    undated = [c for d in devices for c in d.clips if c.started is None]
    if undated:
        # Without a start from the index there is nothing to hold the clip
        # rigid against, so say so rather than letting it silently float.
        warnings.append(
            f"{folder.name}: {len(undated)} clip(s) have no start in the index and "
            "will be synced by audio like any other")
    return devices, warnings, markers


def ingest(paths: Iterable[Path | str], *,
           progress: Optional[Callable[[str], None]] = None) -> Ingest:
    """Read every input into a flat list of devices.

    Timeline inputs are *not* handled here -- they keep their own reader, which
    preserves the tree. This is the folder half.
    """
    result = Ingest()
    seen: set[Path] = set()
    for raw in paths:
        path = Path(raw).expanduser()
        resolved = path.resolve()
        if resolved in seen:
            result.warnings.append(f"{path.name}: listed twice, used once")
            continue
        seen.add(resolved)

        kind = classify(path)
        if kind == "actioncheese":
            devices, warnings, markers = read_actioncheese_folder(path, progress=progress)
            result.devices.extend(devices)
            result.warnings.extend(warnings)
            result.markers.extend(markers)
        elif kind == "camera":
            parts = split_into_devices(path)
            if len(parts) > 1:
                result.warnings.append(
                    f"{path.name}: read as {len(parts)} devices "
                    f"({', '.join(p.name for p in parts)}); pass them separately if "
                    "that is wrong")
            for part in parts:
                # Parts of a split are always named for the parent and the
                # part together. Left to the usual "walk up to a real name"
                # rule, CLIP and MUSIC under one job folder would both come
                # back as the job's name; and qualifying only the generic ones
                # would give the inconsistent pair "wedding CLIP" and "MUSIC".
                part_name = None
                if len(parts) > 1:
                    part_name = (part.name if part.name.lower().startswith(path.name.lower())
                                 else f"{path.name} {part.name}")
                device, warnings = read_camera_folder(part, name=part_name,
                                                      progress=progress)
                if device.clips:
                    result.devices.append(device)
                result.warnings.extend(warnings)

    _drop_impossible_clocks(result)

    # One file must not arrive twice. It happens easily: a shoot folder finds
    # the camera folders nested under it, and the operator names one of those
    # as an input as well -- both readings are reasonable, so accept either and
    # keep the first. Without this the clip lands on two tracks and, worse, is
    # counted twice when choosing the sequence rate.
    claimed: set[Path] = set()
    duplicates = 0
    for device in result.devices:
        keep = []
        for clip in device.clips:
            resolved = clip.path.resolve()
            if resolved in claimed:
                duplicates += 1
                continue
            claimed.add(resolved)
            keep.append(clip)
        device.clips = keep
    if duplicates:
        result.warnings.append(
            f"{duplicates} clip(s) were reached by more than one input and used once")
    result.devices = [d for d in result.devices if d.clips]

    # Two folders can share a leaf name ("CLIP" under two cards of the same
    # shoot, or two days called "A"). Tracks are identified by name in the
    # report and in the GUI, so make them unique.
    used: dict[str, int] = {}
    for device in result.devices:
        count = used.get(device.name, 0) + 1
        used[device.name] = count
        if count > 1:
            device.name = f"{device.name} ({count})"
    return result


def from_timeline(timeline, *, group: Optional[str] = None,
                  name: Optional[str] = None) -> tuple[list[Device], list[str]]:
    """An already-parsed timeline as devices, one per track.

    Used when a timeline is only one of several inputs: it has to be re-laid
    alongside folders of media, and there is no way to patch one document into
    another. The clips keep their files, their in and out points and their
    positions; Premiere-specific decoration on them -- effects, labels, speed
    changes -- does not survive the trip, which is why a lone timeline input
    still goes through the patching reader instead.
    """
    from .model import build_sync_units

    warnings: list[str] = []
    if len(timeline.sequences) > 1:
        warnings.append(
            f"{name or 'timeline'}: contains {len(timeline.sequences)} sequences; "
            f"using the longest ({timeline.sequence.name})")

    sequence = timeline.sequence
    by_track: dict[str, Device] = {}
    offline = 0
    for unit in build_sync_units(sequence):
        clip = unit.primary_clip
        media = clip.file
        if media is None or media.path is None:
            warnings.append(f"{clip.name}: no file path in the timeline, skipped")
            continue
        rate = clip.rate
        in_seconds, span_seconds = clip.source_span_seconds()
        info = probe(media.path)
        if info is None:
            # Offline media is normal -- a drive gets unplugged -- and the
            # patching path has always carried such clips through and reported
            # them rather than dropping them. Describe the clip from the
            # timeline itself so it still reaches the output; it simply cannot
            # be matched on audio.
            offline += 1
            info = MediaInfo(
                path=media.path,
                duration=span_seconds,
                has_video=bool(unit.video_clips),
                has_audio=bool(unit.audio_clips),
                frame_rate=rate.fps_float,
                source="timeline",
            )
        label = ("V" if clip.media_type == "video" else "A") + str(clip.track_index)
        device = by_track.setdefault(
            label, Device(name=label, group=group, origin="timeline"))
        device.clips.append(SourceClip(
            path=media.path,
            name=clip.name,
            duration=max(span_seconds, rate.frames_to_seconds(1)),
            info=info,
            started=info.started,
            in_point=in_seconds,
            timeline_start=rate.frames_to_seconds(clip.start),
        ))

    # V2 before V10: track labels are letter-then-number, and plain string
    # order puts V10 second.
    if offline:
        warnings.append(
            f"{name or 'timeline'}: {offline} clip(s) reference media that is not "
            "available; they keep their positions but cannot be matched on audio")
    devices = [by_track[key] for key in sorted(by_track, key=lambda k: (k[0], int(k[1:])))]
    for device in devices:
        device.clips.sort(key=lambda c: c.timeline_start or 0.0)
    return devices, warnings


#: How far from the rest of the material a recording time may sit before it is
#: treated as a broken clock rather than a long shoot. A job can run over
#: several days; a camera whose clock battery has died stamps 1970 or 2000, and
#: believing it would stretch the built timeline across decades and bury every
#: real clip in one pixel.
CLOCK_SANITY_DAYS = 30


def _drop_impossible_clocks(result: Ingest) -> None:
    """Discard recording times that cannot belong to this job."""
    dated = sorted(c.started for d in result.devices for c in d.clips
                   if c.started is not None)
    if len(dated) < 3:
        return
    middle = dated[len(dated) // 2]
    limit = CLOCK_SANITY_DAYS * 86400.0

    offenders: dict[str, int] = {}
    for device in result.devices:
        for clip in device.clips:
            if clip.started is not None and abs(clip.started - middle) > limit:
                clip.started = None
                offenders[device.name] = offenders.get(device.name, 0) + 1

    for name, count in offenders.items():
        result.warnings.append(
            f"{name}: {count} clip(s) claim a recording time more than "
            f"{CLOCK_SANITY_DAYS} days from the rest of the material -- the clock is "
            "wrong, so they are placed by audio and by shooting order instead")
        for device in result.devices:
            if device.name == name:
                device.clips.sort(key=lambda c: (c.started is None, c.started or 0.0, c.name))


def common_rate(devices: Iterable[Device], fallback: Rate = Rate(25, False)) -> Rate:
    """The sequence rate to build at: whatever most of the footage was shot at.

    Mixed rates are normal on a multi-camera shoot and Premiere handles them
    inside one sequence; what matters is picking the rate that needs the least
    conforming, which is the one the most clips already use.
    """
    tally: dict[tuple[int, bool], float] = {}
    for device in devices:
        for clip in device.clips:
            fps = clip.info.frame_rate
            if not fps or not clip.has_video:
                continue
            # 29.97 and 59.94 are 30000/1001 and 60000/1001; anything within
            # half a percent of a whole number is that number, NTSC or not.
            nearest = round(fps)
            ntsc = abs(fps - nearest) > 1e-4 and abs(fps * 1.001 - nearest) < 1e-2
            tally[(nearest, ntsc)] = tally.get((nearest, ntsc), 0.0) + clip.duration
    if not tally:
        return fallback
    timebase, ntsc = max(tally.items(), key=lambda kv: kv[1])[0]
    return Rate(timebase, ntsc)


def describe(result: Ingest) -> str:
    """A short human summary, for the log and the report."""
    lines = []
    for device in result.devices:
        span = ""
        dated = [c.started for c in device.clips if c.started is not None]
        if dated:
            first = datetime.datetime.fromtimestamp(min(dated))
            span = f", from {first:%H:%M:%S}"
        tag = " (already in sync)" if device.rigid else ""
        lines.append(f"  {device.name}: {len(device.clips)} clip(s){span}{tag}")
    return "\n".join(lines)
