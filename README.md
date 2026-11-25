# Certificate Tracker

Self-hosted web app that tracks certificate expiry dates and sends phone notifications
(via a self-hosted [ntfy](https://ntfy.sh) server) before they expire.
Runs as two small Docker containers on a home NAS, used by several people. Each user has a private list:
they see only their own entries and get reminders only for those.

- Fully customisable table (add, rename, reorder, retype and delete columns)
- Private per user: own entries, own notification topic
- Views: **Cards**, **Grouped**, **Timeline**, **Table** (inline editing), with shared search and filters
- Daily reminders from *expiry date − notification window*, repeated every day until the entry is **renewed** or **snoozed**
- Notification buttons: **Snooze 1 day** and **Open** (longer snoozes from the app)
- Audit log per entry, CSV export and bulk CSV import (with template), dark mode, installable on the phone home screen (PWA)
- Memory at idle (measured): app ≈ 35 MB, ntfy ≈ 50 MB

---

## Contents

1. [How it fits together](#1-how-it-fits-together)
2. [Local development](#2-local-development-console-notifier)
3. [Deploy on the Ugreen NAS (UGOS)](#3-deploy-on-the-ugreen-nas-ugos)
4. [ntfy users and the publisher token](#4-ntfy-users-and-the-publisher-token)
5. [Publish with Tailscale Funnel](#5-publish-the-app-and-ntfy-with-tailscale-funnel)
6. [Subscribe on Android and iPhone](#6-subscribe-on-android-and-iphone)
7. [Using the app](#7-using-the-app)
8. [Backups and updates](#8-backups-and-updates)
9. [Configuration reference](#9-configuration-reference)
10. [Troubleshooting](#10-troubleshooting)
11. [Assumptions](#11-assumptions)
12. [Project layout and tests](#12-project-layout-and-tests)

---

## 1. How it fits together

```
Phone ──HTTPS──► Tailscale Funnel (Tailscale container on the NAS)
                   ├─ :8443  ─► http://<NAS-LAN-IP>:8085 ─► app   (container port 8080)
                   └─ :10000 ─► http://<NAS-LAN-IP>:8095 ─► ntfy  (container port 2586)

app ──http://ntfy:2586 (internal Docker network, write-only token)──► ntfy
ntfy ──wake-up signal only (no message text)──► ntfy.sh ──► Apple push ──► iPhone
```

- Funnel only allows public ports **443, 8443 and 10000**. On this NAS, 443 is already used by
  `tailscale serve` for Jellyfin (tailnet only), so the app uses **8443** and ntfy uses **10000**.
  Jellyfin stays private.
- The app listens on plain HTTP. Tailscale terminates HTTPS and adds `X-Forwarded-For` / `X-Forwarded-Proto`,
  which the app trusts when `TRUST_PROXY=true`.
- Data lives in `./data` (SQLite) and `./ntfy/data` (ntfy users, tokens and the 72-hour message cache).

---

## 2. Local development (Console notifier)

You need Docker. A local Python install is not required.

**Run the tests:**

```sh
sh tools/test.sh            # all tests
sh tools/test.sh -k token   # a subset
```

**Run the app locally** with notifications printed to the log instead of sent:

```sh
docker build -t certtracker .
docker run --rm -p 8080:8080 -v "$PWD/devdata:/data" \
  -e NOTIFIER=console -e PUBLIC_BASE_URL=http://localhost:8080 \
  -e ADMIN_USERNAME=admin -e ADMIN_PASSWORD=dev-password-123 \
  certtracker
```

Open http://localhost:8080 and log in as `admin`. Reminders appear in the container output as
`NOTIFY [p3] Certificate expiring: …`. With Python 3.12 installed you can run `pip install -r requirements-dev.txt`,
then `python -m pytest`, then `DATA_DIR=./devdata NOTIFIER=console python -m app`.

---

## 3. Deploy on the Ugreen NAS (UGOS)

Tested target: Ugreen **DXP2800** (Intel N100, `linux/amd64`). The image also builds on arm64.
All commands run over **SSH** on the NAS. Enable SSH in UGOS under *Control Panel → Terminal*.
The Docker app in UGOS shows the containers afterwards, but building the image needs the command line.

### 3.1 Copy the project to the NAS

```sh
sudo mkdir -p /volume1/docker/certtracker
cd /volume1/docker/certtracker
# Either clone the repository here, or copy the project files into this folder over SMB.
sudo git clone <your-repo-url> .
```

### 3.2 Create the data folders with the right owner

The containers run as a non-root user. Find your user and group ids, then let that user own the data folders:

```sh
id                                   # e.g. uid=1000(you) gid=10(admin)
sudo mkdir -p data ntfy/data
sudo chown -R 1000:1000 data ntfy/data   # use the uid:gid you will put in PUID/PGID
```

### 3.3 Create `.env`

```sh
sudo cp .env.example .env
sudo nano .env
```

Set at least the following:

| Variable | Value |
|---|---|
| `SECRET_KEY` | output of `openssl rand -base64 48` |
| `PUBLIC_BASE_URL` | `https://<nas>.<tailnet>.ts.net:8443` |
| `NTFY_PUBLIC_URL` | `https://<nas>.<tailnet>.ts.net:10000` |
| `NTFY_TOPIC` | topic **prefix**, hard to guess, e.g. `certs-` + `openssl rand -hex 4`. Each user's topic is `<prefix>-<username>` |
| `PUID` / `PGID` | the ids from step 3.2 |
| `APP_HOST_PORT` / `NTFY_HOST_PORT` | `8085` / `8095` (both free on this NAS; qBittorrent uses 8090) |

Your MagicDNS name is the host shown by `sudo docker exec tailscale_tailscale-1 tailscale serve status`
(e.g. `https://<nas>.<tailnet>.ts.net`), or in the Tailscale admin console under *Machines*.
Protect the file: `sudo chmod 600 .env`.
Leave `NTFY_TOKEN` empty for now.

### 3.4 Start ntfy and create the publisher token

```sh
sudo docker compose up -d ntfy
```

Follow [section 4](#4-ntfy-users-and-the-publisher-token). Put the token in `.env` (`NTFY_TOKEN=tk_…`).

### 3.5 Build and start everything

```sh
sudo docker compose up -d --build
sudo docker compose ps          # both should become "healthy"
sudo docker compose logs -f app
```

### 3.6 Create the first admin

There are **no default credentials**. Choose one of these:

- **Setup screen (recommended).** On first start the app writes a one-time code to `data/setup-code.txt`.
  Read it with `sudo cat data/setup-code.txt`. Open the app, go to `/setup`, enter the code, and choose the admin username and password.
  The code file is deleted after use.
  Do this before you enable Funnel, or right after. Without the code nobody else can claim the admin account.
- **Environment.** Set `ADMIN_USERNAME` and `ADMIN_PASSWORD` in `.env` before the first start.
  Remove them afterwards. They are only used when no users exist.

Before Funnel is set up you can reach the app on the LAN at `http://<NAS-LAN-IP>:8085`.
Login cookies are marked `Secure` when `PUBLIC_BASE_URL` is https, so logging in over plain-HTTP LAN does not work.
Use the Funnel URL, or temporarily set `COOKIE_SECURE=false`.

---

## 4. ntfy users and the publisher token

ntfy runs with `auth-default-access: deny-all`, so every user must be created explicitly.
The CLI inside the container reads `/etc/ntfy/server.yml` and finds the user database by itself.

**Two separate logins.** A user created in the app's **Users** tab only gets a *website* login.
For phone notifications the same person also needs an *ntfy* login.
Every person gets a **personal topic**, `<NTFY_TOPIC>-<username>`, for example `certs-49fd2c76-merlaru`.
Reminders for their entries go only there, and their ntfy login can read only that topic.
The **Users** page in the app shows each person's topic and the exact two commands to paste.

```sh
cd /volume1/docker/certtracker
PREFIX=$(sudo grep ^NTFY_TOPIC .env | cut -d= -f2)

# 1) Publisher account used by the app: write-only on all personal topics (the * is a wildcard)
sudo docker exec -e NTFY_PASSWORD="$(openssl rand -hex 24)" certtracker-ntfy ntfy user add publisher
sudo docker exec certtracker-ntfy ntfy access publisher "$PREFIX-*" write-only
sudo docker exec certtracker-ntfy ntfy token add --label=certtracker publisher
#    -> prints "token tk_... created". Put it in .env as NTFY_TOKEN=tk_..., then:
sudo docker compose up -d app

# 2) One ntfy account per person, read-only on their own topic (copy these lines from the Users page)
sudo docker exec -it certtracker-ntfy ntfy user add merlaru            # prompts for a password
sudo docker exec certtracker-ntfy ntfy access merlaru "$PREFIX-merlaru" read-only
```

Useful commands: `ntfy user list`, `ntfy user change-pass alice`, `ntfy user del alice`, `ntfy token list`,
`ntfy token remove publisher tk_…`. Prefix each with `sudo docker exec -it certtracker-ntfy`.

The token carries the publisher account's rights, which are write-only on the personal topics.
The app sends it only to `NTFY_URL` over the internal Docker network.

Check it: in the app go to **Notification setup → Send me a test notification** (every user can do this). The page shows either "sent" or the exact
error from ntfy, for example `HTTP 403` for a missing access rule or `HTTP 401` for a wrong token.

---

## 5. Publish the app and ntfy with Tailscale Funnel

Checked against the Tailscale docs (Funnel and the `tailscale funnel` CLI, validated January 2026).
Funnel needs Tailscale ≥ 1.38.3, and the `--bg` syntax needs ≥ 1.52. This NAS runs 1.104.

### 5.1 One-time setup in the Tailscale admin console

1. **DNS** page: make sure **MagicDNS** is on, and enable **HTTPS Certificates**.
2. **Access controls** (policy file): allow Funnel by adding the `funnel` node attribute, for example:

   ```jsonc
   "nodeAttrs": [
     { "target": ["autogroup:member"], "attr": ["funnel"] },
   ],
   ```

   You can restrict `target` to the NAS's tag or user. If the attribute is missing, the first `tailscale funnel`
   command prints a link that adds it for you.

### 5.2 Turn on Funnel for the two ports

Tailscale runs in a container (`tailscale_tailscale-1`, bridge network), so the targets are the NAS's
**LAN IP** and the **host ports**. These are the same kind of target your existing Jellyfin `serve` rule already uses.

```sh
NAS_IP=192.168.50.133   # your NAS LAN IP. Reserve it in the router's DHCP settings so it never changes.

sudo docker exec tailscale_tailscale-1 tailscale funnel --bg --https=8443  http://$NAS_IP:8085
sudo docker exec tailscale_tailscale-1 tailscale funnel --bg --https=10000 http://$NAS_IP:8095
sudo docker exec tailscale_tailscale-1 tailscale funnel status
```

The status should list `https://<nas>.<tailnet>.ts.net:8443 (Funnel on)` and `…:10000 (Funnel on)`.
Your Jellyfin entry on 443 should still say *tailnet only*. Funnel applies per port, so 443 stays private.

- The configuration is stored in the Tailscale state directory (`TS_STATE_DIR=/var/lib/tailscale`).
  It survives restarts as long as that directory is a mounted volume in your Tailscale compose file.
- To turn a port off: `sudo docker exec tailscale_tailscale-1 tailscale funnel --https=8443 off`.
- To test, use a phone with **Wi-Fi off**: open `https://<nas>.<tailnet>.ts.net:8443`.
  The first request can take a few seconds while the certificate is issued.

**If the LAN-IP target is refused.** Tailscale documents `http://127.0.0.1:<port>` as the supported proxy target.
Your existing serve rule shows that LAN targets work on this version. If a future version refuses them,
switch the Tailscale container to `network_mode: host` in its compose file. Then use
`http://127.0.0.1:8085` and `http://127.0.0.1:8095` as targets.

---

## 6. Subscribe on Android and iPhone

Every user can open **Notification setup** in the app menu. It shows the server URL, **their personal topic**, an
`ntfy://` subscribe link and a QR code.

**Android** (ntfy app from Google Play or F-Droid)

1. Settings → **Manage users** → add user: server `https://<nas>.<tailnet>.ts.net:10000`, plus your ntfy username and password.
2. Tap **+** → topic name → **Use another server** → enter the server URL → *Subscribe*.
   You can also tap the `ntfy://` link or scan the QR code.
3. Allow notifications. Optionally exempt ntfy from battery optimisation.

**iPhone** (ntfy app from the App Store)

1. Settings → **Users** → add user for the server URL above.
2. Tap **+** → topic name → enable **Use another server** → enter the server URL → *Subscribe*.
3. Allow notifications. Instant delivery uses `upstream-base-url: https://ntfy.sh`. Only a wake-up
   signal (message id and a hash of the topic URL) passes through ntfy.sh. The message text never does.
   The phone then fetches the message from your server.

Then press **Notification setup → Send me a test notification** in the app. Its "Snooze 1 day" button answers *Test OK*.

---

## 7. Using the app

| Feature | Where |
|---|---|
| Dashboard counts per status | top of the entries page (tap a tile to filter) |
| Views | tabs *Cards / Grouped / Timeline / Table*. Search, status and Center filters apply to all of them |
| Inline editing | *Table* view: tap a cell, press Enter to save and Esc to cancel |
| Snooze | **Snooze** on any entry (1 day, 3 days, 1 week, 2 weeks, or custom days/weeks); bulk: tick entries, then use the bar at the bottom; or the notification's **Snooze 1 day** button (for longer, tap **Open** and snooze in the app) |
| Renew | **Renewed** / **Mark as renewed**: presets +1/+2/+3 years from the old expiry date, or type DD-MM-YYYY; optional new window. This clears the snooze and resets the reminders |
| Audit log | entry detail page → *History* (edits with old → new values, renewals, snoozes, who and when) |
| Columns (admin) | menu → *Columns*. "Expiry date" and "Notification window" can be renamed but not deleted or retyped |
| Users (admin) | menu → *Users*: create, disable or enable, reset the password (the user must change it), grant or revoke admin. Also shows each user's entry count, ntfy topic and ntfy setup commands |
| Settings (admin) | send time, timezone, summary threshold, "approaching" range, last delivery status, test button |
| Export | **Export CSV** (UTF-8, dates DD-MM-YYYY, current columns) |
| Import | **Import CSV** → download the template (comma, or semicolon for Excel with European settings), one entry per row, upload. All rows are checked first: if any row has a problem nothing is imported and you get a list of row numbers to fix. Imported entries belong to the importer; empty notification window = 1 week. An exported file can be imported back |
| Install on phone | browser menu → *Add to Home Screen* |

**Statuses:** *OK*, *Approaching* (up to N days, default 30, before the window starts), *In notification window*
(reminders are being sent), *Expired*, *Snoozed*, and *No expiry date*.

**Reminder rules:**
- Trigger date = expiry date − notification window. For example, 23‑09‑2026 with a 1‑week window triggers on 16‑09‑2026.
- Every day at the send time (default 09:00 Europe/Bucharest), every entry that has reached its trigger date and is not
  snoozed gets one notification. This continues after expiry ("EXPIRED x days ago", high priority).
- When more than 10 entries (configurable) are due on the same day, one summary notification is sent instead.
- If the NAS was off at the send time, the missed run happens at startup. An entry never gets more than one notification per day.
- If ntfy is unreachable, each message is retried 3 times (2 s, 10 s, 30 s). After that the whole run is retried
  with backoff (5, 10, 20 … 60 minutes) until it succeeds. The result is shown in Settings.

**Privacy:** everyone, admins included, sees and edits only their own entries; another user's entry answers "not found".
To let admins see and edit all entries, set `"view_all_entries": ADMIN` in [app/permissions.py](app/permissions.py).
Reminders always go only to the entry's owner. Column structure and select options are shared by everyone.

Permissions are defined in one place: [app/permissions.py](app/permissions.py).

---

## 8. Backups and updates

**What to back up:** `data/` (SQLite database and the generated secret), `ntfy/data/` (ntfy users and tokens),
`.env` (contains secrets, so keep it private), and `ntfy/server.yml`.

**Consistent hot backup** of the database while the app runs:

```sh
cd /volume1/docker/certtracker
sudo docker exec certtracker python -c "import sqlite3; s=sqlite3.connect('/data/certtracker.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d); d.close()"
sudo tar czf certtracker-backup-$(date +%F).tar.gz data/backup.db ntfy/data .env ntfy/server.yml
```

**Cold backup** (simplest): `sudo docker compose stop && sudo tar czf certtracker-$(date +%F).tar.gz data ntfy .env && sudo docker compose start`.
You can also include `/volume1/docker/certtracker` in a UGOS *Sync & Backup* task.

**Restore:** stop the stack, put the folders back (for a hot backup, rename `backup.db` to `certtracker.db`),
make sure the folders are owned by PUID:PGID, then start the stack.

**Update:**

```sh
cd /volume1/docker/certtracker
sudo git pull
sudo docker compose up -d --build
```

Database migrations run automatically on startup.

---

## 9. Configuration reference

All variables are documented in [.env.example](.env.example).

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | port the app listens on inside the container (plain HTTP) |
| `APP_HOST_PORT` | `8085` | NAS port mapped to the app (Funnel :8443 target) |
| `TZ` | `Europe/Bucharest` | container timezone; default reminder timezone |
| `SECRET_KEY` | generated into `data/.secret_key` | signs notification-button tokens; set a long random value |
| `TRUST_PROXY` | `false` | trust `X-Forwarded-For` (right-most entry) and `X-Forwarded-Proto` from the reverse proxy |
| `PUBLIC_BASE_URL` | `http://localhost:PORT` | public app URL, used in notification links |
| `COOKIE_SECURE` | `true` if `PUBLIC_BASE_URL` is https | mark cookies `Secure` |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | – | optional first-admin bootstrap |
| `MIN_PASSWORD_LENGTH` | `10` | minimum password length (at least 8) |
| `SESSION_DAYS` | `30` | login session lifetime |
| `PUID`, `PGID` | `1000` | user and group the containers run as |
| `NOTIFIER` | `ntfy` if `NTFY_URL` and `NTFY_TOPIC` are set, else `console` | notification channel |
| `NTFY_URL` | – | internal ntfy address (`http://ntfy:2586`) |
| `NTFY_PUBLIC_URL` | – | public ntfy address (Funnel :10000); also ntfy's `base-url` |
| `NTFY_HOST_PORT` | `8095` | NAS port mapped to ntfy |
| `NTFY_TOPIC` | – | prefix of the personal topics (`<prefix>-<username>`) |
| `NTFY_TOKEN` | – | publisher access token (`tk_…`) |

Daily send time, timezone, summary threshold and the "approaching" range are changed in **Settings**, not in `.env`.

ntfy settings are in [ntfy/server.yml](ntfy/server.yml): `listen-http :2586`, `behind-proxy`, `auth-file`,
`auth-default-access: deny-all`, `cache-file`, `cache-duration: 72h`, `upstream-base-url: https://ntfy.sh`.
`base-url` comes from `NTFY_PUBLIC_URL` (as `NTFY_BASE_URL`). The option names were checked against the
ntfy 2.38 documentation and `ntfy serve --help`.

---

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| `PermissionError` or `unable to open database file` in the logs | `data/` or `ntfy/data/` is not owned by `PUID:PGID`; run `sudo chown -R <uid>:<gid> data ntfy/data` |
| Login page reloads without logging in | you are on plain HTTP but cookies are `Secure`; use the https Funnel URL, or set `COOKIE_SECURE=false` for LAN use |
| Test notification: `ntfy returned HTTP 401/403` | wrong `NTFY_TOKEN`, or the publisher lacks `write-only` on `<NTFY_TOPIC>-*` |
| Phone: "websocket … 403" or `401` | the person has no ntfy login (website logins are separate), or no `read-only` access to their own topic; see the Users page |
| Test notification: `Could not reach ntfy` | the ntfy container is down, or `NTFY_URL` is not `http://ntfy:2586` |
| Phone shows nothing | wrong server URL in the app (must include `:10000`), user lacks `read-only` access, or Funnel is off |
| iPhone notifications arrive late | check `upstream-base-url` and that `NTFY_PUBLIC_URL` exactly matches the URL you subscribed with |
| Snooze button says "Invalid or expired link" | the notification is older than 48 hours, or `SECRET_KEY` changed; open the entry instead |
| Reminders at the wrong hour | check the timezone in Settings ("Server time now" is shown there) |

---

## 11. Assumptions

Choices made where the spec left room:

1. **Stack:** Python 3.12, Starlette (the small core FastAPI is built on), Jinja2 server-rendered pages and a little plain JavaScript.
   There's no frontend build and no CDN. Runtime dependencies: starlette, uvicorn, jinja2, python-multipart, argon2-cffi, httpx, segno, tzdata.
2. **Ports:** app on Funnel **8443** and ntfy on **10000**, because 443 is used by Jellyfin's tailnet-only serve.
   NAS host ports are **8085** (app) and **8095** (ntfy), because 8090 is taken by qBittorrent.
3. **Funnel target:** the NAS LAN IP, because the Tailscale container uses bridge networking. A fallback is in section 5.2.
4. **First admin:** the setup screen requires a one-time code written to `data/setup-code.txt` (not to the logs), so
   nobody on the internet can claim the instance before you do. `ADMIN_USERNAME`/`ADMIN_PASSWORD` is the alternative.
5. **Missing `SECRET_KEY`:** a random key is generated and kept in `data/.secret_key`. The app does not refuse to start.
6. **"Approaching"** status = within 30 days (configurable) before the trigger date.
7. **Snooze semantics:** "snoozed until D" means no reminders before D. They resume on D at the send time. Snooze presets count from today.
8. **Snooze from the notification:** the buttons send the signed token in the POST **body**, not the URL, so it never shows up in
   access logs. Replaying a used token returns "Already done" with no second effect.
9. **Renewal "reset reminder state":** clears the snooze and the sent-history for the entry, except today's record, so an entry
   still never gets two notifications on the same day.
10. **Catch-up:** at startup, and every 30 s afterwards, the app checks whether the most recent send time (today's, or
    yesterday's if today's hasn't come yet) was handled. If not, it runs. A daily "send" row per entry, keyed by local date,
    prevents duplicates. Missed days are not replayed one by one: you get the current day's reminders once.
11. **Entries that become due after today's send time** (for example, created at 15:00 with a trigger date of today) are
    first notified at the next day's send time.
12. **Notification title fields:** "Certificate", "Sub-category" and "Center" have soft roles. They can be renamed (titles follow)
    or deleted (the title falls back to "Entry #id"). Only "Expiry date" and "Notification window" are protected.
13. **Single select options** are created on the fly when someone types a new value. Admins can also edit the list.
14. **Changing a column's type** converts existing values where possible. The confirmation states how many values would be cleared.
15. **Dates** are entered as DD-MM-YYYY (also `/` or `.`, or ISO) in text fields rather than the browser date picker, so the
    format is the same on every phone.
16. **Login throttling:** 5 failed attempts lock that account for 15 minutes. 20 failed attempts per client IP within 15 minutes
    block that IP temporarily (in memory). Users created or reset by an admin must change their password at the next login.
17. **Sessions** are stored server-side, so disabling a user or resetting a password logs them out immediately.
    Cookies are `HttpOnly`, `SameSite=Lax` (so tapping "Open" in a notification keeps you logged in), and `Secure` on https.
18. **CSRF:** a per-session token on every form and AJAX call, plus an Origin check.
19. **CSV export** starts with a UTF-8 BOM so Excel shows diacritics correctly. Cells starting with `= + - @` are prefixed with `'`
    to prevent formula injection.
    **CSV import** reads UTF-8 or Excel's Windows-1250 "CSV", comma/semicolon/tab separated (detected from the first row),
    matches columns by name (case-insensitive; unknown columns are ignored with a warning), max 2 MB / 2000 rows.
20. **Audit log times** are stored in UTC and shown in the viewer's local time.
21. **ntfy runs as non-root** on internal port 2586 (instead of 80). Attachments are disabled. `cache-duration` is 72 h.
22. **Memory limits** of 256 MB per container are set in compose as a safety net. Measured idle use is far lower.
23. **Per-user privacy:** every entry has an owner (its creator). When upgrading, existing entries get the user who
    created them according to the history log, or the first admin if that is unknown. A duplicate belongs to whoever made it.
    Entries of a disabled user are kept but get no reminders.
24. **Personal topics** are derived from the username (lower-case; other characters than letters, digits, `-` and `_` are
    replaced, with a short hash added so two users never share a topic). ntfy access rules keep topics private.
25. **Images:** `python:3.12-slim` (multi-arch) and `binwiederhier/ntfy:v2.38.2` (pinned). Build arm64 if needed with
    `docker buildx build --platform linux/amd64,linux/arm64 .`

---

## 12. Project layout and tests

```
app/
  main.py            app factory, middleware (proxy, sessions, security headers, access log), routes
  config.py          environment variables
  db.py              SQLite schema and migrations, default columns
  permissions.py     who may do what, all in one place
  reminders.py       pure reminder logic (trigger date, status, due)
  scheduler.py       daily run, catch-up, dedup, summary, retries, message building
  tokens.py          HMAC-signed action tokens for notification buttons
  security.py        argon2 passwords, sessions, login lockout, IP limiter
  repo.py            columns, entries, audit log, users, settings
  notifiers/         Notifier interface + ntfy and console implementations
  csv_import.py      CSV template and import parsing
  routes/            auth, entries/views/export/import, admin (columns/users/settings), notification actions
  templates/ static/ server-rendered pages, CSS, JS, PWA manifest, service worker, icons
tests/               pytest suite
ntfy/server.yml      ntfy configuration
tools/test.sh        run tests in Docker
tools/make_icons.py  regenerate PNG icons
```

**Adding a channel (Web Push, email):** subclass `Notifier` in `app/notifiers/`, implement `async send()`
(raise `NotifyError` on failure), and return it from `make_notifier()`.

**Tests** (`sh tools/test.sh`) cover: the trigger date (including the 23‑09‑2026 example), daily repeat, snooze and resume,
renewal reset, catch-up after downtime, no duplicate sends in one day, the summary above the threshold, action tokens
(expiry, wrong entry or action, tampering, replay, POST only, never logged), retry after a mocked ntfy failure and backoff,
timezone handling, login lockout and IP limiting, setup code, CSRF, the permission rules, column management, entry CRUD and
audit, inline editing, XSS escaping, CSV export and import, per-user privacy and personal notification topics.
