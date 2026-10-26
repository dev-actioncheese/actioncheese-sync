# SPDX-License-Identifier: GPL-3.0-or-later
"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__, about
from .audio import ffmpeg
from . import compare
from .engine import SyncOptions, load_inputs, sync_timeline
from .errors import SyncMyVidsError
from .formats import fcp7
from .timebase import Rate, format_timecode


def _build_parser() -> argparse.ArgumentParser:
    # argv[0] so the old `syncmyvids` name still prints as itself in help.
    invoked = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else "actioncheese-sync"
    parser = argparse.ArgumentParser(
        prog=invoked if invoked.startswith(("actioncheese", "syncmyvids")) else "actioncheese-sync",
        description=f"{about.APP_NAME}: waveform-based multicam sync for "
                    "FCP7 XML (xmeml) timelines.",
    )
    parser.add_argument("--version", action="version",
                        version=f"{about.APP_NAME} {__version__}")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="show progress (repeat for debug detail)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync = subparsers.add_parser("sync", help="synchronise a timeline")
    sync.add_argument("input", type=Path, nargs="*", metavar="INPUT",
                      help="one or more .xml timelines exported from your NLE, and/or "
                           "folders -- one per camera or recorder. An ActionCheese shoot "
                           "folder is recognised and its clips kept in sync with each other.")
    sync.add_argument("-o", "--output", type=Path, help="where to write the synced .xml")
    sync.add_argument("--report", type=Path, help="write a JSON report here")
    sync.add_argument("--dry-run", action="store_true",
                      help="analyse and report without writing an output file")

    sync.add_argument("--reference", metavar="TRACK",
                      help="track to hold still, e.g. V1 or A3 (default: the busiest)")
    sync.add_argument("--min-confidence", type=float, default=0.45,
                      help="minimum correlation to accept a match (default: %(default)s)")
    sync.add_argument("--min-peak-ratio", type=float, default=1.0,
                      help="minimum peak-to-runner-up ratio (default: %(default)s)")
    sync.add_argument("--min-overlap", type=float, default=20.0, metavar="SECONDS",
                      help="minimum overlap to consider a match (default: %(default)s)")
    sync.add_argument("--unsynced-gap", type=float, default=1.0, metavar="SECONDS",
                      help="gap before the unsynced block (default: %(default)s)")
    sync.add_argument("--islands", choices=("append", "unsynced"), default="append",
                      help="what to do with groups that sync to each other but not to the "
                           "main block (default: %(default)s)")

    sync.add_argument("--sequence-name", help="name for the output sequence")
    sync.add_argument("--sequence-suffix", default="_synced",
                      help="appended to the sequence name when --sequence-name is not given "
                           "(default: %(default)s)")
    sync.add_argument("--unsynced-placement", choices=("inline", "append"), default="inline",
                      help="'inline' drops each unmatched clip in after everything shot "
                           "before it; 'append' puts them all at the end (default: %(default)s)")
    sync.add_argument("--unsynced-video", choices=("keep", "split"), default="keep",
                      help="where an unmatched clip's picture goes: 'keep' leaves it on "
                           "its own track unless that moment is taken; 'split' always "
                           "gives it a new track (default: %(default)s)")
    sync.add_argument("--unsynced-audio", choices=("keep", "split", "merge"), default="keep",
                      help="where unmatched audio goes: 'keep' leaves it on its original "
                           "track (only the picture moves); 'split' gives it a new track "
                           "per channel; 'merge' folds both channels onto one track and "
                           "LOSES the right channel (default: %(default)s)")
    sync.add_argument("--unsynced-label", default="Rose",
                      help="label colour for unmatched clips, or '' for none "
                           "(Premiere's red is Rose; default: %(default)s)")
    sync.add_argument("--trust-synced", action="store_true",
                      help="treat clips that arrive already in sync (an ActionCheese "
                           "shoot, say) as authoritative, instead of letting a "
                           "confident audio match correct them")
    sync.add_argument("--no-clock", action="store_true",
                      help="do not place unmatched clips from their recording "
                           "timestamps, even when the device's clock is known")
    sync.add_argument("--mono-output", action="store_true",
                      help="leave the sequence master mono (default: stereo)")
    sync.add_argument("--keep-empty-tracks", action="store_true",
                      help="leave tracks that end up with no clips (default: remove them)")
    sync.add_argument("--close-gaps", action="store_true",
                      help="remove every stretch of timeline where nothing plays on any track")
    sync.add_argument("--no-drift", action="store_true",
                      help="detect drift but never write speed corrections")
    sync.add_argument("--no-drift-analysis", action="store_true",
                      help="skip drift measurement entirely (faster)")
    sync.add_argument("--drift-threshold-ppm", type=float, default=5.0,
                      help="minimum drift before correcting (default: %(default)s)")
    sync.add_argument("--drift-min-duration", type=float, default=300.0, metavar="SECONDS",
                      help="shortest clip eligible for drift analysis (default: %(default)s)")

    sync.add_argument("--merge", type=Path, nargs="+", action="append",
                      metavar="FOLDER",
                      help="treat these folders as one camera, for a camera that "
                           "filled several cards. Repeat for each camera: "
                           "--merge cam1/card1 cam1/card2 --merge cam2/card1")
    sync.add_argument("--rate", type=float, metavar="FPS",
                      help="frame rate for a timeline built from folders "
                           "(default: whatever most of the footage was shot at)")
    sync.add_argument("--built-xml", type=Path, metavar="PATH",
                      help="also save the timeline assembled from the inputs, before "
                           "syncing -- useful for seeing what was read in")
    sync.add_argument("--media-root", type=Path,
                      help="directory to re-locate media in when paths in the XML are stale")
    sync.add_argument("--jobs", type=int, default=0, help="parallel decode workers")
    sync.add_argument("--cache-dir", type=Path, help="where to cache onset envelopes")
    sync.add_argument("--no-refine", action="store_true",
                      help="skip sub-sample GCC-PHAT refinement")

    probe = subparsers.add_parser("probe", help="show what the inputs contain, without analysing")
    probe.add_argument("input", type=Path, nargs="*", metavar="INPUT")
    probe.add_argument("--merge", type=Path, nargs="+", action="append",
                       metavar="FOLDER", help="folders to read as one camera")

    compare_cmd = subparsers.add_parser(
        "compare",
        help="measure our sync against one known to be right, and write a "
             "report you can send back without sending any footage")
    compare_cmd.add_argument("ours", type=Path,
                             help="the timeline ActionCheese Sync produced")
    compare_cmd.add_argument("theirs", type=Path,
                             help="the same material synced correctly, as FCP7 XML "
                                  "(PluralEyes, Resolve, Premiere's synchronise, or by hand)")
    compare_cmd.add_argument("--report", type=Path,
                             help="the JSON report from that run, which adds every "
                                  "pair we scored and whether it was right")
    compare_cmd.add_argument("-o", "--output", type=Path,
                             help="write the report here, ready to email")
    compare_cmd.add_argument("--names", action="store_true",
                             help="keep real file and folder names (they are hashed "
                                  "by default, because a path names a client)")
    compare_cmd.add_argument("--note", default="",
                             help="anything you want to tell us about this project")

    subparsers.add_parser("changelog", help="what changed, in every version")

    subparsers.add_parser("guide", help="how to use syncmyvids, and what each option does")
    subparsers.add_parser("about", help="version, author and how to report a bug")

    return parser


