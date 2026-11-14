# cambridge-ma-parking-checks

Two unattended checks for one street-parked car in Cambridge, MA, with alerts by email and Telegram.

1. **Towed-car check.** Searches the city's [Towed Vehicle Lookup](https://www.cambridgema.gov/departments/emergencycommunications/towedvehicles) for the plate at 7:30 am and 7:30 pm ET. A weekly freshness check proves the lookup is still live and receiving new tows, so "no hit" can be trusted.
2. **Street-cleaning check.** An iOS Shortcut logs where the car was parked. At 9:00 pm ET a job compares that spot with the cleaning schedule and alerts when cleaning falls on either of the next two mornings.

It runs on any small Linux VM (Ubuntu, 1 GB RAM is plenty) and keeps to itself, so it can share a host with other apps: its own system user (`parkingbot`), directory (`/opt/cambridge-ma-parking-checks`), venv, uv cache, state (`/var/lib/parking-checks/state.db`) and `parking-checks-*` units.

Nothing personal is in this repo. The plate, the coverage point the schedule was built around, alert addresses and all secrets live only in `.env` on the server.

## Commands

All checks accept `--dry-run` (print alerts, change nothing: it works on an in-memory copy of the database) and `--now 2026-12-03T21:00` (pretend time, Eastern).

| Command | Schedule (America/New_York) | Purpose |
| --- | --- | --- |
| `parking-checks tow-plate` | Hourly at :30 | Search the lookup for the plate (2 requests) |
| `parking-checks tow-freshness` | Wednesdays 7:00 am (`FRESHNESS_ON_CALENDAR`) | Confirm the lookup is live and current |
| `parking-checks cleaning-nightly` | 9:00 pm | Compare the last parked spot with the schedule |
| `parking-checks serve-intake` | always on | Receive locations from the Shortcut |
| `parking-checks tow-sweep` | every 3 h for the first 14 days | Sample the lookup for the latency log |
| `parking-checks tow-latency` | manual | Median and worst-case delay before a tow appears |
| `parking-checks notify-test` | manual | Send a test message on every channel |
| `parking-checks telegram-chat-id` | manual | Show chat ids that have messaged the bot |
| `parking-checks last-location` | manual | Print the newest stored location |

On the server, run them as the service user:

```sh
sudo -u parkingbot PARKING_CHECKS_ENV=/opt/cambridge-ma-parking-checks/.env \
  /opt/cambridge-ma-parking-checks/.venv/bin/parking-checks notify-test
```

## Alerts

Every alert goes through `Notifier.notify(kind, subject, body, dedupe_key)`. A failure on one channel never blocks the other, every send is logged in `alerts_sent`, and a key already sent on a channel is skipped there.

| Alert | Trigger | Repeat |
| --- | --- | --- |
| Car towed | Plate found in the lookup | Immediately, then every 12 h, up to 4 reminders (`TOW_REMINDER_*`) |
| Street cleaning | Cleaning on the parked block within 2 mornings | Heads-up two nights before, "move it" the night before |
| Schedule unknown | Parked where the file has no schedule | Once per parking event |
| Tow data may be stale | Freshness scan found no tow newer than 24 h | Once per day while it lasts, then one "recovered" note |
| Site or format changed | Lookup or calendar unreadable | Once per day while it lasts |
| Check cannot run | Any job crashed or lost its data source twice in a row | Once, then one "recovered" note |

Channels:

- **Email** through a Google Apps Script relay, because many cloud providers block outbound SMTP (ports 25, 465 and 587). `deploy/apps_script_email.gs` is a small web app in your Google account: it checks `EMAIL_WEBHOOK_SECRET`, mails only the addresses in its `ALLOWED_TO` list, and sends with `MailApp` (consumer quota: 100 emails a day). Setup steps are at the top of that file; `install.sh` generates the secret. Gmail SMTP with an app password is still supported (`SMTP_*`) on hosts where port 587 is open, and is used only when `EMAIL_WEBHOOK_URL` is empty.
- **Telegram** bot. In Telegram, message **@BotFather**, send `/newbot`, pick a name, and copy the token into `TELEGRAM_BOT_TOKEN`. Open the new bot, press **Start** and send it any message, then run `parking-checks telegram-chat-id` and put the id in `TELEGRAM_CHAT_IDS`. (A bot cannot message a phone number; it can only reply to chats that started with it. The second driver does the same and adds their id.)

## Deploy

Prerequisites on the VM: Ubuntu with Python 3.11+, `uv` on PATH, Tailscale up (`tailscale up --ssh`), and root SSH from your workstation.

```sh
deploy/push.sh root@your-vm     # streams the committed HEAD to /opt/cambridge-ma-parking-checks and runs install.sh
```

