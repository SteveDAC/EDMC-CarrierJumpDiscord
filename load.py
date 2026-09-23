"""
EDMC-CarrierJumpDiscord

Publishes fleet carrier and squadron carrier jump schedule, cancel, and
optional arrival events to Discord via webhook or bot token.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import queue
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import myNotebook as nb
from config import appname, config


def _load_local_module(module_name: str):
    """Load a sibling .py module from this plugin folder (hyphenated folder-safe)."""
    import sys

    path = os.path.join(os.path.dirname(__file__), f"{module_name}.py")
    full_name = f"{plugin_name_guess()}.{module_name}"
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # Required so dataclasses / typing in the sibling module resolve correctly.
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


def plugin_name_guess() -> str:
    return os.path.basename(os.path.dirname(__file__))

# ---------------------------------------------------------------------------
# Plugin identity / logging
# ---------------------------------------------------------------------------

PLUGIN_NAME = "Carrier Jump Discord"
__version__ = "1.7.0-dev"

plugin_name = os.path.basename(os.path.dirname(__file__))
logger = logging.getLogger(f"{appname}.{plugin_name}")
if not logger.hasHandlers():
    logger.setLevel(logging.INFO)
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(module)s:%(lineno)d:%(funcName)s: %(message)s"
        )
    )
    logger.addHandler(_handler)

expedition_mod = _load_local_module("expedition")
system_ac_mod = _load_local_module("system_autocomplete")

# ---------------------------------------------------------------------------
# Config keys (unique prefix to avoid clashes)
# ---------------------------------------------------------------------------

CFG_WEBHOOK = "edmc_cjd_webhook_url"
CFG_DELIVERY_MODE = "edmc_cjd_delivery_mode"
CFG_BOT_TOKEN = "edmc_cjd_bot_token"
CFG_CHANNEL_ID = "edmc_cjd_channel_id"
CFG_ENABLED = "edmc_cjd_enabled"
CFG_NOTIFY_REQUEST = "edmc_cjd_notify_request"
CFG_NOTIFY_CANCEL = "edmc_cjd_notify_cancel"
CFG_NOTIFY_ARRIVAL = "edmc_cjd_notify_arrival"
CFG_NOTIFY_EXPEDITION_COMPLETE = "edmc_cjd_notify_expedition_complete"
CFG_NOTIFY_FLEET = "edmc_cjd_notify_fleet"
CFG_NOTIFY_SQUADRON = "edmc_cjd_notify_squadron"
# Legacy single-carrier overrides (treated as fleet carrier overrides).
CFG_CARRIER_NAME = "edmc_cjd_carrier_name"
CFG_CARRIER_CALLSIGN = "edmc_cjd_carrier_callsign"
CFG_SQUADRON_NAME = "edmc_cjd_squadron_name"
CFG_SQUADRON_CALLSIGN = "edmc_cjd_squadron_callsign"
CFG_MENTION = "edmc_cjd_mention"
CFG_CARRIERS_JSON = "edmc_cjd_carriers_json"
CFG_EXPEDITION_COLLAPSED = "edmc_cjd_expedition_collapsed"

DELIVERY_WEBHOOK = "webhook"
DELIVERY_BOT = "bot"
DISCORD_API_BASE = "https://discord.com/api/v10"

WEBHOOK_RE = re.compile(
    r"^https://(?:discord(?:app)?\.com|discord\.com)/api/webhooks/\d+/[\w-]+$",
    re.IGNORECASE,
)
CHANNEL_ID_RE = re.compile(r"^\d{15,25}$")
BOT_TOKEN_RE = re.compile(r"^[\w-]+\.[\w-]+\.[\w-]+$")

# Typical fleet/squadron carrier pad lockdown is ~3m20s before departure.
LOCKDOWN_BEFORE_DEPARTURE = timedelta(minutes=3, seconds=20)
# Adhoc pre-announce: backstop against rapid re-posts after a successful send.
ADHOC_PREANNOUNCE_COOLDOWN = timedelta(minutes=10)
ADHOC_STATE_FILENAME = "adhoc_preannounce_state.json"

KIND_FLEET = "fleet"
KIND_SQUADRON = "squadron"
KIND_UNKNOWN = "unknown"

KIND_LABELS = {
    KIND_FLEET: "Fleet Carrier",
    KIND_SQUADRON: "Squadron Carrier",
    KIND_UNKNOWN: "Carrier",
}

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_webhook_var: Optional[tk.StringVar] = None
_delivery_mode_var: Optional[tk.StringVar] = None
_bot_token_var: Optional[tk.StringVar] = None
_channel_id_var: Optional[tk.StringVar] = None
_enabled_var: Optional[tk.BooleanVar] = None
_notify_request_var: Optional[tk.BooleanVar] = None
_notify_cancel_var: Optional[tk.BooleanVar] = None
_notify_arrival_var: Optional[tk.BooleanVar] = None
_notify_expedition_complete_var: Optional[tk.BooleanVar] = None
_notify_fleet_var: Optional[tk.BooleanVar] = None
_notify_squadron_var: Optional[tk.BooleanVar] = None
_fleet_name_var: Optional[tk.StringVar] = None
_fleet_callsign_var: Optional[tk.StringVar] = None
_squadron_name_var: Optional[tk.StringVar] = None
_squadron_callsign_var: Optional[tk.StringVar] = None
_mention_var: Optional[tk.StringVar] = None

_status_label: Optional[tk.Label] = None
_prefs_status: Optional[tk.Label] = None
_tracked_label: Optional[tk.Label] = None
_expedition_status_label: Optional[tk.Label] = None
_expedition_carrier_label: Optional[tk.Label] = None
_expedition_final_label: Optional[tk.Label] = None
_expedition_next_label: Optional[tk.Label] = None
_expedition_progress_label: Optional[tk.Label] = None
_expedition_remaining_label: Optional[tk.Label] = None
_expedition_detail_frame: Optional[tk.Frame] = None
_expedition_collapse_btn: Optional[tk.Button] = None
_expedition_announce_btn: Optional[tk.Button] = None
_expedition_collapsed: bool = False
_app_frame: Optional[tk.Frame] = None

_current_system: Optional[str] = None
_current_station: Optional[str] = None
_last_cmdr: Optional[str] = None
# carrier_id -> {name, callsign, kind, carrier_type, system}
_carriers: dict[int, dict[str, str]] = {}
# carrier_id -> latest CarrierJumpRequest entry
_pending_jumps: dict[int, dict[str, Any]] = {}
# Deduplicate Discord posts when the game re-emits the same jump events.
# carrier_id -> DepartureTime last announced as scheduled
_last_notified_request: dict[int, str] = {}
# carrier_id -> DepartureTime (or marker) last announced as cancelled
_last_notified_cancel: dict[int, str] = {}
# carrier_id -> "StarSystem|journal_timestamp" last announced as arrived
_last_notified_arrival: dict[int, str] = {}

_worker_queue: queue.Queue[Optional[dict[str, Any]]] = queue.Queue()
_worker_thread: Optional[threading.Thread] = None
_stop_worker = threading.Event()

_plugin_dir: str = os.path.dirname(__file__)
_expedition: Optional[Any] = None
# Last successful adhoc pre-announce (persisted lightly for anti-spam).
_adhoc_preannounce: dict[str, Any] = {}


def _is_duplicate_notification(
    store: dict[int, str],
    carrier_id: Optional[int],
    token: Optional[str],
) -> bool:
    """Return True if we already notified for this carrier_id + token."""
    if carrier_id is None or not token:
        return False
    if store.get(carrier_id) == token:
        return True
    store[carrier_id] = token
    return False


def _config_bool(key: str, default: bool = True) -> bool:
    try:
        value = config.get_bool(key)
    except Exception:
        return default
    if value is None:
        return default
    return bool(value)


def _config_str(key: str, default: str = "") -> str:
    try:
        value = config.get_str(key)
    except Exception:
        return default
    if value is None:
        return default
    return str(value)


def _set_status(text: str, color: str = "white") -> None:
    if _status_label is None:
        return
    _status_label["text"] = text
    _status_label["foreground"] = color


def _parse_journal_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except Exception:
        logger.exception("Failed to parse journal timestamp: %s", value)
        return None


def _format_discord_time(dt: Optional[datetime]) -> Optional[str]:
    """Format a time with Discord dynamic timestamps.

    Discord renders <t:unix:style> in each viewer's local timezone.
    Styles used:
      f - short date/time (e.g. 19 September 2026 12:46)
      R - relative (e.g. in 15 minutes)
    """
    if dt is None:
        return None
    unix = int(dt.timestamp())
    return f"<t:{unix}:f> (<t:{unix}:R>)"


def _normalize_carrier_id(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _detect_kind(carrier_type: Any, explicit_kind: Optional[str] = None) -> str:
    if explicit_kind in (KIND_FLEET, KIND_SQUADRON, KIND_UNKNOWN):
        return explicit_kind

    text = str(carrier_type or "").strip().lower().replace(" ", "").replace("_", "").replace("-", "")
    if "squadron" in text or "javelin" in text:
        return KIND_SQUADRON
    if "fleet" in text or text in ("carrier", "drakecarrier"):
        return KIND_FLEET
    return KIND_UNKNOWN


def _kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, KIND_LABELS[KIND_UNKNOWN])


def _override_for_kind(kind: str) -> tuple[str, str]:
    if kind == KIND_SQUADRON:
        return (
            _config_str(CFG_SQUADRON_NAME).strip(),
            _config_str(CFG_SQUADRON_CALLSIGN).strip(),
        )
    # Fleet and unknown use the fleet/legacy override fields.
    return (
        _config_str(CFG_CARRIER_NAME).strip(),
        _config_str(CFG_CARRIER_CALLSIGN).strip(),
    )


def _load_carriers_from_config() -> None:
    global _carriers
    raw = _config_str(CFG_CARRIERS_JSON).strip()
    if not raw:
        _carriers = {}
        return

    try:
        data = json.loads(raw)
    except Exception:
        logger.exception("Failed to parse saved carrier cache")
        _carriers = {}
        return

    loaded: dict[int, dict[str, str]] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            carrier_id = _normalize_carrier_id(key)
            if carrier_id is None or not isinstance(value, dict):
                continue
            loaded[carrier_id] = {
                "name": str(value.get("name") or "").strip(),
                "callsign": str(value.get("callsign") or "").strip(),
                "kind": _detect_kind(value.get("carrier_type"), str(value.get("kind") or "")),
                "carrier_type": str(value.get("carrier_type") or "").strip(),
                "system": str(value.get("system") or "").strip(),
            }
    _carriers = loaded


def _save_carriers_to_config() -> None:
    payload = {
        str(carrier_id): {
            "name": info.get("name", ""),
            "callsign": info.get("callsign", ""),
            "kind": info.get("kind", KIND_UNKNOWN),
            "carrier_type": info.get("carrier_type", ""),
            "system": info.get("system", ""),
        }
        for carrier_id, info in _carriers.items()
    }
    try:
        config.set(CFG_CARRIERS_JSON, json.dumps(payload, separators=(",", ":")))
    except Exception:
        logger.exception("Failed to save carrier cache")


def _carrier_record(carrier_id: Optional[int]) -> dict[str, str]:
    if carrier_id is None:
        return {
            "name": "",
            "callsign": "",
            "kind": KIND_UNKNOWN,
            "carrier_type": "",
            "system": "",
        }
    return _carriers.get(
        carrier_id,
        {
            "name": "",
            "callsign": "",
            "kind": KIND_UNKNOWN,
            "carrier_type": "",
            "system": "",
        },
    )


def _notify_allowed_for_carrier(carrier_id: Optional[int]) -> bool:
    """Whether Discord posts are enabled for this carrier's kind."""
    kind = _carrier_record(carrier_id).get("kind") or KIND_UNKNOWN
    if kind == KIND_SQUADRON:
        return _config_bool(CFG_NOTIFY_SQUADRON, True)
    # Fleet and unknown (not yet classified) use the fleet toggle.
    return _config_bool(CFG_NOTIFY_FLEET, True)


