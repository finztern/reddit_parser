# Configuration ownership map

This is a navigation index for configuration. It intentionally documents key
names and consumers, not the current operational values in YAML. Runtime
configuration can contain network details or credentials; do not paste values
from `config.yaml`, `accounts.yaml`, or `scripts/*.yaml` into issues or docs.

For defaults and validation rules, inspect the consumer code. For deployment
procedures, see [DEPLOY.md](../DEPLOY.md) and
[ROTATION_SETUP.md](../ROTATION_SETUP.md).

## Load and reload behavior

| Source | Loader / owner | When changes take effect |
|---|---|---|
| `config.yaml` scraper settings | `ConfigStore` in `scraper/config.py`, path from `CONFIG_PATH` | File is reread every `CONFIG_RELOAD_SECONDS` (default 5). Runtime effect depends on when a consumer reads a setting; services/resources built at startup are not recreated automatically. Invalid reload keeps the last loaded data. |
| `accounts.yaml` | `load_accounts()` in `scraper/config.py`, path from `ACCOUNTS_PATH` | Scraper account list is loaded at startup. Adding/removing accounts or changing their enabled membership requires scraper restart. |
| `scripts/refresh_cookies.yaml` | `scripts/refresh_cookies.py` | Read for each refresh invocation; edits affect later invocations, not an already-running browser job. |
| `scripts/refresh_guest_cookies.yaml` | `scripts/refresh_guest_cookies.py` and tools that read guest definitions | Read by each job/tool invocation. The rotation daemon refreshes its context during its loop. |
| `state/` JSON and marker files | Owner-specific watcher/readers; see [state protocols](./state-protocols.md) | Runtime changes are applied according to each file's mtime/polling rules, not by `ConfigStore`. |

**Hot reload means the YAML dictionary was re-read, not that every initialized
object was rebuilt.** In particular, `main.py` chooses enabled accounts,
creates the `SeenCache`, HTTP executor, metrics collector, and metrics server
at startup. Do not assume account membership, cache capacity, executor size,
metrics enablement, or endpoint binding can be changed safely without restart.
`config.yaml` fields read inside worker/scheduler/sender loops generally take
effect on their next read.

## `config.yaml`

### Stream and Reddit client

| Keys | Primary consumer | Notes |
|---|---|---|
| `stream_subs`, `reddit_base_url`, `fetch_limit` | `scraper/worker.py`, `scraper/http_client.py` | Source listing and first-page size; fetch limit is capped at Reddit's client limit. |
| `posts.enabled`, `every_n_polls`, `max_pages` | `scraper/worker.py`, `scraper/pipeline.py` | Optional extra `.../new.json` request per poll slot; posts share `SeenCache`, `Sender` and the collector endpoint with comments (`summary.data_type: post`). |
| `stream_subs_per_request`, `stream_subs_all_chance` | `scraper/worker.py`, `scraper/sub_rotation.py` | `0` = all `stream_subs` in each request; `N` = per-account rotating window of N subs per poll; `all` is added on top with probability `stream_subs_all_chance`. |
| `stream_start_interval_seconds`, `stream_min_interval_seconds`, `stream_max_interval_seconds`, `stream_jitter_ratio`, `overlap_target_low`, `overlap_target_high`, `full_page_threshold` | `scraper/scheduler.py` | Adaptive fleet polling rate based on overlap; the initial interval is set when the scheduler is constructed. |
| `pagination_max_pages`, `pagination_delay_min_seconds`, `pagination_delay_max_seconds`, `max_age_seconds` | `scraper/worker.py`, `scraper/http_client.py`, `scraper/sender.py` | Pagination bounds and freshness/drop behavior. |
| `request_timeout_seconds`, `connect_timeout_seconds` | `scraper/worker.py`, `scraper/http_client.py` | Request-level timeouts; blocking `curl_cffi` work runs in the managed executor. |
| `respect_ratelimit_headers`, `ratelimit_safety_margin`, `ratelimit_max_interval_seconds` | `scraper/worker.py` | Emergency handling of rate-limit headers; ordinary headers do not set the fleet rate. |
| `base_backoff_seconds`, `max_backoff_seconds`, `auth_dead_cooldown_seconds` | `scraper/worker.py`, `scraper/state.py` | Per-account retry and auth recovery timing. |
| `user_agent`, `impersonate` | `scraper/worker.py` | Fallbacks when account/profile values are missing; account/profile settings normally take precedence. |

### Delivery and backpressure

| Keys | Primary consumer | Notes |
|---|---|---|
| `store_endpoint`, `batch_max_items`, `target_rate_per_second` | `scraper/sender.py`, `scraper/pipeline.py` | Collector destination, batch cap, and queue-disabled/unavailable fallback. |
| `queue_control.enabled`, `url`, `poll_interval_seconds`, `request_timeout_seconds` | `main.py`, `scraper/sender.py` | Queue endpoint and polling. Queue control requires both `enabled` and a usable URL. |
| `queue_control.inflight_target`, `low_queue_fraction`, `flush_age_fraction`, `buffer_target_factor`, `buffer_max_factor`, `min_send_gap_seconds`, `panic_fraction`, `respect_server_action`, `stale_seconds`, `log_every_seconds` | `scraper/sender.py` | Batch sizing, buffering, pressure response, freshness flushing, and fallback transition. |
| `seen_cache_size` | `main.py`, `scraper/state.py` | In-memory confirmed-ID capacity; allocated at startup and cleared on process restart. |

