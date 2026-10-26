# Guidance for `scripts/`

Read the root [CLAUDE.md](../CLAUDE.md) for the repository map. These are
host-side tools, often with filesystem, browser, network, or service side
effects. Before changing behavior, consult the relevant [deployment
runbook](../DEPLOY.md), [rotation runbook](../ROTATION_SETUP.md),
[configuration map](../docs/configuration-map.md), and
[state-file protocols](../docs/state-protocols.md). Read only the documents
relevant to the task.

## Operational boundaries

- Prefer a script's `--help`, `--list`, `--status`, or dry-run mode before any
  write/apply mode. Do not run production mutations during development.
- Check exact target paths, current flags, and backups before changing account
  membership, cookies, profiles, node assignments, or systemd state.
- Never log or document cookies, Playwright storage state, passwords, VLESS
  URIs, noVNC credentials, or sensitive response headers.
- Do not start real browsers, mihomo changes, login sessions, or remote network
  probes as part of ordinary unit-test validation.
- Shared browser jobs use `/tmp/reddit_scraper_browser.lock`; preserve lock
  acquisition/release and cleanup paths.
- Keep login and guest refresh membership separate. Both can write the same
  cookie path; do not allow concurrent jobs for one account.
- For shared `state/` changes, update both the host-side writer and scraper or
  dashboard reader. Preserve atomic replacement where readers can race writers.
- `accounts.yaml` may be edited by login/migration utilities. Preserve the
  repository's locking, backup, and restart guidance for those changes.

## Find the right tool and tests

- Login/cookies: `manual_login.py`, `login_hub.py`, `refresh_cookies.py`,
  `refresh_guest_cookies.py`; tests `test_human_session.py`,
  `test_login_hub.py`, `test_login_fleet.py`.
- Nodes/rotation: `filter_nodes.py`, `rotation_daemon.py`,
  `generate_mihomo_config.py`; tests `test_filter_nodes.py`,
  `test_add_guest_slots.py`, `test_repo_config.py`.
- Account/profile migration: `convert_to_login.py`, `add_login_slots.py`, `reidentify.py`,
  `add_guest_slots.py`, `attach_proxies.py`, `profile.py`, `rotate_fingerprints.py`; tests
  `test_add_guest_slots.py`, `test_attach_proxies.py`, `test_profile_gen.py`, `test_repo_config.py`.
- Metrics/operator UI: `dashboard.py`; tests `test_dashboard.py`,
  `test_metrics.py`.
- Backups/recovery: `backup_sessions.sh`, `restore_sessions.sh`; tests
  `test_backup_sessions.py`.

CI's coverage scope is `main.py` plus `scraper/`, so script behavior is
validated by focused tests and workflow checks rather than the coverage
percentage. Use mocks/temp directories and avoid relying on production state.
