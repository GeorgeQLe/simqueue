# Verification — 5 November 2026

- Python 3.14.3; official `mcp` SDK 2.3.0, locked dependencies; dependency consistency check passed.
- 19 pytest cases passed: FIFO, shared last-holder release, registered child protection,
  live/dead/unknown owner expiry, PID reuse, uncertain creation recovery, unavailable
  inventory, cleanup failure/retry, changed identity, corrupt state, symlink refusal,
  remaining device data, unconfirmed shutdown, retention, six concurrent OS processes,
  and SDK modern/legacy protocol paths.
- Ruff checks and formatting passed; installed stdio launcher discovered ten tools and
  served status using the legacy initialize handshake.
- Real acceptance: two independent stdio server processes used one isolated queue.
  A fresh iPhone 17 Pro / iOS 26.5 device was booted. Shared holders and a registered
  live child each blocked deletion. After the child exited, cleanup shut down and
  deleted the device and granted the queued request a fresh device.
- Both owned UUIDs and data directories were verified absent. All eight pre-existing
  device UUIDs remained, with unchanged boot states. Shared runtimes were untouched.
- Local Codex entry enabled with 180-second tool timeout. User LaunchAgent loaded
  with 60-second sweep interval and verified exit code zero. No resident daemon was
  installed; the sweeper runs short-lived CLI invocations.

Machine-specific reports and configuration backups are retained on T9 under
`Developer/Artifacts/simqueue-acceptance-20261105` and
`Developer/Artifacts/simqueue-install-20261105`, outside this repository.

## T9 image connection — 0.2.0, 5 November 2026

- 31 automated pytest cases passed; Ruff checks and formatting passed. Coverage
  includes backing/image identity, missing T9 refusal, interrupted setup fencing,
  mount collision refusal, normal detach, held-lease protection, staging installs,
  PNG export and modern/legacy MCP protocol errors. The server exposes 12 tools.
- Installed-launcher acceptance used two independent stdio MCP connections with the
  normal shared journal and T9 image. An iPhone 17 Pro / iOS 26.5 device booted;
  the install tool staged an existing ComboThrone Puzzle Quest simulator app inside
  the image. The app remained running from the image's device directory.
- Capture returned a verified 1206 × 2622 PNG; visual inspection confirmed the
  Puzzle Quest menu. SHA-256:
  `572a8f9f8d77c2e7a1ccec5836e14b47c950c98e9e6c69f20149ee5c3825cd49`.
- First release preserved the shared device. Final release deleted UUID
  `54B8D25E-957C-4ED3-AA88-186161A1E40E` and its data directory. All four retained
  internal devices had unchanged UUIDs and boot states. Shared runtimes unchanged.
- Normal image eject succeeded; a subsequent idle sweep left it unmounted, with
  no active requests or remaining devices. Prototype scratch image was also ejected
  and removed. Permanent sparsebundle retained on T9: about 1.8 GiB allocated,
  64 GB maximum virtual size. Deleting files frees image capacity; the sparsebundle's
  host allocation may retain reusable bands rather than immediately shrink.
- Codex timeout updated to 360 seconds; existing launcher and periodic sweeper now
  use the configured image backend. New MCP sessions discover install/capture tools.
- Evidence: `~/Developer/Artifacts/simqueue-image-acceptance-20261105-v2/result.json`
  and `~/Developer/Artifacts/simqueue-captures/3e666cc52ff444eebf45de649c0b9dc8/`.
  Configuration backups remain on T9, outside Git.

## Contacts startup workaround — 0.2.1, 5–6 November 2026

- Reproduced the reported `com.apple.migrationpluginwrapper` crash on a fresh
  iOS 27 device as well as the earlier iOS 26.5 baseline. Simulator coalition UUID
  identifies the device; the fault is `CNContactPosterMigrator performMigration`
  passing a null array into `CFArrayGetCount` during AddressBookLegacy migration.
- Moving only a freshly created device's bundled Contacts seed aside prevented
  the crash. iOS regenerated a fresh Contacts database. The automated workaround
  preserves the seed inside that disposable device, affects only iOS 26.5/27.0
  identifiers, and never changes existing simulators or shared runtimes.
- iOS 26.5 full installed-MCP acceptance passed: normal empty Contacts screen,
  Puzzle Quest launch/menu screenshot, no device-specific migration crash reports,
  shared-holder protection, verified deletion and image eject.
- iOS 27 boot and Contacts launch/screenshot also passed with no migration crash
  report. Its additional Puzzle Quest install timed out after 120 seconds, followed
  by a CoreSimulator inventory timeout. This app check did not pass. A bounded
  service sample was retained; after verifying the only booted simulator belonged
  to this test, a graceful CoreSimulator service restart restored responsiveness.
- Both abandoned test client PIDs were verified gone with no registered children;
  their exact owned requests were released through coordinator recovery. Normal
  simqueue cleanup verified deletion of the iOS 27 test UUID and data directory.
  The known failed install's staging copy was removed and the image cleanly ejected.