### Fleet scheduling and health

| Keys | Primary consumer | Notes |
|---|---|---|
| `fleet_polling.mode`, `target_fleet_rate`, `pause_min_seconds`, `pause_max_seconds`, `jitter_ratio` | `scraper/fleet_polling.py`, `scraper/scheduler.py` | Fleet cadence and per-account pause; `target_fleet_rate: auto` selects adaptive rate. |
| `fleet_polling.cooldown_base_seconds`, `cooldown_max_seconds`, `auth_failed_after`, `degraded_after_empty`, `degraded_recover_ok`, `empty_cooldown_base_seconds`, `empty_cooldown_max_seconds`, `recovery_probe_seconds`, `ratelimit_emergency_remaining` | `scraper/fleet_polling.py`, `scraper/worker.py` | Account state transitions and recovery/rate-limit probes. |
| `fleet_polling.disabled_accounts` | `scraper/fleet_polling.py`, `scraper/scheduler.py` | Configured pause list; combined with `state/runtime_disabled.json`. |
| `rotation.*` | `scripts/rotation_daemon.py`, `scraper/worker.py` | Rotation enablement, slots, retries/cooldowns, node-pool paths, safety limits, and daemon timing. Worker requests rotation; the daemon owns selection and commit. |
| `fleet.*` | `scripts/login_fleet.py`, `scripts/rotation_daemon.py`, `scraper/worker.py` | Login-only replacement policy, guest rotation, and reserve/pool behavior. `legacy_scheduler` is currently not a behavior switch. |

### Browser jobs, profiles, and metrics

| Keys | Primary consumer | Load timing |
|---|---|---|
| `login_hub.*` | `scripts/login_hub.py` | Loaded when the hub starts; includes bind/CIDR, port range, parallelism, and display options. |
| `human.*` | `scripts/human_session.py`, refresh/login jobs | Loaded for a job/session; controls schedule, profile, duration, and read-only maintenance behavior. |
| `metrics.enabled`, `host`, `port`, `snapshot_interval_seconds`, `identity_refresh_seconds`, `events_max_bytes`, `events_backups` | `main.py`, `scraper/metrics.py` | Collector and HTTP server are constructed at scraper startup; restart to reliably change enablement/bind/port or journal policy. |

## `accounts.yaml`

`accounts` is a list loaded by `load_accounts()`. The scraper starts workers
only for entries with `enabled: true`. Common fields are:

| Field | Consumer | Meaning |
|---|---|---|
| `name` | Worker, metrics, state paths, host tools | Stable account/slot identifier; used in file names under `cookies/` and `state/`. |
| `enabled` | `main.py`, refresh/login tools | Whether scraper worker starts; changes to membership require restart. |
| `login` | `main.py`, worker, login-fleet tools | Marks login-specific behavior, including node replacement policy. |
| `proxy_port` | Worker and host browser/network jobs | Local mihomo listener for the slot. |
| `cookie_file` | Worker and refresh tools | Relative/absolute cookie JSON path; worker watches valid file changes. |
| `impersonate`, `user_agent`, `locale`, `timezone_id` | Worker, profile and browser tools | Request/device identity fallbacks; a persistent profile can override these. |
| `storage_state` | Browser login/refresh tools | Playwright login-state path; distinct from scraper cookies. |

