# CLAUDE.md

Conventions for `cambridge-ma-parking-checks`. README.md describes behavior; keep it current.

- The repo is public. Never commit `.env`, the database, the plate, a home address or coordinates, email addresses, hostnames, or lookup HTML with real plates. Personal values live only in `.env` on the server. Scrub fixtures.
- Self-contained on a shared host: this project owns only `parkingbot`, `/opt/cambridge-ma-parking-checks`, `/var/lib/parking-checks`, `/var/cache/parking-checks-uv`, `parking-checks-*` units, and the Tailscale Funnel entry on port 8443. Never touch other apps' users, directories, units, firewall rules or Tailscale serve entries.
- Run `uv run ruff check . && uv run ruff format --check . && uv run pytest` before every commit.
- Tests never call live sites. Use fixtures in `tests/fixtures/` and pass `now` explicitly; never use naive datetimes (zone is `parking_checks.ET`).
- Every alert goes through `Notifier.notify` with a stable `dedupe_key`.
- Deploy with `deploy/push.sh user@host` (git archive over SSH). Nothing deploys automatically.
- Keep dependencies to requests, beautifulsoup4, fastapi, uvicorn.
