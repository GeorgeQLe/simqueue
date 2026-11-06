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
seconds and on the original host’s T9-backed image in 54.4 seconds. Those runs support a bounded
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
5. In `finally`, wait for those processes, retain requested evidence on external developer storage, then
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
the external developer volume. With the image connection configured, disposable device data lives in an APFS
sparsebundle on the external volume and is removed at the end of the task. Disconnecting that volume prevents
the tool from running; reconnect it and sweep to complete interrupted cleanup. Regular working simulators
and shared runtime installations remain internal.

## External APFS image connection (0.2)

On the original host, direct `simctl --set` on removable T9 failed CoreSimulator's
file access checks. An APFS sparsebundle backed by the external drive, mounted at a stable local connection
point, was verified to create and boot a device and run an existing simulator app.
This uses normal Apple disk-image commands without changing privacy permissions.
Shared runtime installations and existing regular simulators remain internal.

```sh
simqueue storage-init             # Preview; does not create an image
simqueue storage-init --apply     # Explicit, one-time provisioning
simqueue storage-status
simqueue storage-detach           # Only when all managed work is finished
```

On this Mac the backing image is `~/Developer/BuildExports/simqueue-storage/simqueue.sparsebundle`,
with a 64 GB maximum virtual size; physical allocation grows as files are written.
Deleted device space is reusable inside the image; backing bands do not necessarily shrink immediately.
It mounts at `~/Library/Application Support/simqueue/DeviceVolume`. Every simulator
command verifies the backing and image volume identities. Missing or changed storage fails
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
removes its temporary copy. Launch through `xcrun simctl --set <device_set>
launch <uuid> <bundle_id>` (prefix with `apple-tools` on hosts that require it). Build with explicit external derived-data/result paths.
Use `capture(lease_token, filename)` for screenshots: Apple's producer writes inside
the image, then the wrapper validates the PNG and exports it to external developer storage
`Artifacts/simqueue-captures/<request-id>/`. It returns path, dimensions and SHA-256.
Failed import/export staging is preserved for diagnosis rather than silently discarded.
Boot can take several minutes; configure the MCP tool timeout to at least 360 seconds.

`scripts/image_acceptance.py --output <unique-artifact-directory> --runtime <installed-id>` exercises the
installed launcher through two independent MCP connections, sharing protection,
app installation/launch, screenshot export, final deletion and normal image eject.
It uses an existing Puzzle Quest simulator build (or `--app /path/to/Simulator.app`)
and accepts `--device-type` for another installed device. Run before enabling clients
or the sweeper: it requires an idle coordinator, holds its lock throughout, and uses a separate acceptance
journal inside its output directory, sharing the configured image only for the test.
It verifies pre-existing device UUIDs remain and reports observed boot-state changes
without mutating those devices. Other tasks may use them during acceptance. The older `acceptance.py` uses an isolated internal test queue.

## Contacts migration startup workaround (0.2.2)

On this host, fresh iOS 26.3, iOS 26.5 (23F77) and iOS 27.0 (24A5390f) devices with Apple's
bundled sample Contacts data crash in `CNContactPosterMigrator performMigration`
(`CFArrayGetCount` receives a null array). The macOS crash dialog may appear or
remain visible later, including after the simulator has shut down.

