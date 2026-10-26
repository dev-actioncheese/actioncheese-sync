# SPDX-License-Identifier: GPL-3.0-or-later
"""Measuring our sync against one known to be right.

A tester can tell us a sync was wrong, but not usefully *how* wrong without
sending us the footage -- and a wedding is forty gigabytes of other people's
faces that nobody should be emailing anywhere.

They do not have to. Two timelines of the same material carry everything that
matters: ours, and one produced by whatever they trust (PluralEyes, Resolve,
Premiere's own synchronise, or dragging clips by hand). Both are XML. Comparing
clip positions needs no audio at all.

Joined against our own report, which records every pair we scored, this answers
the question that actually improves the matcher: for each pair we rejected, was
it right? That is how a threshold gets fixed with evidence rather than taste.

Nothing identifying has to leave the tester's machine. Names are hashed by
default, and the bundle is plain JSON they can read before sending it.
"""

from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .formats import fcp7
from .model import Timeline, build_sync_units

#: Agreement at or under this is as good as the format allows: both timelines
#: round to the frame, and at 25 fps one frame is 40 ms.
FRAME_TOLERANCE_SECONDS = 0.05
#: Past this, a clip is in the wrong place rather than merely imprecise.
WRONG_SECONDS = 1.0

#: How many scored pairs to carry in full. A day's wedding is 553 clips, which
#: is 152,628 pairs and a 34 MB file nobody can email. Almost all of it is the
#: dull case -- pairs correctly rejected -- so the rows that teach us something
#: are kept in full, the rest are sampled, and histograms below preserve the
#: distribution either way.
MAX_PAIR_ROWS = 3000


def anonymise(name: str, salt: str) -> str:
    """A stable short id for a filename, revealing nothing about it.

    Salted per bundle so the same footage cannot be recognised across two
    testers' submissions, and so a guessed filename cannot be confirmed.
    """
    digest = hashlib.sha256(f"{salt}\x00{name}".encode("utf-8")).hexdigest()
    return digest[:12]


@dataclass(slots=True)
class ClipPlacement:
    name: str
    device: str
    start: float
    duration: float


@dataclass(slots=True)
class Difference:
    name: str
    device: str
    ours: float
    theirs: float
    delta: float
    """Seconds we are out by, after the two timelines are brought to a common
    zero. Positive means we placed it late."""


@dataclass(slots=True)
class Comparison:
    differences: list[Difference] = field(default_factory=list)
    only_ours: list[str] = field(default_factory=list)
    only_theirs: list[str] = field(default_factory=list)
    offset: float = 0.0
    """The whole-timeline shift removed before comparing. Two timelines of the
    same shoot often start at different zeros, which is not an error."""

    @property
    def agreed(self) -> list[Difference]:
        return [d for d in self.differences if abs(d.delta) <= FRAME_TOLERANCE_SECONDS]

    @property
    def close(self) -> list[Difference]:
        return [d for d in self.differences
                if FRAME_TOLERANCE_SECONDS < abs(d.delta) <= WRONG_SECONDS]

    @property
    def wrong(self) -> list[Difference]:
        return [d for d in self.differences if abs(d.delta) > WRONG_SECONDS]

    def device_offsets(self) -> dict[str, float]:
        """Median error per device.

        The shape of the error says what kind of fault it is. One device out by
        a constant amount is a placement or clock problem affecting that device
        as a whole; errors scattered within a device are the matcher failing
        clip by clip. They want different fixes.

        Read these as *relative* to each other. Two timelines give relative
        positions and nothing more, so whichever device carries most of the
        clips ends up near zero; which device is "really" wrong is not a
        question this can answer. The gap between two devices is the finding.
        """
        grouped: dict[str, list[float]] = {}
        for difference in self.differences:
            grouped.setdefault(difference.device, []).append(difference.delta)
        return {device: statistics.median(values) for device, values in grouped.items()}


def placements(timeline: Timeline) -> dict[str, ClipPlacement]:
    """Where each media file sits, keyed by filename.

    Keyed by the file rather than by track or order, because the whole point is
    to compare against a timeline somebody else's software built, where track
    numbers and clip order mean nothing in common with ours.
    """
    sequence = timeline.sequence
    rate = sequence.rate
    found: dict[str, ClipPlacement] = {}
    for unit in build_sync_units(sequence):
        clip = unit.primary_clip
        media = clip.file
        if media is None or media.path is None:
            continue
        name = media.path.name
        start = rate.frames_to_seconds(unit.start)
        # A file used more than once keeps its earliest placement; comparing
        # subclips of one file needs in-points, which is a later problem.
        if name in found and found[name].start <= start:
            continue
        found[name] = ClipPlacement(
            name=name,
            device=media.path.parent.name or "unknown",
            start=start,
            duration=rate.frames_to_seconds(unit.duration),
        )
    return found


