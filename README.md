# DayN-NetBox Bridge

A small web tool that standardizes Day-N device deployment across all Webasto regions:

1. Upload a **Cisco Catalyst Center Day-N template import CSV** (bulk template
   deployment export).
2. The tool parses the file — which templates, which devices, which variables are
   empty vs. already filled.
3. It matches every device against **NetBox** (read-only) and auto-fills every
   variable that can be derived from it (site, room, rack, role, asset tag,
   uplinked switch, contact, …).
4. Everything that can't be mapped is offered for manual pre-fill in the GUI,
   with "apply to all devices" convenience.
5. Download the enriched CSV **in the exact same Catalyst Center format**, ready
   for re-import into bulk template deployment. Unchanged rows are reproduced
   byte-exactly, including the original mixed line endings.

The tool never writes to NetBox and never talks to Catalyst Center.

![upload page](docs/screenshot-upload.png) <!-- TODO: add screenshots -->
![review page](docs/screenshot-review.png)

## Quick start

```bash
cp .env.example .env      # fill in NETBOX_URL and NETBOX_TOKEN
docker compose build && docker compose up
# GUI: http://localhost:8070
curl -f http://localhost:8070/healthz
```

If NetBox is unreachable or unconfigured the tool still works — every variable
simply becomes a manual field and a warning banner explains why.

## Settings GUI (`/settings`)

The NetBox connection can be configured entirely in the browser: open
**Settings** in the top bar, enter the NetBox URL and API token, and hit
**Test connection** — it performs a live check and reports the NetBox version
(or the error). **Save settings** persists the values to
`data/runtime_settings.json` (a Docker volume in the default compose file), and
they override the environment variables immediately, no restart needed.

The page is protected by a simple admin login (HTTP basic auth):

- On first run no password exists — the page is open and shows a warning; set
  the admin password right there (min. 8 characters, stored as a salted
  PBKDF2 hash).
- Alternatively set `ADMIN_USERNAME` / `ADMIN_PASSWORD` via environment; the
  env password wins and can then not be changed in the GUI.

The stored API token is write-only: it is never rendered back into the page or
logged. Leaving the token field empty when saving keeps the stored one.

## Environment variables

| Variable                | Default                      | Description                                            |
| ----------------------- | ---------------------------- | ------------------------------------------------------ |
| `NETBOX_URL`            | –                            | Base URL of NetBox (GUI settings override this)        |
| `NETBOX_TOKEN`          | –                            | API token. **Read-only is sufficient.**                |
| `NETBOX_VERIFY_SSL`     | `true`                       | Verify the NetBox TLS certificate                      |
| `ADMIN_USERNAME`        | `admin`                      | Login name for the settings page                       |
| `ADMIN_PASSWORD`        | –                            | If set, fixes the admin password (GUI change disabled) |
| `RUNTIME_SETTINGS_PATH` | `data/runtime_settings.json` | Where GUI-saved settings are persisted                 |
| `APP_PORT`              | `8070`                       | Port for local `uvicorn` runs (container always 8070)  |
| `LOG_LEVEL`             | `INFO`                       | `DEBUG` / `INFO` / `WARNING` / `ERROR`                 |
| `UPLOAD_MAX_BYTES`      | `2097152`                    | Upload size limit (2 MB)                               |
| `SESSION_TTL_SECONDS`   | `3600`                       | How long parsed uploads stay in memory                 |

The token is never logged; uploads are kept in memory only and expire after the TTL.

## How variables are auto-filled — `mappings.yaml`

The mapping from CSV variable name to NetBox attribute is **data-driven**. Each
entry maps a variable to a dotted path on the matched device record, a special
resolver, or `null` (always manual):

```yaml
rack_id:
  source: device.rack.name        # dotted path; any None on the way -> manual
uplink_switch:
  source: connected_device        # special: far end of the device's cabling
support_contact:
  source: device.primary_contact  # special: contact assignment, else tenant name
patch_field:
  source: null                    # no NetBox source -> always manual
```

### Adding a new region field

Add an entry to `mappings.yaml` — no code changes needed:

```yaml
my_new_variable:
  source: device.site.region.name
```

Restart the container (or mount the file, see `docker-compose.yml`). Variables
that appear in a CSV but not in `mappings.yaml` are simply offered as manual
fields. A value that is already non-empty in the uploaded CSV is **never**
overwritten; conflicts with NetBox are flagged for explicit user decision.
Precedence of values: manual GUI input > value from the uploaded file > NetBox.

## Device matching

For each device name in the CSV (FQDNs like `SVEL051CIS.global.web-int.net`):

1. exact name match in NetBox,
2. case-insensitive match on the short hostname (`SVEL051CIS`),
3. starts-with fallback — accepted only if it yields exactly one device.

Multiple hits show an ambiguity picker in the GUI; zero hits make the device's
variables manual. All lookups are batched: one device query per upload, plus one
prefetch each for cabling and contacts.

## Development

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'   # or: pip install -r requirements.txt + dev tools
uvicorn app.main:app --reload --port 8070
ruff check . && mypy app/ && pytest -q
pytest --cov=app --cov-report=term-missing
```

Tests never require a live NetBox — all HTTP is mocked with `responses`.

## Runbook

The service is a single stateless container; restart it freely — active upload
sessions live in memory and are lost on restart (users simply re-upload).
`GET /healthz` returns `{"status": "ok", "netbox": "ok|unreachable|unconfigured"}`;
the container healthcheck uses it, and a degraded NetBox never takes the GUI down.
Logs are single-line JSON-ish records on stdout correlated by `upload_id`. If
auto-fill suddenly stops matching devices, check the NetBox token scope and the
`netbox` field of `/healthz` first.