def _configure_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr)


def _rate_from_flag(value: float | None) -> Rate | None:
    """``--rate 29.97`` means the NTSC 30, not a timebase of 29."""
    if not value:
        return None
    nearest = round(value)
    return Rate(nearest, abs(value - nearest) > 1e-6)


def _command_probe(args: argparse.Namespace) -> int:
    # Folders have to be read before they can be shown, so probe goes through
    # the same loader as sync -- what it prints is exactly what sync will use.
    inputs = list(args.input) + [group for group in (getattr(args, "merge", None) or [])]
    loaded = load_inputs(inputs, rate=_rate_from_flag(getattr(args, "rate", None)))
    for warning in loaded.warnings:
        print(f"note: {warning}", file=sys.stderr)
    timeline = loaded.timeline
    for sequence in timeline.sequences:
        print(f"{sequence.name}  [{sequence.rate}]  "
              f"duration {format_timecode(sequence.duration, sequence.rate)}")
        for track in sequence.tracks:
            print(f"  {track.label}: {len(track.clips)} clip(s)"
                  f"{'' if track.enabled else '  (disabled)'}")
            for clip in track.clips:
                path = clip.file.path if clip.file else None
                missing = "" if (path and path.exists()) else "   [media missing]"
                print(f"    {format_timecode(clip.start, sequence.rate)} "
                      f"-> {format_timecode(clip.end, sequence.rate)}  "
                      f"{clip.name}{missing}")
        if sequence.element is not None:
            found = sequence.element.findall("marker")
            if found:
                print(f"  markers: {len(found)}")
                for element in found:
                    frame = int(element.findtext("in") or 0)
                    print(f"    {format_timecode(frame, sequence.rate)}  "
                          f"{element.findtext('name') or ''}")
    return 0


