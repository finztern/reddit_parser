# Working in this repository

This file is a short navigation guide for coding assistants. Do not treat it as
a substitute for implementation or tests; source code is authoritative.

## Read only what the task needs

- Start with [README.md](./README.md) for project purpose, commands, and the
  documentation map.
- Read [ARCHITECTURE.md](./ARCHITECTURE.md) before changing data flow, worker
  lifecycle, state ownership, scheduling, or metrics.
- Read [DEPLOY.md](./DEPLOY.md) before changing deployment, Compose, systemd,
  host jobs, backup, or recovery.
- Read [ROTATION_SETUP.md](./ROTATION_SETUP.md) before changing account
  migration, proxy pools, node replacement, or guest rotation.
- Read [docs/metrics-schema.md](./docs/metrics-schema.md) before changing
  `scraper/metrics.py` or `scripts/dashboard.py`.
- `docs/tz/` contains original requirements. It is historical context, not a
  production runbook or proof that every acceptance target was measured.

Use `rg` with a narrow path/glob, then read the relevant implementation and
focused tests. Avoid loading every document or source file when the task is
local to one module.

## Source-of-truth rules

- Runtime behavior: `main.py`, `scraper/`, and `scripts/`.
- Scraper settings: `config.yaml`.
- Account identity/settings: `accounts.yaml`.
- Login refresh membership: `scripts/refresh_cookies.yaml`.
- Guest refresh membership: `scripts/refresh_guest_cookies.yaml`.
- Runtime state, cookies, VLESS links, node lists, and generated mihomo config
  are local operational data. Never print, commit, or paste their contents.
- Do not assume the checked-in sample configuration equals a production
  host's state. Verify account lists, flags, mounts, and systemd units.
- `fleet.legacy_scheduler` currently does not switch scheduler behavior.

## Validation

Run the narrowest relevant tests first. The usual local checks are:

```bash
python -m compileall -q main.py scraper scripts tests
python -m pytest -q
```

The GitHub Actions workflow runs compileall and pytest on Python 3.12. Tests
are mostly offline/mocked; they do not prove Reddit, proxy, collector, Docker
host-network, or production systemd connectivity.

## Safety

- Prefer plan/dry-run modes; require `--apply` only after inspecting proposed
  changes.
- Do not run production mutations or host services while investigating.
- Keep noVNC restricted to a trusted LAN/CIDR; do not expose metrics or login
  ports publicly.
- Do not enable Reddit write actions. Session maintenance is intended to be
  read-only.