`install.sh` (root, idempotent): creates `parkingbot`, creates `.env` from `.env.example` with a fresh `INTAKE_TOKEN` if missing, builds the venv with `uv sync --frozen --no-dev` against the system Python, installs the units, renders the freshness timer from `FRESHNESS_ON_CALENDAR`, checks the config as `parkingbot`, enables the intake service and the four timers, and adds a Tailscale Funnel entry `https://<node>.<tailnet>.ts.net:8443 -> 127.0.0.1:8787` (set `SKIP_FUNNEL=1` to skip).

Funnel must be allowed for the node in the tailnet policy (`nodeAttrs` with `"attr": ["funnel"]`); if it is not, `tailscale funnel` prints the link to enable it. No inbound port needs opening in the VM's or provider's firewall: Funnel traffic arrives through Tailscale.

Then fill in `.env` on the VM (at least `PLATE`, `COVERAGE_CENTER_LAT`/`LON`, `ALERT_EMAILS` and one alert channel) and re-run the installer:

```sh
ssh root@your-vm
nano /opt/cambridge-ma-parking-checks/.env
bash /opt/cambridge-ma-parking-checks/deploy/install.sh
```

## `.env` reference

| Key | Default | Notes |
| --- | --- | --- |
| `PLATE`, `PLATE_STATE` | required, `MA` | Matching is exact, ignoring case, spaces and dashes |
| `COVERAGE_CENTER_LAT`, `COVERAGE_CENTER_LON` | | The point the schedule file was built around (e.g. home). Within 600 m, a street missing from the file is "street not in file"; beyond, "outside coverage" |
| `ALERT_EMAILS` | | Comma separated recipients |
| `EMAIL_WEBHOOK_URL`, `EMAIL_WEBHOOK_SECRET` | secret generated | Apps Script web app URL (`https://script.google.com/macros/s/.../exec`) and shared secret |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | Gmail, 587 | Fallback when no relay URL; often blocked on cloud VMs |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS` | | See Alerts |
| `INTAKE_TOKEN` | generated | Bearer token the Shortcut sends; at least 24 characters |
| `INTAKE_HOST`, `INTAKE_PORT` | `127.0.0.1`, `8787` | Funnel forwards here |
| `PUBLIC_BASE_URL` | | For reference only |
| `FRESHNESS_MAX_AGE_HOURS` | `24` | Stale threshold |
| `FRESHNESS_ON_CALENDAR` | `Wed *-*-* 07:00:00 America/New_York` | systemd calendar; re-run install.sh after changing |
| `TOW_REMINDER_HOURS`, `TOW_REMINDER_MAX` | `12`, `4` | |
| `CLEANING_LOOKAHEAD_DAYS` | `2` | |
| `LATENCY_SAMPLE_DAYS` | `14` | `0` disables `tow-sweep` |
| `DB_PATH` | `/var/lib/parking-checks/state.db` | |
| `SCHEDULE_PATH` | `data/street_cleaning.json` | |
| `HTTP_USER_AGENT` | project name | Name the project and a contact email |

## iOS Shortcut

Menu names vary slightly between iOS versions. Do this on each driver's iPhone. Get the token from the VM: `grep INTAKE_TOKEN /opt/cambridge-ma-parking-checks/.env`.

**Build the shortcut**

1. Open Shortcuts, tap **+**, name it `Log Parking Spot`.
2. Add **Get Current Location**. Expand it and set Precision to **Best**.
3. Add **Get Contents of URL**. URL: `https://<node>.<tailnet>.ts.net:8443/park`.
4. Expand it. Method **POST**. Under Headers add `Authorization` = `Bearer <INTAKE_TOKEN>`. (Alternatively skip Headers and add a Text field `token` = `<INTAKE_TOKEN>` to the JSON body below.)
5. Request Body **JSON**, all fields as Text:
   - `lat`: Current Location → **Latitude**
   - `lon`: Current Location → **Longitude**
   - `street`: Current Location → **Street**
   - `city`: Current Location → **City**
   - `device`: a fixed name such as `driver1-iphone`
   - `captured_at`: Current Date, format **ISO 8601**, include time
6. Add **Get Dictionary Value**, key `message`, from Contents of URL.
7. Add **Show Notification** with the Dictionary Value as text.
8. Tap play once. Allow location and network access ("Always Allow").

**Run it automatically**

1. **Automation** tab → **+** → **Bluetooth** → select the car → **Is Disconnected**.
2. **Run Immediately**, turn off **Notify When Run**, then pick `Log Parking Spot`.

**Settings and test**

1. Settings → Privacy & Security → Location Services → Shortcuts: While Using, **Precise Location** on.
2. Second driver: share with **Copy iCloud Link**, change `device`, and repeat the automation.
3. Park, switch the car off, and confirm the notification, e.g. `Saved: Franklin St. Next cleaning Fri Dec 4 (odd side), Mon Dec 14 (even side).` Then check: `curl -H "Authorization: Bearer $TOKEN" https://<host>:8443/last`.