def _command_compare(args: argparse.Namespace) -> int:
    report = None
    if args.report:
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))

    bundle = compare.build_bundle(
        fcp7.read(args.ours), fcp7.read(args.theirs),
        report=report, anonymous=not args.names, note=args.note,
    )
    print(compare.render(bundle))

    if args.output:
        Path(args.output).write_text(json.dumps(bundle, indent=2), encoding="utf-8")
        print(f"\nWritten to {args.output}")
        print(f"It contains no footage and no paths{'' if args.names else ' and no names'}. "
              f"Read it, then send it to {about.BUG_EMAIL}.")
    else:
        print(f"\nAdd -o report.json to write this out and send it to {about.BUG_EMAIL}.")
    return 0


def _command_sync(args: argparse.Namespace) -> int:
    if not args.dry_run and not args.output:
        print("error: give -o/--output, or use --dry-run to analyse without writing",
              file=sys.stderr)
        return 2

    ffmpeg.ensure_available()

    inputs = list(args.input) + [group for group in (args.merge or [])]
    loaded = load_inputs(inputs, sequence_name=args.sequence_name,
                         rate=_rate_from_flag(args.rate),
                         progress=lambda message: print(message, file=sys.stderr))
    timeline = loaded.timeline
    for warning in loaded.warnings:
        print(f"note: {warning}", file=sys.stderr)
    if loaded.built:
        print(loaded.summary, file=sys.stderr)
        if args.built_xml:
            fcp7.write(timeline, args.built_xml)
            print(f"Assembled timeline written to {args.built_xml}", file=sys.stderr)

    if args.media_root:
        relocated = fcp7.relocate_media(timeline, args.media_root)
        if relocated:
            print(f"Relocated {relocated} media file(s) under {args.media_root}", file=sys.stderr)

    options = SyncOptions(
        min_confidence=args.min_confidence,
        min_peak_ratio=args.min_peak_ratio,
        min_overlap_seconds=args.min_overlap,
        reference=args.reference,
        unsynced_gap_seconds=args.unsynced_gap,
        islands=args.islands,
        close_gaps=args.close_gaps,
        prune_empty_tracks=not args.keep_empty_tracks,
        trust_synced=args.trust_synced,
        use_clock=not args.no_clock,
        stereo_output=not args.mono_output,
        unsynced_placement=args.unsynced_placement,
        unsynced_audio=args.unsynced_audio,
        unsynced_video=args.unsynced_video,
        unsynced_label=args.unsynced_label or None,
        sequence_name=args.sequence_name,
        sequence_suffix=args.sequence_suffix,
        correct_drift=not args.no_drift,
        drift_threshold_ppm=args.drift_threshold_ppm,
        drift_min_duration=args.drift_min_duration,
        analyse_drift=not args.no_drift_analysis,
        refine=not args.no_refine,
        jobs=args.jobs,
        cache_dir=args.cache_dir,
    )

    report = sync_timeline(timeline, options,
                           rigid_clip_ids=loaded.rigid_clip_ids,
                           marker_hints=loaded.marker_hints)
    print(report.render())

    if args.report:
        report.write_json(args.report)
        print(f"\nReport written to {args.report}", file=sys.stderr)

    if args.dry_run:
        print("\nDry run: no output written.", file=sys.stderr)
    else:
        fcp7.write(timeline, args.output)
        print(f"\nWrote {args.output}", file=sys.stderr)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    try:
        if args.command == "about":
            print(about.about_text())
            return 0
        if args.command == "changelog":
            print(about.changelog_text())
            return 0
        if args.command == "guide":
            print(about.HELP_TEXT)
            return 0
        if args.command == "probe":
            return _command_probe(args)
        if args.command == "compare":
            return _command_compare(args)
        return _command_sync(args)
    except SyncMyVidsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
