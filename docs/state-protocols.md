# Runtime state file protocols

This document describes files shared by scraper workers, host-side jobs, the
dashboard, and daemons. Runtime files under `state/` are local operational
data, not source-controlled configuration. Do not copy their contents into
issues, logs, or documentation: they may reveal account names, network
topology, or active session information.

The default state root is `state/` under the project. `STATE_DIR` can override
it for the scraper and tools that use `scraper.identity`. Readers/writers must
use the same state root and filesystem view; in Compose, the host and container
share the writable state volume.

## Protocol index

| Path | Writer | Reader(s) | Purpose |
|---|---|---|---|
| `state/requests/<account>.json` | `scraper/worker.py` creates requests; `scripts/rotation_daemon.py` updates them | Worker, rotation daemon, `scripts/refresh_guest_cookies.py` | Request/status handshake for node or identity rotation |
| `state/slots/<account>.json` | `scripts/rotation_daemon.py` | Worker, guest-cookie harvester, profile/metrics tools | Currently active slot node/profile |
| `state/identities.json` | `scripts/rotation_daemon.py` | Rotation daemon, `scripts/login_fleet.py`, status tools | Persistent node/identity registry and daemon-owned slot state |
| `state/runtime_disabled.json` | `scripts/dashboard.py` | `scraper/scheduler.py`, dashboard | Operator pause list, separate from YAML configuration |
| `state/check_login/<account>` | `scripts/rotation_daemon.py` | `scripts/login_fleet.py`, dashboard, login jobs | Marker that a login session must be verified after node replacement |
| `state/login_hub/status.json` | `scripts/login_hub.py` | `scripts/dashboard.py` | Current login-hub queue and browser-session status |
| `state/human/<account>.json` | `scripts/human_session.py` | The same module | Per-account human-session schedule and last result |
| `state/profiles/<account>.json` | `scripts/profile.py` / `scraper/profile_gen.py` | Account worker, metrics, profile tools | Persistent device fingerprint and geo profile |

For a code change, search for both the writer and every reader. The table is an
index; the writer and reader implementations remain authoritative.

## Rotation request handshake

### Request creation

The account worker writes `state/requests/<account>.json` atomically through
`scraper.identity.write_json_atomic()`:

```json
{
  "status": "pending",
  "ts": 0,
  "reason": "empty_listing",
  "empty_streak": 6
}
```

`reason` is currently `empty_listing` or `blocked_403`. `ts` is Unix time;
`empty_streak` records the triggering streak (for the 403 path it represents
the auth-error streak).

While status is `pending` or `working` and the request has not exceeded
`rotation.request_timeout_seconds`, the worker does not poll Reddit. It checks
again on a later scheduler slot. Requests older than that timeout are treated
as stale and removed by the worker.

### Daemon processing and completion

`scripts/rotation_daemon.py` scans request files and handles at most one
pending request per loop. It changes status to `working` and adds `started`.
After processing, it keeps the request file and writes one of:

| Status | Additional fields | Meaning |
|---|---|---|
| `done` | `retry_after` | Replacement/rotation completed; worker clears the request and may resume |
| `rejected` | `reason`, `retry_after` | Request could not be handled now; worker clears it and delays another request until `retry_after` |

On daemon startup, leftover `working` files are reset to `pending`, allowing
recovery after a daemon crash. The worker clears completed/rejected request
files after reading them. A request file is therefore a handshake, not an
append-only history.

The guest-cookie harvester skips accounts whose request is still `pending` or
`working`. It applies the active slot profile when harvesting after rotation.

### Rotation commit ordering

For guest identity rotation, the daemon commits registry state, writes the new
slot profile, replaces the cookie file, then marks the request `done`. For
login-only node replacement, it writes the new slot profile, preserves cookies
and fingerprint, sets a `check_login` marker, then completes the request.
This order is intentional: the worker reads the active profile before applying
new cookies. Keep the files on the shared filesystem and do not reorder these
steps without updating both worker and daemon tests.