def _carrier_display(carrier_id: Optional[int] = None, kind_hint: Optional[str] = None) -> str:
    info = _carrier_record(carrier_id)
    kind = kind_hint or info.get("kind") or KIND_UNKNOWN
    override_name, override_callsign = _override_for_kind(kind)

    name = override_name or info.get("name") or ""
    callsign = override_callsign or info.get("callsign") or ""

    if name and callsign:
        return f"{name} ({callsign})"
    if name or callsign:
        return name or callsign
    return _kind_label(kind)


def _tracked_summary() -> str:
    if not _carriers:
        return "No carriers learned yet"

    parts: list[str] = []
    for carrier_id, info in sorted(_carriers.items(), key=lambda item: item[1].get("kind", "")):
        label = _kind_label(info.get("kind") or KIND_UNKNOWN)
        display = _carrier_display(carrier_id)
        system = str(info.get("system") or "").strip()
        if system:
            parts.append(f"{label}: {display} @ {system}")
        else:
            parts.append(f"{label}: {display}")
    return " | ".join(parts)


def _refresh_tracked_label() -> None:
    """Update prefs tracked-carriers text if that prefs widget still exists."""
    global _tracked_label
    label = _tracked_label
    if label is None:
        return
    try:
        # Prefs widgets are destroyed when Settings closes; a stale reference
        # must not break journal handling / Discord posts.
        if not label.winfo_exists():
            _tracked_label = None
            return
        label["text"] = _tracked_summary()
    except tk.TclError:
        _tracked_label = None
        logger.debug("Tracked carriers prefs label is gone; clearing reference")


def _upsert_carrier(
    carrier_id: Optional[int],
    *,
    name: Optional[str] = None,
    callsign: Optional[str] = None,
    carrier_type: Any = None,
    kind_hint: Optional[str] = None,
    system: Optional[str] = None,
) -> Optional[int]:
    if carrier_id is None:
        return None

    current = _carrier_record(carrier_id)
    current_kind = current.get("kind") or KIND_UNKNOWN
    detected = _detect_kind(carrier_type, None)

    # Preserve a known fleet/squadron kind when the incoming type is generic
    # (CarrierJump often reports StationType=FleetCarrier for both).
    if kind_hint in (KIND_FLEET, KIND_SQUADRON):
        kind = kind_hint
    elif detected == KIND_SQUADRON:
        kind = KIND_SQUADRON
    elif current_kind in (KIND_FLEET, KIND_SQUADRON) and detected in (
        KIND_UNKNOWN,
        KIND_FLEET,
    ):
        kind = current_kind
    elif detected != KIND_UNKNOWN:
        kind = detected
    else:
        kind = current_kind

    previous_system = str(current.get("system") or "").strip()
    new_system = (str(system).strip() if system else "") or previous_system

    updated = {
        "name": (str(name).strip() if name else "") or current.get("name", ""),
        "callsign": (str(callsign).strip() if callsign else "") or current.get("callsign", ""),
        "kind": kind,
        "carrier_type": (
            str(carrier_type).strip()
            if carrier_type
            else current.get("carrier_type", "")
        ),
        "system": new_system,
    }

    _carriers[carrier_id] = updated
    _save_carriers_to_config()
    _refresh_tracked_label()
    if new_system and previous_system and new_system.lower() != previous_system.lower():
        _maybe_clear_adhoc_on_carrier_moved(carrier_id, previous_system)
    _refresh_expedition_ui()
    return carrier_id


def _carrier_system(carrier_id: Optional[int]) -> Optional[str]:
    """Last-known star system for a tracked carrier, if any."""
    system = str(_carrier_record(carrier_id).get("system") or "").strip()
    return system or None


def _origin_for_carrier(
    carrier_id: Optional[int],
    *,
    fallback: Optional[str] = None,
) -> str:
    """
    Prefer the carrier's last-known system (remote jumps), else the CMDR's
    current system, else Unknown.
    """
    carrier_sys = _carrier_system(carrier_id)
    if carrier_sys:
        return carrier_sys
    player = (fallback if fallback is not None else _current_system) or ""
    player = str(player).strip()
    return player or "Unknown"


def _find_carrier_id_by_callsign(station_name: str) -> Optional[int]:
    needle = station_name.strip().upper()
    if not needle:
        return None

    for carrier_id, info in _carriers.items():
        callsign = (info.get("callsign") or "").strip().upper()
        if callsign and callsign == needle:
            return carrier_id

    fleet_cs = _config_str(CFG_CARRIER_CALLSIGN).strip().upper()
    squadron_cs = _config_str(CFG_SQUADRON_CALLSIGN).strip().upper()
    if fleet_cs and fleet_cs == needle:
        for carrier_id, info in _carriers.items():
            if info.get("kind") == KIND_FLEET:
                return carrier_id
    if squadron_cs and squadron_cs == needle:
        for carrier_id, info in _carriers.items():
            if info.get("kind") == KIND_SQUADRON:
                return carrier_id
    return None


def _resolve_event_carrier_id(entry: dict[str, Any]) -> Optional[int]:
    carrier_id = _normalize_carrier_id(entry.get("CarrierID") or entry.get("MarketID"))
    if carrier_id is not None:
        return carrier_id

    station_name = str(entry.get("StationName") or "").strip()
    if station_name:
        return _find_carrier_id_by_callsign(station_name)
    return None


def _discord_session():
    try:
        import timeout_session

        return timeout_session.new_session()
    except Exception:
        import requests

        return requests.Session()


def _delivery_mode() -> str:
    mode = _config_str(CFG_DELIVERY_MODE, DELIVERY_WEBHOOK).strip().lower()
    if mode == DELIVERY_BOT:
        return DELIVERY_BOT
    return DELIVERY_WEBHOOK


def _delivery_configured() -> tuple[bool, str]:
    """Return (ok, user-facing reason/status)."""
    mode = _delivery_mode()
    if mode == DELIVERY_BOT:
        token = _config_str(CFG_BOT_TOKEN).strip()
        channel_id = _config_str(CFG_CHANNEL_ID).strip()
        if not token:
            return False, "Bot token missing"
        if not BOT_TOKEN_RE.match(token):
            return False, "Bot token looks invalid"
        if not channel_id:
            return False, "Channel ID missing"
        if not CHANNEL_ID_RE.match(channel_id):
            return False, "Channel ID looks invalid"
        return True, "Bot ready"

    webhook = _config_str(CFG_WEBHOOK).strip()
    if not webhook:
        return False, "Webhook missing"
    if not WEBHOOK_RE.match(webhook):
        return False, "Webhook URL looks invalid"
    return True, "Webhook ready"


def _refresh_ready_status() -> None:
    if not _config_bool(CFG_ENABLED, True):
        _set_status("Disabled", "orange")
        _refresh_expedition_ui()
        return
    ok, message = _delivery_configured()
    if not ok:
        _set_status(message, "orange")
    elif _carriers:
        _set_status(f"Tracking {len(_carriers)} carrier(s)", "green")
    else:
        _set_status("Ready", "green")
    _refresh_expedition_ui()


def _post_via_webhook(payload: dict[str, Any]) -> tuple[bool, str]:
    webhook = _config_str(CFG_WEBHOOK).strip()
    if not webhook:
        return False, "Discord webhook URL is not configured"
    if not WEBHOOK_RE.match(webhook):
        return False, "Discord webhook URL looks invalid"

    session = _discord_session()
    response = session.post(
        webhook,
        data=json.dumps(payload),
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"EDMC-{plugin_name}/{__version__}",
        },
        timeout=15,
    )
    if 200 <= response.status_code < 300:
        return True, "Posted to Discord (webhook)"
    return False, f"Discord webhook HTTP {response.status_code}: {response.text[:200]}"


def _post_via_bot(payload: dict[str, Any]) -> tuple[bool, str]:
    token = _config_str(CFG_BOT_TOKEN).strip()
    channel_id = _config_str(CFG_CHANNEL_ID).strip()
    if not token:
        return False, "Discord bot token is not configured"
    if not BOT_TOKEN_RE.match(token):
        return False, "Discord bot token looks invalid"
    if not channel_id:
        return False, "Discord channel ID is not configured"
    if not CHANNEL_ID_RE.match(channel_id):
        return False, "Discord channel ID looks invalid"

    # Bot messages use the bot's own name/avatar; username is webhook-only.
    bot_payload = {
        key: value
        for key, value in payload.items()
        if key not in ("username", "avatar_url")
    }

    session = _discord_session()
    response = session.post(
        f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
        data=json.dumps(bot_payload),
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": f"EDMC-{plugin_name}/{__version__}",
        },
        timeout=15,
    )
    if 200 <= response.status_code < 300:
        return True, "Posted to Discord (bot)"
    return False, f"Discord bot HTTP {response.status_code}: {response.text[:200]}"


def _post_discord(payload: dict[str, Any]) -> tuple[bool, str]:
    ok, message = _delivery_configured()
    if not ok:
        return False, message

    try:
        if _delivery_mode() == DELIVERY_BOT:
            return _post_via_bot(payload)
        return _post_via_webhook(payload)
    except Exception as exc:
        logger.exception("Discord request failed")
        return False, f"Discord request failed: {exc}"


def _worker_loop() -> None:
    while not _stop_worker.is_set():
        try:
            item = _worker_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        if item is None:
            _worker_queue.task_done()
            break

        ok, message = _post_discord(item)
        if ok:
            logger.info(message)
        else:
            logger.error(message)
        _worker_queue.task_done()


def _enqueue_discord(payload: dict[str, Any]) -> None:
    if not _config_bool(CFG_ENABLED, True):
        logger.info("Plugin disabled; skipping Discord post")
        _set_status("Disabled", "orange")
        return

    ok, message = _delivery_configured()
    if not ok:
        logger.warning("Discord delivery not configured: %s", message)
        _set_status(message, "orange")
        return

    _worker_queue.put(payload)
    _set_status("Queued Discord post", "cyan")


def _embed_field(name: str, value: Any, inline: bool = True) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return {"name": name, "value": text, "inline": inline}


def _spacer_field() -> dict[str, Any]:
    # Full-width blank field; Discord uses this as a visual break between rows.
    return {"name": "\u200b", "value": "\u200b", "inline": False}