def compare(ours: Timeline, theirs: Timeline) -> Comparison:
    """How far our timeline differs from one that is believed correct."""
    mine, yours = placements(ours), placements(theirs)
    shared = sorted(set(mine) & set(yours))

    result = Comparison(
        only_ours=sorted(set(mine) - set(yours)),
        only_theirs=sorted(set(yours) - set(mine)),
    )
    if not shared:
        return result

    # Remove the whole-timeline shift first. Two syncs of one shoot routinely
    # start at different zeros, and calling that an error would drown every
    # real one.
    raw = [mine[name].start - yours[name].start for name in shared]
    result.offset = statistics.median(raw)

    for name in shared:
        ours_at, theirs_at = mine[name].start, yours[name].start
        result.differences.append(Difference(
            name=name,
            device=mine[name].device,
            ours=ours_at,
            theirs=theirs_at,
            delta=(ours_at - theirs_at) - result.offset,
        ))
    result.differences.sort(key=lambda d: -abs(d.delta))
    return result


def judge_attempts(attempts: Iterable[dict], theirs: dict[str, ClipPlacement],
                   ) -> list[dict]:
    """Mark every pair we scored as right or wrong against the good timeline.

    This is the part that improves the matcher. For each pair we considered,
    we know the score we gave it and the offset it implied; the good timeline
    says what the offset should have been. A pair we rejected that was right
    is a match being thrown away, and a pair we accepted that was wrong is a
    threshold letting noise through.
    """
    judged: list[dict] = []
    for attempt in attempts:
        a_name, b_name = attempt.get("a_file"), attempt.get("b_file")
        if not a_name or not b_name:
            continue
        if a_name not in theirs or b_name not in theirs:
            continue
        implied = attempt.get("delta_seconds")
        if implied is None:
            continue
        truth = theirs[b_name].start - theirs[a_name].start
        error = implied - truth
        judged.append({
            "a": a_name,
            "b": b_name,
            "confidence": attempt.get("confidence"),
            "peak_ratio": attempt.get("peak_ratio"),
            "overlap_seconds": attempt.get("overlap_seconds"),
            "accepted": attempt.get("accepted"),
            "error_seconds": round(error, 4),
            "correct": abs(error) <= FRAME_TOLERANCE_SECONDS,
        })
    return judged


def build_bundle(ours: Timeline, theirs: Timeline, *,
                 report: Optional[dict] = None,
                 anonymous: bool = True,
                 note: str = "") -> dict:
    """Everything useful about one test, and nothing about whose footage it was.

    Names are hashed unless the tester chooses otherwise. Paths never appear:
    the folder a camera's files sat in becomes a device id, because
    "/Volumes/.../BESLIU 10 OKT/" names a client.
    """
    salt = hashlib.sha256(str(sorted(placements(theirs))).encode()).hexdigest()[:16]

    def shown(name: str) -> str:
        return anonymise(name, salt) if anonymous else name

    result = compare(ours, theirs)
    theirs_by_name = placements(theirs)
    devices = {p.device for p in placements(ours).values()}
    device_id = {device: (shown(device) if anonymous else device)
                 for device in sorted(devices)}

    bundle: dict = {
        "bundle_version": 1,
        "note": note,
        "anonymous": anonymous,
        "summary": {
            "clips_compared": len(result.differences),
            "agreed_within_a_frame": len(result.agreed),
            "within_a_second": len(result.close),
            "wrong_by_over_a_second": len(result.wrong),
            "only_in_ours": len(result.only_ours),
            "only_in_theirs": len(result.only_theirs),
            "whole_timeline_offset_removed": round(result.offset, 4),
            "worst_error_seconds": round(
                max((abs(d.delta) for d in result.differences), default=0.0), 4),
        },
        "per_device_median_error_seconds": {
            device_id.get(device, shown(device)): round(error, 4)
            for device, error in sorted(result.device_offsets().items())
        },
        "clips": [
            {
                "clip": shown(d.name),
                "device": device_id.get(d.device, shown(d.device)),
                "ours_at": round(d.ours, 3),
                "theirs_at": round(d.theirs, 3),
                "error_seconds": round(d.delta, 4),
            }
            for d in result.differences
        ],
    }

    if report:
        sequence = report.get("summary") or {}
        bundle["our_run"] = {
            "synced": sequence.get("synced"),
            "islands": sequence.get("islands"),
            "unmatched": sequence.get("unsynced"),
            "clips": sequence.get("units"),
            "rate": (report.get("sequence") or {}).get("fps"),
        }
        judged = judge_attempts(report.get("pairs") or [], theirs_by_name)
        for row in judged:
            row["a"], row["b"] = shown(row["a"]), shown(row["b"])
        kept = choose_pair_rows(judged)
        bundle["scored_pairs"] = kept
        bundle["scored_pairs_total"] = len(judged)
        bundle["scored_pairs_kept"] = len(kept)
        bundle["pair_distribution"] = summarise_pairs(judged)
        bundle["scoring"] = {
            "accepted_and_right": sum(1 for r in judged if r["accepted"] and r["correct"]),
            "accepted_but_wrong": sum(1 for r in judged if r["accepted"] and not r["correct"]),
            "rejected_but_right": sum(1 for r in judged
                                      if not r["accepted"] and r["correct"]),
            "rejected_and_wrong": sum(1 for r in judged
                                      if not r["accepted"] and not r["correct"]),
        }
    return bundle


