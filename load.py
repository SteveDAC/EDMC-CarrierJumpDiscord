"""
EDMC-CarrierJumpDiscord

Publishes fleet carrier and squadron carrier jump schedule, cancel, and
optional arrival events to a Discord webhook.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import tkinter as tk
from tkinter import ttk

import myNotebook as nb
from config import appname, config

# ---------------------------------------------------------------------------
# Plugin identity / logging
# ---------------------------------------------------------------------------

PLUGIN_NAME = "Carrier Jump Discord"
__version__ = "1.3.0"

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

# ---------------------------------------------------------------------------
# Config keys (unique prefix to avoid clashes)
# ---------------------------------------------------------------------------

CFG_WEBHOOK = "edmc_cjd_webhook_url"
CFG_ENABLED = "edmc_cjd_enabled"
CFG_NOTIFY_REQUEST = "edmc_cjd_notify_request"
CFG_NOTIFY_CANCEL = "edmc_cjd_notify_cancel"
CFG_NOTIFY_ARRIVAL = "edmc_cjd_notify_arrival"
# Legacy single-carrier overrides (treated as fleet carrier overrides).
CFG_CARRIER_NAME = "edmc_cjd_carrier_name"
CFG_CARRIER_CALLSIGN = "edmc_cjd_carrier_callsign"
CFG_SQUADRON_NAME = "edmc_cjd_squadron_name"
CFG_SQUADRON_CALLSIGN = "edmc_cjd_squadron_callsign"
CFG_MENTION = "edmc_cjd_mention"
CFG_CARRIERS_JSON = "edmc_cjd_carriers_json"

WEBHOOK_RE = re.compile(
    r"^https://(?:discord(?:app)?\.com|discord\.com)/api/webhooks/\d+/[\w-]+$",
    re.IGNORECASE,
)

# Typical fleet/squadron carrier pad lockdown is ~3m20s before departure.
LOCKDOWN_BEFORE_DEPARTURE = timedelta(minutes=3, seconds=20)

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
_enabled_var: Optional[tk.BooleanVar] = None
_notify_request_var: Optional[tk.BooleanVar] = None
_notify_cancel_var: Optional[tk.BooleanVar] = None
_notify_arrival_var: Optional[tk.BooleanVar] = None
_fleet_name_var: Optional[tk.StringVar] = None
_fleet_callsign_var: Optional[tk.StringVar] = None
_squadron_name_var: Optional[tk.StringVar] = None
_squadron_callsign_var: Optional[tk.StringVar] = None
_mention_var: Optional[tk.StringVar] = None

_status_label: Optional[tk.Label] = None
_prefs_status: Optional[tk.Label] = None
_tracked_label: Optional[tk.Label] = None

_current_system: Optional[str] = None
# carrier_id -> {name, callsign, kind, carrier_type}
_carriers: dict[int, dict[str, str]] = {}
# carrier_id -> latest CarrierJumpRequest entry
_pending_jumps: dict[int, dict[str, Any]] = {}

_worker_queue: queue.Queue[Optional[dict[str, Any]]] = queue.Queue()
_worker_thread: Optional[threading.Thread] = None
_stop_worker = threading.Event()


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
            }
    _carriers = loaded


def _save_carriers_to_config() -> None:
    payload = {
        str(carrier_id): {
            "name": info.get("name", ""),
            "callsign": info.get("callsign", ""),
            "kind": info.get("kind", KIND_UNKNOWN),
            "carrier_type": info.get("carrier_type", ""),
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
        }
    return _carriers.get(
        carrier_id,
        {
            "name": "",
            "callsign": "",
            "kind": KIND_UNKNOWN,
            "carrier_type": "",
        },
    )


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

    updated = {
        "name": (str(name).strip() if name else "") or current.get("name", ""),
        "callsign": (str(callsign).strip() if callsign else "") or current.get("callsign", ""),
        "kind": kind,
        "carrier_type": (
            str(carrier_type).strip()
            if carrier_type
            else current.get("carrier_type", "")
        ),
    }

    _carriers[carrier_id] = updated
    _save_carriers_to_config()
    _refresh_tracked_label()
    return carrier_id


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


def _post_discord(payload: dict[str, Any]) -> tuple[bool, str]:
    webhook = _config_str(CFG_WEBHOOK).strip()
    if not webhook:
        return False, "Discord webhook URL is not configured"

    if not WEBHOOK_RE.match(webhook):
        return False, "Discord webhook URL looks invalid"

    try:
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
            return True, "Posted to Discord"
        return False, f"Discord HTTP {response.status_code}: {response.text[:200]}"
    except Exception as exc:
        logger.exception("Discord webhook request failed")
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

    webhook = _config_str(CFG_WEBHOOK).strip()
    if not webhook:
        logger.warning("No Discord webhook configured")
        _set_status("Webhook missing", "orange")
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


def _collect_fields(*fields: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    return [field for field in fields if field]


def _carrier_fields(carrier_id: Optional[int]) -> list[dict[str, Any]]:
    info = _carrier_record(carrier_id)
    kind = info.get("kind") or KIND_UNKNOWN
    return _collect_fields(
        _embed_field("Carrier", _carrier_display(carrier_id, kind)),
        _embed_field("Type", _kind_label(kind)),
    )


def _build_jump_request_payload(
    entry: dict[str, Any],
    from_system: Optional[str],
    carrier_id: Optional[int],
) -> dict[str, Any]:
    departure = _parse_journal_time(entry.get("DepartureTime"))
    lockdown = departure - LOCKDOWN_BEFORE_DEPARTURE if departure else None
    destination = entry.get("SystemName") or "Unknown"
    body = entry.get("Body")
    display = _carrier_display(carrier_id)

    fields = _carrier_fields(carrier_id) + _collect_fields(
        _embed_field("From", from_system or "Unknown"),
        _embed_field("Destination", destination),
        _embed_field("Body", body),
        _embed_field("Departure", _format_discord_time(departure)),
        _embed_field("Lockdown (approx)", _format_discord_time(lockdown)),
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
                ),
                "color": 0x3498DB,
                "fields": fields,
                "footer": {"text": f"{PLUGIN_NAME} v{__version__}"},
            }
        ],
    }


def _build_jump_cancelled_payload(carrier_id: Optional[int]) -> dict[str, Any]:
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
    fields = _carrier_fields(carrier_id) + _collect_fields(
        _embed_field("Was heading to", pending_dest),
        _embed_field("Body", pending_body),
        _embed_field("Was departing", pending_departure),
    )

    mention = _config_str(CFG_MENTION).strip()
    return {
        "username": "EDMC Carrier Jump",
        "content": mention or None,
        "embeds": [
            {
                "title": "Carrier jump cancelled",
                "description": f"**{display}** jump has been cancelled.",
                "color": 0xE67E22,
                "fields": fields,
                "footer": {"text": f"{PLUGIN_NAME} v{__version__}"},
            }
        ],
    }


def _build_jump_arrival_payload(
    entry: dict[str, Any],
    carrier_id: Optional[int],
) -> dict[str, Any]:
    arrived = entry.get("StarSystem") or "Unknown"
    body = entry.get("Body")
    display = _carrier_display(carrier_id)

    fields = _carrier_fields(carrier_id) + _collect_fields(
        _embed_field("Arrived in", arrived),
        _embed_field("Body", body),
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
    return _upsert_carrier(
        carrier_id,
        name=entry.get("Name") or entry.get("CarrierName"),
        callsign=entry.get("Callsign"),
        carrier_type=entry.get("CarrierType"),
    )


def _remember_system(entry: dict[str, Any], system: Optional[str]) -> None:
    global _current_system
    if entry.get("StarSystem"):
        _current_system = entry["StarSystem"]
    elif system:
        _current_system = system


# ---------------------------------------------------------------------------
# EDMC plugin hooks
# ---------------------------------------------------------------------------


def plugin_start3(plugin_dir: str) -> str:
    """Load this plugin into EDMarketConnector."""
    global _worker_thread

    _load_carriers_from_config()

    _stop_worker.clear()
    _worker_thread = threading.Thread(
        target=_worker_loop,
        name=f"{plugin_name}-discord-worker",
        daemon=True,
    )
    _worker_thread.start()

    logger.info(
        "%s v%s started from %s (%d carrier(s) cached)",
        PLUGIN_NAME,
        __version__,
        plugin_dir,
        len(_carriers),
    )
    return PLUGIN_NAME


def plugin_stop() -> None:
    """Shut down background worker."""
    _save_carriers_to_config()
    _stop_worker.set()
    _worker_queue.put(None)
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=2.0)
    logger.info("%s stopped", PLUGIN_NAME)


def plugin_app(parent: tk.Frame) -> tuple[tk.Label, tk.Label]:
    """Add a status row to the EDMC main window."""
    global _status_label
    label = tk.Label(parent, text="Carrier Discord:")
    _status_label = tk.Label(parent, text="Ready", anchor=tk.W)
    if not _config_str(CFG_WEBHOOK).strip():
        _set_status("Webhook missing", "orange")
    elif not _config_bool(CFG_ENABLED, True):
        _set_status("Disabled", "orange")
    elif _carriers:
        _set_status(f"Tracking {len(_carriers)} carrier(s)", "green")
    else:
        _set_status("Ready", "green")
    return label, _status_label


def plugin_prefs(parent: nb.Notebook, cmdr: str, is_beta: bool) -> tk.Frame:
    """Settings tab for Discord webhook and notification options."""
    global _webhook_var, _enabled_var, _notify_request_var, _notify_cancel_var
    global _notify_arrival_var, _fleet_name_var, _fleet_callsign_var
    global _squadron_name_var, _squadron_callsign_var, _mention_var
    global _prefs_status, _tracked_label

    _webhook_var = tk.StringVar(value=_config_str(CFG_WEBHOOK))
    _enabled_var = tk.BooleanVar(value=_config_bool(CFG_ENABLED, True))
    _notify_request_var = tk.BooleanVar(value=_config_bool(CFG_NOTIFY_REQUEST, True))
    _notify_cancel_var = tk.BooleanVar(value=_config_bool(CFG_NOTIFY_CANCEL, True))
    _notify_arrival_var = tk.BooleanVar(value=_config_bool(CFG_NOTIFY_ARRIVAL, False))
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
    nb.Label(frame, text="Discord webhook URL").grid(
        row=row, column=0, sticky=tk.W, padx=10, pady=(10, 2)
    )
    row += 1
    ttk.Entry(frame, textvariable=_webhook_var, width=70).grid(
        row=row, column=0, columnspan=2, sticky=tk.EW, padx=10, pady=2
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
            "are learned separately. Personal and squadron carriers are tracked by ID."
        ),
        wraplength=560,
        justify=tk.LEFT,
    ).grid(row=row, column=0, columnspan=2, sticky=tk.W, padx=10, pady=(4, 10))

    return frame


def prefs_changed(cmdr: str, is_beta: bool) -> None:
    """Persist settings when the preferences dialog is closed."""
    if _webhook_var is not None:
        config.set(CFG_WEBHOOK, _webhook_var.get().strip())
    if _enabled_var is not None:
        config.set(CFG_ENABLED, bool(_enabled_var.get()))
    if _notify_request_var is not None:
        config.set(CFG_NOTIFY_REQUEST, bool(_notify_request_var.get()))
    if _notify_cancel_var is not None:
        config.set(CFG_NOTIFY_CANCEL, bool(_notify_cancel_var.get()))
    if _notify_arrival_var is not None:
        config.set(CFG_NOTIFY_ARRIVAL, bool(_notify_arrival_var.get()))
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

    _refresh_tracked_label()

    if not _config_str(CFG_WEBHOOK).strip():
        _set_status("Webhook missing", "orange")
    elif not _config_bool(CFG_ENABLED, True):
        _set_status("Disabled", "orange")
    elif _carriers:
        _set_status(f"Tracking {len(_carriers)} carrier(s)", "green")
    else:
        _set_status("Ready", "green")


def _send_test_message() -> None:
    """Post a sample embed using the values currently shown in prefs."""
    if _webhook_var is not None:
        config.set(CFG_WEBHOOK, _webhook_var.get().strip())
    if _mention_var is not None:
        config.set(CFG_MENTION, _mention_var.get().strip())
    if _fleet_name_var is not None:
        config.set(CFG_CARRIER_NAME, _fleet_name_var.get().strip())
    if _fleet_callsign_var is not None:
        config.set(CFG_CARRIER_CALLSIGN, _fleet_callsign_var.get().strip())
    if _squadron_name_var is not None:
        config.set(CFG_SQUADRON_NAME, _squadron_name_var.get().strip())
    if _squadron_callsign_var is not None:
        config.set(CFG_SQUADRON_CALLSIGN, _squadron_callsign_var.get().strip())

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
    )
    if temporary:
        _carriers.pop(test_id, None)
    payload["embeds"][0]["title"] = "Carrier jump scheduled (TEST)"
    payload["embeds"][0]["description"] = (
        f"Test message from **{PLUGIN_NAME}**. "
        f"If you see this, the webhook is working."
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
    event = entry.get("event")
    if not event:
        return None

    if event in ("Location", "FSDJump", "CarrierJump", "StartUp"):
        _remember_system(entry, system)

    if event in ("CarrierStats", "CarrierNameChanged", "CarrierBuy", "CarrierLocation"):
        carrier_id = _update_carrier_from_entry(entry)
        if event != "CarrierLocation":
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

        from_system = _current_system or system
        destination = entry.get("SystemName", "Unknown")
        logger.info(
            "CarrierJumpRequest [%s]: %s -> %s at %s",
            carrier_id,
            from_system,
            destination,
            entry.get("DepartureTime"),
        )
        _set_status(f"Jump to {destination}", "cyan")

        if _config_bool(CFG_NOTIFY_REQUEST, True):
            payload = _build_jump_request_payload(entry, from_system, carrier_id)
            _enqueue_discord(payload)
        return None

    if event == "CarrierJumpCancelled":
        carrier_id = _resolve_event_carrier_id(entry)
        try:
            if carrier_id is not None:
                _upsert_carrier(carrier_id, carrier_type=entry.get("CarrierType"))
        except Exception:
            logger.exception("Failed updating carrier identity on jump cancel")

        logger.info("CarrierJumpCancelled for CarrierID=%s", carrier_id)
        _set_status("Jump cancelled", "orange")

        if _config_bool(CFG_NOTIFY_CANCEL, True):
            payload = _build_jump_cancelled_payload(carrier_id)
            _enqueue_discord(payload)

        if carrier_id is not None:
            _pending_jumps.pop(carrier_id, None)
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
                )
        except Exception:
            logger.exception("Failed updating carrier identity on arrival")

        logger.info("CarrierJump arrival [%s] in %s", carrier_id, arrived)
        _set_status(f"Arrived: {arrived}", "green")

        if _config_bool(CFG_NOTIFY_ARRIVAL, False):
            payload = _build_jump_arrival_payload(entry, carrier_id)
            _enqueue_discord(payload)

        if carrier_id is not None:
            _pending_jumps.pop(carrier_id, None)
        return None

    return None
