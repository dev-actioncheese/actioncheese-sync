# SPDX-License-Identifier: GPL-3.0-or-later
"""The sync report.

Sync is a heuristic process, so the tool has to be able to explain itself: which
clips matched, how confidently, what drifted, and precisely why anything that
ended up on an unsynced track failed to match.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .timebase import Rate, format_timecode


@dataclass(slots=True)
class SyncReport:
    source: Optional[str]
    sequence_name: str
    rate: Rate
    unit_count: int
    reference_track: Optional[str]
    last_synced_end_frames: int
    sequence_duration_frames: int
    placements: list[dict] = field(default_factory=list)
    pairs: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    drift: list[dict] = field(default_factory=list)
    corroborated: list[dict] = field(default_factory=list)
    stated_disagreements: list[dict] = field(default_factory=list)
    """Where clips that arrived already synced were moved by the audio. A
    direct measure of how accurate the stated times were."""
    removed_tracks: list[str] = field(default_factory=list)
    markers_total: int = 0
    markers_moved: int = 0
    gaps_removed: int = 0
    frames_reclaimed: int = 0
    new_tracks: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ counts
    def _count(self, status: str) -> int:
        return sum(1 for p in self.placements if p["status"] == status)

    @property
    def synced_count(self) -> int:
        return self._count("synced")

    @property
    def island_count(self) -> int:
        return self._count("island")

    @property
    def unsynced_count(self) -> int:
        return self._count("unsynced")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "sequence": {
                "name": self.sequence_name,
                "timebase": self.rate.timebase,
                "ntsc": self.rate.ntsc,
                "fps": round(self.rate.fps_float, 6),
                "duration_frames": self.sequence_duration_frames,
                "duration_timecode": format_timecode(self.sequence_duration_frames, self.rate),
            },
            "summary": {
                "units": self.unit_count,
                "synced": self.synced_count,
                "islands": self.island_count,
                "unsynced": self.unsynced_count,
                "reference_track": self.reference_track,
                "last_synced_end_frames": self.last_synced_end_frames,
                "last_synced_end_timecode": format_timecode(self.last_synced_end_frames, self.rate),
                "removed_tracks": self.removed_tracks,
                "gaps_removed": self.gaps_removed,
                "frames_reclaimed": self.frames_reclaimed,
            },
            "new_tracks": self.new_tracks,
            "placements": self.placements,
            "drift": self.drift,
            "corroborated": self.corroborated,
            "conflicts": self.conflicts,
            "pairs": self.pairs,
            "warnings": self.warnings,
        }

    def write_json(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    # ------------------------------------------------------------------ render
    def render(self) -> str:
        rate = self.rate
        lines: list[str] = []
        lines.append(f"Sequence : {self.sequence_name}  ({rate}, {self.unit_count} clips)")
        lines.append(f"Reference: {self.reference_track or 'n/a'}")
        lines.append(
            f"Result   : {self.synced_count} synced, "
            f"{self.island_count} in separate synced groups, "
            f"{self.unsynced_count} unmatched"
        )

        if self.placements:
            lines.append("")
            lines.append("  clip                       status     start          shift")
            lines.append("  " + "-" * 62)
            for placement in self.placements:
                shift_frames = placement["shift_frames"]
                shift_seconds = rate.frames_to_seconds(shift_frames)
                lines.append(
                    f"  {placement['label'][:26]:<26} {placement['status']:<10} "
                    f"{format_timecode(placement['start_frames'], rate):<14} "
                    f"{shift_seconds:+8.3f}s"
                )

        measured = [d for d in self.drift if d.get("measured")]
        if measured:
            lines.append("")
            lines.append("Drift:")
            for entry in measured:
                applied = " -> corrected" if entry.get("corrected") else ""
                lines.append(
                    f"  {entry['label']:<26} {entry['drift_ppm']:+8.2f} ppm "
                    f"({entry['total_error_seconds'] * 1000:.0f} ms over the clip){applied}"
                )

        if self.gaps_removed:
            reclaimed = rate.frames_to_seconds(self.frames_reclaimed)
            lines.append("")
            lines.append(
                f"Closed {self.gaps_removed} empty gap(s), reclaiming "
                f"{format_timecode(self.frames_reclaimed, rate)} ({reclaimed / 60:.1f} min)"
            )

        if self.removed_tracks:
            lines.append("")
            lines.append(f"Removed {len(self.removed_tracks)} empty track(s): "
                         + ", ".join(self.removed_tracks))

        if self.new_tracks:
            lines.append("")
            lines.append("Unmatched clips moved to new tracks:")
            for old, new in self.new_tracks.items():
                lines.append(f"  {old} -> {new}")
            lines.append(
                f"  starting after {format_timecode(self.last_synced_end_frames, rate)}"
            )

        unmatched = [p for p in self.placements if p["status"] == "unsynced"]
        if unmatched:
            lines.append("")
            lines.append("Why clips did not match:")
            for placement in unmatched:
                lines.append(f"  {placement['label']:<26} {placement.get('reason', 'unknown')}")

        if self.conflicts:
            lines.append("")
            lines.append(f"Conflicting matches ({len(self.conflicts)}) - inspect these:")
            for conflict in self.conflicts[:10]:
                lines.append(
                    f"  units {conflict['a']}<->{conflict['b']}: "
                    f"measured {conflict['expected_delta']:+.3f}s, "
                    f"solved {conflict['solved_delta']:+.3f}s"
                )

        if self.markers_total:
            lines.append("")
            lines.append(
                f"Markers: {self.markers_total} kept, {self.markers_moved} moved with "
                "the clips they mark")

        if self.stated_disagreements:
            worst = max(abs(d["moved_by"]) for d in self.stated_disagreements)
            lines.append("")
            lines.append(
                f"Clips that arrived already synced, moved by the audio "
                f"({len(self.stated_disagreements)}, worst {worst:.3f}s):")
            for entry in sorted(self.stated_disagreements,
                                key=lambda d: -abs(d["moved_by"]))[:10]:
                lines.append(
                    f"  {entry['a'][:26]:<28} {entry['moved_by']:+.3f}s "
                    f"(stated {entry['stated_delta']:+.3f}s, "
                    f"used {entry['solved_delta']:+.3f}s)")
            lines.append("  Use --trust-synced to keep the stated timing instead.")

        if self.warnings:
            lines.append("")
            lines.append("Warnings:")
            for warning in self.warnings:
                lines.append(f"  {warning}")

        return "\n".join(lines)