def _bucket(value: Optional[float], edges: tuple[float, ...]) -> str:
    """Which band a number falls in, as a label."""
    if value is None:
        return "unknown"
    for low, high in zip(edges, edges[1:]):
        if low <= value < high:
            return f"{low:g}-{high:g}"
    return f"{edges[-1]:g}+"


#: Band edges chosen around the thresholds they inform.
CONFIDENCE_BANDS = (0.0, 0.15, 0.3, 0.45, 0.6, 0.75, 0.9)
RATIO_BANDS = (0.0, 1.5, 3.0, 6.0, 10.0, 20.0)
OVERLAP_BANDS = (0.0, 10.0, 30.0, 60.0, 180.0, 300.0, 900.0)


def summarise_pairs(judged: list[dict]) -> dict:
    """The distribution of every pair, which survives dropping the rows.

    This is what a threshold is actually chosen from: at each level of
    confidence, of peak ratio, of overlap, how many answers were right and how
    many wrong. It stays true however many individual rows are carried.
    """
    tables: dict[str, dict] = {"confidence": {}, "peak_ratio": {}, "overlap_seconds": {}}
    for field_name, edges in (("confidence", CONFIDENCE_BANDS),
                              ("peak_ratio", RATIO_BANDS),
                              ("overlap_seconds", OVERLAP_BANDS)):
        for row in judged:
            band = _bucket(row.get(field_name), edges)
            cell = tables[field_name].setdefault(
                band, {"right": 0, "wrong": 0, "accepted": 0, "rejected": 0})
            cell["right" if row["correct"] else "wrong"] += 1
            cell["accepted" if row["accepted"] else "rejected"] += 1
    return tables


def choose_pair_rows(judged: list[dict], limit: int = MAX_PAIR_ROWS) -> list[dict]:
    """Keep every row that teaches something; sample the rest.

    A pair we rejected that was right, or accepted that was wrong, is the whole
    reason for doing this and there are never many. Pairs we judged correctly
    are the bulk and are interchangeable, so an evenly spaced sample of them
    says the same thing at a hundredth of the size.
    """
    interesting = [r for r in judged if r["accepted"] != r["correct"]]
    dull = [r for r in judged if r["accepted"] == r["correct"]]

    room = max(0, limit - len(interesting))
    if len(dull) > room:
        step = len(dull) / room if room else 0
        dull = [dull[int(i * step)] for i in range(room)] if room else []
    return interesting + dull


def render(bundle: dict) -> str:
    """The same findings, for a person."""
    summary = bundle["summary"]
    lines = [
        f"Compared {summary['clips_compared']} clip(s) against the good timeline.",
        "",
        f"  agreed to the frame   {summary['agreed_within_a_frame']}",
        f"  within a second       {summary['within_a_second']}",
        f"  out by over a second  {summary['wrong_by_over_a_second']}",
        f"  worst error           {summary['worst_error_seconds']}s",
    ]
    if summary["only_in_ours"] or summary["only_in_theirs"]:
        lines.append(f"  in only one timeline  {summary['only_in_ours']} ours, "
                     f"{summary['only_in_theirs']} theirs")

    offsets = bundle.get("per_device_median_error_seconds") or {}
    if offsets:
        lines += ["", "Median error per device -- a constant one is a placement",
                  "fault for that device, scattered ones are the matcher:"]
        for device, error in offsets.items():
            lines.append(f"  {device:<16} {error:+.3f}s")

    scoring = bundle.get("scoring")
    if scoring:
        lines += ["", "Every pair we scored, judged against the good timeline:",
                  f"  accepted and right    {scoring['accepted_and_right']}",
                  f"  accepted but WRONG    {scoring['accepted_but_wrong']}",
                  f"  rejected but RIGHT    {scoring['rejected_but_right']}",
                  f"  rejected and wrong    {scoring['rejected_and_wrong']}"]
        if scoring["rejected_but_right"]:
            lines.append("  (matches being thrown away -- the thresholds are too strict)")
        if scoring["accepted_but_wrong"]:
            lines.append("  (noise getting through -- the thresholds are too loose)")
    return "\n".join(lines)
