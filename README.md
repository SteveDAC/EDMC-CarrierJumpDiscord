# EDMC Carrier Jump Discord

Elite Dangerous Market Connector (EDMC) plugin that posts to a Discord channel when your fleet carrier schedules or cancels a jump.

## What it does

- Watches the game journal via EDMC for:
  - `CarrierJumpRequest` — jump scheduled
  - `CarrierJumpCancelled` — jump cancelled
  - `CarrierJump` — arrival (optional; only when docked on a *tracked* carrier)
- Tracks **personal fleet carriers and squadron carriers separately** by Carrier ID
- Learns name / callsign / type from `CarrierStats`, `CarrierNameChanged`, and `CarrierBuy`
- Remembers tracked carriers across EDMC restarts
- Posts a concise Discord embed with carrier, type, route, body, and timing
- Uses Discord dynamic timestamps so departure/lockdown times show in each viewer's local timezone
- Sends Discord HTTP requests on a background thread so EDMC stays responsive
- Settings tab with webhook URL, toggles, dual carrier overrides, and a **Send test message** button

## Requirements

- Elite Dangerous (PC) with a fleet carrier
- [EDMarketConnector](https://github.com/EDCD/EDMarketConnector)
- A Discord channel webhook URL

## Install

1. Clone or download this repository into your EDMC plugins directory:
   - Windows: `%LOCALAPPDATA%\EDMarketConnector\plugins`
   - Or in EDMC: **File → Settings → Plugins → Open**
2. Ensure the folder is named `EDMC-CarrierJumpDiscord` and contains `load.py`.
3. Restart EDMC.
4. Confirm **Carrier Jump Discord** appears under Plugins.

## Configure

1. In Discord: channel settings → **Integrations → Webhooks → New Webhook** → copy the URL.
2. In EDMC: **File → Settings → Carrier Jump Discord**
3. Paste the webhook URL.
4. Optionally set:
   - Enable / disable notifications
   - Whether to notify on schedule, cancel, and/or arrival
   - A mention such as `@here`, `<@&role_id>`, or `<@user_id>`
   - Separate Fleet Carrier and Squadron Carrier name / callsign overrides
5. Click **Send test message** to verify the webhook.
6. Click OK to save.

**Tip:** Open management for each carrier in-game once so names and callsigns are learned independently. The settings tab shows currently tracked carriers.

## How it works

When you schedule a carrier jump, Elite writes a `CarrierJumpRequest` journal event (including destination and `DepartureTime`). EDMC forwards that event to this plugin, which builds an embed and POSTs it to your Discord webhook.

Approximate lockdown time is calculated as **departure − 3 minutes 20 seconds** (standard pad lockdown window). Treat it as a guide; Frontier timing can vary with jump queues.

## Development layout

```
EDMC-CarrierJumpDiscord/
  load.py      # EDMC plugin entry point
  README.md
```

Plugin version is `__version__` in `load.py` (`1.3.0`).

Times in Discord posts use Discord's `<t:unix:f>` / `<t:unix:R>` markup, so each viewer sees local date/time plus a relative countdown (for example "in 15 minutes").

Arrival notifications are off by default. Enable **Notify on jump arrival** in settings. Arrival posts only when the journal `CarrierJump` event matches a tracked carrier (by Carrier ID, callsign, or a pending jump to that system), so hitchhiking on an unrelated carrier should not notify.

Pending jumps are tracked per carrier ID, so a fleet carrier jump and a squadron carrier jump can be outstanding at the same time without overwriting each other.

## Notes

- This is intended for **your own** carrier jump schedule/cancel events (owner journal).
- Keep your webhook URL private; anyone with it can post to that channel.
- If the main-window status says **Webhook missing**, configure the URL in settings.