def _collect_fields(*fields: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    return [field for field in fields if field]


def _join_field_groups(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Concatenate field groups with spacer rows between non-empty groups."""
    joined: list[dict[str, Any]] = []
    for group in groups:
        if not group:
            continue
        if joined:
            joined.append(_spacer_field())
        joined.extend(group)
    return joined


def _refresh_expedition_ui() -> None:
    if _expedition is not None:
        pending = _expedition_pending_destination()
        lines = _expedition.status_lines(
            current_system=_current_system,
            pending_destination=pending,
        )
        # When collapsed, keep a bit of route context on the header line.
        header_status = lines["status"]
        if _expedition_collapsed and lines["status"] in (
            "Following route",
            "Unbound — assign a carrier",
        ):
            nxt = lines.get("next") or "-"
            progress = lines.get("progress") or "-"
            carrier = lines.get("carrier") or "-"
            header_status = f"{lines['status']} — {carrier} · next {nxt} ({progress})"

        mapping = (
            (_expedition_status_label, header_status),
            (_expedition_carrier_label, lines.get("carrier", "-")),
            (_expedition_final_label, lines["final"]),
            (_expedition_next_label, lines["next"]),
            (_expedition_progress_label, lines["progress"]),
            (_expedition_remaining_label, lines["remaining"]),
        )
        for label, text in mapping:
            if label is None:
                continue
            try:
                if label.winfo_exists():
                    label["text"] = text
            except tk.TclError:
                pass

    if _expedition_announce_btn is not None:
        try:
            if _expedition_announce_btn.winfo_exists():
                enabled = _can_announce_departure()
                _expedition_announce_btn.configure(
                    state=tk.NORMAL if enabled else tk.DISABLED
                )
        except tk.TclError:
            pass


def _is_expedition_announce_mode() -> bool:
    """True when Announce departure should use the expedition one-shot path."""
    if _expedition is None:
        return False
    if not _expedition.state.waypoints:
        return False
    if not _expedition.is_active:
        return False
    if _expedition.state.completed:
        return False
    return True


def _can_announce_pre_departure() -> bool:
    """Whether expedition-mode Announce departure is allowed."""
    if not _is_expedition_announce_mode():
        return False
    if _expedition is not None and _expedition.state.pre_announced:
        if not _allow_repeat_pre_announce():
            return False
    return True


def _can_announce_adhoc() -> bool:
    """Whether adhoc (non-expedition) Announce departure is allowed."""
    if _is_expedition_announce_mode():
        return False
    if not _carriers:
        return False
    if not _config_bool(CFG_ENABLED, True):
        return False
    ok, _message = _delivery_configured()
    if not ok:
        return False
    if _allow_repeat_pre_announce():
        return True
    return _adhoc_announce_allowed()


def _can_announce_departure() -> bool:
    """Whether the shared Announce departure button should be enabled."""
    if _is_expedition_announce_mode():
        return _can_announce_pre_departure()
    return _can_announce_adhoc()


def _expedition_pending_destination() -> Optional[str]:
    """In-flight jump destination for the carrier bound to the expedition."""
    if _expedition is None or not _expedition.is_bound:
        return None
    carrier_id = _expedition.state.carrier_id
    if carrier_id is None:
        return None
    pending = _pending_jumps.get(carrier_id)
    if not pending:
        return None
    dest = str(pending.get("SystemName") or "").strip()
    return dest or None


def _expedition_field_group(carrier_id: Optional[int] = None) -> list[dict[str, Any]]:
    if _expedition is None:
        return []
    if not _expedition.is_bound_to(carrier_id):
        return []
    return _expedition.discord_fields(
        current_system=_current_system,
        pending_destination=_expedition_pending_destination(),
    )


def _build_expedition_complete_payload(cmdr: Optional[str] = None) -> dict[str, Any]:
    lines = (
        _expedition.status_lines(current_system=_current_system) if _expedition else {}
    )
    final_dest = lines.get("final") or "Unknown"
    bound_id = _expedition.state.carrier_id if _expedition is not None else None
    kind = KIND_UNKNOWN
    if bound_id is not None:
        kind = _carrier_record(bound_id).get("kind") or KIND_UNKNOWN
    elif _expedition is not None and "squadron" in str(
        _expedition.state.carrier_label or ""
    ).lower():
        kind = KIND_SQUADRON

    carrier_name = _expedition_carrier_name()

    fields = _join_field_groups(
        _collect_fields(
            _embed_field("Carrier", carrier_name),
            _embed_field("Type", _kind_label(kind)),
            _embed_field(_cmdr_role_field_name(kind), _owner_label(cmdr)),
        ),
        _collect_fields(
            _embed_field("Final destination", final_dest),
            _embed_field("Progress", lines.get("progress")),
        ),
    )
    mention = _config_str(CFG_MENTION).strip()
    return {
        "username": "EDMC Carrier Jump",
        "content": mention or None,
        "embeds": [
            {
                "title": "Final destination reached",
                "description": (
                    f"**{carrier_name or 'Carrier'}** completed the route — "
                    f"arrived at **{final_dest}**.\n\u200b"
                ),
                "color": 0x9B59B6,
                "fields": fields,
                "footer": {"text": f"{PLUGIN_NAME} v{__version__}"},
            }
        ],
    }


def _build_expedition_pre_departure_payload(
    departure: datetime,
    cmdr: Optional[str] = None,
) -> dict[str, Any]:
    """Narrative embed announcing an upcoming expedition departure."""
    carrier_name = _expedition_carrier_name()
    bound_id = _expedition.state.carrier_id if _expedition is not None else None
    origin = _origin_for_carrier(bound_id)
    if origin == "Unknown" and _expedition is not None and _expedition.state.waypoints:
        origin = str(_expedition.state.waypoints[0].system or "").strip() or "Unknown"

    final_dest = "Unknown"
    distance_text = "Unknown"
    hops = 0
    if _expedition is not None:
        final_dest = _expedition.final_destination() or "Unknown"
        hops = _expedition.hops_total()
        remaining = _expedition.distance_remaining(
            current_system=_carrier_system(bound_id) or _current_system
        )
        if remaining is not None:
            distance_text = f"{remaining:.1f} LY"

    time_text = _format_discord_time(departure) or "soon"
    owner = _owner_label(cmdr) or "CMDR"

    description = (
        f"**{carrier_name}** will be departing from **{origin}** at {time_text} "
        f"and heading to **{final_dest}**. This will cover **{distance_text}** "
        f"and take **{hops}** jumps.\n\n"
        f"- {owner}\n\n"
        f"If you would like to join, please make your way to **{origin}** "
        f"before departure.\n\n"
        f"*(Departure time is approximate and potentially subject to change.)*"
    )

    mention = _config_str(CFG_MENTION).strip()
    return {
        "username": "EDMC Carrier Jump",
        "content": mention or None,
        "embeds": [
            {
                "title": "Carrier departing soon",
                "description": description,
                "color": 0x1ABC9C,
                "footer": {"text": f"{PLUGIN_NAME} v{__version__}"},
            }
        ],
    }


def _build_adhoc_pre_departure_payload(
    departure: datetime,
    *,
    carrier_id: Optional[int],
    destination: str,
    origin: Optional[str] = None,
    cmdr: Optional[str] = None,
) -> dict[str, Any]:
    """Narrative embed announcing an upcoming single (non-expedition) jump."""
    kind = KIND_UNKNOWN
    if carrier_id is not None:
        kind = _carrier_record(carrier_id).get("kind") or KIND_UNKNOWN
    carrier_name = _carrier_display(carrier_id, kind) if carrier_id is not None else ""
    if not carrier_name:
        carrier_name = _kind_label(kind)

    from_system = (origin or _origin_for_carrier(carrier_id)).strip() or "Unknown"
    dest = (destination or "").strip() or "Unknown"
    time_text = _format_discord_time(departure) or "soon"
    owner = _owner_label(cmdr) or "CMDR"

    description = (
        f"**{carrier_name}** will be departing from **{from_system}** at {time_text} "
        f"and heading to **{dest}**.\n\n"
        f"- {owner}\n\n"
        f"If you would like to join, please make your way to **{from_system}** "
        f"before departure.\n\n"
        f"*(Departure time is approximate and potentially subject to change.)*"
    )

    mention = _config_str(CFG_MENTION).strip()
    return {
        "username": "EDMC Carrier Jump",
        "content": mention or None,
        "embeds": [
            {
                "title": "Carrier departing soon",
                "description": description,
                "color": 0x1ABC9C,
                "footer": {"text": f"{PLUGIN_NAME} v{__version__}"},
            }
        ],
    }


def _handle_expedition_progress(
    system: Optional[str],
    cmdr: Optional[str] = None,
    *,
    carrier_id: Optional[int] = None,
    complete_on_final: bool = True,
) -> bool:
    """
    Advance expedition on arrival when the system matches the route.

    Returns True when an expedition-completion Discord post was queued, so the
    caller can skip a normal jump-arrival notify for the same event.
    """
    if _expedition is None or not system:
        return False
    if not _expedition.is_active:
        return False
    if not _expedition.is_bound_to(carrier_id):
        return False

    before_completed = bool(_expedition.state.completed)
    result = _expedition.advance_to_system(
        system,
        complete_on_final=complete_on_final,
    )
    if result.get("advanced") or result.get("completed"):
        nxt = _expedition.next_waypoint()
        logger.info(
            "Expedition advanced via %s on carrier %s (next=%s completed=%s complete_on_final=%s)",
            system,
            carrier_id,
            nxt.system if nxt else None,
            result.get("completed"),
            complete_on_final,
        )
        _set_status(
            "Final destination reached" if result.get("completed") else f"Route -> {system}",
            "green",
        )
    # Only announce completion when the carrier has actually arrived.
    if (
        complete_on_final
        and result.get("completed")
        and not before_completed
        and _config_bool(CFG_NOTIFY_EXPEDITION_COMPLETE, True)
        and _notify_allowed_for_carrier(carrier_id)
    ):
        _enqueue_discord(_build_expedition_complete_payload(cmdr=cmdr))
        return True
    return False


def _owner_label(cmdr: Optional[str]) -> Optional[str]:
    name = (cmdr or "").strip()
    if not name:
        return None
    if name.upper().startswith("CMDR "):
        return name
    return f"CMDR {name}"


def _remember_cmdr(cmdr: Optional[str]) -> None:
    """Cache the last-seen commander name for UI-driven Discord posts."""
    global _last_cmdr
    name = (cmdr or "").strip()
    if name:
        _last_cmdr = name


def _allow_repeat_pre_announce() -> bool:
    """Dev builds may re-send pre-departure announces for testing."""
    return "-dev" in __version__


def _adhoc_state_path() -> str:
    return os.path.join(_plugin_dir, ADHOC_STATE_FILENAME)


def _load_adhoc_preannounce_state() -> None:
    global _adhoc_preannounce
    path = _adhoc_state_path()
    if not os.path.isfile(path):
        _adhoc_preannounce = {}
        return
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            _adhoc_preannounce = data
        else:
            _adhoc_preannounce = {}
    except Exception:
        logger.exception("Failed loading adhoc pre-announce state from %s", path)
        _adhoc_preannounce = {}


def _save_adhoc_preannounce_state() -> None:
    path = _adhoc_state_path()
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(_adhoc_preannounce, handle, indent=2)
    except Exception:
        logger.exception("Failed saving adhoc pre-announce state to %s", path)


def _parse_iso_utc(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        value = datetime.fromisoformat(text)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    except Exception:
        return None


def _adhoc_locked_carrier_id() -> Optional[int]:
    return _normalize_carrier_id(_adhoc_preannounce.get("carrier_id"))


def _adhoc_route_key(
    carrier_id: Optional[int],
    origin: Optional[str],
    destination: Optional[str],
) -> tuple[Optional[int], str, str]:
    return (
        carrier_id,
        (origin or "").strip().lower(),
        (destination or "").strip().lower(),
    )


def _adhoc_cooldown_active() -> bool:
    posted = _parse_iso_utc(_adhoc_preannounce.get("last_posted_at"))
    if posted is None:
        return False
    return datetime.now(timezone.utc) < posted + ADHOC_PREANNOUNCE_COOLDOWN


def _adhoc_is_locked() -> bool:
    return bool(_adhoc_preannounce.get("locked"))


def _adhoc_announce_allowed(
    *,
    carrier_id: Optional[int] = None,
    origin: Optional[str] = None,
    destination: Optional[str] = None,
) -> bool:
    """
    Anti-spam gate for adhoc pre-announce.

    Without a concrete route (button enable): blocked only by the global cooldown.
    With a concrete route (confirm): also blocked when that same route is still locked
    or when re-posting the same route during the cooldown window.
    """
    if _allow_repeat_pre_announce():
        return True
    if not _adhoc_preannounce:
        return True

    if destination is None:
        return not _adhoc_cooldown_active()

    current_origin = (origin if origin is not None else _current_system) or ""
    locked_origin = str(_adhoc_preannounce.get("origin") or "").strip()
    locked_dest = str(_adhoc_preannounce.get("destination") or "").strip()
    locked_carrier = _adhoc_locked_carrier_id()
    proposed = _adhoc_route_key(carrier_id, current_origin, destination)
    locked = _adhoc_route_key(locked_carrier, locked_origin, locked_dest)

    if proposed == locked:
        if _adhoc_is_locked():
            return False
        if _adhoc_cooldown_active():
            return False
        return True

    # Different route: global cooldown still applies as a misclick backstop.
    return not _adhoc_cooldown_active()


def _mark_adhoc_preannounce(
    carrier_id: int,
    origin: str,
    destination: str,
) -> None:
    global _adhoc_preannounce
    _adhoc_preannounce = {
        "locked": True,
        "carrier_id": carrier_id,
        "origin": origin,
        "destination": destination,
        "last_posted_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_adhoc_preannounce_state()


def _clear_adhoc_preannounce_lock(
    *,
    carrier_id: Optional[int] = None,
    reason: str = "",
) -> None:
    """Clear the route lock (cooldown timestamp is kept)."""
    global _adhoc_preannounce
    if not _adhoc_preannounce:
        return
    locked_carrier = _adhoc_locked_carrier_id()
    if carrier_id is not None and locked_carrier is not None and carrier_id != locked_carrier:
        return
    if not _adhoc_is_locked():
        return
    _adhoc_preannounce["locked"] = False
    _save_adhoc_preannounce_state()
    if reason:
        logger.info("Cleared adhoc pre-announce lock (%s)", reason)


def _maybe_clear_adhoc_on_carrier_moved(
    carrier_id: Optional[int],
    previous_system: Optional[str],
) -> None:
    """Clear adhoc lock when the announced carrier leaves its locked origin."""
    if carrier_id is None or not previous_system:
        return
    locked_origin = str(_adhoc_preannounce.get("origin") or "").strip()
    if not locked_origin:
        return
    if locked_origin.lower() != previous_system.strip().lower():
        return
    _clear_adhoc_preannounce_lock(
        carrier_id=carrier_id,
        reason=f"carrier left origin {previous_system}",
    )


def _expedition_carrier_name() -> str:
    """Display name for the carrier bound to the current expedition."""
    if _expedition is None:
        return "Carrier"
    bound_id = _expedition.state.carrier_id
    kind = KIND_UNKNOWN
    if bound_id is not None:
        kind = _carrier_record(bound_id).get("kind") or KIND_UNKNOWN
    elif "squadron" in str(_expedition.state.carrier_label or "").lower():
        kind = KIND_SQUADRON

    carrier_name = _carrier_display(bound_id, kind) if bound_id is not None else ""
    if not carrier_name or carrier_name == _kind_label(kind):
        label = str(_expedition.state.carrier_label or "").strip()
        if label:
            # "Fleet Carrier: GALACTICA (V0B-12T)" -> "GALACTICA (V0B-12T)"
            if ": " in label:
                carrier_name = label.split(": ", 1)[1].strip() or label
            else:
                carrier_name = label
    return carrier_name or "Carrier"


def _cmdr_role_field_name(kind: Optional[str]) -> str:
    """Squadron carriers may be flown by any authorized captain, not the owner."""
    if kind == KIND_SQUADRON:
        return "Captain"
    return "Owner"


def _carrier_fields(
    carrier_id: Optional[int],
    cmdr: Optional[str] = None,
) -> list[dict[str, Any]]:
    info = _carrier_record(carrier_id)
    kind = info.get("kind") or KIND_UNKNOWN
    return _collect_fields(
        _embed_field("Carrier", _carrier_display(carrier_id, kind)),
        _embed_field("Type", _kind_label(kind)),
        _embed_field(_cmdr_role_field_name(kind), _owner_label(cmdr)),
    )


def _build_jump_request_payload(
    entry: dict[str, Any],
    from_system: Optional[str],
    carrier_id: Optional[int],
    cmdr: Optional[str] = None,
) -> dict[str, Any]:
    departure = _parse_journal_time(entry.get("DepartureTime"))
    lockdown = departure - LOCKDOWN_BEFORE_DEPARTURE if departure else None
    destination = entry.get("SystemName") or "Unknown"
    body = entry.get("Body")
    display = _carrier_display(carrier_id)

    fields = _join_field_groups(
        _carrier_fields(carrier_id, cmdr),
        _collect_fields(
            _embed_field("From", from_system or "Unknown"),
            _embed_field("Destination", destination),
            _embed_field("Body", body),
        ),
        _collect_fields(
            _embed_field("Departure", _format_discord_time(departure)),
            _embed_field("Lockdown (approx)", _format_discord_time(lockdown)),
        ),
        _expedition_field_group(carrier_id),
    )

    mention = _config_str(CFG_MENTION).strip()
    return {
        "username": "EDMC Carrier Jump",
        "content": mention or None,
        "embeds": [
            {
                "title": "Carrier jump scheduled",
                "description": (
                    f"**{display}** is jumping to "
                    f"**{destination}**"
                    + (f" ({body})" if body else "")
                    + "\n\u200b"
                ),
                "color": 0x3498DB,
                "fields": fields,
                "footer": {"text": f"{PLUGIN_NAME} v{__version__}"},
            }
        ],
    }


def _build_jump_cancelled_payload(
    carrier_id: Optional[int],
    cmdr: Optional[str] = None,
) -> dict[str, Any]:
    pending = _pending_jumps.get(carrier_id) if carrier_id is not None else None
    pending_dest = None
    pending_body = None
    pending_departure = None
    if pending:
        pending_dest = pending.get("SystemName")
        pending_body = pending.get("Body")
        pending_departure = _format_discord_time(
            _parse_journal_time(pending.get("DepartureTime"))
        )

    display = _carrier_display(carrier_id)
    fields = _join_field_groups(
        _carrier_fields(carrier_id, cmdr),
        _collect_fields(
            _embed_field("Was heading to", pending_dest),
            _embed_field("Body", pending_body),
            _embed_field("Was departing", pending_departure),
        ),
    )

    mention = _config_str(CFG_MENTION).strip()
    return {
        "username": "EDMC Carrier Jump",
        "content": mention or None,
        "embeds": [
            {
                "title": "Carrier jump cancelled",
                "description": f"**{display}** jump has been cancelled.\n\u200b",
                "color": 0xE67E22,
                "fields": fields,
                "footer": {"text": f"{PLUGIN_NAME} v{__version__}"},
            }
        ],
    }


def _build_jump_arrival_payload(
    entry: dict[str, Any],
    carrier_id: Optional[int],
    cmdr: Optional[str] = None,
) -> dict[str, Any]:
    arrived = entry.get("StarSystem") or "Unknown"
    body = entry.get("Body")
    display = _carrier_display(carrier_id)

    fields = _join_field_groups(
        _carrier_fields(carrier_id, cmdr),
        _collect_fields(
            _embed_field("Arrived in", arrived),
            _embed_field("Body", body),
        ),
        _expedition_field_group(carrier_id),
    )

    mention = _config_str(CFG_MENTION).strip()
    return {
        "username": "EDMC Carrier Jump",
        "content": mention or None,
        "embeds": [
            {
                "title": "Carrier jump complete",
                "description": (
                    f"**{display}** has arrived in "
                    f"**{arrived}**"
                    + (f" ({body})" if body else "")
                    + "\n\u200b"
                ),
                "color": 0x2ECC71,
                "fields": fields,
                "footer": {"text": f"{PLUGIN_NAME} v{__version__}"},
            }
        ],
    }


def _is_tracked_carrier_jump(entry: dict[str, Any]) -> tuple[bool, Optional[int]]:
    """Return whether CarrierJump is one of our tracked carriers, plus its id."""
    market_id = _normalize_carrier_id(entry.get("MarketID"))
    if market_id is not None and market_id in _carriers:
        return True, market_id

    station_name = str(entry.get("StationName") or "").strip()
    by_callsign = _find_carrier_id_by_callsign(station_name) if station_name else None
    if by_callsign is not None:
        return True, by_callsign

    arrived = str(entry.get("StarSystem") or "").strip().lower()
    for carrier_id, pending in _pending_jumps.items():
        pending_id = _normalize_carrier_id(pending.get("CarrierID"))
        if market_id is not None and pending_id == market_id:
            return True, carrier_id
        pending_dest = str(pending.get("SystemName") or "").strip().lower()
        if pending_dest and arrived and pending_dest == arrived:
            return True, carrier_id

    return False, market_id


def _update_carrier_from_entry(entry: dict[str, Any]) -> Optional[int]:
    carrier_id = _normalize_carrier_id(entry.get("CarrierID") or entry.get("MarketID"))
    system = entry.get("StarSystem")
    # CarrierJumpRequest uses SystemName for the *destination*, not current location.
    if entry.get("event") == "CarrierJumpRequest":
        system = None
    return _upsert_carrier(
        carrier_id,
        name=entry.get("Name") or entry.get("CarrierName"),
        callsign=entry.get("Callsign"),
        carrier_type=entry.get("CarrierType"),
        system=system,
    )


def _remember_system(entry: dict[str, Any], system: Optional[str]) -> None:
    global _current_system
    previous = _current_system
    if entry.get("StarSystem"):
        _current_system = entry["StarSystem"]
    elif system:
        _current_system = system
    if _current_system and _current_system != previous:
        _refresh_expedition_ui()


def _remember_station(entry: dict[str, Any], station: Optional[str] = None) -> None:
    """Track the current station/callsign for expedition carrier preselect."""
    global _current_station
    station_name = str(entry.get("StationName") or station or "").strip()
    if station_name:
        _current_station = station_name
    elif entry.get("event") in ("Location", "StartUp", "FSDJump") and not entry.get(
        "StationName"
    ):
        # Undocked / in space — clear docked station hint.
        if not station:
            _current_station = None


def _maybe_learn_carrier_system_from_presence(
    entry: dict[str, Any],
    system: Optional[str] = None,
    station: Optional[str] = None,
) -> None:
    """
    If the CMDR is docked on a tracked carrier, that carrier is in the current system.
    """
    station_name = str(entry.get("StationName") or station or "").strip()
    if not station_name:
        return
    carrier_id = _find_carrier_id_by_callsign(station_name)
    if carrier_id is None:
        return
    star = str(entry.get("StarSystem") or system or "").strip()
    if not star:
        return
    _upsert_carrier(carrier_id, system=star)


def _carrier_label_for_id(carrier_id: Optional[int]) -> str:
    """Human-readable label for a tracked carrier (kind + display name)."""
    if carrier_id is None:
        return "Unknown carrier"
    info = _carrier_record(carrier_id)
    kind = info.get("kind") or KIND_UNKNOWN
    display = _carrier_display(carrier_id, kind)
    return f"{_kind_label(kind)}: {display}"


def _preferred_carrier_id() -> Optional[int]:
    """Best default carrier for expedition binding."""
    if not _carriers:
        return None
    if len(_carriers) == 1:
        return next(iter(_carriers))

    if _current_station:
        docked = _find_carrier_id_by_callsign(_current_station)
        if docked is not None:
            return docked

    for carrier_id, info in _carriers.items():
        if info.get("kind") == KIND_FLEET:
            return carrier_id
    return next(iter(_carriers))


def _prompt_carrier_choice(
    title: str = "Select expedition carrier",
    prompt: str = "Which carrier is flying this expedition route?",
) -> Optional[tuple[int, str]]:
    """
    Modal picker listing tracked carriers.

    Returns (carrier_id, label) or None if cancelled / none available.
    """
    if not _carriers:
        try:
            messagebox.showerror(
                PLUGIN_NAME,
                "No carriers learned yet.\n\n"
                "Open management for each carrier in-game once so names and "
                "callsigns are tracked, then try again.",
            )
        except Exception:
            pass
        return None

    parent = None
    if _app_frame is not None:
        try:
            parent = _app_frame.winfo_toplevel()
        except tk.TclError:
            parent = None

    dialog = tk.Toplevel(parent) if parent is not None else tk.Toplevel()
    dialog.title(title)
    dialog.transient(parent) if parent is not None else None
    dialog.grab_set()
    dialog.resizable(False, False)

    result: dict[str, Any] = {"value": None}

    tk.Label(
        dialog,
        text=prompt,
        justify=tk.LEFT,
    ).grid(row=0, column=0, columnspan=2, sticky=tk.W, padx=12, pady=(12, 8))

    choices: list[tuple[int, str]] = []
    for carrier_id in sorted(
        _carriers.keys(),
        key=lambda cid: (
            0 if (_carriers[cid].get("kind") == KIND_FLEET) else 1,
            _carrier_display(cid).lower(),
        ),
    ):
        choices.append((carrier_id, _carrier_label_for_id(carrier_id)))

    preferred = _preferred_carrier_id()

    listbox = tk.Listbox(dialog, height=min(8, max(3, len(choices))), width=56, exportselection=False)
    listbox.grid(row=1, column=0, columnspan=2, sticky=tk.EW, padx=12, pady=4)
    for _cid, label in choices:
        listbox.insert(tk.END, label)

    # Preselect preferred row.
    preselect_index = 0
    for idx, (cid, _label) in enumerate(choices):
        if preferred is not None and cid == preferred:
            preselect_index = idx
            break
    listbox.selection_set(preselect_index)
    listbox.activate(preselect_index)
    listbox.see(preselect_index)

    def _confirm(_event: Optional[Any] = None) -> None:
        selection = listbox.curselection()
        if not selection:
            return
        idx = int(selection[0])
        carrier_id, label = choices[idx]
        result["value"] = (carrier_id, label)
        dialog.destroy()

    def _cancel(_event: Optional[Any] = None) -> None:
        result["value"] = None
        dialog.destroy()

    button_row = tk.Frame(dialog)
    button_row.grid(row=2, column=0, columnspan=2, sticky=tk.E, padx=12, pady=(8, 12))
    tk.Button(button_row, text="Cancel", command=_cancel, width=10).grid(
        row=0, column=0, padx=(0, 6)
    )
    tk.Button(button_row, text="OK", command=_confirm, width=10).grid(row=0, column=1)

    listbox.bind("<Double-Button-1>", _confirm)
    dialog.bind("<Return>", _confirm)
    dialog.bind("<Escape>", _cancel)
    dialog.protocol("WM_DELETE_WINDOW", _cancel)

    dialog.wait_window()
    return result["value"]


# ---------------------------------------------------------------------------
# EDMC plugin hooks
# ---------------------------------------------------------------------------


def plugin_start3(plugin_dir: str) -> str:
    """Load this plugin into EDMarketConnector."""
    global _worker_thread, _plugin_dir, _expedition

    _plugin_dir = plugin_dir
    _load_carriers_from_config()
    _load_adhoc_preannounce_state()
    _expedition = expedition_mod.ExpeditionManager(
        plugin_dir,
        on_change=_refresh_expedition_ui,
    )

    _stop_worker.clear()
    _worker_thread = threading.Thread(
        target=_worker_loop,
        name=f"{plugin_name}-discord-worker",
        daemon=True,
    )
    _worker_thread.start()

    logger.info(
        "%s v%s started from %s (%d carrier(s) cached, expedition_active=%s)",
        PLUGIN_NAME,
        __version__,
        plugin_dir,
        len(_carriers),
        bool(_expedition and _expedition.is_active),
    )
    return PLUGIN_NAME


def plugin_stop() -> None:
    """Shut down background worker."""
    _save_carriers_to_config()
    _save_adhoc_preannounce_state()
    if _expedition is not None:
        _expedition.save()
    _stop_worker.set()
    _worker_queue.put(None)
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=2.0)
    logger.info("%s stopped", PLUGIN_NAME)


def _import_expedition_csv() -> None:
    path = filedialog.askopenfilename(
        title="Import Spansh Fleet Carrier route CSV",
        filetypes=[
            ("CSV files", "*.csv"),
            ("All files", "*.*"),
        ],
    )
    if not path:
        return
    if _expedition is None:
        _set_status("Expedition unavailable", "red")
        return

    choice = _prompt_carrier_choice("Select expedition carrier")
    if choice is None:
        return
    carrier_id, carrier_label = choice

    try:
        state = _expedition.import_csv(
            path,
            current_system=_current_system,
            carrier_id=carrier_id,
            carrier_label=carrier_label,
        )
        hops = max(0, len(state.waypoints) - 1)
        final_dest = state.waypoints[-1].system if state.waypoints else "Unknown"
        lines = _expedition.status_lines(current_system=_current_system)
        logger.info(
            "Imported expedition route from %s for %s (%d hops -> %s); status=%s next=%s progress=%s remaining=%s current=%s",
            path,
            carrier_label,
            hops,
            final_dest,
            lines.get("status"),
            lines.get("next"),
            lines.get("progress"),
            lines.get("remaining"),
            _current_system,
        )
        if state.completed:
            _set_status(f"Expedition already complete -> {final_dest}", "green")
        else:
            _set_status(f"Following route on {carrier_label} -> {final_dest}", "green")
        _refresh_expedition_ui()
    except Exception as exc:
        logger.exception("Failed importing expedition CSV")
        _set_status("Import failed", "red")
        try:
            messagebox.showerror(PLUGIN_NAME, f"Could not import route:\n{exc}")
        except Exception:
            pass


def _assign_expedition_carrier() -> None:
    """Bind or re-bind the current expedition to a tracked carrier."""
    if _expedition is None:
        return
    if not _expedition.state.waypoints:
        try:
            messagebox.showinfo(
                PLUGIN_NAME,
                "No expedition route loaded.\n\nImport a Spansh CSV first.",
            )
        except Exception:
            pass
        return

    choice = _prompt_carrier_choice("Assign expedition carrier")
    if choice is None:
        return
    carrier_id, carrier_label = choice
    if _expedition.assign_carrier(carrier_id, carrier_label):
        logger.info("Expedition assigned to %s (%s)", carrier_label, carrier_id)
        _set_status(f"Expedition bound to {carrier_label}", "green")
        _refresh_expedition_ui()
    else:
        _set_status("Assign carrier failed", "red")


def _clear_expedition_route() -> None:
    if _expedition is None:
        return
    _expedition.clear()
    _set_status("Expedition cleared", "orange")
    _refresh_expedition_ui()


def _prompt_pre_departure_delay() -> Optional[timedelta]:
    """Ask how long until the first jump; returns None if cancelled."""
    result = _prompt_pre_departure_details(include_destination=False)
    if result is None:
        return None
    return result.get("delay")


def _prompt_pre_departure_details(
    *,
    include_destination: bool,
) -> Optional[dict[str, Any]]:
    """
    Shared announce dialog.

    Returns {"delay": timedelta, "destination": str|None} or None if cancelled.
    """
    parent = None
    if _app_frame is not None:
        try:
            parent = _app_frame.winfo_toplevel()
        except tk.TclError:
            parent = None

    dialog = tk.Toplevel(parent) if parent is not None else tk.Toplevel()
    dialog.title("Announce departure")
    if parent is not None:
        dialog.transient(parent)
    dialog.grab_set()
    dialog.resizable(False, False)

    result: dict[str, Any] = {"value": None}
    dest_ac: Optional[Any] = None

    prompt = (
        "How long until you initiate the jump?"
        if include_destination
        else "How long until you initiate the first jump?"
    )
    tk.Label(
        dialog,
        text=prompt,
        justify=tk.LEFT,
    ).grid(row=0, column=0, columnspan=4, sticky=tk.W, padx=12, pady=(12, 8))

    tk.Label(dialog, text="Hours:").grid(row=1, column=0, sticky=tk.W, padx=(12, 4))
    hours_var = tk.StringVar(value="0")
    hours_spin = tk.Spinbox(
        dialog,
        from_=0,
        to=168,
        width=5,
        textvariable=hours_var,
    )
    hours_spin.grid(row=1, column=1, sticky=tk.W, padx=(0, 12))

    tk.Label(dialog, text="Minutes:").grid(row=1, column=2, sticky=tk.W, padx=(0, 4))
    minutes_var = tk.StringVar(value="30")
    minutes_spin = tk.Spinbox(
        dialog,
        from_=0,
        to=59,
        width=5,
        textvariable=minutes_var,
    )
    minutes_spin.grid(row=1, column=3, sticky=tk.W, padx=(0, 12))

    next_row = 2
    announce_btn: Optional[tk.Button] = None

    def _destination_ready() -> bool:
        if not include_destination:
            return True
        if dest_ac is None:
            return False
        return bool(dest_ac.get_system_name())

    def _refresh_announce_btn(*_args: Any) -> None:
        if announce_btn is None:
            return
        try:
            if announce_btn.winfo_exists():
                announce_btn.configure(
                    state=tk.NORMAL if _destination_ready() else tk.DISABLED
                )
        except tk.TclError:
            pass

    if include_destination:
        tk.Label(dialog, text="Destination:").grid(
            row=next_row, column=0, sticky=tk.W, padx=(12, 4), pady=(8, 0)
        )
        dest_ac = system_ac_mod.SystemAutoCompleter(
            dialog,
            "System name",
            width=36,
            user_agent=f"EDMC-{plugin_name}/{__version__}",
            on_select=lambda _value: _refresh_announce_btn(),
        )
        dest_ac.grid(
            row=next_row,
            column=1,
            columnspan=3,
            sticky=tk.EW,
            padx=(0, 12),
            pady=(8, 0),
        )
        dest_ac.var.trace_add(
            "write",
            lambda *_args: dialog.after_idle(_refresh_announce_btn),
        )
        # Reserve a row for the autocomplete dropdown.
        next_row += 2

    if include_destination:
        if _allow_repeat_pre_announce():
            warning = (
                "Dev build: you can send this announce more than once while testing."
            )
        else:
            warning = (
                "Limited to one announce per destination until that jump is "
                "scheduled, cancelled, completed, or the carrier leaves the "
                "origin system. "
                f"A {int(ADHOC_PREANNOUNCE_COOLDOWN.total_seconds() // 60)}-minute "
                "cooldown also applies."
            )
    else:
        warning = (
            "This action can only be performed once for this expedition."
            if not _allow_repeat_pre_announce()
            else "Dev build: you can send this announce more than once while testing."
        )
    tk.Label(
        dialog,
        text=warning,
        justify=tk.LEFT,
        wraplength=360,
        fg="#b35c00",
    ).grid(row=next_row, column=0, columnspan=4, sticky=tk.W, padx=12, pady=(8, 4))
    next_row += 1

    def _parse_nonneg_int(raw: str) -> Optional[int]:
        text = (raw or "").strip()
        if not text:
            return 0
        try:
            value = int(text)
        except ValueError:
            return None
        if value < 0:
            return None
        return value

    def _confirm(_event: Optional[Any] = None) -> None:
        if dest_ac is not None and dest_ac.lb_up:
            dest_ac.selection()
            _refresh_announce_btn()
            return
        if include_destination and not _destination_ready():
            return
        hours = _parse_nonneg_int(hours_var.get())
        minutes = _parse_nonneg_int(minutes_var.get())
        if hours is None or minutes is None:
            try:
                messagebox.showerror(
                    PLUGIN_NAME,
                    "Enter whole numbers for hours and minutes.",
                    parent=dialog,
                )
            except Exception:
                pass
            return
        if hours == 0 and minutes == 0:
            try:
                messagebox.showerror(
                    PLUGIN_NAME,
                    "Enter a delay greater than zero.",
                    parent=dialog,
                )
            except Exception:
                pass
            return

        destination: Optional[str] = None
        if include_destination:
            if dest_ac is None:
                return
            destination = dest_ac.get_system_name()
            if not destination:
                return
            if not dest_ac.has_selected and not dest_ac.last_fetch_ok():
                try:
                    proceed = messagebox.askyesno(
                        PLUGIN_NAME,
                        "Could not verify that system with Spansh.\n\n"
                        f"Post announce to Discord using “{destination}” as typed?",
                        parent=dialog,
                    )
                except Exception:
                    proceed = True
                if not proceed:
                    return

        result["value"] = {
            "delay": timedelta(hours=hours, minutes=minutes),
            "destination": destination,
        }
        dialog.destroy()

    def _cancel(_event: Optional[Any] = None) -> None:
        result["value"] = None
        dialog.destroy()

    button_row = tk.Frame(dialog)
    button_row.grid(
        row=next_row, column=0, columnspan=4, sticky=tk.E, padx=12, pady=(8, 12)
    )
    tk.Button(button_row, text="Cancel", command=_cancel, width=10).grid(
        row=0, column=0, padx=(0, 6)
    )
    announce_btn = tk.Button(
        button_row,
        text="Announce",
        command=_confirm,
        width=10,
        state=tk.DISABLED if include_destination else tk.NORMAL,
    )
    announce_btn.grid(row=0, column=1)

    dialog.bind("<Return>", _confirm)
    dialog.bind("<Escape>", _cancel)
    dialog.protocol("WM_DELETE_WINDOW", _cancel)

    dialog.wait_window()
    return result["value"]


def _resolve_adhoc_announce_carrier() -> Optional[tuple[int, str]]:
    """Pick the carrier for an adhoc announce (auto or prompt)."""
    preferred = _preferred_carrier_id()
    if preferred is not None and len(_carriers) == 1:
        return preferred, _carrier_label_for_id(preferred)
    if preferred is not None and _current_station:
        docked = _find_carrier_id_by_callsign(_current_station)
        if docked is not None and docked == preferred:
            return preferred, _carrier_label_for_id(preferred)
    return _prompt_carrier_choice(
        "Select carrier to announce",
        prompt="Which carrier are you announcing a departure for?",
    )


def _announce_departure() -> None:
    """Shared Announce departure entry: expedition or adhoc."""
    if _is_expedition_announce_mode():
        _announce_expedition_departure()
    else:
        _announce_adhoc_departure()


def _announce_expedition_departure() -> None:
    """Post a one-shot pre-departure announce for the imported expedition."""
    if _expedition is None or not _can_announce_pre_departure():
        return

    if not _config_bool(CFG_ENABLED, True):
        _set_status("Disabled", "orange")
        try:
            messagebox.showinfo(PLUGIN_NAME, "Discord posting is disabled in settings.")
        except Exception:
            pass
        return

    ok, message = _delivery_configured()
    if not ok:
        _set_status(message, "orange")
        try:
            messagebox.showerror(PLUGIN_NAME, f"Discord delivery not ready:\n{message}")
        except Exception:
            pass
        return

    details = _prompt_pre_departure_details(include_destination=False)
    if details is None:
        return
    delay = details.get("delay")
    if not isinstance(delay, timedelta):
        return

    departure = datetime.now(timezone.utc) + delay
    payload = _build_expedition_pre_departure_payload(
        departure,
        cmdr=_last_cmdr,
    )
    ok, message = _post_discord(payload)
    if not ok:
        logger.error("Pre-departure announce failed: %s", message)
        _set_status("Pre-departure announce failed", "red")
        try:
            messagebox.showerror(PLUGIN_NAME, f"Could not post to Discord:\n{message}")
        except Exception:
            pass
        return

    logger.info("Pre-departure announce posted (%s)", message)
    if not _allow_repeat_pre_announce():
        _expedition.mark_pre_announced()
    _set_status("Pre-departure announced", "green")
    _refresh_expedition_ui()


def _announce_adhoc_departure() -> None:
    """Post a pre-departure announce for a single non-expedition jump."""
    if not _can_announce_adhoc():
        return

    if not _config_bool(CFG_ENABLED, True):
        _set_status("Disabled", "orange")
        try:
            messagebox.showinfo(PLUGIN_NAME, "Discord posting is disabled in settings.")
        except Exception:
            pass
        return

    ok, message = _delivery_configured()
    if not ok:
        _set_status(message, "orange")
        try:
            messagebox.showerror(PLUGIN_NAME, f"Discord delivery not ready:\n{message}")
        except Exception:
            pass
        return

    if not _carriers:
        try:
            messagebox.showerror(
                PLUGIN_NAME,
                "No carriers learned yet.\n\n"
                "Open management for each carrier in-game once so names and "
                "callsigns are tracked, then try again.",
            )
        except Exception:
            pass
        return

    choice = _resolve_adhoc_announce_carrier()
    if choice is None:
        return
    carrier_id, _carrier_label = choice

    details = _prompt_pre_departure_details(include_destination=True)
    if details is None:
        return
    delay = details.get("delay")
    destination = str(details.get("destination") or "").strip()
    if not isinstance(delay, timedelta) or not destination:
        return

    origin = _origin_for_carrier(carrier_id)
    if not _adhoc_announce_allowed(
        carrier_id=carrier_id,
        origin=origin,
        destination=destination,
    ):
        try:
            messagebox.showinfo(
                PLUGIN_NAME,
                "That departure was already announced.\n\n"
                "Wait for the jump to schedule/complete, change destination, "
                "or wait for the cooldown before announcing again.",
            )
        except Exception:
            pass
        _refresh_expedition_ui()
        return

    departure = datetime.now(timezone.utc) + delay
    payload = _build_adhoc_pre_departure_payload(
        departure,
        carrier_id=carrier_id,
        destination=destination,
        origin=origin,
        cmdr=_last_cmdr,
    )
    ok, message = _post_discord(payload)
    if not ok:
        logger.error("Adhoc pre-departure announce failed: %s", message)
        _set_status("Pre-departure announce failed", "red")
        try:
            messagebox.showerror(PLUGIN_NAME, f"Could not post to Discord:\n{message}")
        except Exception:
            pass
        return

    logger.info(
        "Adhoc pre-departure announce posted (%s -> %s) via %s",
        origin,
        destination,
        message,
    )
    if not _allow_repeat_pre_announce():
        _mark_adhoc_preannounce(carrier_id, origin, destination)
    _set_status("Pre-departure announced", "green")
    _refresh_expedition_ui()


def _apply_expedition_collapse() -> None:
    """Show or hide expedition detail rows and update the toggle button."""
    global _expedition_collapse_btn, _expedition_detail_frame

    collapsed = bool(_expedition_collapsed)
    if _expedition_collapse_btn is not None:
        try:
            if _expedition_collapse_btn.winfo_exists():
                _expedition_collapse_btn.configure(text="⏵" if collapsed else "⏷")
        except tk.TclError:
            _expedition_collapse_btn = None

    if _expedition_detail_frame is not None:
        try:
            if _expedition_detail_frame.winfo_exists():
                if collapsed:
                    _expedition_detail_frame.grid_remove()
                else:
                    _expedition_detail_frame.grid()
        except tk.TclError:
            _expedition_detail_frame = None

    # Ask EDMC's window to re-fit after collapsing/expanding.
    root = None
    if _app_frame is not None:
        try:
            root = _app_frame.winfo_toplevel()
        except tk.TclError:
            root = None
    if root is not None:
        try:
            root.update_idletasks()
        except tk.TclError:
            pass


def _toggle_expedition_collapse() -> None:
    global _expedition_collapsed
    _expedition_collapsed = not _expedition_collapsed
    try:
        config.set(CFG_EXPEDITION_COLLAPSED, 1 if _expedition_collapsed else 0)
    except Exception:
        logger.debug("Failed saving expedition collapse state", exc_info=True)
    _apply_expedition_collapse()
    _refresh_expedition_ui()


def plugin_app(parent: tk.Frame) -> tk.Frame:
    """Add Discord status and expedition controls to the EDMC main window."""
    global _status_label, _app_frame, _expedition_collapsed
    global _expedition_status_label, _expedition_carrier_label, _expedition_final_label
    global _expedition_next_label, _expedition_progress_label, _expedition_remaining_label
    global _expedition_detail_frame, _expedition_collapse_btn, _expedition_announce_btn

    _expedition_collapsed = _config_bool(CFG_EXPEDITION_COLLAPSED, False)

    frame = tk.Frame(parent)
    _app_frame = frame
    frame.columnconfigure(1, weight=1)

    tk.Label(frame, text="Carrier Discord:").grid(row=0, column=0, sticky=tk.W)
    _status_label = tk.Label(frame, text="Ready", anchor=tk.W)
    _status_label.grid(row=0, column=1, sticky=tk.EW)
    _refresh_ready_status()

    header = tk.Frame(frame)
    header.grid(row=1, column=0, columnspan=2, sticky=tk.EW, pady=(4, 0))
    header.columnconfigure(2, weight=1)

    _expedition_collapse_btn = tk.Button(
        header,
        text="⏷",
        width=2,
        command=_toggle_expedition_collapse,
        takefocus=0,
    )
    _expedition_collapse_btn.grid(row=0, column=0, sticky=tk.W, padx=(0, 4))

    tk.Label(header, text="Expedition:").grid(row=0, column=1, sticky=tk.W)
    _expedition_status_label = tk.Label(header, text="No expedition", anchor=tk.W)
    _expedition_status_label.grid(row=0, column=2, sticky=tk.EW)

    _expedition_detail_frame = tk.Frame(frame)
    _expedition_detail_frame.grid(row=2, column=0, columnspan=2, sticky=tk.EW)
    _expedition_detail_frame.columnconfigure(1, weight=1)

    tk.Label(_expedition_detail_frame, text="Carrier:").grid(row=0, column=0, sticky=tk.W)
    _expedition_carrier_label = tk.Label(_expedition_detail_frame, text="-", anchor=tk.W)
    _expedition_carrier_label.grid(row=0, column=1, sticky=tk.EW)

    tk.Label(_expedition_detail_frame, text="Final:").grid(row=1, column=0, sticky=tk.W)
    _expedition_final_label = tk.Label(_expedition_detail_frame, text="-", anchor=tk.W)
    _expedition_final_label.grid(row=1, column=1, sticky=tk.EW)

    tk.Label(_expedition_detail_frame, text="Next:").grid(row=2, column=0, sticky=tk.W)
    _expedition_next_label = tk.Label(_expedition_detail_frame, text="-", anchor=tk.W)
    _expedition_next_label.grid(row=2, column=1, sticky=tk.EW)

    tk.Label(_expedition_detail_frame, text="Progress:").grid(row=3, column=0, sticky=tk.W)
    _expedition_progress_label = tk.Label(_expedition_detail_frame, text="-", anchor=tk.W)
    _expedition_progress_label.grid(row=3, column=1, sticky=tk.EW)

    tk.Label(_expedition_detail_frame, text="Remaining:").grid(row=4, column=0, sticky=tk.W)
    _expedition_remaining_label = tk.Label(_expedition_detail_frame, text="-", anchor=tk.W)
    _expedition_remaining_label.grid(row=4, column=1, sticky=tk.EW)

    buttons = tk.Frame(_expedition_detail_frame)
    buttons.grid(row=5, column=0, columnspan=2, sticky=tk.W, pady=(4, 0))
    tk.Button(buttons, text="Import route CSV", command=_import_expedition_csv).grid(
        row=0, column=0, padx=(0, 6)
    )
    tk.Button(buttons, text="Assign carrier", command=_assign_expedition_carrier).grid(
        row=0, column=1, padx=(0, 6)
    )
    tk.Button(buttons, text="Clear route", command=_clear_expedition_route).grid(
        row=0, column=2, padx=(0, 6)
    )
    _expedition_announce_btn = tk.Button(
        buttons,
        text="Announce departure",
        command=_announce_departure,
        state=tk.DISABLED,
    )
    _expedition_announce_btn.grid(row=0, column=3)

    _refresh_expedition_ui()
    _apply_expedition_collapse()
    return frame


def plugin_prefs(parent: nb.Notebook, cmdr: str, is_beta: bool) -> tk.Frame:
    """Settings tab for Discord delivery and notification options."""
    _remember_cmdr(cmdr)
    global _webhook_var, _delivery_mode_var, _bot_token_var, _channel_id_var
    global _enabled_var, _notify_request_var, _notify_cancel_var
    global _notify_arrival_var, _notify_expedition_complete_var
    global _notify_fleet_var, _notify_squadron_var
    global _fleet_name_var, _fleet_callsign_var
    global _squadron_name_var, _squadron_callsign_var, _mention_var
    global _prefs_status, _tracked_label

    _webhook_var = tk.StringVar(value=_config_str(CFG_WEBHOOK))
    _delivery_mode_var = tk.StringVar(value=_delivery_mode())
    _bot_token_var = tk.StringVar(value=_config_str(CFG_BOT_TOKEN))
    _channel_id_var = tk.StringVar(value=_config_str(CFG_CHANNEL_ID))
    _enabled_var = tk.BooleanVar(value=_config_bool(CFG_ENABLED, True))
    _notify_request_var = tk.BooleanVar(value=_config_bool(CFG_NOTIFY_REQUEST, True))
    _notify_cancel_var = tk.BooleanVar(value=_config_bool(CFG_NOTIFY_CANCEL, True))
    _notify_arrival_var = tk.BooleanVar(value=_config_bool(CFG_NOTIFY_ARRIVAL, False))
    _notify_expedition_complete_var = tk.BooleanVar(
        value=_config_bool(CFG_NOTIFY_EXPEDITION_COMPLETE, True)
    )
    _notify_fleet_var = tk.BooleanVar(value=_config_bool(CFG_NOTIFY_FLEET, True))
    _notify_squadron_var = tk.BooleanVar(value=_config_bool(CFG_NOTIFY_SQUADRON, True))
    _fleet_name_var = tk.StringVar(value=_config_str(CFG_CARRIER_NAME))
    _fleet_callsign_var = tk.StringVar(value=_config_str(CFG_CARRIER_CALLSIGN))
    _squadron_name_var = tk.StringVar(value=_config_str(CFG_SQUADRON_NAME))
    _squadron_callsign_var = tk.StringVar(value=_config_str(CFG_SQUADRON_CALLSIGN))
    _mention_var = tk.StringVar(value=_config_str(CFG_MENTION))

    frame = nb.Frame(parent)
    frame.columnconfigure(1, weight=1)

    row = 0
    nb.Label(frame, text=f"{PLUGIN_NAME} v{__version__}").grid(
        row=row, column=0, columnspan=2, sticky=tk.W, padx=10, pady=(8, 4)
    )

    row += 1
    nb.Checkbutton(
        frame,
        text="Enable Discord notifications",
        variable=_enabled_var,
    ).grid(row=row, column=0, columnspan=2, sticky=tk.W, padx=10, pady=2)

    row += 1
    nb.Checkbutton(
        frame,
        text="Notify on jump scheduled",
        variable=_notify_request_var,
    ).grid(row=row, column=0, columnspan=2, sticky=tk.W, padx=10, pady=2)

    row += 1
    nb.Checkbutton(
        frame,
        text="Notify on jump cancelled",
        variable=_notify_cancel_var,
    ).grid(row=row, column=0, columnspan=2, sticky=tk.W, padx=10, pady=2)

    row += 1
    nb.Checkbutton(
        frame,
        text="Notify on jump arrival (when docked on your carrier)",
        variable=_notify_arrival_var,
    ).grid(row=row, column=0, columnspan=2, sticky=tk.W, padx=10, pady=2)

    row += 1
    nb.Checkbutton(
        frame,
        text="Notify on expedition completion",
        variable=_notify_expedition_complete_var,
    ).grid(row=row, column=0, columnspan=2, sticky=tk.W, padx=10, pady=2)

    row += 1
    nb.Checkbutton(
        frame,
        text="Notify for Fleet Carrier",
        variable=_notify_fleet_var,
    ).grid(row=row, column=0, columnspan=2, sticky=tk.W, padx=10, pady=2)

    row += 1
    nb.Checkbutton(
        frame,
        text="Notify for Squadron Carrier",
        variable=_notify_squadron_var,
    ).grid(row=row, column=0, columnspan=2, sticky=tk.W, padx=10, pady=2)

    row += 1
    nb.Label(frame, text="Delivery method").grid(
        row=row, column=0, columnspan=2, sticky=tk.W, padx=10, pady=(10, 2)
    )
    row += 1
    mode_row = nb.Frame(frame)
    mode_row.grid(row=row, column=0, columnspan=2, sticky=tk.W, padx=10, pady=2)
    nb.Radiobutton(
        mode_row,
        text="Webhook URL",
        variable=_delivery_mode_var,
        value=DELIVERY_WEBHOOK,
    ).grid(row=0, column=0, sticky=tk.W, padx=(0, 12))
    nb.Radiobutton(
        mode_row,
        text="Bot token + channel ID",
        variable=_delivery_mode_var,
        value=DELIVERY_BOT,
    ).grid(row=0, column=1, sticky=tk.W)

    row += 1
    nb.Label(frame, text="Discord webhook URL").grid(
        row=row, column=0, sticky=tk.W, padx=10, pady=(10, 2)
    )
    row += 1
    ttk.Entry(frame, textvariable=_webhook_var, width=70).grid(
        row=row, column=0, columnspan=2, sticky=tk.EW, padx=10, pady=2
    )

    row += 1
    nb.Label(frame, text="Discord bot token").grid(
        row=row, column=0, sticky=tk.W, padx=10, pady=(10, 2)
    )
    row += 1
    ttk.Entry(frame, textvariable=_bot_token_var, width=70, show="*").grid(
        row=row, column=0, columnspan=2, sticky=tk.EW, padx=10, pady=2
    )

    row += 1
    nb.Label(frame, text="Discord channel ID").grid(
        row=row, column=0, sticky=tk.W, padx=10, pady=(8, 2)
    )
    row += 1
    ttk.Entry(frame, textvariable=_channel_id_var, width=40).grid(
        row=row, column=0, columnspan=2, sticky=tk.W, padx=10, pady=2
    )

    row += 1
    nb.Label(
        frame,
        text="Optional mention (e.g. @here, <@&role_id>, or <@user_id>)",
    ).grid(row=row, column=0, columnspan=2, sticky=tk.W, padx=10, pady=(10, 2))
    row += 1
    ttk.Entry(frame, textvariable=_mention_var, width=40).grid(
        row=row, column=0, columnspan=2, sticky=tk.W, padx=10, pady=2
    )

    row += 1
    nb.Label(
        frame,
        text="Optional overrides (used before CarrierStats has been seen)",
    ).grid(row=row, column=0, columnspan=2, sticky=tk.W, padx=10, pady=(10, 2))

    row += 1
    nb.Label(frame, text="Fleet Carrier name").grid(
        row=row, column=0, sticky=tk.W, padx=10, pady=2
    )
    ttk.Entry(frame, textvariable=_fleet_name_var, width=40).grid(
        row=row, column=1, sticky=tk.W, padx=10, pady=2
    )

    row += 1
    nb.Label(frame, text="Fleet Carrier callsign").grid(
        row=row, column=0, sticky=tk.W, padx=10, pady=2
    )
    ttk.Entry(frame, textvariable=_fleet_callsign_var, width=20).grid(
        row=row, column=1, sticky=tk.W, padx=10, pady=2
    )

    row += 1
    nb.Label(frame, text="Squadron Carrier name").grid(
        row=row, column=0, sticky=tk.W, padx=10, pady=2
    )
    ttk.Entry(frame, textvariable=_squadron_name_var, width=40).grid(
        row=row, column=1, sticky=tk.W, padx=10, pady=2
    )

    row += 1
    nb.Label(frame, text="Squadron Carrier callsign").grid(
        row=row, column=0, sticky=tk.W, padx=10, pady=2
    )
    ttk.Entry(frame, textvariable=_squadron_callsign_var, width=20).grid(
        row=row, column=1, sticky=tk.W, padx=10, pady=2
    )

    row += 1
    nb.Label(frame, text="Tracked carriers").grid(
        row=row, column=0, columnspan=2, sticky=tk.W, padx=10, pady=(10, 2)
    )
    row += 1
    _tracked_label = nb.Label(frame, text=_tracked_summary(), wraplength=560, justify=tk.LEFT)
    _tracked_label.grid(row=row, column=0, columnspan=2, sticky=tk.W, padx=10, pady=2)

    def _on_prefs_destroy(_event: tk.Event) -> None:
        global _tracked_label, _prefs_status
        _tracked_label = None
        _prefs_status = None

    frame.bind("<Destroy>", _on_prefs_destroy, add="+")

    row += 1
    button_row = nb.Frame(frame)
    button_row.grid(row=row, column=0, columnspan=2, sticky=tk.W, padx=10, pady=(12, 4))
    nb.Button(button_row, text="Send test message", command=_send_test_message).grid(
        row=0, column=0, sticky=tk.W
    )

    row += 1
    _prefs_status = nb.Label(frame, text="")
    _prefs_status.grid(row=row, column=0, columnspan=2, sticky=tk.W, padx=10, pady=(4, 8))

    row += 1
    nb.Label(
        frame,
        text=(
            "Tip: open management for each carrier in-game once so names/callsigns "
            "are learned separately. For bot mode, invite a bot with Send Messages + "
            "Embed Links, then paste its token and the target channel ID."
        ),
        wraplength=560,
        justify=tk.LEFT,
    ).grid(row=row, column=0, columnspan=2, sticky=tk.W, padx=10, pady=(4, 10))

    return frame


def _persist_prefs_from_vars() -> None:
    """Write current prefs widget values into EDMC config."""
    if _webhook_var is not None:
        config.set(CFG_WEBHOOK, _webhook_var.get().strip())
    if _delivery_mode_var is not None:
        mode = _delivery_mode_var.get().strip().lower()
        if mode not in (DELIVERY_WEBHOOK, DELIVERY_BOT):
            mode = DELIVERY_WEBHOOK
        config.set(CFG_DELIVERY_MODE, mode)
    if _bot_token_var is not None:
        config.set(CFG_BOT_TOKEN, _bot_token_var.get().strip())
    if _channel_id_var is not None:
        config.set(CFG_CHANNEL_ID, _channel_id_var.get().strip())
    if _enabled_var is not None:
        config.set(CFG_ENABLED, bool(_enabled_var.get()))
    if _notify_request_var is not None:
        config.set(CFG_NOTIFY_REQUEST, bool(_notify_request_var.get()))
    if _notify_cancel_var is not None:
        config.set(CFG_NOTIFY_CANCEL, bool(_notify_cancel_var.get()))
    if _notify_arrival_var is not None:
        config.set(CFG_NOTIFY_ARRIVAL, bool(_notify_arrival_var.get()))
    if _notify_expedition_complete_var is not None:
        config.set(
            CFG_NOTIFY_EXPEDITION_COMPLETE,
            bool(_notify_expedition_complete_var.get()),
        )
    if _notify_fleet_var is not None:
        config.set(CFG_NOTIFY_FLEET, bool(_notify_fleet_var.get()))
    if _notify_squadron_var is not None:
        config.set(CFG_NOTIFY_SQUADRON, bool(_notify_squadron_var.get()))
    if _fleet_name_var is not None:
        config.set(CFG_CARRIER_NAME, _fleet_name_var.get().strip())
    if _fleet_callsign_var is not None:
        config.set(CFG_CARRIER_CALLSIGN, _fleet_callsign_var.get().strip())
    if _squadron_name_var is not None:
        config.set(CFG_SQUADRON_NAME, _squadron_name_var.get().strip())
    if _squadron_callsign_var is not None:
        config.set(CFG_SQUADRON_CALLSIGN, _squadron_callsign_var.get().strip())
    if _mention_var is not None:
        config.set(CFG_MENTION, _mention_var.get().strip())


def prefs_changed(cmdr: str, is_beta: bool) -> None:
    """Persist settings when the preferences dialog is closed."""
    _remember_cmdr(cmdr)
    _persist_prefs_from_vars()
    _refresh_tracked_label()
    _refresh_ready_status()


def _send_test_message() -> None:
    """Post a sample embed using the values currently shown in prefs."""
    _persist_prefs_from_vars()

    # Prefer a known fleet carrier for the test, else any tracked carrier.
    test_id = None
    for carrier_id, info in _carriers.items():
        if info.get("kind") == KIND_FLEET:
            test_id = carrier_id
            break
    if test_id is None and _carriers:
        test_id = next(iter(_carriers))

    sample_entry = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": "CarrierJumpRequest",
        "CarrierID": test_id or 3700000000,
        "CarrierType": "FleetCarrier",
        "SystemName": "Sol",
        "Body": "Earth",
        "DepartureTime": (
            datetime.now(timezone.utc) + timedelta(minutes=15)
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    # Avoid polluting the saved carrier cache with a synthetic test ID.
    temporary = False
    if test_id is None:
        test_id = int(sample_entry["CarrierID"])
        temporary = True
        _carriers[test_id] = {
            "name": _config_str(CFG_CARRIER_NAME) or "Test Carrier",
            "callsign": _config_str(CFG_CARRIER_CALLSIGN) or "ABC-123",
            "kind": KIND_FLEET,
            "carrier_type": "FleetCarrier",
        }

    payload = _build_jump_request_payload(
        sample_entry,
        from_system=_current_system or "Shinrarta Dezhra",
        carrier_id=test_id,
        cmdr="Test CMDR",
    )
    # Keep test embeds plain even when an expedition is bound to this carrier.
    if payload.get("embeds"):
        expedition_names = {
            "Expedition",
            "Final destination",
            "Progress",
            "Next waypoint",
            "Distance left",
            "Restock",
        }
        cleaned: list[dict[str, Any]] = []
        for field in payload["embeds"][0].get("fields") or []:
            if field.get("name") in expedition_names:
                continue
            if field.get("name") == "\u200b" and field.get("value") == "\u200b":
                if cleaned and cleaned[-1].get("name") != "\u200b":
                    cleaned.append(field)
                continue
            cleaned.append(field)
        while cleaned and cleaned[-1].get("name") == "\u200b":
            cleaned.pop()
        payload["embeds"][0]["fields"] = cleaned

    if temporary:
        _carriers.pop(test_id, None)
    mode = _delivery_mode()
    payload["embeds"][0]["title"] = "Carrier jump scheduled (TEST)"
    payload["embeds"][0]["description"] = (
        f"Test message from **{PLUGIN_NAME}** via **{mode}**. "
        f"If you see this, Discord delivery is working."
    )

    ok, message = _post_discord(payload)
    if _prefs_status is not None:
        _prefs_status["text"] = message
    if ok:
        _set_status("Test posted", "green")
    else:
        _set_status("Test failed", "red")


def journal_entry(
    cmdr: str,
    is_beta: bool,
    system: str,
    station: str,
    entry: dict[str, Any],
    state: dict[str, Any],
) -> Optional[str]:
    """Handle Elite Dangerous journal events."""
    _remember_cmdr(cmdr)
    event = entry.get("event")
    if not event:
        return None

    if event in ("Location", "FSDJump", "CarrierJump", "StartUp", "Docked", "Undocked"):
        _remember_system(entry, system)
        _remember_station(entry, station)
        if event in ("Location", "StartUp", "Docked"):
            _maybe_learn_carrier_system_from_presence(entry, system, station)

    if event in ("CarrierStats", "CarrierNameChanged", "CarrierBuy", "CarrierLocation"):
        carrier_id = _update_carrier_from_entry(entry)
        if event == "CarrierLocation":
            loc = _carrier_system(carrier_id)
            if loc:
                logger.info(
                    "CarrierLocation [%s] in %s",
                    carrier_id,
                    loc,
                )
        else:
            _set_status(f"Carrier: {_carrier_display(carrier_id)}", "green")
        return None

    if event == "CarrierJumpRequest":
        # Learn/update first, but never let UI refresh failures block Discord.
        try:
            carrier_id = _update_carrier_from_entry(entry)
        except Exception:
            logger.exception("Failed updating carrier identity on jump request")
            carrier_id = _normalize_carrier_id(entry.get("CarrierID"))

        if carrier_id is not None:
            _pending_jumps[carrier_id] = dict(entry)
            # A new schedule supersedes any prior cancel/arrival dedupe for this carrier.
            _last_notified_cancel.pop(carrier_id, None)
            _last_notified_arrival.pop(carrier_id, None)

        from_system = _origin_for_carrier(carrier_id, fallback=system)
        destination = entry.get("SystemName", "Unknown")
        departure = str(entry.get("DepartureTime") or "").strip()
        logger.info(
            "CarrierJumpRequest [%s]: %s -> %s at %s",
            carrier_id,
            from_system,
            destination,
            departure or entry.get("DepartureTime"),
        )
        _set_status(f"Jump to {destination}", "cyan")

        # Consume adhoc pre-announce lock when this schedule matches.
        if carrier_id is not None and _adhoc_is_locked():
            locked_carrier = _adhoc_locked_carrier_id()
            locked_dest = str(_adhoc_preannounce.get("destination") or "").strip()
            dest_name = str(destination or "").strip()
            if (
                locked_carrier == carrier_id
                and locked_dest
                and dest_name
                and locked_dest.lower() == dest_name.lower()
            ):
                _clear_adhoc_preannounce_lock(
                    carrier_id=carrier_id,
                    reason="matching CarrierJumpRequest",
                )

        _refresh_expedition_ui()

        if _config_bool(CFG_NOTIFY_REQUEST, True):
            if not _notify_allowed_for_carrier(carrier_id):
                logger.info(
                    "Skipping CarrierJumpRequest notify for %s (carrier kind disabled)",
                    carrier_id,
                )
            elif _is_duplicate_notification(_last_notified_request, carrier_id, departure):
                logger.info(
                    "Skipping duplicate CarrierJumpRequest notify for %s at %s",
                    carrier_id,
                    departure,
                )
            else:
                payload = _build_jump_request_payload(
                    entry, from_system, carrier_id, cmdr=cmdr
                )
                _enqueue_discord(payload)

        return None

    if event == "CarrierJumpCancelled":
        carrier_id = _resolve_event_carrier_id(entry)
        try:
            if carrier_id is not None:
                _upsert_carrier(carrier_id, carrier_type=entry.get("CarrierType"))
        except Exception:
            logger.exception("Failed updating carrier identity on jump cancel")

        pending = _pending_jumps.get(carrier_id) if carrier_id is not None else None
        cancel_token = str(
            (pending or {}).get("DepartureTime")
            or entry.get("timestamp")
            or "cancelled"
        ).strip()

        logger.info("CarrierJumpCancelled for CarrierID=%s", carrier_id)
        _set_status("Jump cancelled", "orange")

        if _config_bool(CFG_NOTIFY_CANCEL, True):
            if not _notify_allowed_for_carrier(carrier_id):
                logger.info(
                    "Skipping CarrierJumpCancelled notify for %s (carrier kind disabled)",
                    carrier_id,
                )
            elif _is_duplicate_notification(_last_notified_cancel, carrier_id, cancel_token):
                logger.info(
                    "Skipping duplicate CarrierJumpCancelled notify for %s",
                    carrier_id,
                )
            else:
                payload = _build_jump_cancelled_payload(carrier_id, cmdr=cmdr)
                _enqueue_discord(payload)

        if carrier_id is not None:
            _pending_jumps.pop(carrier_id, None)
            _last_notified_request.pop(carrier_id, None)
            _clear_adhoc_preannounce_lock(
                carrier_id=carrier_id,
                reason="CarrierJumpCancelled",
            )
        _refresh_expedition_ui()
        return None

    if event == "CarrierJump":
        arrived = entry.get("StarSystem") or system or "Unknown"
        is_ours, carrier_id = _is_tracked_carrier_jump(entry)
        if not is_ours:
            logger.info(
                "Ignoring CarrierJump in %s (not a tracked carrier)",
                arrived,
            )
            return None

        try:
            if carrier_id is not None:
                _upsert_carrier(
                    carrier_id,
                    callsign=entry.get("StationName"),
                    carrier_type=entry.get("CarrierType") or entry.get("StationType"),
                    system=arrived if arrived and arrived != "Unknown" else None,
                )
        except Exception:
            logger.exception("Failed updating carrier identity on arrival")

        logger.info("CarrierJump arrival [%s] in %s", carrier_id, arrived)
        _set_status(f"Arrived: {arrived}", "green")

        # Advance on arrival only; clear pending before Discord so Progress/Next
        # reflect post-arrival state (not the hop that just finished).
        completion_posted = _handle_expedition_progress(
            arrived,
            cmdr=cmdr,
            carrier_id=carrier_id,
            complete_on_final=True,
        )
        if carrier_id is not None:
            _pending_jumps.pop(carrier_id, None)
            _last_notified_request.pop(carrier_id, None)
            _clear_adhoc_preannounce_lock(
                carrier_id=carrier_id,
                reason="CarrierJump arrival",
            )
        _refresh_expedition_ui()

        arrival_token = f"{arrived}|{entry.get('timestamp') or ''}"
        if _config_bool(CFG_NOTIFY_ARRIVAL, False):
            if completion_posted:
                logger.info(
                    "Skipping jump-arrival notify for %s; expedition completion posted instead",
                    carrier_id,
                )
            elif not _notify_allowed_for_carrier(carrier_id):
                logger.info(
                    "Skipping CarrierJump arrival notify for %s (carrier kind disabled)",
                    carrier_id,
                )
            elif _is_duplicate_notification(_last_notified_arrival, carrier_id, arrival_token):
                logger.info(
                    "Skipping duplicate CarrierJump arrival notify for %s in %s",
                    carrier_id,
                    arrived,
                )
            else:
                payload = _build_jump_arrival_payload(entry, carrier_id, cmdr=cmdr)
                _enqueue_discord(payload)

        return None

    return None
