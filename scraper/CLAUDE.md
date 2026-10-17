# Claude guidance for `scraper/`

Start with the root [CLAUDE.md](../CLAUDE.md) code map. For a local task, read
only the relevant implementation and focused tests. Use
[AGENTS.md](./AGENTS.md) for the subsystem boundaries and test index,
[ARCHITECTURE.md](../ARCHITECTURE.md) for runtime flow,
[state-protocols.md](../docs/state-protocols.md) for shared files, and
[configuration-map.md](../docs/configuration-map.md) for YAML consumers and
reload rules.

Preserve these contracts:

- An ID moves from `try_claim` to `confirm` only after collector confirmation;
  every abandoned pending item must be `release`d.
- The scheduler owns slot dispatch; fleet health owns account transitions;
  workers report outcomes but must not let one account failure stop the fleet.
- Treat `CancelledError` as cancellation and preserve the `supervised()` worker
  restart boundary.
- Config reload does not recreate startup resources or reload `accounts.yaml`.
  Cookie and identity files have separate watcher semantics.
- Metrics are auxiliary. Do not make successful fetch or delivery depend on
  metrics being available.

Run the focused tests listed in `AGENTS.md`, then the full pytest suite for
cross-cutting changes.
