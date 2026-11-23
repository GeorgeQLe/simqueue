# simqueue

Host-local MCP coordination for disposable Apple simulators. Independent agent servers
share a durable queue, with a default capacity of one device on the managed host.
Isolated test queues can choose their own capacity. Each request gets an
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

## Resource observations

The default queue sends enqueue, device reservation, and terminal events to
`developer-host queue-metrics`. Managed XCTest samples about every 30 seconds
from its own detached worker, including time spent queued. The existing
minute-based sweeper can provide additional samples for manual leases.
There is no extra model call or dependency for a test to proceed. Counters and
the shared rolling log format are documented under **Queue resource observations**
in the developer-host README.

Managed XCTest also records `phase` rows (`booting`, `testing`) whose `run_ms`
is the time since submission, and its `finish` row carries `failed_phase` and
the error class as `reason` when the run stopped before testing.

## Shared host slot for cold boots

On the default queue, a cold boot (device `Shutdown` or `Booting`) first takes
the developer-host heavy-phase slot it shares with FormSeed3D Blender renders
(**Shared heavy-phase slot** in the developer-host README). The wait happens
after the lease and device are ready and outside the native boot budget, so it
never counts toward the boot timeout. The boot and its same-device recheck run
inside the slot, which is released when the device is ready or the attempt
ends. An already `Booted` device, install, capture and XCTest itself never take
the slot. Managed XCTest status shows the wait under `boot.initial.host_slot`
(`waiting` with `reason`, then `acquired` with `wait_ms`). If the slot stays
busy past `SIMQUEUE_HOST_SLOT_MAX_WAIT_SECONDS` (default 900) the boot is
refused with `HostSlotBusy` before any native command runs; the device is not
marked failed and can be booted again. A missing or failing helper lets the
boot proceed without the slot. `SIMQUEUE_HOST_SLOT=off` disables it; custom
state directories and fixtures do not use it. An absolute
`DEVELOPER_HOST_LAUNCHER` selects a trial developer-host release instead of
`~/.local/bin/developer-host`; a missing one disables the slot rather than
falling back.

## Agent workflow

For a built XCTest plan, submit `run_xctest` once. Simqueue queues, renews, boots,
runs stable Xcode on its own leased simulator, and verifies cleanup. Wait with
`xctest_wait` and the returned run ID. It returns the outcome and cleanup or a
state requiring attention. Its 45-second maximum lets it return before a typical
MCP deadline. For multi-minute runs, prefer one local CLI wait (up to 900 seconds)
with a host timeout that covers the wait:

```sh
simqueue call xctest-wait <<'JSON'
{"run_id":"<the returned run ID>","timeout_seconds":900}
JSON
```

`wait_result: finished` means the run ended and cleanup is verified or it never
created a device. Check `outcome` separately for the test result.
`wait_result: attention` means recovery may be needed. A timeout returns the
current phase and the same run ID. The wait is read-only, so it is safe to wait
again on that ID. Use `xctest_status` for intermediate diagnostics. A host that
supports deferred responses can deliver the wait result when it is ready.
Closing the submitting CLI or MCP connection leaves the worker
running. For safe resubmission after a lost reply, supply a client-generated
32-character lowercase hexadecimal `run_id` and reuse it with the same arguments.
Choose a new run ID and result path for a new test attempt.

The CLI takes the same inputs on JSON stdin:

```sh
simqueue call run-xctest <<'JSON'
{
  "owner": "agent",
  "task": "campaign smoke",
  "device_type": "com.apple.CoreSimulator.SimDeviceType.iPhone-17-Pro",
  "arguments": [
    "-xctestrun", "/absolute/path/to/built/Tests.xctestrun",
    "-resultBundlePath", "/absolute/path/on/T9/Smoke.xcresult",
    "-only-testing:YourAppUITests/SmokeTests/testLaunch"
  ]
}
JSON
simqueue call xctest-status <<'JSON'
{"run_id": "<returned run ID>"}
JSON
```

Build before submitting. Give each run an unused result destination on verified
external developer storage. Progress includes queue position, phase and elapsed
time. During boot, the run records the current command and last observed native
status for each attempt, including the Apple migration plugin when reported. On a
boot failure, the run records the native log directory and reports `not_run`.
Completed runs include the XCTest console log, result bundle when produced, and
cleanup state. `cleanup: verified` means both the device UUID and device directory
were verified gone. `cleanup: pending` can be checked again after coordinator
reconciliation. The runner uses the existing boot budget and same-UUID recheck.

The default `simqueue status` and MCP `status` show active queue entries and
attention. For retained requests, devices and events, use
`simqueue --history status` or MCP `status(history=true)`.

### Manual leases

1. `inventory`: choose an exact installed device type. Omit the runtime to use
   iOS 26.5 when available, or supply the exact runtime needed for the test.
2. `acquire(owner, task, device_type, runtime)`: retain the returned private
   `lease_token`. A queued request waits for capacity to create its own device.
   Poll the existing request. A normal queue entry cannot reserve another task's UUID.