- No migration helper crashes were recorded for the empty-Contacts probe or either
  fixed-runtime test, including a scan after shutdown. Crash reporting stays enabled.
- 35 automated tests passed, including both affected runtime identifiers, preservation
  of existing contact data, refusal of a symlinked seed, and unchanged initialization
  for other runtimes. Ruff and formatting checks passed.
- The acceptance runner now retains private cleanup capabilities until verified
  deletion and always writes a failure/cleanup report even if native inventory hangs.
- T9 evidence directories: `simqueue-migration-ios27-20261105`,
  `simqueue-migration-fresh-contacts-20261105`, `simqueue-contacts-fix-ios26-20261105`,
  `simqueue-contacts-fix-ios27-20261105`; screenshots under `simqueue-captures/`.


## T7 installation and portable storage — 6 November 2026

This section records measurements on this Mac; earlier sections describe the original host.

- Installed an editable checkout at `~/Developer/Projects/tools/simqueue` on the
  physically mounted external `T7-Developer` APFS volume. Python 3.12.14, official
  MCP SDK 2.3.0 and locked dependencies. `uv pip check` passed.
- Baseline: 36 tests passed. After changes: 47 tests passed; Ruff lint, formatting
  and `git diff --check` passed. Tests/dependencies/caches remain on T7.
- Storage config v2 uses `external_root` and `external_uuid`, accepts any volume
  label, and supports `storage-init --storage-root /absolute/mount`. Legacy v1
  T9 keys remain readable. Native checks require a mounted external writable APFS
  volume and pin its UUID plus the image identity. Missing/replaced backing is
  refused. Host readiness helpers remain enforced when present; otherwise native
  checks provide local verification, not fleet enrollment or a distributed lease.
- Missing `apple-tools` now falls back to `/usr/bin/xcrun`, preserving selected
  Xcode/DEVELOPER_DIR. No shared simulator runtime or existing device was modified
  by simqueue operations.
- The image is `~/Developer/BuildExports/simqueue-storage/simqueue.sparsebundle`
  (64 GB maximum virtual size). The developer volume root is administrator-owned;
  BuildExports is writable and designated for host-local outputs.
- First iPhone 17 Pro / iOS 26.3 acceptance reproduced Apple's null-array
  `CNContactPosterMigrator` crash, plus Contacts/contactsd crashes. Its screenshot
  showed SpringBoard rather than Contacts. The existing fresh-device-only seed
  workaround was extended to this exact runtime. The next run had no device-specific
  migration crash report, but Contacts launch exceeded 45 seconds; full Contacts
  acceptance therefore remains FAILED. A calaccessd crash was also recorded in that
  second run. This does not establish a cause for the launch stall.
- The final app lifecycle acceptance explicitly skipped Contacts and PASSED through
  two installed-launcher MCP connections with a separate journal while fencing the
  normal coordinator. Puzzle Quest installed in 14.96 seconds, launched, and
  remained running from the image. Visual inspection confirmed its menu in a
  1206 x 2622 capture. First release preserved the other holder; final release
  verified UUID and directory deletion. Image eject succeeded. All pre-existing
  UUIDs and boot states were unchanged in this successful run.
- Earlier failed runs observed an existing device transition from Shutdown to
  Booted. No simqueue command targeted that UUID; its state was left alone.
  All three test-owned UUIDs and directories were subsequently verified absent
  using a mounted, empty custom inventory. Private recovery capabilities were
  removed only after that verification. Failed-run reports are retained unchanged,
  with separate cleanup verification files.
- Installed stdio MCP smoke discovered all 12 tools, served status, attached the
  image lazily and verified an empty managed inventory before normal detach.
  Codex's global simqueue entry points to `~/.local/bin/simqueue serve` with a
  360-second tool timeout. The user sweeper is loaded with a 60-second interval.
  Its initial start waited for macOS removable-volume permission for the uv Python
  interpreter. The user approved the access and completed the protected system
  dialog; automation could not operate UserNotificationCenter. LaunchAgent exit
  code zero was observed, then a second invocation was explicitly triggered and
  also verified to exit zero. The queue stayed empty and the image stayed detached.
- iOS 27.2 is installed but was not tested. Physical disconnect/reconnect, reboot,
  and long-running multi-task usage were not qualified. Missing-drive behavior is
  covered by automated tests; image detach/reattach was exercised natively.
- Evidence: `~/Developer/Artifacts/simqueue-evaluation-20261106/` contains failed
  baseline, fixed Contacts attempt, successful `ios26-3-app/result.json`, crash
  summaries and cleanup verification. Retained menu screenshot:
  `~/Developer/Artifacts/simqueue-captures/9fde9cfe6e624c9c8100015398ab1c1b/puzzle-quest.png`.
  SHA-256: `f6c016e0c55071e7e05ac624bbc62a742fb3a8d199a81541baa623542c869219`.

The portable changes are local on `feat/portable-external-storage`; they have not
been pushed or released. App lifecycle use is demonstrated on iOS 26.3. Contacts
and other runtime coverage remain experimental.

