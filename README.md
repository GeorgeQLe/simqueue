# simqueue

Host-local MCP coordination for disposable Apple simulators. Independent agent servers
share a durable queue, with a default capacity of two devices. Each request gets an
isolated device; collaborators can explicitly join a shared device using a private
capability. The last release shuts down and deletes the device and verifies its UUID
and device directory are gone.

## Why simqueue exists

Several coding agents can request simulators at once. Uncoordinated tests leave
large device directories behind, compete for resources, or delete a device another
agent still needs. Simqueue provides one host-local queue shared by separate MCP
server processes, private per-agent leases, explicit collaboration, and verified
cleanup after the final holder finishes. It uses stdio MCP; it opens no network port.

The core queue works with the normal internal CoreSimulator device set. An optional
APFS disk-image connection keeps disposable device data on external storage while
leaving regular working simulators and shared runtime installations alone.

**Status:** experimental macOS developer tooling, version 0.2.2. This repository
contains the source and tests; local simulator data, disk images, screenshots,
ownership journals and recovery credentials are not included.

## iOS 27 installation: mitigation and open issue

Version 0.2.2 allows up to **300 seconds** for native app installation, instead of
120 seconds. Keep the MCP tool timeout at least 360 seconds. A timeout still means
**outcome uncertain**: it does not prove installation failed, and simqueue never
reports success just because a bundle exists. It preserves the import copy for
inspection and does not automatically restart shared simulator services.

The original iOS 27.0 (24A5390f) timeout is intermittent. Investigation reproduced
an installation timeout on a fresh **internal** device, so external storage is not
a requirement for the failure. The original external-device MobileInstallation log
recorded successful developer installation despite the caller timing out. A native
stack sample from a later external run showed `simctl` waiting for the simulator's
installation bridge to reply. Cold installation/LaunchServices work is a plausible
contributor; the exact Apple-side cause remains **open**.

Follow-up runs of the same simulator app completed on internal storage in 14.8
seconds and on the T9-backed image in 54.4 seconds. Those runs support a bounded
wait mitigation, but do not prove that increasing the timeout permanently resolves
every stall. App installation, a live process and a visible app screen are separate
checks. Separate Apple `AppIntentsLiveEntityService` assertion crashes were also
observed and remain unresolved. See [the investigation notes](docs/ios27-investigation.md) for evidence and
remaining limits. The separate fresh-device Contacts workaround is described below.

| Tested runtime | External-image result |
| --- | --- |
| iOS 26.5 (23F77) | Boot, Contacts, app install/launch, screenshots, shared leases and final cleanup passed. |
| iOS 27.0 (24A5390f) | Full MCP install, live app process, visually reviewed game menu, Contacts, screenshots, shared leases, deletion and image eject passed in a follow-up run. Intermittent cold-start stalls remain open. |

Use iOS 26.5 when reliable app testing matters more than investigating this iOS 27
beta runtime. No automatic service restart or shared-runtime replacement is used.

## Agent workflow

1. `inventory`: choose exact installed device-type and runtime identifiers.
2. `acquire(owner, task, device_type, runtime)`: retain the returned private
   `lease_token`. A queued request has no device yet. Poll it; do not submit duplicates.
3. `heartbeat(lease_token)` before expiry (default 900 seconds). `boot` the granted
   device and target only its returned UUID in tests. Always pass its returned
   `device_set` to `simctl --set` (and `SIMULATOR_DEVICE_SET_PATH` for Xcode tests).
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
T9. With the image connection configured, disposable device data lives in an APFS
sparsebundle on T9 and is removed at the end of the task. Disconnecting T9 prevents
the tool from running; reconnect it and sweep to complete interrupted cleanup. Regular working simulators
and shared runtime installations remain internal.

## T9 image connection (0.2)

Direct `simctl --set` on this Mac's removable T9 fails CoreSimulator's file access
checks. An APFS sparsebundle backed by T9, mounted at a stable local connection
point, was verified to create and boot a device and run an existing simulator app.
This uses normal Apple disk-image commands without changing privacy permissions.
Shared runtime installations and existing regular simulators remain internal.

```sh
simqueue storage-init             # Preview; does not create an image
simqueue storage-init --apply     # Explicit, one-time provisioning
simqueue storage-status
simqueue storage-detach           # Only when all managed work is finished
```

On this Mac the backing image is `~/Developer/SimulatorStorage/simqueue.sparsebundle`,
with a 64 GB maximum virtual size; physical allocation grows as files are written.
Deleted device space is reusable inside the image; backing bands do not necessarily shrink immediately.
It mounts at `~/Library/Application Support/simqueue/DeviceVolume`. Every simulator
command verifies T9 and image volume identities. Missing or changed storage fails
closed; it never silently writes device data onto the internal mount directory.
Config and the small ownership journal remain internal. Provisioning refuses active
leases or existing image files; interrupted setup leaves an intent for inspection.

`inventory`/allocation lazily attach the configured image. Empty periodic sweeps do
not attach it. `storage-detach` refuses held, pending or uncertain work and requires
an empty custom device inventory; it performs a normal eject, never a force eject.
Protected ready devices do not trigger expensive periodic native inventory probes.
A busy coordinator returns a retry error; retain the original lease capability.

