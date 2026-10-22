# SPDX-License-Identifier: GPL-3.0-or-later
"""Waveform-based multicam timeline sync for FCP7 XML (xmeml) timelines."""

from .errors import (
    FFmpegFailedError,
    FFmpegMissingError,
    MediaMissingError,
    SyncMyVidsError,
    XmemlParseError,
)
from .model import Clip, MediaFile, Sequence, SyncUnit, Timeline, Track, build_sync_units
from .timebase import Rate, format_timecode, parse_timecode

__version__ = "0.1.0"

__all__ = [
    "Clip",
    "FFmpegFailedError",
    "FFmpegMissingError",
    "MediaFile",
    "MediaMissingError",
    "Rate",
    "Sequence",
    "SyncMyVidsError",
    "SyncUnit",
    "Timeline",
    "Track",
    "XmemlParseError",
    "__version__",
    "build_sync_units",
    "format_timecode",
    "parse_timecode",
]
