"""
Expedition mode: import and track a Spansh Fleet Carrier route.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

STATE_FILENAME = "expedition_state.json"

# Spansh Fleet Carrier Router CSV (website export):
# System Name,Distance,Distance Remaining,Fuel Used,Icy Ring,Pristine,Restock Tritium
#
# Spansh Tools "export route" CSV:
# Done,System Name,Is Waypoint,Distance (LY),Remaining (LY),...,Icy ring,Restock?,...
_SYSTEM_HEADERS = ("system name", "system", "name")
_DISTANCE_HEADERS = ("distance", "distance (ly)")
_REMAINING_HEADERS = ("distance remaining", "remaining (ly)")
_FUEL_HEADERS = ("fuel used", "fuel used (t)")
_ICY_HEADERS = ("icy ring", "icy_ring")
_PRISTINE_HEADERS = ("pristine",)
_RESTOCK_HEADERS = ("restock tritium", "restock?", "restock")
_DONE_HEADERS = ("done",)


@dataclass
class Waypoint:
    system: str
    distance: Optional[float] = None
    distance_remaining: Optional[float] = None
    fuel_used: Optional[float] = None
    icy_ring: bool = False
    pristine: bool = False
    restock: bool = False
    done: bool = False


@dataclass
class ExpeditionState:
    active: bool = False
    source_name: str = ""
    waypoints: list[Waypoint] = field(default_factory=list)
    # Index of the next system we still need to reach (0 = origin / first row).
    next_index: int = 0
    completed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "source_name": self.source_name,
            "next_index": self.next_index,
            "completed": self.completed,
            "waypoints": [asdict(wp) for wp in self.waypoints],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExpeditionState":
        waypoints = [
            Waypoint(**wp) for wp in data.get("waypoints", []) if isinstance(wp, dict) and wp.get("system")
        ]
        return cls(
            active=bool(data.get("active")),
            source_name=str(data.get("source_name") or ""),
            waypoints=waypoints,
            next_index=int(data.get("next_index") or 0),
            completed=bool(data.get("completed")),
        )


def _norm_header(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _find_column(fieldnames: list[str], candidates: tuple[str, ...]) -> Optional[str]:
    normalized = {_norm_header(name): name for name in fieldnames if name}
    for candidate in candidates:
        if candidate in normalized:
            return normalized[candidate]
    return None


def _parse_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_bool(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "restock"}


def _parse_done(value: Any) -> bool:
    """Parse Spansh Tools Done column (■/□) or yes/no style flags."""
    text = str(value or "").strip().lower()
    if not text:
        return False
    if text in {"□", "☐", "no", "n", "false", "0"}:
        return False
    if text in {"■", "☑", "✓", "yes", "y", "true", "1", "done", "x"}:
        return True
    # Filled square and similar glyphs often survive as non-ascii.
    if any(ch in str(value) for ch in ("■", "☑", "✓")):
        return True
    return False


def _parse_icy_ring(value: Any) -> tuple[bool, bool]:
    """Return (icy_ring, pristine) from Spansh icy-ring cells."""
    text = str(value or "").strip().lower()
    if text in {"pristine"}:
        return True, True
    if text in {"1", "true", "yes", "y"}:
        return True, False
    return False, False


def _is_summary_row(system: str, row: dict[str, Any]) -> bool:
    system_norm = _norm_header(system)
    if system_norm == "total" or system_norm.startswith("total "):
        return True
    if re.fullmatch(r"\d+\s+systems?", system_norm):
        return True
    # Spansh Tools footer uses Done == "Total"
    for key, value in row.items():
        if _norm_header(str(key or "")) == "done" and _norm_header(str(value or "")) == "total":
            return True
    return False


def parse_spansh_fc_csv(path: str) -> list[Waypoint]:
    """Parse a Spansh Fleet Carrier / Spansh Tools route CSV into waypoints."""
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(handle, dialect=dialect)
        if not reader.fieldnames:
            raise ValueError("CSV has no header row")

        fieldnames = [name for name in reader.fieldnames if name]
        system_col = _find_column(fieldnames, _SYSTEM_HEADERS)
        if not system_col:
            raise ValueError(
                "Could not find a system name column "
                "(expected 'System Name' from Spansh Fleet Carrier CSV)"
            )

        distance_col = _find_column(fieldnames, _DISTANCE_HEADERS)
        remaining_col = _find_column(fieldnames, _REMAINING_HEADERS)
        fuel_col = _find_column(fieldnames, _FUEL_HEADERS)
        icy_col = _find_column(fieldnames, _ICY_HEADERS)
        pristine_col = _find_column(fieldnames, _PRISTINE_HEADERS)
        restock_col = _find_column(fieldnames, _RESTOCK_HEADERS)
        done_col = _find_column(fieldnames, _DONE_HEADERS)

        waypoints: list[Waypoint] = []
        for row in reader:
            system = str(row.get(system_col) or "").strip()
            if not system or _is_summary_row(system, row):
                continue

            icy_ring = False
            pristine = False
            if icy_col:
                icy_ring, pristine_from_icy = _parse_icy_ring(row.get(icy_col))
                pristine = pristine_from_icy
            if pristine_col:
                pristine = pristine or _parse_bool(row.get(pristine_col))

            waypoints.append(
                Waypoint(
                    system=system,
                    distance=_parse_float(row.get(distance_col)) if distance_col else None,
                    distance_remaining=(
                        _parse_float(row.get(remaining_col)) if remaining_col else None
                    ),
                    fuel_used=_parse_float(row.get(fuel_col)) if fuel_col else None,
                    icy_ring=icy_ring,
                    pristine=pristine,
                    restock=_parse_bool(row.get(restock_col)) if restock_col else False,
                    done=_parse_done(row.get(done_col)) if done_col else False,
                )
            )

    if len(waypoints) < 2:
        raise ValueError("Route needs at least an origin and one destination system")
    return waypoints


def _systems_match(left: Optional[str], right: Optional[str]) -> bool:
    if not left or not right:
        return False
    return left.strip().lower() == right.strip().lower()


class ExpeditionManager:
    """Owns expedition state, persistence, and progress helpers."""

    def __init__(
        self,
        plugin_dir: str,
        on_change: Optional[Callable[[], None]] = None,
    ) -> None:
        self.plugin_dir = plugin_dir
        self.on_change = on_change
        self.state = ExpeditionState()
        self.load()

    @property
    def state_path(self) -> str:
        return os.path.join(self.plugin_dir, STATE_FILENAME)

    def _notify(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                logger.exception("Expedition on_change callback failed")

    def load(self) -> None:
        path = self.state_path
        if not os.path.isfile(path):
            self.state = ExpeditionState()
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("Invalid expedition state file")
            self.state = ExpeditionState.from_dict(data)
            self._recompute_flags()
        except Exception:
            logger.exception("Failed to load expedition state from %s", path)
            self.state = ExpeditionState()

    def save(self) -> None:
        path = self.state_path
        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(self.state.to_dict(), handle, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            logger.exception("Failed to save expedition state to %s", path)
            try:
                if os.path.isfile(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass

    def _recompute_flags(self) -> None:
        if not self.state.waypoints:
            self.state.active = False
            self.state.completed = False
            self.state.next_index = 0
            return

        total = len(self.state.waypoints)
        self.state.next_index = max(0, min(self.state.next_index, total - 1))

        # Keep done flags consistent with next_index.
        for idx, waypoint in enumerate(self.state.waypoints):
            waypoint.done = idx < self.state.next_index

        if self.state.next_index >= total - 1 and self.state.waypoints[-1].done:
            self.state.completed = True
            self.state.active = False
        elif self.state.next_index >= total - 1 and _systems_match(
            self.state.waypoints[-1].system,
            self.state.waypoints[self.state.next_index].system,
        ):
            # Sitting on / pointing at final system without completion yet.
            self.state.completed = False
            self.state.active = True
        else:
            self.state.completed = False
            if self.state.waypoints:
                self.state.active = True

    def _apply_progress_from_done_flags(self) -> None:
        """Set next_index from waypoint.done flags already present on import."""
        next_idx = len(self.state.waypoints) - 1
        for idx, waypoint in enumerate(self.state.waypoints):
            if not waypoint.done:
                next_idx = idx
                break
        else:
            # All marked done => expedition complete.
            self.state.next_index = max(0, len(self.state.waypoints) - 1)
            self.state.completed = True
            self.state.active = False
            return
        self.state.next_index = next_idx
        self.state.completed = False
        self.state.active = True

    def resume_from_system(self, system: Optional[str]) -> bool:
        """
        Align progress to a known current system.

        Marks that system and all prior waypoints done, and sets next to the
        following hop (or completes if this is the final system).
        """
        if not system or not self.state.waypoints:
            return False

        match_idx = None
        for idx, waypoint in enumerate(self.state.waypoints):
            if _systems_match(waypoint.system, system):
                match_idx = idx
                break
        if match_idx is None:
            return False

        for idx, waypoint in enumerate(self.state.waypoints):
            waypoint.done = idx <= match_idx

        if match_idx >= len(self.state.waypoints) - 1:
            self.state.next_index = len(self.state.waypoints) - 1
            self.state.completed = True
            self.state.active = False
        else:
            self.state.next_index = match_idx + 1
            self.state.completed = False
            self.state.active = True

        self.save()
        self._notify()
        return True

    def import_csv(
        self,
        path: str,
        current_system: Optional[str] = None,
    ) -> ExpeditionState:
        waypoints = parse_spansh_fc_csv(path)
        # Ensure origin is reachable progress even when Done column is absent.
        if not any(wp.done for wp in waypoints):
            waypoints[0].done = True

        self.state = ExpeditionState(
            active=True,
            source_name=os.path.basename(path),
            waypoints=waypoints,
            next_index=1 if len(waypoints) > 1 else 0,
            completed=False,
        )
        self._apply_progress_from_done_flags()

        # Live location wins over CSV Done flags when we can match it.
        if current_system:
            resumed = self.resume_from_system(current_system)
            if resumed:
                logger.info(
                    "Resumed expedition at current system '%s' (next_index=%s)",
                    current_system,
                    self.state.next_index,
                )
            else:
                logger.info(
                    "Current system '%s' not found in imported route; using CSV Done flags",
                    current_system,
                )

        self._recompute_flags()
        self.save()
        self._notify()
        return self.state

    def clear(self) -> None:
        self.state = ExpeditionState()
        try:
            if os.path.isfile(self.state_path):
                os.remove(self.state_path)
        except OSError:
            logger.exception("Failed removing expedition state file")
        self._notify()

    @property
    def is_active(self) -> bool:
        return bool(self.state.active and self.state.waypoints and not self.state.completed)

    def final_destination(self) -> Optional[str]:
        if not self.state.waypoints:
            return None
        return self.state.waypoints[-1].system

    def next_waypoint(self) -> Optional[Waypoint]:
        if not self.is_active:
            return None
        idx = self.state.next_index
        if idx < 0 or idx >= len(self.state.waypoints):
            return None
        # If next_index is last and not done, that is the final destination to reach.
        return self.state.waypoints[idx]

    def hops_total(self) -> int:
        return max(0, len(self.state.waypoints) - 1)

    def hops_done(self) -> int:
        if not self.state.waypoints:
            return 0
        if self.state.completed:
            return self.hops_total()
        # next_index 1 (pointing at first destination) => 0 hops completed.
        return max(0, min(self.state.next_index - 1, self.hops_total()))

    def distance_remaining(self) -> Optional[float]:
        nxt = self.next_waypoint()
        if nxt and nxt.distance_remaining is not None:
            return nxt.distance_remaining
        # Fallback: sum remaining hop distances from next_index onward.
        if not self.state.waypoints or self.state.next_index >= len(self.state.waypoints):
            return None
        total = 0.0
        found = False
        for waypoint in self.state.waypoints[self.state.next_index :]:
            if waypoint.distance is not None:
                total += waypoint.distance
                found = True
        return total if found else None

    def status_lines(self) -> dict[str, str]:
        if self.state.completed and self.state.waypoints:
            return {
                "status": "Expedition complete",
                "final": self.final_destination() or "Unknown",
                "next": "-",
                "progress": f"{self.hops_total()} / {self.hops_total()}",
                "remaining": "0 LY",
            }
        if not self.is_active:
            return {
                "status": "No expedition",
                "final": "-",
                "next": "-",
                "progress": "-",
                "remaining": "-",
            }

        nxt = self.next_waypoint()
        remaining = self.distance_remaining()
        remaining_text = f"{remaining:.1f} LY" if remaining is not None else "Unknown"
        return {
            "status": "Following route",
            "final": self.final_destination() or "Unknown",
            "next": nxt.system if nxt else "Unknown",
            "progress": f"{self.hops_done()} / {self.hops_total()}",
            "remaining": remaining_text,
        }

    def discord_fields(self) -> list[dict[str, Any]]:
        """Embed fields describing expedition context."""
        if not self.is_active and not (self.state.completed and self.state.waypoints):
            return []

        lines = self.status_lines()
        nxt = self.next_waypoint()
        fields = [
            {"name": "Expedition", "value": lines["status"], "inline": True},
            {"name": "Final destination", "value": lines["final"], "inline": True},
            {"name": "Progress", "value": lines["progress"], "inline": True},
            {"name": "Next waypoint", "value": lines["next"], "inline": True},
            {"name": "Distance left", "value": lines["remaining"], "inline": True},
        ]
        if nxt and nxt.restock:
            fields.append(
                {
                    "name": "Restock",
                    "value": "Tritium restock flagged at/before this hop",
                    "inline": True,
                }
            )
        return fields

    def advance_to_system(self, system: Optional[str]) -> dict[str, Any]:
        """
        Mark progress when a schedule/arrival system matches a future waypoint.

        Returns a result dict:
          matched, advanced, completed, waypoint
        """
        result = {
            "matched": False,
            "advanced": False,
            "completed": False,
            "waypoint": None,
            "off_route": False,
        }
        if not system or not self.state.waypoints or not self.state.active:
            return result

        # Find the furthest matching waypoint at/after current next_index.
        match_idx = None
        for idx in range(self.state.next_index, len(self.state.waypoints)):
            if _systems_match(self.state.waypoints[idx].system, system):
                match_idx = idx
                break

        if match_idx is None:
            # If it matches an earlier waypoint, ignore; else optional off-route.
            for idx, waypoint in enumerate(self.state.waypoints):
                if _systems_match(waypoint.system, system):
                    result["matched"] = True
                    return result
            result["off_route"] = True
            return result

        result["matched"] = True
        result["waypoint"] = deepcopy(self.state.waypoints[match_idx])

        if match_idx < self.state.next_index:
            return result

        # Reaching waypoint N means all before it are done, and next is N+1.
        new_next = match_idx + 1
        if new_next >= len(self.state.waypoints):
            # Reached final destination.
            for waypoint in self.state.waypoints:
                waypoint.done = True
            self.state.next_index = len(self.state.waypoints) - 1
            self.state.completed = True
            self.state.active = False
            result["advanced"] = True
            result["completed"] = True
        else:
            if new_next != self.state.next_index:
                result["advanced"] = True
            self.state.next_index = new_next
            self._recompute_flags()

        if result["advanced"] or result["completed"]:
            self.save()
            self._notify()
        return result