Known limits: Bluetooth must be on; the trigger can fire a few seconds late; a fix beside buildings can be 10 to 30 m off. If there is no Bluetooth trigger, put the shortcut on the Home Screen and tap it after parking. When `street` is empty the endpoint reverse-geocodes with OpenStreetMap Nominatim.

## How it works

**Tow lookup (Step 0 findings, Nov 2026).** The page is ASP.NET WebForms. A search GETs the page, then POSTs `__VIEWSTATE`, `__VIEWSTATEGENERATOR`, `__EVENTVALIDATION` with `txtPlateNum` (contains-match, at least 3 characters) or `ddlMake`, plus `btnSearchTowLog`. Posting the disabled `ddlModel` causes HTTP 500. A **make-only search returns every tow for that make**, so the freshness check scans makes (about 40), not make and model pairs. Responses are a grid (`#gvSearchResults`, times with seconds), a single-tow detail view (times in minutes), or "No records were found". Tow times are compared at minute precision and treated as Eastern. The lookup held about 64 tows covering 14 days, about 4 to 5 a day; some makes (BENT, CHRYSLER) return 404/500 on search and are skipped.

**Freshness.** Makes are searched most-towed first (Honda, Toyota, Ford, Nissan, Subaru, Hyundai, Chevrolet, "Not Available", then the rest), stopping at the first tow under `FRESHNESS_MAX_AGE_HOURS`. Isolated HTTP errors are tolerated; an unparseable page, an unreachable site, or more failures than successes is reported as "site or format changed", separately from "data may be stale". Capped at 200 requests.

**Latency log.** Every parsed tow is saved with `first_seen_at`. Because the freshness check is weekly, `tow-sweep` samples all makes every 3 h for the first `LATENCY_SAMPLE_DAYS`, then stops itself. `tow-latency` reports delays for tows that first appeared after the first sampling run. It separates prompt postings from late entries (listed days after their tow time), which more frequent polling cannot catch any sooner. In November 2026, 19 of 24 tows appeared within about 3 h of their tow time; that sampling is why the plate check runs hourly.

**Cleaning schedule.** `data/street_cleaning.json` (schema 4: adds `street_aliases`, corrects `meta.fine`, keeps the coverage point in `.env`). The matcher splits the house number, expands abbreviations, applies aliases, takes **both** sides of the street, and picks segments by number (inside a range: that segment; in a gap or past the ends: every adjacent segment; no number: all). Null schedules flag `schedule_unknown`. Dates come from the city calendar (Recollect, unofficial, `after`=today, `before`=today+3), which includes holiday moves; a plain rule date the calendar does not list is still alerted, labelled. If the calendar fails, nth-weekday rules are used (April to December), confirmed relocations from the file are applied, unconfirmed holiday conflicts are flagged, and alerts say "computed from rule, not confirmed".

## Development

```sh
uv sync
uv run ruff check . && uv run ruff format --check . && uv run pytest
uv run parking-checks --env local.env tow-plate --dry-run
```

Tests never call the live sites; they use scrubbed fixtures in `tests/fixtures/` (real plates replaced) and a frozen clock. Never commit `.env`, the database, or saved lookup HTML with real plates.

## Troubleshooting

- **Status and logs:** `systemctl list-timers 'parking-checks-*'`, `journalctl -u 'parking-checks*' --since today`, `systemctl status parking-checks-intake`.
- **Run history:** `sqlite3 /var/lib/parking-checks/state.db "select * from runs order by id desc limit 20"`.
- **No alerts arrive:** `parking-checks notify-test`. Email relay: `unauthorized` means the script's `SECRET` differs from `.env`; `no allowed recipients` means `ALERT_EMAILS` is not in `ALLOWED_TO`; a non-JSON reply usually means the web app is not deployed with access "Anyone". Telegram: the chat must have messaged the bot first.
- **"The network connection was lost" from Get Contents of URL:** Tailscale's HTTPS front end rejected a malformed header (logged by `tailscaled` as `http2: ... PROTOCOL_ERROR`), usually a header key with a stray space or colon. Retype the key, or drop the header and send `token` in the JSON body.
- **Shortcut gets 401:** the `Authorization` header must be exactly `Bearer <token>`. 429 means more than 6 requests a minute.
- **Shortcut cannot connect:** `tailscale funnel status` should list `:8443`; `curl http://127.0.0.1:8787/healthz` on the VM.
- **"Site or format changed":** fetch the lookup by hand and compare with `tests/fixtures/`; update `tow/client.py` and the fixtures.
- **Signs win.** Walk the blocks you usually park on once and compare the posted signs with the districts' days in the schedule file. Re-verify the schedule file each April.