After `boot`, use `install(lease_token, app_path)` for a built simulator `.app`:
it stages the app inside the image so Apple's service can read it, installs it and
removes its temporary copy. Launch through `apple-tools xcrun simctl --set <device_set>
launch <uuid> <bundle_id>`. Build with explicit T9 derived-data/result paths.
Use `capture(lease_token, filename)` for screenshots: Apple's producer writes inside
the image, then the wrapper validates the PNG and exports it to T9
`Artifacts/simqueue-captures/<request-id>/`. It returns path, dimensions and SHA-256.
Failed import/export staging is preserved for diagnosis rather than silently discarded.
Boot can take several minutes; configure the MCP tool timeout to at least 360 seconds.

`scripts/image_acceptance.py --output <unique-T9-artifact-directory>` exercises the
installed launcher through two independent MCP connections, sharing protection,
app installation/launch, screenshot export, final deletion and normal image eject.
It uses an existing Puzzle Quest simulator build and verifies regular devices are
unchanged. The older `acceptance.py` uses an isolated internal test queue.

## Contacts migration startup workaround (0.2.2)

On this host, fresh iOS 26.5 (23F77) and iOS 27.0 (24A5390f) devices with Apple's
bundled sample Contacts data crash in `CNContactPosterMigrator performMigration`
(`CFArrayGetCount` receives a null array). The macOS crash dialog may appear or
remain visible later, including after the simulator has shut down.

For those two runtime identifiers, the image backend moves the sample AddressBook
seed aside immediately after creating a new disposable device, before its first
boot. iOS then initializes an empty Contacts database normally. The original seed
is preserved inside that same disposable device until normal final-release cleanup.
No migration service is disabled, crash reporting remains enabled, and no shared
runtime or existing simulator is modified. Contacts tests must supply their own
fixtures rather than depend on Apple's bundled sample contacts. Other runtimes
retain their normal initialization behavior.

The installed-launcher app acceptance now checks Contacts launch and screenshot,
Puzzle Quest launch and screenshot, absence of device-specific migration crash
reports, shared-holder protection and verified cleanup. The crash-report reader
matches the simulator coalition UUID, so another task's crash cannot fail the test.
The acceptance runner keeps private, mode-0600 cleanup capabilities in its T9 run
directory until verified cleanup succeeds. If cleanup fails, preserve that file,
recover with the original capabilities, and remove it only after verified deletion.
Never copy those credentials into reports or Git.

## Installation and host-specific requirements

Requirements: macOS with Xcode and its command-line tools, an installed simulator
runtime, Python 3.11+, and `uv`. The official MCP Python SDK v2 and development tools
are pinned in `uv.lock`.

The current Apple backend invokes `~/.local/bin/apple-tools xcrun simctl ...`.
That host-provided wrapper selects the intended Xcode and isolates Apple's tool
PATH; **it is not bundled here**. Supply that wrapper on another host before native
simulator operations. The queue tests use a fake backend and need no running
simulator.

The optional external-image integration is currently specific to the original host:
it expects external storage mounted at `~/Developer` and a `~/.local/bin/developer-host`
helper returning verified JSON storage readiness. That helper is also not bundled.
The image backing path and mount point are provisioned by `storage-init`; this is
not a general external-drive configuration wizard. Review `src/simqueue/storage.py`
and adapt host integration before provisioning elsewhere. Without image configuration,
the backend uses the normal internal device set.

Clone the public repository, then install dependencies:

```sh
git clone https://github.com/GeorgeQLe/simqueue.git
cd simqueue
uv sync --locked
uv run simqueue --help
```

On the original T9-configured host, verify `developer-host storage` and use:

```sh
UV_CACHE_DIR="$HOME/Developer/PackageCaches/uv" uv sync --locked
python3 scripts/install.py --apply
codex mcp add simqueue -- "$HOME/.local/bin/simqueue" serve
```

The installer creates a launcher pointing to this checkout's `.venv` and a user
LaunchAgent at `~/Library/LaunchAgents/com.georgele.simqueue.sweep.plist`.
Load it using `launchctl bootstrap "gui/$(id -u)" <plist-path>`. New Codex sessions
load the MCP entry; existing tasks do not need to be restarted.

For Claude Code, add a project-local stdio entry where testing is needed:

```sh
claude mcp add --scope local simqueue -- "$HOME/.local/bin/simqueue" serve
```

For clients using an MCP JSON configuration, replace the absolute executable path
below with your checkout's installed entry point. Example server object:

```json
{"mcpServers":{"simqueue":{"command":"/absolute/path/to/simqueue/.venv/bin/simqueue","args":["serve"]}}}
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
`launchctl bootout "gui/$(id -u)/com.georgele.simqueue.sweep"`, then remove only the managed
launcher and LaunchAgent definition. Preserve the live journal until every owned
device is verified deleted. Configuration backups and local acceptance evidence are
retained separately on T9; they are never committed to this repository.