Do not infer login/guest membership from this file alone. The refresh job lists
are separately configured in `scripts/refresh_cookies.yaml` and
`scripts/refresh_guest_cookies.yaml`; see [README](../README.md#режимы-аккаунтов-и-конфигурация).

## Refresh job YAML

### `scripts/refresh_cookies.yaml`

- `login_accounts`: explicit login job membership; separate from `enabled`.
- `headless`, `login_check_timeout_seconds`, `stagger_seconds`,
  `storage_state_dir`, `reddit_base_url`, `login_check_path`: browser launch,
  timing, login verification, and storage settings.
- `geo_defaults` and `account_geo`: locale/timezone defaults and per-account
  overrides.

### `scripts/refresh_guest_cookies.yaml`

- `guest_accounts`: guest slot definitions including engine/device/fingerprint
  and optional geo; do not overlap membership with `login_accounts`.
- `headless`, `reddit_base_url`, `warmup_path`, `warmup_wait_until`,
  `warmup_page_load_timeout_seconds`, `warmup_retries`,
  `post_load_settle_seconds`, `verify_subreddit`, `verify_timeout_seconds`,
  `retry_failed_passes`, `stagger_seconds`: browser preparation and verification.
- `geo_defaults` and `account_geo`: geo defaults and per-account overrides.

Exact accepted fields and defaults are in the respective loader and consumer.
The checked-in YAML may be an operational fixture; do not treat its current
account membership or values as a production inventory.

## Environment overrides

The scraper's environment variable source of truth is
[`scraper/constants.py`](../scraper/constants.py); relevant overrides include:

| Variable | Overrides |
|---|---|
| `CONFIG_PATH`, `ACCOUNTS_PATH` | Scraper YAML paths |
| `CONFIG_RELOAD_SECONDS` | `ConfigStore` polling interval |
| `PROXY_HOST` | Host part of account proxy URLs |
| `STORE_ENDPOINT`, `QUEUE_CONTROL_URL` | Collector endpoints; override corresponding config values |
| `STATE_DIR` | Runtime state root used by scraper/tools |
| `METRICS_BIND`, `METRICS_PORT` | Metrics HTTP bind and port |
| `LOG_FILE`, `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT` | Log destination and rotation |
| `EXECUTOR_HEALTH_LOG_INTERVAL_SECONDS`, `EXECUTOR_SWAP_THRESHOLD`, `EXECUTOR_SWAP_COOLDOWN_SECONDS`, `EXECUTOR_FATAL_LEAK_MULTIPLIER` | Managed curl executor health policy |

Other scripts may define their own environment overrides (for example refresh
config path or login-hub password). Inspect that script's argument parser and
`os.environ` reads rather than assuming the scraper's environment table
applies to host jobs.

## `config.yaml` key inventory

This inventory is checked against the checked-in `config.yaml` by
`scripts/check_docs.py`. It lists names only; descriptions and runtime values
remain in the sections above and in the consuming code.

```text
stream_subs
stream_subs_per_request
stream_subs_all_chance
posts.enabled
posts.every_n_polls
posts.max_pages
reddit_base_url
fetch_limit
stream_start_interval_seconds
stream_min_interval_seconds
stream_max_interval_seconds
stream_jitter_ratio
overlap_target_low
overlap_target_high
full_page_threshold
pagination_max_pages
pagination_delay_min_seconds
pagination_delay_max_seconds
max_age_seconds
target_rate_per_second
queue_control.enabled
queue_control.url
queue_control.poll_interval_seconds
queue_control.request_timeout_seconds
queue_control.inflight_target
queue_control.low_queue_fraction
queue_control.flush_age_fraction
queue_control.buffer_target_factor
queue_control.buffer_max_factor
queue_control.min_send_gap_seconds
queue_control.panic_fraction
queue_control.respect_server_action
queue_control.stale_seconds
queue_control.log_every_seconds
seen_cache_size
store_endpoint
batch_max_items
user_agent
request_timeout_seconds
connect_timeout_seconds
cookie_reload_check_interval_seconds
respect_ratelimit_headers
ratelimit_safety_margin
ratelimit_max_interval_seconds
base_backoff_seconds
max_backoff_seconds
auth_dead_cooldown_seconds
fleet_polling.mode
fleet_polling.pause_min_seconds
fleet_polling.pause_max_seconds
fleet_polling.jitter_ratio
fleet_polling.target_fleet_rate
fleet_polling.cooldown_base_seconds
fleet_polling.cooldown_max_seconds
fleet_polling.auth_failed_after
fleet_polling.degraded_after_empty
fleet_polling.degraded_recover_ok
fleet_polling.empty_cooldown_base_seconds
fleet_polling.empty_cooldown_max_seconds
fleet_polling.recovery_probe_seconds
fleet_polling.ratelimit_emergency_remaining
fleet_polling.disabled_accounts
rotation.enabled
rotation.slots
rotation.empty_streak_trigger
rotation.request_cooldown_seconds
rotation.request_timeout_seconds
rotation.nodes_file
rotation.mihomo_api
rotation.mihomo_secret
rotation.ban_cooldown_hours
rotation.max_rotations_per_day
rotation.max_candidates_per_request
rotation.bad_node_ttl_hours
rotation.global_outage_fraction
rotation.global_outage_pause_seconds
rotation.failed_all_pause_seconds
rotation.poll_seconds
rotation.reconcile_seconds
rotation.lock_timeout_seconds
fleet.reserve_nodes
fleet.min_good_nodes_warn
fleet.node_check_interval_hours
fleet.node_replace_only
fleet.guest_enabled
fleet.legacy_scheduler
fleet.use_reddit_ok_file
login_hub.port_base
login_hub.max_parallel
login_hub.ram_per_session_mb
login_hub.bind
login_hub.allowed_cidr
login_hub.public_host
login_hub.screen
human.enabled
human.profile
human.min_interval_hours
human.interval_jitter
human.refresh_before_hours
human.min_session_seconds
human.max_session_seconds
human.on_first_login
human.start_gap_seconds
metrics.enabled
metrics.host
metrics.port
metrics.snapshot_interval_seconds
metrics.identity_refresh_seconds
metrics.events_max_bytes
metrics.events_backups
```

## When changing configuration

1. Search for every `get()`/loader use of the key; a YAML key may be consumed
   by multiple processes with different reload behavior.
2. Update the loader/defaults, validation, CLI/config docs, and tests together.
3. Decide whether the setting is read dynamically or captured at startup. If
   captured, document the restart requirement.
4. Never add secret values to examples. Use key names and placeholders only.
