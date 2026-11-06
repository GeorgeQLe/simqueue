# simqueue

Host-local MCP coordination for disposable Apple simulators. Independent agent servers
share a durable queue, with a default capacity of two devices. Each request gets an
isolated device; collaborators can explicitly join a shared device using a private
capability. The last release shuts down and deletes the device and verifies its UUID
and device directory are gone.

## Agent workflow

1. `inventory`: choose exact installed device-type and runtime identifiers.
2. `acquire(owner, task, device_type, runtime)`: retain the returned private
   `lease_token`. A queued request has no device yet. Poll it; do not submit duplicates.
3. `heartbeat(lease_token)` before expiry (default 900 seconds). `boot` the granted
   device and target only its returned UUID in tests.
4. `track_process(lease_token, pid)` for every build/test process that might outlive
   the task or its MCP server. Registration records PID plus start identity.
5. In `finally`, wait for those processes, retain requested evidence on T9, then
   `release(lease_token)`. A queued request is cancelled by the same operation.
6. Confirm `device_state: deleted`. An `attention` result means preservation and
   follow-up are required. `sweep` retries safe reconciliation; it never force-deletes.

Sharing requires `acquire(..., share=True)`, followed by each collaborator calling
`join(share_token, owner, task)`. Each gets its own lease and must release it. Sharing
permits simultaneous access; it does not serialize simulator UI or app mutations.
Coordinate those separately. Unrelated tasks are never matched just because they ask
for the same runtime.

Tokens authorize operations. Keep them out of Git, reports and shell arguments.
Status/events contain identifiers and labels but omit tokens and token hashes.
Expired/released token records are retained for seven days after device cleanup.

## Safety and recovery

- Only fresh devices named `agent-simqueue-<random-id>` with a persisted creation
  intent are managed. Existing working devices and shared runtimes are never adopted.
- One file lock serializes all servers and CLI calls. State is private (0700 directory,
  0600 files), fsynced and atomically replaced. Device identity is checked before use
  and deletion; shutdown is verified before deletion.
- A creation timeout reserves capacity. An exact unique name/type/runtime match can
  recover its UUID; an uncertain outcome cannot cause another creation attempt.
- A live or unverifiable owner protects an expired device (`stale-held`). Only an
  expired lease whose owning server process is demonstrably gone can auto-expire.
  Registered processes protect the device even after explicit release. PID identity
  uses the OS start stamp (one-second resolution); uncertain probes preserve devices.
- The owner is the MCP server process, or the short-lived CLI process for CLI calls.
  A shared long-lived server cannot prove an individual task has ended. Tasks must
  release explicitly; TTL alone is not permission to delete a live owner's device.
- Every mutation/poll/sweep reconciles work. The installed user LaunchAgent also
  sweeps once a minute, allowing cleanup after all MCP servers disconnect. `status`
  itself is read-only. There is no persistent daemon beyond active MCP connections.
- This is a cooperative local coordinator. It cannot protect a process or agent that
  uses an unmanaged UUID without registering/joining, nor stop manual simctl changes.
  Always register asynchronous test children before they can outlive the owner.
- Invalid state, capacity mismatches, identity changes, unavailable probes and blocked
  deletion preserve resources. Inspect `status`, the journal and `simctl list`; restore
  verified records if needed. There is deliberately no force/adopt/bulk-delete endpoint.
- The state directory must be common to all cooperating agents on a host. Independent
  directories are isolated coordinators and do not share a capacity limit.

The small live journal defaults to `~/Library/Application Support/simqueue`. Source,
virtual environment, dependency caches, scratch and retained test evidence belong on
T9. CoreSimulator's managed device data stays in its standard internal location and
is removed at the end of the task. Disconnecting T9 prevents the tool from running;
reconnect it and sweep to complete interrupted cleanup. Regular working simulators
and shared runtime installations remain internal.

## Install on this Mac

Python 3.11+ and the official MCP Python SDK v2 are required. `uv.lock` pins the full
resolved environment. Verify T9 with `developer-host storage` before installing.

```sh
UV_CACHE_DIR="$HOME/Developer/PackageCaches/uv" uv sync --locked
python3 scripts/install.py --apply
codex mcp add simqueue -- "$HOME/.local/bin/simqueue" serve
```

The installer creates a launcher pointing to this checkout's `.venv` and a user
LaunchAgent at `~/Library/LaunchAgents/com.georgele.simqueue.sweep.plist`.
Load it using `launchctl bootstrap "gui/$(id -u)" <plist-path>`. New Codex sessions
load the MCP entry; existing tasks do not need to be restarted.

For headless Claude, the installed minimal config is `~/.config/simqueue/mcp.json`;
pass `--mcp-config` with that path to `claude-review` when simulator access is needed.
For interactive Claude, add a project-local stdio entry where testing is needed:

```sh
claude mcp add --scope local simqueue -- "$HOME/.local/bin/simqueue" serve
```

Headless `claude-review` uses strict MCP configuration. Pass an explicit minimal
config containing simqueue only when the task will use it; keep read-only reviews
without MCP servers. Example server object:

```json
{"mcpServers":{"simqueue":{"command":"/Users/georgele/.local/bin/simqueue","args":["serve"]}}}
```

Do not change capacity while leases exist. Every process (including the sweeper) must
use the same `--max-devices` setting. Default is two. An alternate state directory is
for isolated acceptance tests, not normal agents.

## CLI and verification

`simqueue inventory`, `simqueue status`, `simqueue sweep`.
For capability-bearing calls, use JSON on stdin, e.g. `simqueue call release` with
`{"lease_token":"..."}` delivered from a private file/pipe. CLI `acquire` owns its
lease only for that process lifetime; register test children and heartbeat via MCP
for long-running tasks. A lost acquire response may leave a protected request: inspect
status rather than submitting repeated requests. There is no network listener or
arbitrary shell-command tool.

```sh
developer-host python-exec --venv "$PWD/.venv" -- -m pytest -q \
  --basetemp="$DEVELOPER_SCRATCH_ROOT/simqueue-tests"
developer-host python-exec --venv "$PWD/.venv" -- -m ruff check src tests scripts
```

`scripts/acceptance.py --output <unique-T9-task-directory>` explicitly runs the real
simulator test. It inventories existing devices, uses two stdio MCP servers, boots
one fresh device, checks shared/process protection and FIFO handoff, then verifies
both disposable UUIDs and directories are gone and pre-existing devices unchanged.
It selects the installed iPhone 17 Pro / iOS 26.5 pair on this host; adjust the script
for another host. Routine tests use a fake backend, including six concurrent OS
processes and interrupted/uncertain operation recovery.

Official dependencies/protocol: [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk),
[stdio transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports),
[Codex MCP configuration](https://learn.chatgpt.com/docs/developer-commands#codex-mcp).

## Disable / uninstall

Release and verify all leases first. Run `codex mcp remove simqueue` and
`launchctl bootout gui/501/com.georgele.simqueue.sweep`, then remove only the managed
launcher and LaunchAgent definition. Preserve the live journal until every owned
device is verified deleted. Configuration backups and local acceptance evidence are
retained separately on T9; they are never committed to this repository.