## Slot profile and identity registry

`state/slots/<account>.json` is the current runtime slot view. The daemon
increases `rev` on replacement and writes identifying fields such as:

- `rev`, `identity`, `node`, `updated`;
- `impersonate`, `user_agent`, and optional `engine`, `channel`, `device`;
- `geo`, containing the active locale/timezone/geo data when known.

The worker watches the file by modification time. For accounts with a
persistent profile at `state/profiles/<account>.json`, that permanent device
fingerprint wins; a slot profile node change refreshes geo but must not silently
replace the permanent fingerprint. Without a permanent profile, slot profile
values override static account settings.

`state/identities.json` is daemon-owned persistent state with top-level
`identities`, `slots`, `bad_nodes`, and `ban_lifetimes` mappings/lists. It
contains selection history and node/identity allocation details, so consumers
should use helpers in `scripts/login_fleet.py` or `rotation_daemon.py` instead
of editing it directly. It is not interchangeable with `state/slots/`.

## Operator pause and login verification

### Runtime pause

`state/runtime_disabled.json` has this shape:

```json
{"disabled": ["account_name"], "updated_at": 0}
```

The dashboard writes it via a same-directory temporary file and `os.replace`.
The scheduler watches its mtime and combines these names with
`fleet_polling.disabled_accounts`. Removing a name resumes it without changing
`config.yaml`. Missing or malformed data is treated as no runtime pauses;
avoid hand-editing while the dashboard is writing the file.

### `check_login` marker

`state/check_login/<account>` is a marker file, not JSON. After a login-only
node replacement, the rotation daemon creates it (optionally with timestamp
and reason). A confirmed login through `manual_login.py` or `refresh_cookies.py`
removes it. `state/identities.json` separately tracks `needs_node` when no
replacement node is available. Do not conflate “node unavailable” with
“session needs verification.”

## Login-hub status

`scripts/login_hub.py` atomically replaces `state/login_hub/status.json`
periodically. Top-level fields are `updated_at`, `bind`, `pending`, and
`sessions`. Each session row includes `account`, `port`, `link`, `status`,
`message`, `exit_ip`, and `started_at`; final session statuses include `done`,
`failed`, `timeout`, and `cancelled`, while in-progress statuses are defined in
`scripts/login_hub.py`.

The dashboard treats the timestamp as a liveness signal and displays active
session status. This file is diagnostic state, not a command channel; changing
it does not start, stop, or authenticate a browser. Keep it local because the
`link` and network details are operational information.

## Human-session state

`scripts/human_session.py` stores `state/human/<account>.json` with fields
`last_run_ts`, `next_due_ts`, `status`, `detail`, `duration`, `actions`, and
`blocked_writes`. Times are Unix seconds. `status` is `ok`,
`needs_attention`, or another result emitted by that module. A skipped run
does not necessarily rewrite prior state.

The `WriteGuard` blocks configured write actions. A change to the human
session's behavior must preserve the read-only guarantee and update
`tests/test_human_session.py`.

## Safe file protocol changes

1. Find and update all writers and readers, including host jobs and scraper
   code. Search for the path or helper in `scraper/identity.py`.
2. Preserve atomic replacement for files consumed concurrently. Write the
   temporary file in the destination directory before `os.replace`.
3. Treat missing, malformed, and stale state explicitly; do not turn a failed
   read into a success-shaped “completed” result.
4. Update focused protocol tests and the relevant runbook/documentation.
5. Never add cookies, passwords, proxy URIs/credentials, noVNC passwords, or session
   contents to examples or test logs.

Useful tests: `tests/test_identity_state.py`,
`tests/test_worker_branches.py`, `tests/test_login_hub.py`,
`tests/test_login_fleet.py`, `tests/test_human_session.py`, and
`tests/test_dashboard.py`.
