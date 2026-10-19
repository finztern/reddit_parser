# Claude guidance for `scripts/`

Start with the root [CLAUDE.md](../CLAUDE.md) code map. Use
[AGENTS.md](./AGENTS.md) for script-specific operational safety and the
script-to-test index. Read only the relevant runbook:
[DEPLOY.md](../DEPLOY.md) for deployment/host jobs,
[ROTATION_SETUP.md](../ROTATION_SETUP.md) for node and account changes,
[configuration-map.md](../docs/configuration-map.md) for YAML ownership, and
[state-protocols.md](../docs/state-protocols.md) for shared files.

Before changing a script, check its CLI and distinguish read-only/status,
dry-run, and write/apply modes. Do not run production mutations, real browser
logins, node changes, or remote probes as unit-test validation.

For shared state, update all writers and readers and preserve atomic writes.
Keep login and guest refresh membership separate; both may target the same
cookie file. Preserve browser locks, cleanup, backup, and account-file update
semantics. Never log or document cookies, storage-state contents, credentials,
proxy URIs/credentials, or sensitive headers.

Use the focused tests listed in `AGENTS.md`. CI coverage currently excludes
`scripts/`; validate script changes with targeted tests and the docs checks.