3. `heartbeat(lease_token)` before expiry (default 900 seconds). `boot` the granted
   device and target only its returned UUID in tests. Always pass its returned
   `device_set` to `simctl --set` (and `SIMULATOR_DEVICE_SET_PATH` for Xcode tests).
   With Xcode 27, also append the returned `xcodebuild_args` to the command as
   individual arguments. `SIMULATOR_DEVICE_SET_PATH` alone does not make Xcode
   discover custom-set devices. The argument is
   `"-DVTSimulatorSetLocation=<returned device_set>"` (one argument, including `=`).
   Keep the destination fixed to the returned UUID. This is a per-process setting;
   no global preference or default internal device-set redirect is needed.
   For a built XCTest plan, use `simqueue call xcode-test` with the private
   `lease_token` and an `arguments` array (for example, `["-xctestrun", "/path/to/Tests.xctestrun",
   "-resultBundlePath", "/path/on/T9/Tests.xcresult"]`). Pass the JSON on stdin.
   The command registers its PID, checks boot readiness, and runs stable Xcode
   against the lease with parallel testing disabled. The caller releases the lease.
4. `track_process(lease_token, pid)` for every build/test process that might outlive
   the task or its MCP server. Registration records PID plus start identity.
5. In `finally`, wait for those processes, retain requested evidence on external developer storage, then
   `release(lease_token)`. A queued request is cancelled by the same operation.
6. The final holder confirms `device_state: deleted`. With another holder still
   granted, the released lease continues to show the shared device's current
   state. An `attention` result means preservation and follow-up are required.
   `sweep` retries safe reconciliation.

Sharing requires `acquire(..., share=True)`, followed by each collaborator calling
`join(share_token, owner, task)`. Each gets its own lease and must release it. Sharing
permits simultaneous access; it does not serialize simulator UI or app mutations.
Coordinate those separately. For a same-device handoff, the incoming agent
joins and confirms its own granted lease and the same UUID before the outgoing
agent releases. The final holder is responsible for verified deletion.

Tokens authorize operations. Keep them out of Git, reports and shell arguments.
Status/events contain identifiers and labels but omit tokens and token hashes.
Expired/released token records are retained for seven days after device cleanup.

## Safety and recovery

- Only fresh devices named `agent-simqueue-<random-id>` with a persisted creation
  intent are managed. Existing working devices and shared runtimes are never adopted.
- One file lock protects the shared ownership journal. Create, boot, install and capture
  record a per-device operation and release the journal lock before waiting on
  Apple tools. Status and heartbeats report the active operation. Release records
  the request immediately and defers cleanup until the operation finishes.
  An interrupted operation whose owner cannot be verified preserves its device.
  A boot timeout triggers one short readiness check on the same UUID. If the
  check cannot establish readiness, inspect its CoreSimulator logs and release.
  State is private (0700 directory,
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
- Poll and sweep reconcile work. Heartbeat renews active leases without native probes. The installed user LaunchAgent also
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
On a timeout, status reports `boot_error` and the journal records how long the
operation ran. The error includes the last numeric native boot status when
available, without retaining raw command output. A device already in `Booting`
is monitored on its existing UUID.

Readiness also does not establish that an app's assets have finished rendering.
The native acceptance runner allows 180 seconds for app launch: a cold 16e/iOS
26.3.1 launch on the 8 GB T7 host completed in 95.8 seconds. It still requires a
live app and screenshot evidence; elapsed waiting alone is not acceptance.

Inventory, readiness and subsequent native commands share one operation deadline
(120 seconds for the internal backend, 300 seconds for the image backend). File
staging and storage checks consume elapsed time rather than starting a fresh
native wait window. These are native-command limits, not hard interruption of
filesystem I/O; retain client transport overhead as described above.

Native device startup uses this same budget, including `simctl boot` itself;
it no longer has a separate 45-second cutoff. If boot times out, keep the lease
while the coordinator performs one additional readiness check on the same device,
with a 30-second budget. If that check also times out, collect diagnostics for
the UUID, release the lease, and verify deletion. A fresh device restarts cold
migration. Start tests only after receiving `boot_ready: true`.

`simctl bootstatus` can also exit zero while reporting terminal status 3, “Data
Migration Failed.” Simqueue rejects this result and records the failure in its
device journal. Later boot/install/capture calls cannot bypass it through an
“already booted” response. Retain diagnostics, then release that failed disposable
device. This terminal failure differs from a timeout while migration is progressing.

Device creation reserves a journal entry before calling CoreSimulator. Status
and heartbeats can run while the create operation is in progress. The create
command shares the backend operation budget with inventory and verification.
After a timeout, use the original lease capability to poll for the device.
CoreSimulator can publish it minutes after the client has timed out. The queue
reconciles the exact generated name, device type and runtime before allocating
another device. A missing device in inventory leaves the intent unresolved.

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
