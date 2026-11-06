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