For those three runtime identifiers, the image backend moves the sample AddressBook
seed aside immediately after creating a new disposable device, before its first
boot. iOS then initializes an empty Contacts database normally. The original seed
is preserved inside that same disposable device until normal final-release cleanup.
No migration service is disabled, crash reporting remains enabled, and no shared
runtime or existing simulator is modified. Contacts tests must supply their own
fixtures rather than depend on Apple's bundled sample contacts. Other runtimes
retain their normal initialization behavior. On the T7 host, iOS 26.3 reproduced the
same migration crash. Its fresh-device seed workaround prevented that migration
crash in the next run, but Contacts launch still timed out. App lifecycle acceptance
passed separately with `--skip-contacts`; Contacts acceptance remains unresolved.
See [T7 verification](VERIFICATION.md#t7-installation-and-portable-storage--6-november-2026).

The installed-launcher app acceptance now checks Contacts launch and screenshot,
Puzzle Quest launch and screenshot, absence of device-specific migration crash
reports, shared-holder protection and verified cleanup. The crash-report reader
matches the simulator coalition UUID, so another task's crash cannot fail the test.
The acceptance runner keeps private, mode-0600 cleanup capabilities in its external developer storage run
directory until verified cleanup succeeds. If cleanup fails, preserve that file,
recover with the original capabilities, and remove it only after verified deletion.
Never copy those credentials into reports or Git.

## Installation and host-specific requirements

Requirements: macOS with Xcode and its command-line tools, an installed simulator
runtime, Python 3.11+, and `uv`. The official MCP Python SDK v2 and development tools
are pinned in `uv.lock`.

The Apple backend uses `~/.local/bin/apple-tools xcrun simctl ...` when that
host wrapper is present. Otherwise it uses `/usr/bin/xcrun simctl ...` with the
selected Xcode (or `DEVELOPER_DIR`). No wrapper installation is required.

External-image storage accepts any drive label. It defaults to the external APFS
volume mounted at `~/Developer`; use `simqueue storage-init --storage-root /absolute/mount`
to select another physical mount before provisioning. Each operation verifies the
configured backing volume UUID, external/writable APFS properties, image path and
mounted image identity. Missing or replaced storage fails closed. If the host has
`~/.local/bin/developer-host`, its verified readiness check is also required before
provisioning or attaching; hosts without that helper use native macOS verification.
This is host-local verification, not a distributed writer lease or fleet enrollment.
Config version 2 uses `external_root` and `external_uuid`; version 1 `t9_root` and
`t9_uuid` configurations remain readable without rewriting or adopting their images.
Without image configuration, the backend uses the normal internal device set.

Clone the public repository, then install dependencies:

```sh
git clone https://github.com/GeorgeQLe/simqueue.git
cd simqueue
uv sync --locked
uv run simqueue --help
```

Install the launcher and sweep definition after verifying the external volume:

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

## Readiness and recovery checks

`device_state: ready` means allocation is complete. It does not establish native
boot readiness. A successful `boot` now returns `boot_ready: true`, rechecks
`bootstatus` even when inventory already says `Booted`, and verifies the device
has not stopped during the wait. `install` and `capture` each check native
readiness again before doing app or framebuffer work. A timeout remains uncertain;
never substitute polling, a service PID or `Booted` inventory for successful boot.
Readiness also does not establish that an app's assets have finished rendering.
The native acceptance runner allows 180 seconds for app launch: a cold 16e/iOS
26.3.1 launch on the 8 GB T7 host completed in 95.8 seconds. It still requires a
live app and screenshot evidence; elapsed waiting alone is not acceptance.

Inventory, readiness and subsequent native commands share one operation deadline
(120 seconds for the internal backend, 300 seconds for the image backend). File
staging and storage checks consume elapsed time rather than starting a fresh
native wait window. These are native-command limits, not hard interruption of
filesystem I/O; retain client transport overhead as described above.

Recovery of an interrupted image-device create must verify the affected runtime's
Contacts initialization marker and preserved seed before granting its lease. A
missing or inconsistent proof preserves the device for inspection; release can
still clean up that exact journal-owned resource. Recovery never reruns the seed
workaround against a device that might already have booted.

Screenshot export validates PNG chunks/checksums, compressed pixel lengths and
row filters, including interlaced images. Limits are 64 MiB encoded and 16 million
pixels. Invalid producer output remains staged for diagnosis. Export uses a unique
temporary file and atomic no-clobber publication, so existing destinations and
partial-file symlinks are preserved. Integrity checks establish a complete image;
visually inspect it to establish which app or screen rendered.

## CLI and verification

`simqueue inventory`, `simqueue status`, `simqueue sweep`.
For capability-bearing calls, use JSON on stdin, e.g. `simqueue call release` with
`{"lease_token":"..."}` delivered from a private file/pipe. CLI `acquire` owns its
lease only for that process lifetime; register test children and heartbeat via MCP
for long-running tasks. A lost acquire response may leave a protected request: inspect
status rather than submitting repeated requests. There is no network listener or
arbitrary shell-command tool.

```sh
.venv/bin/python -m pytest -q \
  --basetemp="$HOME/Developer/Scratch/simqueue-tests"
.venv/bin/ruff check src tests scripts
```

`scripts/acceptance.py --output <unique-artifact-directory>` explicitly runs the real
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
retained separately on external developer storage; they are never committed to this repository.
