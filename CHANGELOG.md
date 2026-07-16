# Changelog

## 1.0.0 — 2026-07-16

First stable release of the DayN-NetBox Bridge.

### Features

- **Day-N CSV parser/exporter**: handles the Catalyst Center header/data
  block-pair format with per-template variable columns and mixed line endings;
  unchanged rows round-trip byte-exactly.
- **NetBox auto-fill** (read-only): three-stage device matching (exact FQDN,
  case-insensitive short hostname, unambiguous starts-with), batched queries,
  graceful degradation when NetBox is unreachable.
- **Data-driven mapping** via `mappings.yaml`: dotted device paths plus special
  resolvers —
  - `connected_device`: uplink switch from cable traces,
  - `uplink_ports`: local names of the cabled interfaces (e.g. `Te1/1/3,Te1/1/4`),
  - `site_vlans`: all VLANs of the device's site as `(vid,name);(vid,name);…`,
  - `site_contact:<role>`: site contact by role (e.g. `Local IT`), falling back
    to device contact, then tenant.
- **Review GUI**: device-grouped tables with file/NetBox/final value and status
  (auto / file / conflict / ambiguous / missing / manual), summary banner,
  ambiguity pickers, apply-to-all, warning count instead of blocked export.
- **Settings GUI** (`/settings`): NetBox URL/token/TLS configurable at runtime
  with live connection test; protected by an admin login (first-run password
  setup or `ADMIN_PASSWORD` env); token is write-only.
- **Values precedence**: manual input > uploaded file value > NetBox; non-empty
  file values are never overwritten.
- **Container**: multi-stage image, non-root user, port 8070, healthcheck,
  docker-compose with persistent settings volume.

### Guardrails

- 75 tests (no live NetBox needed), ≥85 % coverage on parser/matcher/mapper,
  perf test: ≤5 NetBox requests and <2 s review render for 200 devices.
