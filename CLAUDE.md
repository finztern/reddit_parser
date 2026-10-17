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
local to one module. For behavior changes, trace the call from its entry point
through the owner of the state and the side-effect boundary; changing a helper
alone may miss callers or cleanup paths.

## Code map

| Concern | Start here | Follow the flow into | Focused tests |
|---|---|---|---|
| Startup, task supervision, shutdown | `main.py` | `scraper/worker.py`, `scraper/scheduler.py`, `scraper/sender.py`, `scraper/health.py` | `tests/test_main_startup.py`, `tests/test_main_lifecycle.py` |
| One Reddit poll and error handling | `scraper/worker.py` | `scraper/http_client.py`, `scraper/pipeline.py`, `scraper/state.py`, `scraper/identity.py` | `tests/test_worker_helpers.py`, `tests/test_worker_success.py`, `tests/test_worker_branches.py`, `tests/test_delivery_path.py` |
| Reddit requests and pagination | `scraper/http_client.py` | `scraper/profile_gen.py`, `scraper/geo.py` | `tests/test_http_client.py`, `tests/test_parallel_harvest.py` |
| Poll tempo, account health, cooldown | `scraper/scheduler.py` | `scraper/fleet_polling.py`; worker reports each poll outcome back to the scheduler | `tests/test_fleet_polling.py`, `tests/test_grouping_health.py`, `tests/test_worker_branches.py` |
| Dedupe and retry after failed delivery | `scraper/state.py` | `scraper/worker.py`, `scraper/sender.py`, `scraper/pipeline.py` | `tests/test_delivery_path.py`, `tests/test_sender_edges.py`, `tests/test_pipeline_payload.py` |
| Queue-aware buffering and collector delivery | `scraper/sender.py` | `scraper/pipeline.py`, `scraper/state.py` | `tests/test_sender_edges.py`, `tests/test_delivery_path.py` |
| Config reload and cookie file updates | `scraper/config.py` | `main.py`, `scraper/worker.py` | `tests/test_config_runtime.py`, `tests/test_repo_config.py`, `tests/test_worker_branches.py` |
| Account/profile geo and fingerprints | `scraper/profile_gen.py` | `scraper/geo.py`, `scraper/geoip.py`, `scraper/identity.py` | `tests/test_profile_gen.py`, `tests/test_profile_edges.py`, `tests/test_geo.py`, `tests/test_geoip.py`, `tests/test_identity_state.py` |
| Metrics payload, endpoint, dashboard | `scraper/metrics.py` | `scripts/dashboard.py`, `docs/metrics-schema.md` | `tests/test_metrics.py`, `tests/test_dashboard.py` |
| Login and cookie maintenance | Relevant script in `scripts/` | `scripts/human_session.py`, `scripts/login_hub.py`, `scripts/login_fleet.py`, config files under `scripts/` | `tests/test_human_session.py`, `tests/test_login_hub.py`, `tests/test_login_fleet.py` |
| Node checks, migration, replacement | `scripts/filter_nodes.py`, `scripts/convert_to_login.py`, or `scripts/rotation_daemon.py` | `scraper/identity.py`, `scraper/profile_gen.py`, `state/` contracts | `tests/test_filter_nodes.py`, `tests/test_add_guest_slots.py`, `tests/test_backup_sessions.py` |

The table is a starting index, not an exhaustive dependency graph. Find the
specific symbol and its callers before changing a cross-cutting behavior.
`ARCHITECTURE.md` describes runtime interactions; tests are the executable
examples for edge cases and expected contracts.

## Runtime flow and ownership

For a Reddit poll, follow this path:

1. `main.py` loads the enabled accounts and constructs shared runtime services.
2. `PollScheduler` grants one account slot only when there is demand and the
   account is eligible. Account health and rate-limit cooldown are owned by
   `fleet_polling.py`; scheduling/slot dispatch are owned by `scheduler.py`.
3. `account_worker` uses the account's cookies, active identity/profile, and
   curl session to fetch and paginate. It filters old/duplicate items and
   claims new IDs in `SeenCache`.
4. The worker hands claimed payloads to `Sender`. `Sender` owns the outgoing
   buffer and collector retries; `pipeline.py` owns collector payload/result
   handling.
5. An ID stays pending while buffered/in flight: confirm it only after the
   collector accepts it; release it when delivery cannot be completed so a
   later poll can retry it. Keep this lifecycle intact when moving code.

Shared runtime state and account worker state have different lifetimes. Config
reload does not mean `accounts.yaml` is reloaded; cookie and slot-profile
watchers have their own update rules. Check the owner and refresh mechanism
before adding a cache or changing when files are read. Metrics are auxiliary:
they must not become a dependency for successful polling or delivery.

## Tests and CI

Install the test/coverage tools and run the focused module test(s) first; use
the full suite before finishing a cross-cutting change:

```bash
python -m pip install -r requirements.txt -r requirements-refresh.txt pytest pytest-cov diff-cover
python -m pytest -q tests/test_worker_branches.py
python -m pytest -q --cov=main --cov=scraper --cov-report=term-missing
python -m compileall -q main.py scraper scripts tests
```

Use the test file(s) in the code map that exercise the changed contract. Most
tests mock network and filesystem boundaries; a passing test does not prove
production Reddit, proxy, mihomo, collector, or host-service connectivity.
CI publishes XML/HTML coverage and checks changed lines in the measured
`main.py` + `scraper/` scope on pull requests; the initial diff-coverage gate
is 80%. Scripts and tests are not currently part of the measured coverage
scope. There is deliberately no minimum total-project coverage gate.

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
