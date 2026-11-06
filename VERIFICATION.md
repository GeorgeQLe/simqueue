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