## Readiness and lifecycle audit corrections — 6 November 2026

- Preserved the pre-existing dirty portable-storage work; scoped baseline and
  incremental patch retained in `Developer/Artifacts/simqueue-corrections-20261106`.
  No commit, push, runtime relocation or production work was performed.
- Boot rechecks native readiness for already-Booted devices and returns
  `boot_ready: true` only on success. Install/capture recheck readiness, including
  a post-wait check for concurrent shutdown. Native commands share an operation
  deadline rather than receiving consecutive full timeout windows.
- Interrupted create recovery requires proof of the image backend's fresh Contacts
  initialization. Missing proof preserves the device; explicit release can still
  clean up the exact owned resource. Initialization I/O errors retain the normal
  uncertain-create recovery path rather than aborting capability delivery.
- Screenshot export validates PNG checksums, compressed scanlines and row filters,
  uses bounded input/pixel sizes, and publishes without overwriting existing files
  or following a fixed partial-file symlink. Failed producer output is preserved.
- Final unit/protocol/concurrency suite: **64 passed in 0.88 seconds**. Ruff lint,
  formatting (17 files) and `git diff --check` passed. Existing malformed PNG test
  fixture had an incorrect checksum; replaced with a valid equivalent image.
- Fresh installed-launcher MCP smoke discovered all 12 tools, served status,
  reported attention null, no active requests and all managed devices deleted.
  Existing long-lived clients need to reconnect to import the updated code.
- First native harness stopped before boot because macOS's setuid-root `top`
  process could not pass caller-ownership registration. Exact device cleanup
  succeeded. Harness changed to non-privileged iostat; ownership checks unchanged.
- One actual native iPhone 16e/iOS 26.3.1 boot on verified T7 image: corrected Queue
  boot passed in **93.97s**, already-Booted readiness in **7.39s**, and installation
  of the retained Civ Debug simulator app in **33.93s**. Native phase output and
  I/O/CPU measurements retained. Test used a 120-second native-operation budget.
- The separate native launch check reached its **30-second bound**. Outcome was
  uncertain; no launch retry, larger device, service reset or further local boot
  was attempted. App liveness, new screenshot export and native shared-holder
  acceptance did not complete in this run. The later initialization-I/O error
  wrapper was unit-tested; no new native run followed it. Do not describe this as
  full native app lifecycle acceptance or an Apple startup fix.
- Both test-owned UUIDs/directories were deleted. All 32 pre-existing UUIDs/states
  remained unchanged, with no new default devices. Image remained mounted as found;
  private cleanup capabilities removed after verified deletion. Full remaining
  native lifecycle validation belongs on the more capable Mac under local policy.


## Owner-authorized extended native acceptance — 6 November 2026

This follow-up completes the app/framebuffer validation left incomplete above.
The owner explicitly authorized exceeding the prior test-harness launch bound.
The 30-second limit was a harness choice, not a macOS requirement or fixed
simqueue policy. No service reset, cable change or launch retry was used.

- Fresh T7-image iPhone 16e/iOS 26.3.1; exact corrected core-source hashes retained.
  Used the normal 300-second image-operation budget and one 180-second launch
  window. Boot passed in **133.90s**, already-Booted readiness in **5.11s**, app
  installation in **37.71s**, launch in **95.81s**, capture in **20.25s**.
- The app remained alive through the 20-second observation and native capture.
  Visual inspection confirmed the readable Civilization main menu, building art,
  New Game and Settings in the 2532 x 1170 screenshot. PNG integrity and SHA-256
  verified. This is app/framebuffer acceptance, not gameplay/input or media QA.
- First release preserved the other shared holder; final release verified exact
  UUID/directory deletion. All 32 pre-existing device UUIDs/states were preserved;
  no new default device, private cleanup capability or cleanup error remained.
- The app was first observed in sampled process listings about 97.5s after the
  launch request, just after the successful native reply. Sampling was periodic,
  not continuous proof of absence. The planned launch stack sample found no
  pending matching command after launch finished; no stack-sample claim is made.
- This run demonstrates that 30 seconds can prematurely cut off a successful
  launch on this Mac. The acceptance runner now explicitly allows 180 seconds and
  records launch duration. Generic native-command limits and ownership/readiness
  checks are unchanged. A longer bound is a mitigation, not an Apple-side fix or
  an exact 17 Pro/iOS 27 qualification.
- After the acceptance-runner update: **64 tests passed in 1.19s**; Ruff lint,
  formatting and diff checks passed. The dedicated native runner exercised the
  accepted core with the same explicit 180-second native launch call; no extra
  native boot was run merely to repeat the harness edit.
- Evidence: `Developer/Artifacts/simqueue-native-extended-20261106/`, including
  native result/phase/resource logs, source hashes, visual review and final
  preservation proof. Screenshot SHA-256:
  `dfea546134acdbbec49d137220fddb84cd0dbfea220ce0c4243ad4c333d1e3f2`.
  No recording or publishing occurred; Civ production remains paused.
