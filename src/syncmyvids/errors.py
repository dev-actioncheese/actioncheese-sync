# SPDX-License-Identifier: GPL-3.0-or-later
"""Exception types raised by syncmyvids."""


class SyncMyVidsError(Exception):
    """Base class for every error this package raises deliberately."""


class FFmpegMissingError(SyncMyVidsError):
    """ffmpeg or ffprobe could not be found on PATH."""


class FFmpegFailedError(SyncMyVidsError):
    """An ffmpeg/ffprobe invocation exited non-zero."""


class XmemlParseError(SyncMyVidsError):
    """The input file is not a usable FCP7 XML (xmeml) timeline."""


class MediaMissingError(SyncMyVidsError):
    """A clip's media file could not be located on disk."""


class InvalidTrackLabelError(SyncMyVidsError, ValueError):
    """A track label such as ``V1`` or ``A2`` could not be parsed.

    Subclasses :class:`ValueError` as well, since that is what a caller
    validating a string would naturally catch, while still being handled by the
    CLI's blanket :class:`SyncMyVidsError` handler.
    """
