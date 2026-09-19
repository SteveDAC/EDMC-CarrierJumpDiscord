# EDMC Carrier Jump Discord

Elite Dangerous Market Connector (EDMC) plugin that posts to a Discord channel when your fleet carrier or squadron carrier schedules, cancels, or (optionally) completes a jump.

## What it does

- Watches the game journal via EDMC for:
  - `CarrierJumpRequest` — jump scheduled
  - `CarrierJumpCancelled` — jump cancelled
  - `CarrierJump` — arrival (optional; only when docked on a *tracked* carrier)
- Tracks **personal fleet carriers and squadron carriers separately** by Carrier ID
- Learns name / callsign / type from `CarrierStats`, `CarrierNameChanged`, `CarrierBuy`, and `CarrierLocation`
- Remembers tracked carriers across EDMC restarts
- Posts a concise Discord embed with carrier, type, route, body, and timing
- Uses Discord dynamic timestamps so departure/lockdown times show in each viewer's local timezone
- Supports **webhook** or **bot token + channel ID** delivery
- Sends Discord HTTP requests on a background thread so EDMC stays responsive
- Settings tab with delivery options, toggles, dual carrier overrides, and a **Send test message** button

## Requirements

- Elite Dangerous (PC) with a fleet and/or squadron carrier
- [EDMarketConnector](https://github.com/EDCD/EDMarketConnector) on **Windows or Linux**
- Either:
  - A Discord channel webhook URL, **or**
  - A Discord bot token and channel ID

This plugin is pure Python and uses only EDMC's plugin APIs, so it works the same on Linux as on Windows. macOS is not supported.

## Install

### From a release (recommended)

1. Download the latest `EDMC-CarrierJumpDiscord-vX.Y.Z.zip` from
   [Releases](https://github.com/SteveDAC/EDMC-CarrierJumpDiscord/releases).
2. In EDMC open **File → Settings → Plugins → Open**.
3. Extract the zip so you get a folder named `EDMC-CarrierJumpDiscord` containing `load.py`
   directly inside the plugins directory (not nested an extra level).
4. Restart EDMC.
5. Confirm **Carrier Jump Discord** appears under Plugins.

Default plugin locations:

| OS | Plugins folder |
|---|---|
| Windows | `%LOCALAPPDATA%\EDMarketConnector\plugins` |
| Linux | `~/.local/share/EDMarketConnector/plugins` (or `$XDG_DATA_HOME/EDMarketConnector/plugins` if set) |

### From Git (optional)

```bash
git clone https://github.com/SteveDAC/EDMC-CarrierJumpDiscord.git \
  ~/.local/share/EDMarketConnector/plugins/EDMC-CarrierJumpDiscord
```

On Windows, clone into `%LOCALAPPDATA%\EDMarketConnector\plugins\EDMC-CarrierJumpDiscord` instead, then restart EDMC.

### Linux notes

- EDMC must be able to read your Elite Dangerous journal files. If you play via Steam Play / Proton, journals are commonly under:

  ```text
  ~/.steam/steam/steamapps/compatdata/359320/pfx/drive_c/users/steamuser/Saved Games/Frontier Developments/Elite Dangerous
  ```

  Point **Settings → Configuration → E:D journal file location** at that folder if EDMC does not find journals automatically. See the [EDMC Installation & Setup wiki](https://github.com/EDCD/EDMarketConnector/wiki/Installation-&-Setup) for details.
- Discord setup (webhook or bot) is identical on Linux; no extra Linux-specific Discord steps are required.

## Configure

### Option A — Webhook (default)

1. In Discord: channel settings → **Integrations → Webhooks → New Webhook** → copy the URL.
2. In EDMC: **File → Settings → Carrier Jump Discord**
3. Choose **Webhook URL** as the delivery method.
4. Paste the webhook URL.
5. Click **Send test message**.

### Option B — Bot token

1. Create a Discord application/bot at the [Discord Developer Portal](https://discord.com/developers/applications).
2. Copy the bot token.
3. Invite the bot to your server with at least **View Channel**, **Send Messages**, and **Embed Links**.
4. Enable Developer Mode in Discord, right-click the target channel → **Copy Channel ID**.
5. In EDMC settings, choose **Bot token + channel ID**.
6. Paste the bot token and channel ID.
7. Click **Send test message**.

### Shared options

- Enable / disable notifications
- Notify on schedule, cancel, and/or arrival
- Optional mention such as `@here`, `<@&role_id>`, or `<@user_id>`
- Separate Fleet Carrier and Squadron Carrier name / callsign overrides

**Tip:** Open management for each carrier in-game once so names and callsigns are learned independently. The settings tab shows currently tracked carriers.

## How it works

When you schedule a carrier jump, Elite writes a `CarrierJumpRequest` journal event (including destination and `DepartureTime`). EDMC forwards that event to this plugin, which builds an embed and posts it using your chosen Discord delivery method.

Approximate lockdown time is calculated as **departure − 3 minutes 20 seconds** (standard pad lockdown window). Treat it as a guide; Frontier timing can vary with jump queues.

## Development layout

```
EDMC-CarrierJumpDiscord/
  load.py      # EDMC plugin entry point
  README.md
```

Plugin version is `__version__` in `load.py` (`1.4.0`).

Times in Discord posts use Discord's `<t:unix:f>` / `<t:unix:R>` markup, so each viewer sees local date/time plus a relative countdown (for example "in 15 minutes").

Arrival notifications are off by default. Enable **Notify on jump arrival** in settings. Arrival posts only when the journal `CarrierJump` event matches a tracked carrier (by Carrier ID, callsign, or a pending jump to that system), so hitchhiking on an unrelated carrier should not notify.

Pending jumps are tracked per carrier ID, so a fleet carrier jump and a squadron carrier jump can be outstanding at the same time without overwriting each other.

## Notes

- This is intended for carrier jump schedule/cancel events from the commander who plots them.
- Keep webhook URLs and especially bot tokens private.
- Bot tokens are more sensitive than webhooks; treat them like passwords.
- If the main-window status says **Webhook missing** / **Bot token missing** / **Channel ID missing**, finish delivery setup in settings.
