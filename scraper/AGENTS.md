# Guidance for `scraper/`

Read the root [CLAUDE.md](../CLAUDE.md) for the repository map. For cross-module
behavior, use [ARCHITECTURE.md](../ARCHITECTURE.md); for shared files under
`state/`, use [state-protocols.md](../docs/state-protocols.md); for YAML
consumers and reload behavior, use [configuration-map.md](../docs/configuration-map.md).
Do not load these documents all at once for a local change—follow the relevant
links and focused tests.

## Ownership boundaries

- `main.py` constructs shared services and chooses the worker set. Account
  membership is startup-only; a `ConfigStore` reload does not rebuild services.
- `scheduler.py` owns slot timing/dispatch and consumes `Sender.demand()`.
  `fleet_polling.py` owns account health transitions and cooldown calculations.
- `worker.py` owns one account's poll cycle, file watchers, and translation of
  fetch results into scheduler outcomes. Preserve cancellation and the
  `supervised()` restart boundary.
- `state.py` owns in-memory dedupe claims. `sender.py` owns the outgoing buffer
  and delivery retry; `pipeline.py` owns payload and collector result parsing.
- `identity.py` defines the filesystem protocol shared with host jobs. Find and
  update both writer and reader when changing a state file.
- `metrics.py` is auxiliary/diagnostic. Metrics failures must not become a
  prerequisite for polling or delivery.

## Invariants to preserve

- Keep the item lifecycle `try_claim -> confirm` only after collector
  confirmation, or `release` on every path that abandons a pending item.
- Keep blocking `curl_cffi` and filesystem work off the event loop where the
  current code does so; follow `ExecutorHandle` ownership for HTTP work.
- Preserve per-account error isolation: a single account's auth, network, or
  rate-limit failure must not halt the fleet.
- Treat `CancelledError` as cancellation, not as a recoverable worker failure.
- Keep file updates atomic when another process may read them concurrently.
- Do not assume configured account fields and persistent device profiles have
  the same precedence; see the architecture/profile documentation.

## Focused tests

Use the narrowest relevant tests first:

- Worker: `tests/test_worker_helpers.py`, `tests/test_worker_success.py`,
  `tests/test_worker_branches.py`, `tests/test_delivery_path.py`.
- Scheduler and health: `tests/test_fleet_polling.py`,
  `tests/test_grouping_health.py`.
- HTTP, payload, and sender: `tests/test_http_client.py`,
  `tests/test_pipeline_payload.py`, `tests/test_sender_edges.py`.
- Config, identity, geo, profiles: `tests/test_config_runtime.py`,
  `tests/test_identity_state.py`, `tests/test_geo.py`, `tests/test_geoip.py`,
  `tests/test_profile_gen.py`, `tests/test_profile_edges.py`.
- Metrics: `tests/test_metrics.py`, `tests/test_dashboard.py`.

Run the full suite and compile check for cross-cutting changes. CI coverage
measures `main.py` and `scraper/`; it does not measure `scripts/` or tests.
